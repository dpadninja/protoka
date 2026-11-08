#!/usr/bin/env bash
# Build, smoke-test and publish the image to Docker Hub.
#
#   ./scripts/publish.sh <dockerhub-user>/protoka [--dry-run] [--force]
#
#   The version is the git tag of the commit being published: `git tag v1.2.3` → image <repo>:1.2.3.
#   Publishing needs HEAD to carry a version tag and a clean work tree, so the image is exactly that commit.
#   --dry-run  build and smoke-test only, push nothing; works on an untagged or dirty tree too
#              (version 1.2.4.dev3+g<sha>, image tag with "+" replaced by "-")
#   --force    push even if this version tag is already published (normally refused)
#
# Security gates (scripts/scan.sh, same policy as CI): dependencies and Dockerfile before the build, the image
# after the smoke test; any fixable HIGH/CRITICAL finding stops the release before anything is pushed.
# Attestations: with buildx and the containerd image store the pushed image gets an SBOM and max-mode provenance.
# They are created by `buildx build --push` itself (a `docker push` of a --load'ed image may drop them); that
# build is served from the cache of the build that was just tested and scanned, and the pushed layers are compared
# with the scanned image before the release counts. REQUIRE_ATTESTATIONS=1 (CI) makes their absence an error.
#
# Log in first: docker login
set -euo pipefail

cd "$(dirname "$0")/.."

REPO="" DRY_RUN=0 FORCE=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        --force) FORCE=1 ;;
        -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
        -*) echo "unknown option: $arg" >&2; exit 2 ;;
        *) if [[ -z "$REPO" ]]; then REPO="$arg"; else
               echo "unexpected argument: $arg (the version comes from the git tag)" >&2; exit 2; fi ;;
    esac
