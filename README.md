# protoka

**An alternative Stremio streaming server with extras**

Stremio plays torrents through a separate local service, the *streaming server*. protoka replaces it: keep using
the Stremio app and point it at protoka.

> protoka is an independent project. It is not affiliated with or endorsed by Stremio. The name "Stremio" is used
> only to describe compatibility.

---

## Features

- **External subtitles.** Subtitle files shipped separately in a torrent are found automatically and muxed into the
  video stream, so the player shows them as ordinary subtitle tracks.
- **External audio tracks.** Dubs shipped as separate files are found and added to the video stream as extra audio
  tracks.
- **Rich subtitle support (ASS/SSA).** Styled subtitles keep their styles, positioning and karaoke: the fonts they
  need are attached to the stream, and fonts missing from the torrent are replaced by similar bundled ones.
- **The next episode is already there.** While you watch, the start and end of the next episode download in the
  background, so it starts instantly.
- **Faster peer discovery.** Playback starts sooner, and other peers can connect to protoka too, which the
  official server does not allow.
- **Persistent seeding.** Everything in the cache keeps seeding, even after a restart, with configurable limits.
  What you are watching always gets the bandwidth first.
- **Drop-in replacement.** Implements the torrent-streaming part of the Stremio streaming server's API (v4.22.5).

---

## Limitations

- **No HLS, so no Stremio web app.** protoka sends the original video, and browsers often cannot play its codecs 
(HEVC, AC3, DTS, etc) or its ASS subtitles.
- **ASS subtitles depend on the player.** mpv and VLC render them in full; ExoPlayer (Android, Android TV) shows only
  basic styles without the attached fonts.
- **Also not implemented:** the HTTPS endpoint on 12470, casting, the bundled web UI.

---

## Quick start

