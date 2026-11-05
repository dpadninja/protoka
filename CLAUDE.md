# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is
**protoka** — a Python reimplementation of the **Stremio streaming server** HTTP API (torrent core only) on
libtorrent + aiohttp (package `protoka`, CLI `protoka`, image `<user>/protoka`; the checkout directory still has its old
name). User-facing docs: `README.md` (English); license MIT (`LICENSE`). `analysis/` is git-ignored: never
publish the upstream bundle (closed source) and do not link the notes from `README.md`. The upstream server is closed-source; its webpack bundle
is kept at `analysis/server-v4.22.5.js` and is the reference for API behaviour (notes cite its line
numbers). Research notes, one per stage, live in `analysis/NN-*.md` (Russian); add a new note
for each new stage of work. `analysis/02-peer-discovery.md` (P1–P11) and `analysis/03-other-bugs.md`
(B1–B19) list the upstream defects this implementation deliberately fixes. Code comments reference those ids.

## Commands
All dependencies are managed with **uv** (add with `uv add …`, never pip).
- Run: `uv run protoka` (or `uv run python -m protoka --port 11470 --app-path DIR --bt-port N`)
- Tests: `uv run pytest` (fully offline, ~45 s; subtitle/mux tests need `ffmpeg`/`ffprobe` and are skipped without them); single test: `uv run pytest tests/test_api.py::test_stream_ranges -q`
- Lint/format/types: `uv run ruff check .`, `uv run ruff format .`, `uv run mypy protoka`
- Absolute imports only (`from protoka.x import y`; ruff TID252 enforces it)
- Docker image (one container, env-only config, one `/data` mount):
  publish with `./scripts/publish.sh <user>/protoka [--dry-run] [--force]` (tests, build, smoke tests as the default UID 10001 and an arbitrary UID, push).
  The version is the **git tag** (`git tag v1.2.3`; hatch-vcs, `dynamic = ["version"]`): publishing needs a tagged,
  clean HEAD, `--dry-run` also works untagged (1.2.4.devN+g<sha>). The image build has no `.git` and gets it as
  `--build-arg VERSION` → `SETUPTOOLS_SCM_PRETEND_VERSION`; `protoka.__version__` reads package metadata; an already
  published tag is refused. Never put a version into `pyproject.toml`.
- Security gates: `scripts/scan.sh source|image <ref>` (Trivy from a pinned container; fail on fixable
  HIGH/CRITICAL; exceptions only in `.trivyignore.yaml` with `statement` + `expired_at`). `publish.sh` runs both
  before pushing; CI (`.github/workflows/ci.yml`) runs them on every push/PR. Ruff includes bandit (`S`): fix, or
  `# noqa: S…` with a reason. Base images are pinned by digest; actions by commit SHA (zizmor checks the workflows).
  The image runs as UID 10001; `/data` is `10001:0` mode 0770 (arbitrary UIDs run with GID 0); bind mounts need
  `--user <owner>` (TrueNAS: 568:568). Never make it world-writable.
  Container config is env-only: `BT_PORT`, `CACHE_ROOT`, `CACHE_SIZE`, `PREFERRED_AUDIO_LANG`, `MUX_AUDIO_WAIT`,
  `FONT_FALLBACK`, `FALLBACK_FONTS_DIR`,
  `PORT`, `APP_PATH`, `LOG_LEVEL`
  (`config.env_overrides`, applied on every start over saved settings). One mount point `/data`:
  `/data/config` (APP_PATH) and `/data/stremio-cache` (CACHE_ROOT=/data).
- Docker (Alpine; libtorrent from its musllinux wheel via uv): `docker compose up -d` (named volume `stremio-data:/data`
  keeps DHT state and cache) or `docker build -t protoka .`

## Architecture
- `session.py` (`TorrentSession`) owns the **single** `lt.session` for the process (shared DHT whose state
  is persisted to `APP_PATH/bt-session.state`: 2 min after start, every 5 min, and on shutdown; an empty table
  never overwrites a saved one; `/stats.json?sys=1` → `sys.dht`), listen port, UPnP), the engine registry, the inactivity
  lifecycle (120 s) and cache cleanup. Alerts arrive via `set_alert_fd` + `loop.add_reader` and are
  dispatched by info-hash to `Engine.on_alert`.
- `engine.py` (`Engine`) wraps one `torrent_handle`. Futures for metadata/pieces are resolved from alerts.
  Data is read via `handle.read_piece()` (not from disk files: write-back caching). Piece deadlines are
  ref-counted across concurrent streams.
- `stream.py`: Range parsing plus `FileStream`, which keeps a sliding window of `set_piece_deadline` ahead of the read position.
- `http_app.py`: aiohttp routes. Specific routes must be registered before `/{ih}/{idx}`. `create_app(..., manage_session=False)`
  is used by the CLI (`__main__.py`), which owns the session lifecycle itself. Tests let the app manage it.
