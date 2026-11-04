# istok

**Stremio-compatible torrent streaming server: external subtitles, fonts and dubs muxed into the stream, fast peer discovery and persistent seeding**

istok speaks the same HTTP API as the official Stremio streaming server (v4.22.5), so the regular Stremio apps
(desktop, Android TV, mobile) use it unchanged: point *Settings → Streaming → Streaming server URL* at it. Under
the hood it is Python + [libtorrent](https://libtorrent.org) + [aiohttp](https://docs.aiohttp.org) and runs as a
single Docker container.

> istok is an independent project. It is not affiliated with or endorsed by Stremio. The name "Stremio" is used
> only to describe compatibility.

---

## Features

### Peer discovery that actually works
The official server has several defects that make it slow to find peers. istok fixes them:

- **It accepts incoming connections.** istok listens on a BitTorrent port (TCP + uTP, IPv4 and IPv6) and maps
  it on the router via UPnP / NAT-PMP. The official server announces a port it never opens.
- **One DHT node for the whole process, warm from the start.** The DHT routing table is kept on disk
  (saved shortly after start, every 5 minutes and on shutdown), so a restart does not bootstrap from zero.
  The official server starts a cold DHT for every torrent.
- **uTP, PEX and Local Service Discovery** are on. It announces to all trackers and tiers, and a real peer id
  is used.
- **Extra trackers are added, not substituted.** Trackers passed by an addon or a `.torrent` come on top of the
  default list and the DHT instead of replacing them.
- **No download throttling that pauses the swarm.** An optional per-torrent limit (`btDownloadLimit`) is
  available.

### External subtitles and fonts, embedded into the stream
Many releases (anime especially) put subtitles in separate folders such as `RUS Subs/`, `Subs/` or `Fonts.zip`.
istok finds the subtitles and fonts that belong to the episode being played and builds them into the video
stream. The player shows them as ordinary subtitle tracks, with no addon and no player support needed.

- **MKV becomes a virtual MKV.** The original clusters are copied byte for byte, subtitle blocks and font
  attachments are inserted, and the Cues are rewritten. Seeking and Range requests keep working, and the bytes
  are deterministic, so players can cache them.
- **AVI / MP4** are remuxed with `ffmpeg -c copy`. Seeking does not work in this mode.
- **Formats:** ASS/SSA, SRT, VTT, in any legacy encoding (charset detection). Fonts are taken from loose files
  and from ZIP / 7z / RAR archives. Only the fonts a script actually references are attached.
- **Matching can be explained.** Every subtitle-to-episode match comes with its reasons (episode number, name
  similarity, folder language…) in `/:ih/:idx/tracks.json`. Embedded tracks are never touched. If anything
  fails, the original file is served.

### Fallback fonts with matching letterforms
If a script asks for a font the torrent does not have, istok replaces it with a similar bundled one and renames
it in the script. The rename is needed because libass matches attached fonts by family name only.

- **Metric twins.** Arial / Helvetica become Liberation Sans, Times New Roman becomes Liberation Serif, Courier
  New becomes Liberation Mono. The glyph widths are identical, so positioned signs keep their layout.
- **Matching by letterform.** Other fonts are replaced by Noto (sans, serif or mono, bold or italic). The choice
  comes from a table of about 70 common fonts and from the words in the font name. Narrow faces (Impact,
  "… Condensed") get a width correction.
- **CJK.** Noto Sans / Serif CJK are used when the font or the text is Japanese, Chinese or Korean. The
  substitute must cover every character it has to draw.
- **Small attachments.** Only the glyphs actually used are embedded (fontTools subset), so even CJK
  substitutes weigh a few KB.

### External dubs (audio tracks)
Separate audio tracks (`RUS Sound/`, `Звук/[Studio]/…`) are added to the stream as extra audio tracks.

- A `.mka` track is spliced in without re-encoding.
- Raw streams (`.ac3`, `.dts`, `.flac`, `.aac`…) are repacked once with `ffmpeg -c copy`. The repack runs in a
  priority queue: the episode being opened goes first.
- `PREFERRED_AUDIO_LANG` makes a dub in your language the default track.

### A seeding cache instead of a leech
- **Everything in the cache keeps seeding.** That includes torrents restored after a restart: they come back
  without a recheck and are started in small batches.
- **The video being watched always wins.** While it is still downloading, other torrents only upload. After that,
  background work resumes: finishing files that were only partly watched, and prefetching the start and end of
  the **next episode** so it starts instantly.
- **Eviction.** When the cache would exceed `CACHE_SIZE` (50 GB by default) or the disk runs low, istok removes
  whole torrents, least recently watched first. Torrents that reached their seeding limits go before that, and
  the one being watched is never removed.
- **Configurable limits:** upload caps, an upload share while watching, ratio and time limits, the number of
  active seeds.
- **`/seeding.json`** shows every torrent's state and the reason for it.

### Compatible where it matters
- `stats.json` matches the strict schema stremio-core parses, so peers and speed show up in the player. This is
  checked by a contract test.
- File indices follow the torrent's raw file order, including pad files, as addons expect.

---

## Quick start

### Docker
```sh
docker run -d --name istok --restart unless-stopped \
  -p 11470:11470 -p 42069:42069/tcp -p 42069:42069/udp \
  -v istok-data:/data \
  -e CACHE_SIZE=50G -e PREFERRED_AUDIO_LANG=eng \
  <user>/istok:latest
```

- **Data:** everything lives under `/data`. `/data/config` holds settings and the DHT state, and
  `/data/stremio-cache` holds the torrent cache. Keep the volume, or the DHT starts cold.
- **Port:** forward the BitTorrent port (42069, TCP and UDP) on your router, unless UPnP / NAT-PMP does it for
  you. On Linux, `--network host` lets istok map the port itself.
- **User:** the container runs as UID 568. Any UID works if `/data` is writable for it.

Check it works: `http://<host>:11470/heartbeat` returns `{"success": true}`.

### Connect Stremio
In Stremio go to *Settings → Streaming → Streaming server URL* and enter `http://<host>:11470`.

This works for desktop and Android TV. The web app (`web.stremio.com`) needs HTTPS, so put istok behind a TLS
reverse proxy and set `CORS_ALLOW_ORIGINS`.

### From source
```sh
uv sync
uv run istok --port 11470 --app-path ./data --bt-port 42069
```
Requires Python ≥ 3.13 and [uv](https://docs.astral.sh/uv/). `ffmpeg`/`ffprobe` are needed for AVI/MP4 remuxing
and raw audio. `bsdtar` is needed for fonts in RAR archives.

---

## Configuration

Environment variables are applied on every start and override saved settings. They are all optional.

| Variable | Default | Meaning |
|---|---|---|
| `PORT` | `11470` | HTTP API port |
| `BT_PORT` | `42069` | BitTorrent listen port (TCP + uTP) |
| `APP_PATH` | `~/.stremio-server` (`/data/config` in Docker) | settings, DHT state, library state |
| `CACHE_ROOT` | app path (`/data` in Docker) | the cache goes to `<CACHE_ROOT>/stremio-cache` |
| `CACHE_SIZE` | `50G` | cache budget: `50G`, `512M`, `unlimited`, `0` = no caching |
| `MIN_FREE_DISK` | `2G` | evict when the filesystem has less free space than this |
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
| `CORS_ALLOW_ORIGINS` | — | extra allowed origins (comma-separated) |
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

**istok additions:**

| Method | Path | |
|---|---|---|
| `GET` | `/:infoHash/:fileIdx?mux=0` | the original file, without embedded tracks |
| `GET` | `/:infoHash/:fileIdx/tracks.json` | what was found for this video (subtitles, audio, fonts, font substitutes) and why |
| `GET` | `/:infoHash/:fileIdx/subtitles/:id.(ass\|srt\|vtt)` | an external subtitle, converted |
| `GET` | `/:infoHash/fonts/:font` | a font from the torrent |
| `GET` | `/seeding.json` | cache budget and usage, background state, every torrent's state and reasons, recent evictions |

**Not implemented** (istok is the torrent core only): HLS / transcoding, the HTTPS endpoint on 12470, casting,
the bundled web UI.

---

## Development

```sh
uv sync                     # dependencies (always via uv)
uv run pytest               # fully offline, ~80 s; ffmpeg-dependent tests are skipped without ffmpeg
uv run ruff check . && uv run ruff format --check . && uv run mypy istok
./scripts/publish.sh <user>/istok --dry-run   # tests, image build, smoke test as UID 568
```

The tests run real libtorrent sessions against local seeders, generate MKV/AVI media and fonts on the fly, and
check the stream with ffprobe.

**Layout:**
- `istok/` — the session, engine and streaming core, plus the HTTP layer;
- `istok/subs/` — subtitle and font discovery, conversion and font fallback;
- `istok/mux/` — EBML / Matroska writer, virtual MKV, ffmpeg;
- `istok/library/` — seeding policy, next episode, eviction.

Complex logic is split into pure, testable steps and thin I/O services. Decisions such as matches, font
substitutes and seeding states carry their reasons.

The version is the git tag of the commit (`git tag v1.2.3`), read by hatch-vcs; untagged commits get
`1.2.4.devN+g<sha>`. `scripts/publish.sh` publishes only a tagged, clean commit, tags the image with that version and
refuses to overwrite a published tag.

---

## Third-party components

istok's own code is MIT-licensed. It depends on, and its Docker image ships:

- **Python packages:** libtorrent (BSD), aiohttp (Apache-2.0), fontTools (MIT), psutil (BSD), regex (Apache-2.0),
  charset-normalizer (MIT), rarfile (ISC), py7zr and its codecs (LGPL-2.1+). They are installed unmodified.
- **ffmpeg** (LGPL/GPL, Alpine package). It is run as a separate process, not linked.
- **Fonts:** Noto, Noto CJK and Liberation, all under the SIL Open Font License 1.1. They are unmodified in the
  image. Subsets are made at runtime, on your machine, for your own playback.

## License

[MIT](LICENSE)
