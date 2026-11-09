<div align="center">

# ≋ protoka

**An alternative Stremio streaming server with extras**

[![CI](https://github.com/dpadninja/protoka/actions/workflows/ci.yml/badge.svg)](https://github.com/dpadninja/protoka/actions/workflows/ci.yml)
[![Docker Hub](https://img.shields.io/docker/v/horsetail/protoka?sort=semver&label=docker)](https://hub.docker.com/r/horsetail/protoka)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

[Features](#features) · [Quick start](#quick-start) · [Configuration](#configuration) · [HTTP API](#http-api) · [Development](#development)

</div>

Stremio plays torrents through a separate local service, the *streaming server*. protoka replaces it: keep using
the Stremio app and point it at protoka.

> [!NOTE]
> protoka is an independent project. It is not affiliated with or endorsed by Stremio. The name "Stremio" is used
> only to describe compatibility.

## Features

- **External subtitles.** Subtitle files shipped next to the video are found and muxed into the stream, so the
  player shows them as ordinary subtitle tracks.
- **External audio tracks.** Dubs shipped as separate files are added to the stream as extra audio tracks.
- **Rich ASS/SSA subtitles.** Styles, positioning and karaoke are kept. The fonts a script needs are attached to
  the stream, and fonts missing from the torrent are replaced by similar bundled ones.
- **The next episode is already there.** Once the episode you watch is downloaded, the next one downloads with its
  subtitles, dubs and fonts, so it starts instantly. Nothing downloads once you stop watching.
- **Faster peer discovery.** Playback starts sooner, and other peers can connect to protoka too, which the
  official server does not allow.
- **Persistent seeding.** The cache keeps seeding, even after a restart, within configurable limits. What you are
  watching always gets the bandwidth first.
- **Drop-in replacement.** Implements the torrent-streaming part of the Stremio streaming server API (v4.22.5).

> [!IMPORTANT]
> **Limitations**
> - **No Stremio web app.** protoka sends the original video without HLS, and browsers often cannot play its
>   codecs (HEVC, AC3, DTS…) or ASS subtitles.
> - **ASS rendering depends on the player.** mpv and VLC render it in full; ExoPlayer (Android, Android TV) shows
>   basic styles only, without the attached fonts.
> - **Not implemented:** the HTTPS endpoint on 12470, casting, the bundled web UI.

## Quick start

1. Run the image from Docker Hub:

   ```sh
   docker run -d --name protoka --restart unless-stopped \
     -p 11470:11470 -p 42069:42069/tcp -p 42069:42069/udp \
     -v protoka-data:/data \
     horsetail/protoka:latest
   ```

2. Check it: `http://<host>:11470/heartbeat` returns `{"success": true}`.
3. In Stremio open *Settings* → *Streaming server URL* and enter `http://<host>:11470`, where `<host>` is the LAN
   address of the machine running protoka. This works for the desktop, Android TV and mobile apps and for external
   players.

**Good to know:**
- **Data:** the DHT state, the library state and the torrent cache live in the `protoka-data` volume. Keep it, or the DHT
  starts cold.
- **User:** the container runs as UID 10001. With a host directory instead of a volume (`-v /srv/protoka:/data`),
  run it as the owner of that directory: `--user "$(id -u):$(id -g)"`, or `--user 568:568` for a TrueNAS dataset
  of the `apps` user. A volume created by protoka 0.1.x belongs to 568: add `--user 568:568` to keep using it.
- **Peers:** forward the BitTorrent port 42069 (TCP and UDP) on your router. On Linux, `--network host` instead
  of the `-p` options lets protoka open it via UPnP / NAT-PMP itself.
- **Settings:** environment variables, e.g. `-e CACHE_SIZE=100G`, see [Configuration](#configuration).

> [!TIP]
> protoka writes only to `/data` and `/tmp` and needs no capabilities, so it also runs locked down:
> `--read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges`.

### TrueNAS

1. *Apps → Discover Apps → ⋮ → Install via YAML*, paste [`truenas/compose.yaml`](truenas/compose.yaml) as is.
2. Point Stremio at `http://<TrueNAS IP>:11470`.

Nothing to set up, as with the catalog apps: the data goes to an app volume that TrueNAS manages (a new volume
takes the image's `/data`, already writable by protoka), and protoka runs locked down (read-only root, no
capabilities), with 30 s on stop to save the DHT state.

Updates come by themselves: on every start the app checks Docker Hub for a newer `:latest`, so restarting it (or
the NAS) brings the latest release. It needs network at start: without it the app stays down until started again.

Optional: your own dataset instead of the volume (replace `data:/data` with `/mnt/<pool>/<dataset>:/data` and
add `user: "568:568"`; the dataset must be writable by the `apps` user, e.g. the *Apps* ACL preset), settings as `environment` entries (see [Configuration](#configuration)),
a pinned version (`horsetail/protoka:0.2.0`) instead of `:latest`.

## Configuration

The server is configured only by environment variables, all optional. Stremio's settings screen shows these values
but cannot change them: `POST /settings` is accepted and ignored, nothing is saved.

| Variable | Default | Meaning |
|---|---|---|
| `PORT` | `11470` | HTTP API port |
| `BT_PORT` | `42069` | BitTorrent listen port (TCP + uTP) |
| `APP_PATH` | `~/.stremio-server` (`/data/config` in Docker) | DHT state, library state |
| `CACHE_ROOT` | app path (`/data` in Docker) | the cache goes to `<CACHE_ROOT>/stremio-cache` |
| `CACHE_SIZE` | `128G` | cache budget: `128G`, `512M`, `unlimited` (recently watched torrents may exceed it) |
| `MIN_FREE_DISK` | `2G` | evict when the filesystem has less free space than this |
| `CACHE_KEEP_RECENT` | `24h` | `CACHE_SIZE` never evicts what was watched this recently (`0` = off); low disk space still does |
| `BT_MAX_CONNECTIONS` | `200` | connections per torrent being watched |
| `BT_DOWNLOAD_LIMIT` | `0` | download cap per torrent, bytes/s (`2M`; `0` = unlimited; any cap delays each request ~0.5 s) |
| `SUBTITLES_MUX` | `true` | embed the torrent's external subtitles and fonts into the video stream |
| `PREFERRED_AUDIO_LANG` | — | an external dub in this language becomes the default track (`rus`, `en`, `japanese`…) |
| `MUX_AUDIO_WAIT` | `90` | seconds to wait for a raw audio repack before playback starts (`0` = don't wait; the track is added next time) |
| `MUX_REPACK_CONCURRENCY` | `1` | audio repacks running at once |
| `FONT_FALLBACK` | `1` | replace missing subtitle fonts with bundled ones (`0` = leave it to the player) |
| `FALLBACK_FONTS_DIR` | `/usr/share/fonts` | where the bundled fonts are |
| `SEED_UPLOAD_LIMIT` | `0` | global upload cap, bytes/s (`2M`, `512K`; `0` = unlimited) |
| `STREAM_BG_UPLOAD_LIMIT` | `0` | while watching: total upload of the *other* torrents |
| `SEED_RATIO_LIMIT` / `SEED_TIME_LIMIT` | `0` | stop seeding a torrent after this ratio (`2.0`) / time (`7d`); `0` = forever |
| `SEED_MAX_ACTIVE` | `0` | active seeds at once (libtorrent rotates the rest); `0` = all |
| `BG_MAX_CONNECTIONS` | `20` | connections per torrent that is not being watched |
| `STREAM_GRACE` | `120` | seconds a torrent still counts as "being watched" after the last request |
| `PREFETCH_NEXT` | `true` | once the watched episode is downloaded, download the next one with its subtitles, audio and fonts |
| `LOG_LEVEL` | `INFO` | `DEBUG` also logs every range request and ffmpeg command |

## HTTP API

**Compatible with the official server:**

| Method | Path | |
|---|---|---|
| `GET` | `/heartbeat` | liveness |
| `*` | `/create` | add a torrent from `{"blob": "<hex .torrent>"}` |
| `*` | `/:infoHash/create` | add by info hash (+ sources, `guessFileIdx`, `fileMustInclude`) |
| `GET`/`HEAD` | `/:infoHash/:fileIdx` | stream a file (Range); the index may also be a file name |
| `GET` | `/stats.json`, `/:infoHash/stats.json`, `/:infoHash/:fileIdx/stats.json` | statistics (`?sys=1` adds system and DHT info) |
| `GET` | `/:infoHash/remove`, `/removeAll` | stop watching (the torrent keeps seeding; `?purge=1` deletes it) |
| `GET`/`POST` | `/settings` | the effective settings; a POST succeeds and changes nothing |
| `GET` | `/network-info`, `/device-info` | host information |

**protoka additions:**

| Method | Path | |
|---|---|---|
| `GET` | `/:infoHash/:fileIdx?mux=0` | the original file, without embedded tracks |
| `GET` | `/:infoHash/:fileIdx/tracks.json` | what was found for this video (subtitles, audio, fonts, font substitutes) and why |
| `GET` | `/:infoHash/:fileIdx/subtitles/:id.(ass\|srt\|vtt)` | an external subtitle, converted |
| `GET` | `/:infoHash/fonts/:fileIdx` | a font file from the torrent |
| `GET` | `/seeding.json` | cache budget and usage, whether the next episode may download, every torrent's state and reasons, recent evictions |

## Development

### Run from source

```sh
uv sync
uv run protoka --port 11470 --app-path ./data --bt-port 42069
```

Requires Python ≥ 3.13 and [uv](https://docs.astral.sh/uv/) (dependencies are managed only through uv). For the
full feature set the host also needs `ffmpeg`/`ffprobe` (AVI/MP4 remuxing, raw audio) and the Noto, Noto CJK and
Liberation fonts (font fallback). Data goes to `./data`.

### Build and run the image locally

```sh
./scripts/build.sh                    # protoka:dev from the work tree, uncommitted changes included
docker run --rm --name protoka-dev \
  -p 11470:11470 -p 42069:42069/tcp -p 42069:42069/udp \
  -v protoka-dev-data:/data \
  protoka:dev
```

`./scripts/build.sh <name:tag>` picks another image name. The script only builds: no tests, scans or push. The
container runs in the foreground with its log in the terminal; Ctrl+C stops it, and `protoka-dev-data` keeps its
state.

> [!NOTE]
> The ports clash with a running `protoka` container: stop it first, or map other host ports (`-p 11471:11470`).

### Checks

```sh
uv run pytest                                   # fully offline, ~100 s; ffmpeg tests are skipped without ffmpeg
uv run ruff check . && uv run ruff format --check . && uv run mypy protoka
./scripts/publish.sh <user>/protoka --dry-run   # tests, scans, image build, smoke tests
```

The tests run real libtorrent sessions against local seeders, generate MKV/AVI media and fonts on the fly, and
check the stream with ffprobe.

### Layout

| Path | |
|---|---|
| `protoka/` | session, engine, streaming core, HTTP layer |
| `protoka/subs/` | subtitle and font discovery, conversion, font fallback |
| `protoka/mux/` | EBML / Matroska writer, virtual MKV, ffmpeg |
| `protoka/library/` | seeding policy, next episode, eviction |

Complex logic is split into pure, testable steps and thin I/O services. Decisions such as matches, font
substitutes and seeding states carry their reasons.

### Versions and releases

The version is the git tag of the commit (`git tag v1.2.3`), read by hatch-vcs; untagged commits get
`1.2.4.devN+g<sha>`. `scripts/publish.sh` publishes only a tagged, clean commit, tags the image with that version
and refuses to overwrite a published tag.

## Security

Images are published only after they pass the same checks locally (`scripts/publish.sh`) and in CI:

- **Scanning.** [Trivy](https://trivy.dev) checks `uv.lock`, the Dockerfile and secrets before the build, then the
  image (Alpine packages and the Python environment). A fixable HIGH or CRITICAL finding stops the release before
  anything is pushed. Accepted exceptions live in `.trivyignore.yaml`, each with a reason and an expiry date.
- **Hardened image.** Base images are pinned by digest, `apk upgrade` runs at build time, pip is removed. The
  container runs as UID 10001, and `/data` is writable only for that user and GID 0 (arbitrary UIDs, OpenShift
  style), not for everyone.
- **Untrusted media.** ffmpeg and ffprobe open torrent files only with media demuxers (no concat, HLS or image
  sequences) and only over HTTP from protoka itself, so a crafted file cannot make them read local files or
  other hosts. Each process is capped in memory and CPU time and gives up after 5 minutes without data.
- **CI on every push and PR.** ruff with the bandit rules, mypy, the tests, the Trivy scans (reports in
  *Security → Code scanning*), CodeQL and [zizmor](https://docs.zizmor.sh) on the workflows. Actions are pinned by
  commit SHA.
- **Staying clean.** Dependabot sends weekly PRs for `uv.lock`, the base image digests and the actions. A weekly
  re-scan of the published `:latest` opens an issue when a new fixable CVE appears; it is fixed by a new patch
  release, and published tags are never rewritten.
- **Supply chain.** Released images carry SBOM and SLSA provenance attestations and a keyless
  [cosign](https://docs.sigstore.dev) signature. The `cosign verify` command for each image is in its GitHub
  release notes.

## Third-party components

protoka's own code is MIT-licensed. The Docker image also ships, unmodified:

- **Python packages:** libtorrent (BSD), aiohttp (Apache-2.0), fontTools (MIT), psutil (BSD), regex (Apache-2.0),
  charset-normalizer (MIT).
- **ffmpeg** (LGPL/GPL, Alpine package), run as a separate process, not linked.
- **Fonts:** Noto, Noto CJK and Liberation (SIL Open Font License 1.1). Subsets are made at runtime, on your
  machine, for your own playback.
