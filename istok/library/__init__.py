"""Everything in the cache keeps seeding: torrent states, background work, eviction.

    model.py         TorrentView (a snapshot of one torrent), Decision, Limits
    policy.py        decide(): state of every torrent, with reasons                 (pure)
    next_episode.py  which file comes after the one being watched, what to prefetch (pure)
    eviction.py      choose_victims(): what to delete when the cache is full         (pure)
    store.py         library.json: name, added, last watched                         (I/O)
    service.py       LibraryService: snapshots → decide → apply to libtorrent → evict (I/O)

States: streaming → completing / prefetching / waiting → seeding (libtorrent's queue) → done.
"Background allowed" = no streaming torrent still has bytes to download: the episode being watched
always gets the line first. See analysis/15-seeding.md.
"""
