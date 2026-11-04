#!/usr/bin/env bash
# Build, smoke-test and publish the image to Docker Hub.
#
#   ./scripts/publish.sh <dockerhub-user>/istok [--dry-run] [--force]
#
#   The version is the git tag of the commit being published: `git tag v1.2.3` → image <repo>:1.2.3.
#   Publishing needs HEAD to carry a version tag and a clean work tree, so the image is exactly that commit.
#   --dry-run  build and smoke-test only, push nothing; works on an untagged or dirty tree too
#              (version 1.2.4.dev3+g<sha>, image tag with "+" replaced by "-")
#   --force    push even if this version tag is already published (normally refused)
#
# Log in first: docker login
set -euo pipefail

cd "$(dirname "$0")/.."

REPO="" DRY_RUN=0 FORCE=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        --force) FORCE=1 ;;
        -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
        -*) echo "unknown option: $arg" >&2; exit 2 ;;
        *) if [[ -z "$REPO" ]]; then REPO="$arg"; else
               echo "unexpected argument: $arg (the version comes from the git tag)" >&2; exit 2; fi ;;
    esac
done
if [[ -z "$REPO" || "$REPO" != */* ]]; then
    echo "usage: $0 <dockerhub-user>/istok [--dry-run] [--force]" >&2
    exit 2
fi
SEMVER='^[0-9]+\.[0-9]+\.[0-9]+([.-]?(a|b|rc)[0-9]+)?$'
TAG=$(git describe --tags --exact-match --match 'v[0-9]*' HEAD 2>/dev/null || true)
DIRTY=$(git status --porcelain --untracked-files=normal 2>/dev/null || echo "not a git repository")
if [[ -n "$TAG" && -z "$DIRTY" ]]; then
    VERSION=${TAG#v}
    [[ "$VERSION" =~ $SEMVER ]] || { echo "tag $TAG is not a version (expected v1.2.3)" >&2; exit 1; }
elif (( DRY_RUN )); then
    # what the package itself reports for this tree (hatch-vcs): 1.2.4.dev3+g<sha>[.d<date>]
    VERSION=$(uv run --quiet python -c "import istok; print(istok.__version__)")
    echo "note: HEAD is ${TAG:-not tagged}${DIRTY:+, work tree not clean}: dry run with version $VERSION"
else
    [[ -z "$TAG" ]] && echo "HEAD has no version tag: commit, then tag it, e.g. git tag v1.2.3" >&2
    [[ -n "$DIRTY" ]] && echo "the work tree is not clean (commit or stash first):" >&2 && echo "$DIRTY" | head -10 >&2
    exit 1
fi
IMAGE="$REPO:${VERSION//+/-}"
echo "version: $VERSION → image $IMAGE"
if (( ! DRY_RUN && ! FORCE )) && docker manifest inspect "$IMAGE" >/dev/null 2>&1; then
    echo "$IMAGE is already published. Tag a new version (git tag v1.2.4)," >&2
    echo "or pass --force to overwrite it (a host that already has this tag will not re-pull it)." >&2
    exit 1
fi
step() { printf '\n==> %s\n' "$*"; }

step "checks: lockfile and tests"
uv lock --check
uv run pytest -q

step "build $IMAGE (linux/amd64)"
docker build --platform linux/amd64 --build-arg VERSION="$VERSION" -t "$IMAGE" -t "$REPO:latest" .

step "smoke test: run as 568:568 (TrueNAS 'apps'), wait for /heartbeat"
WORK=$(mktemp -d)
NAME="stremio-smoke-$$"
cleanup() {
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    docker run --rm -v "$WORK:/w" alpine:3 rm -rf /w/data >/dev/null 2>&1 || true
    rm -rf "$WORK"
}
trap cleanup EXIT
mkdir -p "$WORK/data"
chmod 0777 "$WORK/data"
# host networking: the bridge network needs NAT support that some hosts lack; the test only talks to localhost
SMOKE_PORT=11499
docker run -d --name "$NAME" --network host --user 568:568 \
    -e PORT="$SMOKE_PORT" -e BT_PORT=42099 -e CACHE_SIZE=1G \
    -v "$WORK/data:/data" "$IMAGE" >/dev/null
for _ in $(seq 1 60); do
    if curl -sf -m 1 "http://127.0.0.1:$SMOKE_PORT/heartbeat" >/dev/null; then break; fi
    sleep 0.5
done
curl -sf -m 2 "http://127.0.0.1:$SMOKE_PORT/heartbeat" >/dev/null || { docker logs "$NAME"; echo "smoke test: no heartbeat" >&2; exit 1; }
SETTINGS=$(curl -sf -m 5 "http://127.0.0.1:$SMOKE_PORT/settings")
echo "$SETTINGS" | grep -q '"btListenPort": 42099' || { echo "smoke test: BT_PORT not applied" >&2; exit 1; }
echo "$SETTINGS" | grep -q '"cacheRoot": "/data"' || { echo "smoke test: CACHE_ROOT not applied" >&2; exit 1; }
[[ "$(docker exec "$NAME" id -u)" == "568" ]] || { echo "smoke test: not running as 568" >&2; exit 1; }
INNER=$(docker exec "$NAME" python -c "import istok; print(istok.__version__)")
[[ "$INNER" == "$VERSION" ]] || { echo "smoke test: image reports version $INNER, expected $VERSION" >&2; exit 1; }
FONTS=$(docker exec "$NAME" python -c "from istok.subs import fallback
print(sorted({f.family for f in fallback.library('/usr/share/fonts').faces}))")
for family in "Noto Sans" "Noto Serif" "Noto Sans Mono" "Noto Sans CJK JP" "Noto Serif CJK JP" "Liberation Sans" "Liberation Serif" "Liberation Mono"; do
    [[ "$FONTS" == *"'$family'"* ]] || { echo "smoke test: fallback font $family is missing ($FONTS)" >&2; exit 1; }
done
docker stop -t 30 "$NAME" >/dev/null   # graceful stop must save the DHT state
[[ -s "$WORK/data/config/bt-session.state" ]] || { docker logs "$NAME"; echo "smoke test: no DHT state saved on stop" >&2; exit 1; }
[[ -d "$WORK/data/stremio-cache" ]] || { echo "smoke test: no cache directory under /data" >&2; exit 1; }
echo "smoke test passed"

if (( DRY_RUN )); then
    step "dry run: not pushing $IMAGE"
    exit 0
fi

step "push $IMAGE and $REPO:latest"
if ! docker push "$IMAGE"; then
    echo "push failed — are you logged in? run: docker login" >&2
    exit 1
fi
docker push "$REPO:latest"
DIGEST=$(docker inspect --format '{{index .RepoDigests 0}}' "$IMAGE" 2>/dev/null || true)
step "published"
echo "image:  $IMAGE"
[[ -n "$DIGEST" ]] && echo "digest: $DIGEST   (pin this in TrueNAS for a reproducible install)"
