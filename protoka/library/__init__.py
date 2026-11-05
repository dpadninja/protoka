"""Everything in the cache keeps seeding: torrent states, background work, eviction.

    model.py         TorrentView (a snapshot of one torrent), Decision, Limits
    policy.py        decide(): state of every torrent, with reasons                 (pure)
    next_episode.py  which file comes after the one being watched, which files make an episode (pure)
    eviction.py      choose_victims(): what to delete when the cache is full         (pure)
    store.py         library.json: name, added, last watched                         (I/O)
    service.py       LibraryService: snapshots → decide → apply to libtorrent → evict (I/O)

States: streaming (downloads; once the watched episode is on disk, the next one too) → seeding (upload only,
libtorrent's queue) → done. Nothing downloads once nobody watches, like TorrServer and the original Stremio
server. See analysis/21-watch-only.md (and 15-seeding.md for the seeding part).
"""
