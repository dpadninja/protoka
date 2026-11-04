# Stremio-compatible streaming server (Python + libtorrent) on Alpine.
# libtorrent comes from its musllinux wheel on PyPI, so every dependency is installed by uv from uv.lock.

ARG PYTHON_VERSION=3.13
ARG UV_VERSION=0.13.6

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM python:${PYTHON_VERSION}-alpine AS build
COPY --from=uv /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
# dependencies first (cached layer)
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project --no-cache
COPY README.md LICENSE ./
COPY istok ./istok
# the build context has no .git: the version (the git tag, from scripts/publish.sh) comes in as an argument
ARG VERSION=0.0.0
RUN SETUPTOOLS_SCM_PRETEND_VERSION="$VERSION" uv sync --locked --no-dev --no-editable --no-cache

FROM python:${PYTHON_VERSION}-alpine
ARG VERSION=dev
LABEL org.opencontainers.image.title="istok" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.description="Stremio-compatible streaming server (libtorrent, sidecar subtitles embedded into the stream)" \
      org.opencontainers.image.version="${VERSION}"
# libstdc++: C++ runtime for the libtorrent extension; tini: proper signal handling as PID 1;
# ffmpeg: remuxing non-MKV videos with their subtitles; libarchive-tools (bsdtar): fonts in RAR archives.
# User 568:568 = "apps" on TrueNAS, the owner of app datasets. Any other UID works too, as long as
# /data is writable for it (world-writable here for the unmounted default).
# Fonts: substitutes for subtitle fonts a torrent lacks (analysis/17-font-fallback.md): Noto (Latin, Cyrillic,
# Greek), Noto CJK (Japanese/Chinese/Korean), Liberation (metric twins of Arial / Times New Roman / Courier New).
RUN apk add --no-cache libstdc++ tini ffmpeg libarchive-tools font-noto font-noto-cjk font-liberation \
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
CMD ["istok"]
