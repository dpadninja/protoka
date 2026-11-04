"""Sidecar subtitles and fonts found inside a torrent.

Pipeline (each step is a separate module; pure steps do no I/O):

    names.parse_files    torrent file list  -> [FileName]           (pure)
    matching.assign      videos x subtitles -> Assignment           (pure, explainable)
    fonts.locate         files, video       -> [FontSource]         (pure)
    convert / ass        bytes              -> SubtitleTrack        (pure)
    fonts.families/pick  font bytes         -> [FontFace]           (pure)
    service              orchestrates the above, downloads only the needed files   (I/O)

The `mux` package then embeds the result into the video stream.
See analysis/10-subtitles.md for the overall picture.
"""