### From Docker
The image is on Docker Hub: [`horsetail/protoka`](https://hub.docker.com/r/horsetail/protoka).

```sh
docker run -d --name protoka --restart unless-stopped \
  -p 11470:11470 -p 42069:42069/tcp -p 42069:42069/udp \
  -v protoka-data:/data \
  horsetail/protoka:latest
```

- **Data:** settings, the DHT state and the torrent cache live in the `protoka-data` volume. Keep it, or the DHT
  starts cold.
- **User:** the container runs as UID 10001. With a host directory instead of a volume
  (`-v /srv/protoka:/data`), run it as the owner of that directory: `--user "$(id -u):$(id -g)"`, or
  `--user 568:568` for a TrueNAS dataset of the `apps` user. A volume created by protoka 0.1.x belongs to 568: add
  `--user 568:568` to keep using it.
- **Peers:** forward the BitTorrent port 42069 (TCP and UDP) on your router. On Linux, `--network host` instead of
  the `-p` options lets protoka open it via UPnP / NAT-PMP itself.

### From source
```sh
uv sync
uv run protoka --port 11470 --app-path ./data --bt-port 42069
```
Requires Python ≥ 3.13 and [uv](https://docs.astral.sh/uv/). `ffmpeg`/`ffprobe` are needed for AVI/MP4 remuxing
and raw audio, `bsdtar` for fonts in RAR archives, and the Noto, Noto CJK and Liberation font packages for
replacing fonts a torrent lacks. Data goes to `./data`.

### Check
`http://<host>:11470/heartbeat` returns `{"success": true}`.

### Connect Stremio
In the Stremio app open *Settings*, find *Streaming server URL* and enter `http://<host>:11470`, where `<host>` is
the LAN address of the machine running protoka.

This works for the desktop, Android TV and mobile apps and for external players. The web app (`web.stremio.com`)
is not supported, see [Limitations](#limitations).

---

## Configuration

Environment variables are applied on every start and override saved settings. They are all optional.

| Variable | Default | Meaning |
|---|---|---|
| `PORT` | `11470` | HTTP API port |
| `BT_PORT` | `42069` | BitTorrent listen port (TCP + uTP) |
| `APP_PATH` | `~/.stremio-server` (`/data/config` in Docker) | settings, DHT state, library state |
| `CACHE_ROOT` | app path (`/data` in Docker) | the cache goes to `<CACHE_ROOT>/stremio-cache` |
| `CACHE_SIZE` | `50G` | cache budget: `50G`, `512M`, `unlimited`, `0` = no caching (recently watched torrents may exceed it, see below) |
| `MIN_FREE_DISK` | `2G` | evict when the filesystem has less free space than this |
| `CACHE_KEEP_RECENT` | `24h` | `CACHE_SIZE` never evicts what was watched this recently (`0` = off); low disk space still does |
| `PREFERRED_AUDIO_LANG` | — | external dub in this language becomes the default track (`rus`, `en`, `japanese`…) |
| `MUX_AUDIO_WAIT` | `90` | seconds to wait for a raw audio repack before starting playback (`0` = don't wait; the track is added next time) |
| `MUX_REPACK_CONCURRENCY` | `1` | audio repacks running at once |
| `FONT_FALLBACK` | `1` | replace missing subtitle fonts with bundled ones (`0` = leave it to the player) |
| `FALLBACK_FONTS_DIR` | `/usr/share/fonts` | where the bundled fonts are |
| `SEED_UPLOAD_LIMIT` | `0` | global upload cap, bytes/s (`2M`, `512K`; `0` = unlimited) |
| `STREAM_BG_UPLOAD_LIMIT` | `0` | while watching: total upload of the *other* torrents |
| `SEED_RATIO_LIMIT` / `SEED_TIME_LIMIT` | `0` | stop seeding a torrent after this ratio (`2.0`) / time (`7d`); `0` = forever |
| `SEED_MAX_ACTIVE` | `0` | active seeds at once (libtorrent rotates the rest); `0` = all |
| `BG_DOWNLOADS` | `1` | background downloads at once (finishing watched files, prefetch) |
| `BG_MAX_CONNECTIONS` | `20` | connections per torrent that is not being watched |
| `STREAM_GRACE` | `120` | seconds a torrent still counts as "being watched" after the last request |
| `PREFETCH_NEXT` | `true` | prefetch the next episode's head and tail |
| `PREFETCH_HEAD` / `PREFETCH_TAIL` | `32M` / `8M` | how much of the next episode to prefetch |
| `LOG_LEVEL` | `INFO` | `DEBUG` also logs every range request and ffmpeg command |

The rest of the settings (`btMaxConnections`, `btDownloadLimit`, `subtitlesMux`…) can be changed through
`POST /settings`, like with the official server.

---

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
| `GET`/`POST` | `/settings` | read / change settings |
| `GET` | `/network-info`, `/device-info` | host information |

**protoka additions:**

| Method | Path | |
|---|---|---|
| `GET` | `/:infoHash/:fileIdx?mux=0` | the original file, without embedded tracks |
| `GET` | `/:infoHash/:fileIdx/tracks.json` | what was found for this video (subtitles, audio, fonts, font substitutes) and why |
| `GET` | `/:infoHash/:fileIdx/subtitles/:id.(ass\|srt\|vtt)` | an external subtitle, converted |
| `GET` | `/:infoHash/fonts/:font` | a font from the torrent |
| `GET` | `/seeding.json` | cache budget and usage, background state, every torrent's state and reasons, recent evictions |

---

## Development

```sh
uv sync                     # dependencies (always via uv)
uv run pytest               # fully offline, ~80 s; ffmpeg-dependent tests are skipped without ffmpeg
uv run ruff check . && uv run ruff format --check . && uv run mypy protoka
./scripts/publish.sh <user>/protoka --dry-run   # tests, image build, smoke tests (default and arbitrary UID)
```

The tests run real libtorrent sessions against local seeders, generate MKV/AVI media and fonts on the fly, and
check the stream with ffprobe.

**Layout:**
- `protoka/` — the session, engine and streaming core, plus the HTTP layer;
- `protoka/subs/` — subtitle and font discovery, conversion and font fallback;
- `protoka/mux/` — EBML / Matroska writer, virtual MKV, ffmpeg;
- `protoka/library/` — seeding policy, next episode, eviction.

Complex logic is split into pure, testable steps and thin I/O services. Decisions such as matches, font
substitutes and seeding states carry their reasons.

The version is the git tag of the commit (`git tag v1.2.3`), read by hatch-vcs; untagged commits get
`1.2.4.devN+g<sha>`. `scripts/publish.sh` publishes only a tagged, clean commit, tags the image with that version and
refuses to overwrite a published tag.

---

## Security

Images are published only after they pass the same checks locally (`scripts/publish.sh`) and in CI:

- **Scanning.** [Trivy](https://trivy.dev) checks `uv.lock`, the Dockerfile and secrets before the build, then the
  built image: Alpine packages and the Python environment. A HIGH or CRITICAL finding that has a fix stops the
  release before anything is pushed (`scripts/scan.sh`). Accepted exceptions live in `.trivyignore.yaml`, each with
  a reason and an expiry date.
- **Hardened image.** Base images are pinned by digest. `apk upgrade` runs at build time, and pip is removed from
  the runtime image. The container runs as a non-root user (UID 10001; override it with `--user`), and `/data` is writable
  only for that user and for GID 0 (arbitrary UIDs, OpenShift style), not for everyone.
- **CI on every push and PR.** It runs ruff with the bandit rules, mypy, the tests, the Trivy scans (reports go to
  *Security → Code scanning*), CodeQL and [zizmor](https://docs.zizmor.sh) on the workflows. All actions are pinned
  by commit SHA.
- **Staying clean.** Dependabot sends weekly PRs for `uv.lock`, the base image digests and the actions. A weekly
  re-scan of the published `:latest` opens an issue when a new fixable CVE appears; it is fixed by a new patch
  release, and published tags are never rewritten.
- **Supply chain.** Released images carry an SBOM and SLSA provenance attestations and a keyless
  [cosign](https://docs.sigstore.dev) signature from the release workflow. The exact `cosign verify` command for
  each image is in its GitHub release notes.

---

## Third-party components

protoka's own code is MIT-licensed. It depends on, and its Docker image ships:

- **Python packages:** libtorrent (BSD), aiohttp (Apache-2.0), fontTools (MIT), psutil (BSD), regex (Apache-2.0),
  charset-normalizer (MIT), rarfile (ISC), py7zr and its codecs (LGPL-2.1+). They are installed unmodified.
- **ffmpeg** (LGPL/GPL, Alpine package). It is run as a separate process, not linked.
- **Fonts:** Noto, Noto CJK and Liberation, all under the SIL Open Font License 1.1. They are unmodified in the
  image. Subsets are made at runtime, on your machine, for your own playback.

---

## License

[MIT](LICENSE)
