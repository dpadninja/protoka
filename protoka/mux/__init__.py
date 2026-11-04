"""Embedding subtitle tracks and font attachments into the video stream.

MKV  → a *virtual* file: the original bytes, untouched, plus a rebuilt header (new tracks and
       attachments), subtitle clusters inserted between the original clusters, and rebuilt Cues.
       Nothing is re-encoded and Range requests/seeking work (see layout.py for the byte layout).
other → ffmpeg `-c copy` remux into Matroska, streamed (no seeking until downloaded).

    ebml.py     EBML primitives                      (pure)
    mkv.py      read the structure of a source MKV   (pure over an async `read(offset, n)`)
    layout.py   build the virtual file layout        (pure)
    virtual.py  serve a layout from the torrent      (I/O)
    ffmpeg.py   remux non-MKV sources                (I/O)
    service.py  choose a path, build once, cache     (I/O)
"""
