# Stremio-compatible streaming server (Python + libtorrent) on Alpine.
# libtorrent comes from its musllinux wheel on PyPI, so every dependency is installed by uv from uv.lock.

# Base images are pinned by digest (Dependabot keeps them current): a rebuild of the same commit gets the same base,
# and a security fix in the base arrives as a reviewed PR that runs the whole CI, image scan included.
FROM ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 AS uv

FROM python:3.13-alpine@sha256:7984cf56ceaddebe4fefeb75d64000b9f45d8b77b11a09b24524da6cdc3af0c8 AS build
COPY --from=uv /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
# dependencies first (cached layer)
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project --no-cache
COPY README.md LICENSE ./
COPY protoka ./protoka
# the build context has no .git: the version (the git tag, from scripts/publish.sh) comes in as an argument
ARG VERSION=0.0.0
RUN SETUPTOOLS_SCM_PRETEND_VERSION="$VERSION" uv sync --locked --no-dev --no-editable --no-cache

FROM python:3.13-alpine@sha256:7984cf56ceaddebe4fefeb75d64000b9f45d8b77b11a09b24524da6cdc3af0c8
ARG VERSION=dev
LABEL org.opencontainers.image.title="protoka" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.description="Stremio-compatible streaming server (libtorrent, sidecar subtitles embedded into the stream)" \
      org.opencontainers.image.version="${VERSION}"
# libstdc++: C++ runtime for the libtorrent extension; tini: proper signal handling as PID 1;
# ffmpeg: remuxing non-MKV videos with their subtitles; libarchive-tools (bsdtar): fonts in RAR archives.
# User 568:568 = "apps" on TrueNAS, the owner of app datasets. Any other UID works too, as long as
# /data is writable for it (world-writable here for the unmounted default).
# Fonts: substitutes for subtitle fonts a torrent lacks (analysis/17-font-fallback.md): Noto (Latin, Cyrillic,
# Greek), Noto CJK (Japanese/Chinese/Korean), Liberation (metric twins of Arial / Times New Roman / Courier New).
# apk upgrade: Alpine security fixes released after the base image was built go in right away.
# pip/ensurepip are removed: nothing is installed at runtime (uv did it in the build stage), and pip is a
# frequent source of scanner findings in otherwise clean images.
RUN apk upgrade --no-cache \
    && apk add --no-cache libstdc++ tini ffmpeg libarchive-tools font-noto font-noto-cjk font-liberation \
    && python -m pip uninstall -y -q pip \
    && rm -rf /usr/local/lib/python3.13/ensurepip /usr/local/bin/pip* \
    && addgroup -g 568 apps \
    && adduser -D -u 568 -G apps -h /home/apps apps \
    && mkdir -p /data \
    && chown 568:568 /data \
    && chmod 0777 /data
COPY --from=build /app/.venv /app/.venv
# Configuration is environment-only (set them in the TrueNAS Custom App form):
#   PORT        HTTP API port (11470)            BT_PORT     BitTorrent TCP+uTP port (42069)
#   CACHE_SIZE  cache limit: 50G, 0, unlimited   LOG_LEVEL   INFO / DEBUG
#   FONT_FALLBACK  0 = leave missing subtitle fonts to the player
# Everything lives under ONE mount point, /data:
#   /data/config          settings, DHT routing table           (APP_PATH)
#   /data/stremio-cache/  torrent cache, one folder per torrent (CACHE_ROOT=/data → <root>/stremio-cache)
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    APP_PATH=/data/config \
    CACHE_ROOT=/data \
    HOST=0.0.0.0 \
    PORT=11470 \
    BT_PORT=42069
USER 568:568
# mount /data (a host path / dataset on TrueNAS, a named volume elsewhere) or a new container starts cold
VOLUME ["/data"]
# HTTP API
EXPOSE 11470
# BitTorrent (TCP + uTP); publish/forward it for incoming peers
EXPOSE 42069/tcp 42069/udp
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD wget -qO- "http://127.0.0.1:${PORT}/heartbeat" >/dev/null || exit 1
ENTRYPOINT ["/sbin/tini", "--"]
CMD ["protoka"]
