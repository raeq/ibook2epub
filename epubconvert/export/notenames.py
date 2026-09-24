"""
Which note in a vault each book's highlights are written into.

Split from :mod:`epubconvert.export.notes`, which renders and rewrites a note,
as that module neared the line limit; this only names one.

A name depends on the library and on the notes already in the vault, never
on which books have highlights today. Only the books with highlights once
claimed names, so ``Dune.pdf``, highlighted alone, was given ``Dune.md``;
the day ``Dune.epub`` gained a highlight it claimed that name first, and the
PDF's note -- with the reader's writing in it -- was refused on every later
run or stranded while the PDF moved on to ``Dune (2).md``. Every named book
claims its name now, whether or not it has anything to write.
"""

from __future__ import annotations

import os
import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from ..utils.policy import Assignment
from .naming import MAX_FILENAME_BYTES, encode_name, filesystem_key, truncate_bytes
from .noteformat import (
    END_PATTERN,
    book_tags,
    first_start,
    normalise,
    quoted,
    readable,
)

#: Highest ``" (n)"`` a note is numbered with before its book is reported as
#: a collision. The planner's own limit, ``claims.MAX_SUFFIX``, which this
#: layer cannot import.
MAX_NOTE_SUFFIX = 99

#: What is left out when a highlight and a note's quoted block are compared:
#: white space, which an editor may trim or a no-break space stand in for,
#: the quote marks, and the backslashes the escaper puts before an opener.
#: What is left is the words, which is what says whose highlights they are.
UNQUOTED = re.compile(r"[\s>\\]+")


class Holding(Enum):
    """What the file at a note's name is, to the book that wants the name."""

    #: Nothing is there.
    ABSENT = "absent"
    #: A note of this book's: tagged for it, or holding its highlights.
    MINE = "mine"
    #: A note of another book's: tagged for one this run knows, or naming
    #: another edition in its frontmatter.
    ANOTHER = "another"
    #: A note that says nothing either way: tagged for no book this run
    #: knows, or not tagged at all, and holding none of this book's
    #: highlights.
    UNCLAIMED = "unclaimed"
    #: A file this tool did not write.
    FOREIGN = "foreign"
    #: Something that could not be read, and so cannot be judged.
    UNREADABLE = "unreadable"


@dataclass(frozen=True)
class Held:
    """What one file in the vault says about whose note it is."""

    #: ABSENT, FOREIGN or UNREADABLE for a file that is not a note, and
    #: UNCLAIMED for one that is, until :func:`holding` judges it against a
    #: book.
    kind: Holding
    #: The book the note's start marker is tagged for, if any.
    tag: str | None = None
    #: Each quoted block of the generated region, as :func:`_words` has it.
    quoted: frozenset[str] = frozenset()
    #: The ``identifier:`` lines of the frontmatter above the marker.
    identifiers: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Claimant:
    """A book that wants a note, as a note already there is judged against."""

    #: Every tag this book's note may carry: one for each of its asset ids.
    tags: frozenset[str]
    #: Its highlights, as :func:`_words` has them.
    words: frozenset[str]
    #: Its ``identifier:`` line, as :func:`~.noteformat.quoted` writes it,
    #: in each form an older version could have written.
    identifiers: frozenset[str]


def claimant(found: list[dict[str, Any]], also: Iterable[str] = ()) -> Claimant:
    """
    Describe a book by its highlights, for judging the notes it may write.

    :param found: The book's highlights.
    :param also: More tags the book answers to: the library's other asset
        ids for the same package, which have no highlights to say so.

    :return: The book.
    """
    identifiers = set()
    for item in found:
        book = item.get("book")
        for key in ("identifier", "declaredIdentifier"):
            value = book.get(key) if isinstance(book, dict) else None
            if value:
                identifiers.add(f"identifier: {quoted(value)}")
    return Claimant(
        frozenset(book_tags(found)) | frozenset(also),
        frozenset(_words(str(item.get("text", ""))) for item in found),
        frozenset(identifiers),
    )


