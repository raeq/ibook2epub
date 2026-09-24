"""
Which note in a vault each book's highlights are written into.

Split from :mod:`epubconvert.export.notes`, which renders and rewrites a note,
as that module neared the line limit; this only names one.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from ..utils.policy import Assignment
from .naming import MAX_FILENAME_BYTES, encode_name, filesystem_key, truncate_bytes

#: Highest ``" (n)"`` a note is numbered with before its book is reported as
#: a collision. The planner's own limit, ``claims.MAX_SUFFIX``, which this
#: layer cannot import.
MAX_NOTE_SUFFIX = 99


def note_names(named: Sequence[Assignment], *, suffix: bool) -> dict[Path, str | None]:
    """
    Give each book a note that no other book of the run writes.

    A note's name is its book's with the extension swapped, but a book's name
    is claimed with the extension on: ``Dune.epub`` and ``Dune.pdf`` are two
    names to the run and one note to the vault, and each run wrote one book's
    highlights over the other's. Claimed here as the filesystem compares
    names (:func:`~epubconvert.export.naming.filesystem_key`), so ``dune.pdf``
    beside ``Dune.epub`` loses too on a case-insensitive volume.

    Every book's own note name is claimed before any book is numbered, so a
    numbered note never takes the name another book's file gives it. In the
    order given, which is the run's own order, so the same book wins each run.

    :param named: The books with highlights and a name, in the run's order.
    :param suffix: Whether a book that loses is numbered, rather than left out.

    :return: Each package's note name, or None when it has none.
    """
    claimed: set[str] = set()
    result: dict[Path, str | None] = {}
    for item in named:
        own = Path(item.filename).stem + ".md"
        key = filesystem_key(own)
        result[item.package] = None if key in claimed else own
        claimed.add(key)
    for item in named:
        if result[item.package] is not None or not suffix:
            continue
        stem = Path(item.filename).stem
        for position in range(2, MAX_NOTE_SUFFIX + 1):
            candidate = _numbered(stem, position)
            if filesystem_key(candidate) not in claimed:
                claimed.add(filesystem_key(candidate))
                result[item.package] = candidate
                break
    return result


def _numbered(stem: str, position: int) -> str:
    """Render ``stem (n).md``, cutting the stem so the name fits a filesystem."""
    tail = f" ({position}).md"
    budget = MAX_FILENAME_BYTES - len(encode_name(tail))
    return truncate_bytes(stem, budget) + tail
