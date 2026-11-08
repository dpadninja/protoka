# protoka: Stremio-compatible streaming server (Python + libtorrent) on Alpine.
# Base images are pinned by digest; Dependabot updates them through reviewed PRs.
FROM ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 AS uv

FROM python:3.13-alpine@sha256:7984cf56ceaddebe4fefeb75d64000b9f45d8b77b11a09b24524da6cdc3af0c8 AS build
COPY --from=uv /uv /bin/uv
RUN apk add --no-cache binutils
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
# dependencies first, so a code change does not reinstall them
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project
COPY README.md LICENSE ./
COPY protoka ./protoka
# the context has no .git: scripts/build.sh and publish.sh pass the version in
ARG VERSION=0.0.0
# --no-cache: uv's cache key for the project ignores SETUPTOOLS_SCM_PRETEND_VERSION, so a cached wheel would carry
# an old version. strip: wheels ship native extensions with debug sections (libtorrent 37 → 18 MB).
RUN SETUPTOOLS_SCM_PRETEND_VERSION="$VERSION" uv sync --locked --no-dev --no-editable --no-cache \
    && find /app/.venv -type f \( -name '*.so' -o -name '*.so.*' \) -exec strip --strip-unneeded {} +

FROM python:3.13-alpine@sha256:7984cf56ceaddebe4fefeb75d64000b9f45d8b77b11a09b24524da6cdc3af0c8
ARG VERSION=0.0.0
LABEL org.opencontainers.image.title="protoka" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.licenses="MIT"
# libstdc++: libtorrent; tini: PID 1; ffmpeg: remuxing; fonts: substitutes for subtitle fonts a torrent lacks.
# apk upgrade: Alpine fixes newer than the base image.
# pip is removed: nothing is installed at runtime, and it only adds scanner findings.
# /data: owner 10001, group 0, mode 0770. Any UID started without a group runs with GID 0, so it can write too.
RUN apk upgrade --no-cache \
    && apk add --no-cache libstdc++ tini ffmpeg font-noto font-noto-cjk font-liberation \
    && python -m pip uninstall -y -q pip \
    && rm -rf /usr/local/lib/python3.13/ensurepip /usr/local/bin/pip* \
    && install -d -o 10001 -g 0 -m 0770 /data
COPY --link --from=build /app/.venv /app/.venv
# configuration is env-only (see README); HOME=/tmp is writable for any UID
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    APP_PATH=/data/config \
    CACHE_ROOT=/data \
    HOST=0.0.0.0 \
    PORT=11470 \
    BT_PORT=42069
USER 10001:10001
VOLUME ["/data"]
EXPOSE 11470 42069/tcp 42069/udp
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD wget -qO- "http://127.0.0.1:${PORT}/heartbeat" >/dev/null || exit 1
ENTRYPOINT ["/sbin/tini", "--"]
CMD ["protoka"]