- `http_app.py` (core + service routes, middleware, app factory), `http_subtitles.py` (subtitle/font endpoints),
  `http_common.py` (app keys, shared helpers). `stream_file` serves either the original file, a virtual MKV
  (both through `_serve_ranges`) or an ffmpeg pipe.
- **Sidecar subtitles → stream** (`subs/`, `mux/`, see `analysis/10-subtitles.md`): pure steps
  (`subs/names` → `subs/matching` → `subs/convert`/`subs/ass`/`subs/fonts`; `mux/ebml` → `mux/mkv` → `mux/layout`)
  and I/O services (`subs/service.SubtitleService`, `mux/service.MuxService`). Matching is a list of named
  rules returning `Evidence`; reasons are exposed in `/:ih/:idx/subtitles.json`. New release layout = new row in
  `LAYOUTS` (`tests/test_subs.py`) + a rule. MKV is rebuilt virtually (original clusters byte-for-byte, subtitle
  clusters inserted before cue clusters, Cues rewritten, fixed-width positions → deterministic bytes);
  other containers go through `ffmpeg -c copy` (no Range). Any failure must fall back to the original file.
- **Font fallback** (`analysis/17-font-fallback.md`): a font the script needs but the torrent lacks is replaced by a
  bundled one (Noto, Noto CJK, Liberation metric twins of MS fonts): `subs/letterform` (named rules on the name) →
  `subs/fallback` (family by coverage, faces from the *rewritten* script, fontTools subset) → `ass.rename_fonts`.
  libass matches attachments by family name only, hence the rename. Tests point `fallbackFontsDir` at an empty dir
  (`conftest.settings`) so host fonts never change results; `tests/media.write_fallback_fonts` builds a fake bundle.
- **Sidecar audio** (`analysis/14-audio.md`): same matching (`matching.assign(files, Kind.AUDIO)`). A fitting `.mka`
  is copied lazily as `Patched` chunks (whole spans between its cue clusters, 1-byte track number rewritten and
  cluster CRC-32 recomputed by `mux/patch.py`, length unchanged); raw streams / unfit `.mka` are repacked by
  `mux/convert.ensure_mka` (task per output file, survives the engine; the build waits `muxAudioWait`).
  `VirtualFile(layout, read)` reads sources by key: `video`, `file:<idx>`, `local:<path>`.
- **Seeding library** (`library/`, `analysis/15-seeding.md`): torrents are never unloaded on inactivity; the
  `LibraryService` tick snapshots every torrent (`TorrentView`), `policy.decide` gives states with reasons
  (streaming / completing / prefetching / waiting / seeding = libtorrent auto-managed queue / done), applies them
  idempotently (`Engine.applied`), then `eviction.choose_victims` (never streaming; done first, then LRU by
  watching). "Background allowed" = no streaming torrent still downloads. `/remove` ends watching only;
  `?purge=1` deletes. Torrents in the cache are restored on start (paused, released in batches).
- `stats.py` must stay field-compatible with upstream `getStatistics`: stremio-core parses it with a **strict serde
  schema** (mirrored in `tests/stremio_contract.py`); one missing/mistyped required field (e.g. a null
  `sources[].lastStarted`, a missing `opts.growler`) makes Stremio drop the stats → no peers/speed in the player.
  Extra fields are fine. Keep `test_stats_satisfy_the_stremio_core_contract` green.
- Torrents are added with `default_dont_download`: nothing downloads until a stream opens a file.
  File indices follow raw `info.files` order, **including pad files**, to match addon `fileIdx`.

## Gotchas
- Don't apply libtorrent per-torrent rate limits by default: every request then waits for the next quota tick (~0.5 s)
  and startup becomes slow. The opt-in setting is `btDownloadLimit`. `btDownloadSpeed*` settings exist only for UI compatibility.
- Test fixtures must wait for the seeder's `listen_succeeded_alert` and `torrent_resumed_alert` before connecting,
  otherwise the first uTP SYN is lost and libtorrent retries only after ~7 s.
- Mux builds run in a service-owned task (`MuxService._run_build`) awaited via `asyncio.shield`: a request that is
  cancelled (client disconnect) must not leave the shared cache entry unfinished.
- `ffmpeg -err_detect crccheck` does not verify cluster CRC-32s: test CRCs with an independent implementation.
- CPU-heavy pure steps (charset detection, fontTools, archive extraction) run in `asyncio.to_thread`.
- The libtorrent binding cannot read `session_params.dht_state.nodes` (TypeError); count nodes from the bencoded
  state with `session.count_saved_nodes`.
- fontTools-generated fonts embed a timestamp: compare fonts by family name in tests, not by bytes.
- `TorrentSession(..., default_trackers=False)` plus the `OFFLINE` overrides in `tests/conftest.py` keep tests off the network.
