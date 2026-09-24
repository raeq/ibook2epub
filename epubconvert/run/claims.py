"""
Which output names a run has spoken for, and what to say to a book that lost.

The bookkeeping :func:`epubconvert.run.planning.assign_names` settles names
with, kept apart from the rules it applies, and the candidates a name is
tried as, which placing a book on the shelf tries too
(:func:`epubconvert.run.placing.place`).
"""

from __future__ import annotations

import unicodedata
from collections.abc import Collection, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from ..collect.identifiers import usable_identifier
from ..export.archive import COPYABLE_SUFFIXES, PARTIAL_PREFIX
from ..export.naming import encode_name, filesystem_key, split_extension, truncate_bytes

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..utils.opf import Package


#: Highest ``" (n)"`` suffix the planner will try before giving up on a name.
MAX_SUFFIX = 99


def suffixed(filename: str, position: int, max_bytes: int) -> str:
    """
    Render the *position*-th candidate name for a filename.

    The suffix is applied within the budget the naming policy declares.
    Appending to a name already at that limit would push it over and the
    export would fail at the closing rename with a filesystem error rather
    than a name collision. A policy declaring no budget is left alone: its
    names come from the source directory, and truncating one would break the
    identity round trip that rerun safety depends on.

    :param filename: The base filename.
    :param position: 1 for the base name itself, 2 upwards for suffixes.
    :param max_bytes: The policy's byte budget, or 0 for no clamping.

    :return: The candidate filename.
    """
    if position == 1:
        return filename
    return marked(filename, f" ({position})", max_bytes)


def marked(filename: str, marker: str, max_bytes: int) -> str:
    """
    Insert *marker* before the extension, within the policy's byte budget.

    :param filename: The base filename.
    :param marker: Text to insert, its own leading space included.
    :param max_bytes: The policy's byte budget, or 0 for no clamping.

    :return: The marked filename.
    """
    # The module's own splitter, not Path().suffix: pathlib treats ".epub" as
    # extension-less, so the marker landed after it -- ".epub (2)" -- and no
    # *.epub glob matches that.
    stem_text, extension = split_extension(filename)
    candidate = f"{stem_text}{marker}{extension}"
    if not max_bytes or len(encode_name(candidate)) <= max_bytes:
        return candidate

    budget = max_bytes - len(encode_name(marker))
    budget -= len(encode_name(extension))
    stem_text = truncate_bytes(stem_text, max(budget, 1)).rstrip(" .") or "_"
    return f"{stem_text}{marker}{extension}"


class Claims:
    """
    Which output names are spoken for, and where each search left off.

    Two sets rather than one. ``identity`` answers whether two books are the
    same book; ``filesystem_key`` answers whether two names are the same
    *file*, which on a case-insensitive volume is a looser question and the one
    that decides whether a write destroys another write.

    The resume positions exist because a colliding group that exhausted
    ``MAX_SUFFIX`` made every later member retry all 99 candidates, recomputing
    identity each time, before losing.
    """

    def __init__(self) -> None:
        self.identities: set[str] = set()
        self.paths: set[str] = set()
        self.positions: dict[str, int] = {}
        self.holders: dict[str, str] = {}

    def resume(self, group: str) -> int:
        """Return the first position worth trying for this group."""
        return self.positions.get(group, 1)

    def take(self, group: str, position: int, key: str, candidate: str) -> bool:
        """Claim a candidate if both its identity and its path are free."""
        path_key = filesystem_key(candidate)
        if key in self.identities or path_key in self.paths:
            return False
        self.identities.add(key)
        self.paths.add(path_key)
        self.positions[group] = position + 1
        self.holders.setdefault(group, candidate)
        return True

    def holder(self, group: str) -> str | None:
        """Return the name that took this group, if anything did."""
        return self.holders.get(group)

    def exhaust(self, group: str, limit: int) -> None:
        """Record that this group has no positions left to try."""
        self.positions[group] = limit + 1


def lost_to(holder: str | None, metadata: Package | None) -> str:
    """
    Explain which book holds the name, and say what this one is.

    Naming the winner turns "another book already claims this name" into
    something a person can act on, and the loser's identifier is what tells
    them whether the two are the same book stored twice or genuinely different
    editions. In a surveyed library both cases are common.

    :param holder: The filename that took the name, if one did.
    :param metadata: The losing package's document, if it was read.

    :return: The reason to record on the decision.
    """
    reason = (
        f"{holder} already holds this name"
        if holder
        else "another book already claims this name"
    )
    identifier = usable_identifier(metadata)
    return f"{reason}; this book is {identifier}" if identifier else reason


def shelf_files(output_dir: Path) -> list[Path]:
    """
    Find every file on the shelf a run could have put there.

    Every kind :func:`~epubconvert.export.archive.collect_copyable` takes
    along, whatever the case of the extension, and no partial. The shelf was
    read with ``glob("*.epub")``, which is case-sensitive, so a zipped book
    copied through as ``Foo.EPUB`` was invisible to the plan, the orphan
    check and the placing: a package ``Foo.epub`` was judged free and, on a
    case-insensitive volume, written over it. PDFs were invisible the same
    way.

    :param output_dir: Directory holding exported files.

    :return: The files, sorted; none when the directory is missing, which is
        what a dry run or a first run finds.
    """
    try:
        entries = sorted(output_dir.iterdir())
    except OSError:
        return []
    return [
        found
        for found in entries
        if found.suffix.lower() in COPYABLE_SUFFIXES
        and not found.name.startswith(PARTIAL_PREFIX)
        and found.is_file()
    ]


def shelf_names(output_dir: Path | None) -> frozenset[str]:
    """
    Read the name of every file on the shelf, for the claim pass to weigh.

    :param output_dir: Directory holding exported files, or None for none.

    :return: The names, NFC-normalized, as :func:`claim_order` compares them;
        empty when there is no shelf to read.
    """
    if output_dir is None:
        return frozenset()
    try:
        return frozenset(
            unicodedata.normalize("NFC", found.name)
            for found in output_dir.iterdir()
            if found.is_file()
        )
    except OSError:
        # Missing, which is what a dry run or a first run finds.
        return frozenset()


def claim_order(candidates: Sequence[str], shelf: Collection[str]) -> list[int]:
    """
    Order books for the claim pass: first those whose name is on the shelf.

    The pass walked the library in sorted order and never looked at the
    shelf. So ``a/Dune.epub``, added beside ``b/dune.epub`` already exported
    alone, took the name a case-insensitive volume gives both, as it sorts
    first: in suffix mode ``b`` was written again under a suffix and its
    archive listed as an orphan, and in skip mode both were collisions, on
    every run. A book whose exact name is a file on the shelf claims it
    first; otherwise the sorted order stands, so a library with nothing on
    the shelf is named as before.

    :param candidates: The first name each book tries, in sorted order.
    :param shelf: The shelf's names, from :func:`shelf_names`.

    :return: Indices into *candidates*, in the order to claim.
    """
    if not shelf:
        return list(range(len(candidates)))
    return sorted(
        range(len(candidates)),
        key=lambda index: unicodedata.normalize("NFC", candidates[index]) not in shelf,
    )