class Vault:
    """
    The notes already in a vault, each read at most once.

    Listed once rather than probed per name: under ``--on-collision suffix``
    each book with highlights looks for its own note among 99 numbered
    names, and a stat of each on a vault of thousands of notes is the cost
    a listing avoids.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._listed: dict[str, str] | None = None
        self._held: dict[str, Held] = {}

    def spelling(self, name: str) -> str | None:
        """The name a file the filesystem takes for *name* is listed under."""
        if self._listed is None:
            try:
                with os.scandir(self.directory) as entries:
                    self._listed = {filesystem_key(e.name): e.name for e in entries}
            except OSError:
                # Nothing is known to be there, so nothing is passed over for
                # it; writing each note still reads what is there first.
                self._listed = {}
        return self._listed.get(filesystem_key(name))

    def held(self, name: str) -> Held:
        """What the file at *name* is, read once."""
        listed = self.spelling(name)
        if listed is None:
            return Held(Holding.ABSENT)
        if listed not in self._held:
            self._held[listed] = _read(self.directory / listed)
        return self._held[listed]


def _read(target: Path) -> Held:
    """Read one file of the vault for whose note it is."""
    if not readable(target):
        return Held(Holding.UNREADABLE)
    try:
        return parse(target.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError):
        return Held(Holding.UNREADABLE)


def parse(text: str) -> Held:
    """
    Read a file's contents for whose note it is.

    :param text: The file, decoded.

    :return: What it says: FOREIGN when it carries no start marker.
    """
    lines = normalise(text).split("\n")
    start = first_start(lines)
    if start is None:
        return Held(Holding.FOREIGN)
    index, marker = start
    blocks: list[list[str]] = []
    for position, line in enumerate(lines[index + 1 :], start=index + 1):
        if END_PATTERN.match(line):
            break
        if line.startswith(">"):
            if not lines[position - 1].startswith(">"):
                blocks.append([])
            blocks[-1].append(line)
    quoted_blocks = (_words("\n".join(block)) for block in blocks)
    identifiers = (line.rstrip() for line in lines[:index])
    return Held(
        Holding.UNCLAIMED,
        marker.group(2),
        frozenset(filter(None, quoted_blocks)),
        frozenset(line for line in identifiers if line.startswith("identifier: ")),
    )


def _words(text: str) -> str:
    """A highlight or a quoted block, reduced to what the two have in common."""
    return UNQUOTED.sub("", text)


def holding(held: Held, book: Claimant, known: Collection[str] | None) -> Holding:
    """
    Judge whose the file is, to a book that wants its name.

    A tag says whose a note is only when it names a book this run knows of:
    every book in Apple's library, highlighted or not, and every book with
    highlights. Removing a book from Books and adding it again gives it a
    new asset id, and its note, tagged for the old one, was refused on every
    later run as another book's. A tag no book answers to says nothing,
    and neither does a note an older version wrote with no tag at all.

    Then the other evidence. A frontmatter naming another identifier is
    another edition's note. A note holding the book's highlights is its
    own. Holding none -- every one of them deleted in Books -- it says
    nothing, and goes with its name, as it always did.

    :param held: The file, as :class:`Vault` read it.
    :param book: The book that wants its name.
    :param known: The tags of every book this run knows of, or None to take
        every tag as a known book's.

    :return: What the file is, to that book.
    """
    if held.kind is not Holding.UNCLAIMED:
        return held.kind
    if held.tag is not None and book.tags:
        if held.tag in book.tags:
            return Holding.MINE
        if known is None or held.tag in known:
            return Holding.ANOTHER
    if (
        held.identifiers
        and book.identifiers
        and not held.identifiers & book.identifiers
    ):
        return Holding.ANOTHER
    return Holding.MINE if held.quoted & book.words else Holding.UNCLAIMED


class _Names:
    """The names given so far, and every filesystem key they take."""

    def __init__(self) -> None:
        self.given: dict[Path, str | None] = {}
        self.taken: set[str] = set()

    def free(self, name: str) -> bool:
        """Whether no book has been given *name*, as the filesystem compares."""
        return filesystem_key(name) not in self.taken

    def give(self, item: Assignment, name: str) -> None:
        """Give *item* the note *name*."""
        self.taken.add(filesystem_key(name))
        self.given[item.package] = name


def note_names(
    named: Sequence[Assignment],
    *,
    suffix: bool,
    claimants: Mapping[Path, Claimant],
    vault: Vault,
    known: Collection[str] | None,
) -> dict[Path, str | None]:
    """
    Give each book a note that no other book of the run writes.

    A note's name is its book's with the extension swapped, but a book's name
    is claimed with the extension on: ``Dune.epub`` and ``Dune.pdf`` are two
    names to the run and one note to the vault, and each run wrote one book's
    highlights over the other's. Claimed here as the filesystem compares
    names (:func:`~epubconvert.export.naming.filesystem_key`), so ``dune.pdf``
    beside ``Dune.epub`` loses too on a case-insensitive volume.

    Three passes. A note already a book's -- tagged for it, or holding its
    highlights -- stays its own, before anything else is named: a note is
    the book's it was written for, whichever book has highlights today. Then
    every book's own note name, in the order given, which is the run's own
    order, so the same book wins each run. Then, under suffix, a number for
    each book left without one, never onto a name another book's file gives
    it and never onto a file already there: a leftover note is never taken
    over.

    Under suffix a book with highlights also passes over its own name when
    the file there is not its to write: another book's note, or a file this
    tool did not write. Without suffix it keeps the name, and writing it
    reports the file, since a number is what the reader did not ask for.

    :param named: Every book of the run with a name, in the run's order,
        whether or not it has highlights.
    :param suffix: Whether a book that loses is numbered, rather than left out.
    :param claimants: Each book with highlights; a book left out has none,
        and writes nothing.
    :param vault: The notes already there.
    :param known: The tags of every book this run knows of; see
        :func:`holding`.

    :return: Each package's note name, or None when it has none.
    """
    names = _Names()
    _reserve(named, claimants, vault, names, suffix=suffix, known=known)
    for item in named:
        if item.package in names.given:
            continue
        names.given[item.package] = None
        own = Path(item.filename).stem + ".md"
        if not names.free(own):
            continue
        book = claimants.get(item.package)
        if suffix and book is not None and _not_its(vault.held(own), book, known):
            continue
        # The spelling on disk: the book's own on a case-sensitive volume is
        # a second file beside the note it adopted.
        names.give(item, vault.spelling(own) or own)
    for item in named:
        if names.given[item.package] is not None or not suffix:
            continue
        stem = Path(item.filename).stem
        for position in range(2, MAX_NOTE_SUFFIX + 1):
            candidate = _numbered(stem, position)
            if names.free(candidate) and vault.spelling(candidate) is None:
                names.give(item, candidate)
                break
    return names.given


def _reserve(
    named: Sequence[Assignment],
    claimants: Mapping[Path, Claimant],
    vault: Vault,
    names: _Names,
    *,
    suffix: bool,
    known: Collection[str] | None,
) -> None:
    """
    Give each book with highlights the note already its own, if one is.

    Looked for among the names the book could be given: its own, and under
    suffix its numbered ones. A note tagged for the book first, over every
    book, and only then one the other evidence gives it -- untagged, or
    tagged for no book the run knows, and holding its highlights -- so such
    a note never goes to one book while another's tagged note is still to
    be found.

    :param named: Every book of the run with a name, in the run's order.
    :param claimants: Each book with highlights.
    :param vault: The notes already there.
    :param names: The names given so far, added to in place.
    :param suffix: Whether a book's numbered names are its too.
    :param known: The tags of every book this run knows of.
    """
    for by_tag in (True, False):
        for item in named:
            book = claimants.get(item.package)
            if book is None or item.package in names.given:
                continue
            for name in _candidates(item, suffix=suffix):
                listed = vault.spelling(name)
                if listed is None or not names.free(listed):
                    continue
                held = vault.held(listed)
                if by_tag and held.tag not in book.tags:
                    continue
                if holding(held, book, known) is Holding.MINE:
                    names.give(item, listed)
                    break


def _not_its(held: Held, book: Claimant, known: Collection[str] | None) -> bool:
    """Whether a file is another book's note, or not a note at all."""
    return holding(held, book, known) in (Holding.ANOTHER, Holding.FOREIGN)


def _candidates(item: Assignment, *, suffix: bool) -> list[str]:
    """Every name a book's note could have: its own, then numbered."""
    stem = Path(item.filename).stem
    numbered = range(2, MAX_NOTE_SUFFIX + 1) if suffix else range(0)
    return [stem + ".md", *(_numbered(stem, n) for n in numbered)]


def _numbered(stem: str, position: int) -> str:
    """Render ``stem (n).md``, cutting the stem so the name fits a filesystem."""
    tail = f" ({position}).md"
    budget = MAX_FILENAME_BYTES - len(encode_name(tail))
    return truncate_bytes(stem, budget) + tail