done
if [[ -z "$REPO" || "$REPO" != */* ]]; then
    echo "usage: $0 <dockerhub-user>/protoka [--dry-run] [--force]" >&2
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
    VERSION=$(uv run --quiet python -c "import protoka; print(protoka.__version__)")
    echo "note: HEAD is ${TAG:-not tagged}${DIRTY:+, work tree not clean}: dry run with version $VERSION"
else
    [[ -z "$TAG" ]] && echo "HEAD has no version tag: commit, then tag it, e.g. git tag v1.2.3" >&2
    [[ -n "$DIRTY" ]] && echo "the work tree is not clean (commit or stash first):" >&2 && echo "$DIRTY" | head -10 >&2
    exit 1
fi
IMAGE="$REPO:${VERSION//+/-}"
echo "version: $VERSION → image $IMAGE"
if (( ! DRY_RUN && ! FORCE )) && docker manifest inspect "$IMAGE" >/dev/null 2>&1; then
    # an earlier run of this very tag that pushed the image but failed before attaching attestations is
    # incomplete, not published: CI may finish it (re-run the job) instead of needing a new version
    if [[ "${REQUIRE_ATTESTATIONS:-0}" == 1 ]] \
        && [[ "$(docker buildx imagetools inspect "$IMAGE" --format '{{json .SBOM}}' 2>/dev/null)" != *SPDX* ]]; then
        echo "$IMAGE exists without attestations (an unfinished release of this tag): publishing it again"
    else
        echo "$IMAGE is already published. Tag a new version (git tag v1.2.4)," >&2
        echo "or pass --force to overwrite it (a host that already has this tag will not re-pull it)." >&2
        exit 1
    fi
fi
step() { printf '\n==> %s\n' "$*"; }

step "checks: lockfile and tests"
uv lock --check
uv run pytest -q

step "security: dependencies, secrets, Dockerfile"
./scripts/scan.sh source

# SBOM + provenance need BuildKit (buildx) and an image store that keeps attestations (containerd)
ATTEST=()
if docker buildx version >/dev/null 2>&1 \
    && docker info --format '{{range .DriverStatus}}{{.}}{{end}}' 2>/dev/null | grep -q containerd.snapshotter; then
    ATTEST=(--sbom=true --provenance=mode=max)
elif [[ "${REQUIRE_ATTESTATIONS:-0}" == 1 ]]; then
    echo "attestations required, but buildx or the containerd image store is missing" >&2
    exit 1
else
    echo "warning: no buildx/containerd image store here: the image is built without SBOM/provenance attestations" >&2
fi

BUILD_ARGS=(--platform linux/amd64 --build-arg VERSION="$VERSION" -t "$IMAGE" -t "$REPO:latest")
step "build $IMAGE (linux/amd64)"
docker build "${BUILD_ARGS[@]}" .

step "smoke test: default user on a fresh volume, wait for /heartbeat"
NAME="stremio-smoke-$$"
VOL="stremio-smoke-$$"
VOL2="stremio-smoke-uid-$$"
META=""
cleanup() {
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    docker volume rm -f "$VOL" "$VOL2" >/dev/null 2>&1 || true
    if [[ -n "$META" ]]; then rm -f "$META"; fi
}
trap cleanup EXIT
# host networking: the bridge network needs NAT support that some hosts lack; the test only talks to localhost
SMOKE_PORT=11499
wait_heartbeat() {
    for _ in $(seq 1 60); do
        if curl -sf -m 1 "http://127.0.0.1:$SMOKE_PORT/heartbeat" >/dev/null; then return 0; fi
        sleep 0.5
    done
    docker logs "$NAME"
    echo "smoke test: no heartbeat ($1)" >&2
    exit 1
}
# a new named volume gets the image's /data (10001:0, 0770): the default user must be able to write to it
docker run -d --name "$NAME" --network host \
    -e PORT="$SMOKE_PORT" -e BT_PORT=42099 -e CACHE_SIZE=1G \
    -v "$VOL:/data" "$IMAGE" >/dev/null
wait_heartbeat "default user"
SETTINGS=$(curl -sf -m 5 "http://127.0.0.1:$SMOKE_PORT/settings")
echo "$SETTINGS" | grep -q '"btListenPort": 42099' || { echo "smoke test: BT_PORT not applied" >&2; exit 1; }
echo "$SETTINGS" | grep -q '"cacheRoot": "/data"' || { echo "smoke test: CACHE_ROOT not applied" >&2; exit 1; }
[[ "$(docker exec "$NAME" id -u)" == "10001" ]] || { echo "smoke test: not running as 10001" >&2; exit 1; }
INNER=$(docker exec "$NAME" python -c "import protoka; print(protoka.__version__)")
[[ "$INNER" == "$VERSION" ]] || { echo "smoke test: image reports version $INNER, expected $VERSION" >&2; exit 1; }
FONTS=$(docker exec "$NAME" python -c "from protoka.subs import fallback
print(sorted({f.family for f in fallback.library('/usr/share/fonts').faces}))")
for family in "Noto Sans" "Noto Serif" "Noto Sans Mono" "Noto Sans CJK JP" "Noto Serif CJK JP" "Liberation Sans" "Liberation Serif" "Liberation Mono"; do
    [[ "$FONTS" == *"'$family'"* ]] || { echo "smoke test: fallback font $family is missing ($FONTS)" >&2; exit 1; }
done
docker stop -t 30 "$NAME" >/dev/null   # graceful stop must save the DHT state
docker run --rm -v "$VOL:/data" --entrypoint sh "$IMAGE" -c \
    'test -s /data/config/bt-session.state && test -d /data/stremio-cache && test "$(stat -c %a /data)" = 770' \
    || { docker logs "$NAME"; echo "smoke test: no DHT state / cache dir under /data, or /data is not 0770" >&2; exit 1; }
docker rm -f "$NAME" >/dev/null

step "smoke test: an arbitrary UID without a group (runs with GID 0) on a fresh volume"
docker run -d --name "$NAME" --network host --user 4242 \
    -e PORT="$SMOKE_PORT" -e BT_PORT=42099 -e CACHE_SIZE=1G \
    -v "$VOL2:/data" "$IMAGE" >/dev/null
wait_heartbeat "--user 4242"
docker rm -f "$NAME" >/dev/null
echo "smoke test passed"

step "security: image"
./scripts/scan.sh image "$IMAGE"

if (( DRY_RUN )); then
    step "dry run: not pushing $IMAGE"
    exit 0
fi

step "push $IMAGE and $REPO:latest"
if (( ${#ATTEST[@]} )); then
    META=$(mktemp)
    # same Dockerfile, context and arguments as the tested build: every step comes from its cache
    if ! docker buildx build --push "${ATTEST[@]}" --metadata-file "$META" "${BUILD_ARGS[@]}" .; then
        echo "push failed — are you logged in? run: docker login" >&2
        exit 1
    fi
    DIGEST=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["containerimage.digest"])' "$META")
    # what was pushed must be what was scanned: compare the layer lists (rootfs diff ids)
    LOCAL_LAYERS=$(docker image inspect --format '{{json .RootFS.Layers}}' "$IMAGE")
    PUSHED_LAYERS=$(docker buildx imagetools inspect "$REPO@$DIGEST" --format '{{json .Image}}' \
        | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["rootfs"]["diff_ids"]))')
    if [[ "$(tr -d ' ' <<<"$LOCAL_LAYERS")" != "$(tr -d ' ' <<<"$PUSHED_LAYERS")" ]]; then
        echo "the pushed image differs from the scanned one (the push build missed the cache?)" >&2
        echo "  scanned: $LOCAL_LAYERS" >&2
        echo "  pushed:  $PUSHED_LAYERS" >&2
        exit 1
    fi
    SBOM=$(docker buildx imagetools inspect "$REPO@$DIGEST" --format '{{json .SBOM}}')
    PROVENANCE=$(docker buildx imagetools inspect "$REPO@$DIGEST" --format '{{json .Provenance}}')
    if [[ "$SBOM" != *SPDX* || "$PROVENANCE" != *SLSA* ]]; then
        echo "the pushed image lacks attestations:" >&2
        docker buildx imagetools inspect --raw "$REPO@$DIGEST" >&2
        exit 1
    fi
    echo "attestations: SBOM (SPDX) and provenance (SLSA) are attached"
else
    if ! docker push "$IMAGE"; then
        echo "push failed — are you logged in? run: docker login" >&2
        exit 1
    fi
    docker push "$REPO:latest"
    DIGEST=$(docker inspect --format '{{index .RepoDigests 0}}' "$IMAGE" 2>/dev/null | sed 's/.*@//' || true)
fi
step "published"
echo "image:  $IMAGE"
# `if`, not `[[ … ]] && …`: a false test as the last command would make a successful release exit 1
if [[ -n "$DIGEST" ]]; then
    echo "digest: $DIGEST   (pin $REPO@$DIGEST for a reproducible install)"
    if [[ -n "${GITHUB_OUTPUT:-}" ]]; then echo "digest=$DIGEST" >>"$GITHUB_OUTPUT"; fi
fi
