"""Detecting that the semantic-layer TOMLs on disk have changed.

Both loaders cached the configs behind a `_loaded` flag and dropped that flag
when the editor published. That works in ONE process. Production runs uvicorn
with four workers, so a publish cleared the cache of whichever worker served the
request and left the other three serving their old view — a freshly published
database came back documented from one worker in four, which read as "the
semantic layer did not appear at all".

Nothing coordinates those processes, and the only shared thing they have is the
directory itself. So instead of trusting an in-process flag, each loader
compares a cheap fingerprint of the directory on every read and reloads when it
differs. Every worker notices the same change on its next request, with no
message passing and nothing to keep in sync.
"""

from __future__ import annotations

from pathlib import Path

#: (name, mtime_ns, size) per file. Size and name are carried alongside the
#: timestamp because a filesystem with coarse mtime resolution can rewrite a
#: file inside the same tick — and because a fingerprint built from timestamps
#: alone would not notice a file being added or deleted.
Fingerprint = tuple[tuple[str, int, int], ...]


def directory_fingerprint(directory: Path, pattern: str = "*.toml") -> Fingerprint:
    """A value that changes whenever the matching files in `directory` change.

    Empty for a missing or unreadable directory, which is the same answer as an
    empty one: the loaders already treat "no configs" as valid.
    """
    try:
        paths = sorted(directory.glob(pattern))
    except OSError:
        return ()

    out: list[tuple[str, int, int]] = []
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            # Vanished between glob and stat — a publish replacing a file. The
            # next read fingerprints again, so skipping it here is safe.
            continue
        out.append((path.name, stat.st_mtime_ns, stat.st_size))
    return tuple(out)
