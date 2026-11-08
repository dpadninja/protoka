#!/usr/bin/env bash
# Security scans of the project and of its image, with ONE policy for local releases and CI.
#
#   ./scripts/scan.sh source              uv.lock (dependencies) + Dockerfile (misconfigurations) + secrets
#   ./scripts/scan.sh image <ref>         the built image: Alpine packages and the Python venv, plus secrets
#   SARIF=out.sarif ./scripts/scan.sh …   also write a SARIF report (all findings, for GitHub code scanning)
#
# Policy: fail on HIGH/CRITICAL findings that have a fix (an unfixable CVE is not something a rebuild can cure).
# Exceptions live in .trivyignore.yaml, each with a statement and an expiry date; an expired one fails again.
# Trivy runs from a pinned container (no local install needed); its DB is cached in the volume protoka-trivy-cache.
set -euo pipefail
cd "$(dirname "$0")/.."

TRIVY_IMAGE="aquasec/trivy:0.76.1@sha256:790fd66d1697395d8693d1acaa1ea702406a20f109b6fc7bac8db623eca6c06c"
# shellcheck disable=SC2054  # HIGH,CRITICAL is one argument
POLICY=(--severity HIGH,CRITICAL --ignore-unfixed --exit-code 1 --ignorefile .trivyignore.yaml --table-mode detailed)

DOCKER_SOCK=()  # only the image scan needs the Docker socket (it reads the image from the daemon)

trivy() {
    docker run --rm "${DOCKER_SOCK[@]}" \
        -v protoka-trivy-cache:/root/.cache \
        -v "$PWD:/src:ro" -w /src \
        -e TRIVY_DISABLE_VEX_NOTICE=1 \
        "$TRIVY_IMAGE" "$@"
}

sarif() {  # same target, every severity, never failing: the report is for reading, the policy run decides
    [[ -n "${SARIF:-}" ]] || return 0
    trivy "$@" --quiet --format sarif --ignorefile .trivyignore.yaml >"$SARIF"
}

case "${1:-}" in
    source)
        echo "scan: uv.lock and secrets"
        trivy fs --quiet --scanners vuln,secret --skip-dirs .venv --skip-dirs analysis --skip-dirs tests "${POLICY[@]}" .
        echo "scan: Dockerfile"
        trivy config --quiet --severity HIGH,CRITICAL --exit-code 1 Dockerfile
        sarif fs --scanners vuln,secret,misconfig --skip-dirs .venv --skip-dirs analysis --skip-dirs tests .
        ;;
    image)
        ref=${2:?usage: $0 image <ref>}
        DOCKER_SOCK=(-v /var/run/docker.sock:/var/run/docker.sock)
        echo "scan: image $ref"
        trivy image --quiet --scanners vuln,secret "${POLICY[@]}" "$ref"
        sarif image --scanners vuln,secret "$ref"
        ;;
    *)
        sed -n '2,10p' "$0" >&2
        exit 2
        ;;
esac
echo "scan: passed"
