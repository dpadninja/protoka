#!/usr/bin/env bash
# Build the image from the current work tree only: no tests, no scans, no smoke test, no push.
#
#   ./scripts/build.sh [image]        default image: protoka:dev
#
#   The version is what the package reports for this tree (hatch-vcs): the tag on a tagged clean commit,
#   otherwise 1.2.4.devN+g<sha>[.d<date>]. Uncommitted changes are included (the build context is the tree).
#   For a release use scripts/publish.sh.
set -euo pipefail

cd "$(dirname "$0")/.."

case "${1:-}" in
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
    -*) echo "unknown option: $1" >&2; exit 2 ;;
esac
(( $# <= 1 )) || { echo "usage: $0 [image]" >&2; exit 2; }
IMAGE=${1:-protoka:dev}

VERSION=$(uv run --quiet python -c "import protoka; print(protoka.__version__)")
echo "version: $VERSION → image $IMAGE"
docker build --platform linux/amd64 --build-arg VERSION="$VERSION" -t "$IMAGE" .
echo "built $IMAGE"
