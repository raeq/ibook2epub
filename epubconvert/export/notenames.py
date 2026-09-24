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
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from ..collect.identifiers import canonical_identifier
from ..utils.display import collapse
from ..utils.policy import Assignment
from .naming import (
    MAX_FILENAME_BYTES,
    disambiguator,
    encode_name,
    filesystem_key,
    truncate_bytes,
)
from .noteformat import (
    END_PATTERN,
    book_source,
    book_tags,
    first_start,
    normalise,
    readable,
    scalar,
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

#: A frontmatter line naming the book's identifier, and its value.
IDENTIFIER = re.compile(r"identifier:[ \t]+(.+)")


class Holding(Enum):
    """What the file at a note's name is, to the book that wants the name."""

    #: Nothing is there.
    ABSENT = "absent"
    #: A note of this book's: tagged for it, or holding every one of its
    #: highlights in the note.
    MINE = "mine"
    #: A note of another book's: tagged for one this run knows, naming
    #: another edition in its frontmatter or another file in its marker.
    ANOTHER = "another"
    #: A note that says nothing either way: tagged for no book this run
    #: knows, or not tagged at all, naming no other file, and holding not
    #: every one of this book's highlights.
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
    #: The file the note's start marker names, if any.
    source: str | None = None
    #: Each quoted block of the generated region, as :func:`_words` has it.
    quoted: frozenset[str] = frozenset()
    #: The ``identifier:`` values of the frontmatter above the marker, each
    #: read as YAML would and canonicalised. The line was compared as this
    #: tool writes it, double-quoted; Obsidian's property editor writes a
    #: plain scalar back unquoted, and the book's own note then named
    #: "another edition" and was refused on every run.
    identifiers: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Claimant:
    """A book that wants a note, as a note already there is judged against."""

    #: Every tag this book's note may carry: one for each of its asset ids.
    tags: frozenset[str]
    #: Its highlights, as :func:`_words` has them.
    words: frozenset[str]
    #: Its identifier and its declared one, canonicalised.
    identifiers: frozenset[str]
    #: The file it is read from, as :func:`~.noteformat.book_source` has it.
    source: str | None = None


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
                identifiers.add(canonical_identifier(collapse(value)))
    return Claimant(
        frozenset(book_tags(found)) | frozenset(also),
        frozenset(_words(str(item.get("text", ""))) for item in found),
        frozenset(identifiers),
        book_source(found),
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
    identifiers = (IDENTIFIER.fullmatch(line.rstrip()) for line in lines[:index])
    return Held(
        Holding.UNCLAIMED,
        marker.group(2),
        marker.group(3),
        frozenset(filter(None, quoted_blocks)),
        frozenset(
            canonical_identifier(scalar(line.group(1))) for line in identifiers if line
        ),
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
    another edition's note, and a marker naming another file than the one
    the book is read from is that file's book's. A note holding the book's
    highlights, every one of its quoted blocks, is its own -- though
    another book may hold them too (:func:`_by_evidence`). Holding only
    some of them proves nothing:
    two editions share a passage, and the note of one held the other's
    highlight. Holding none -- every one of them deleted in Books -- it
    says nothing either.

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
    if held.source and book.source and held.source != book.source:
        return Holding.ANOTHER
    mine = held.quoted and held.quoted <= book.words
    return Holding.MINE if mine else Holding.UNCLAIMED


@dataclass
class Naming:
    """Which note each book of a run writes, as :func:`note_names` decides."""

    #: Each package's note name, or None when it has none.
    given: dict[Path, str | None] = field(default_factory=dict)
    #: The packages given a name whose note is another book's all the same:
    #: without suffix a book keeps its name, and writing it reports the note.
    refused: set[Path] = field(default_factory=set)


class _Names:
    """The names given so far, and what is known of the notes at them."""

    def __init__(
        self,
        named: Sequence[Assignment],
        vault: Vault,
        known: Collection[str] | None,
        *,
        suffix: bool,
    ) -> None:
        self.named = named
        self.vault = vault
        self.known = known
        self.suffix = suffix
        self.naming = Naming()
        self.given = self.naming.given
        self.taken: set[str] = set()
        #: Each note the evidence gives a book, by its listed name: the
        #: package it is the note of, or None when two books hold it alike.
        self.owners: dict[str, Path | None] = {}
        self._wanting: dict[str, list[Assignment]] | None = None

    def free(self, name: str) -> bool:
        """Whether no book has been given *name*, as the filesystem compares."""
        return filesystem_key(name) not in self.taken

    def give(self, item: Assignment, name: str) -> None:
        """Give *item* the note *name*."""
        self.taken.add(filesystem_key(name))
        self.given[item.package] = name

    def judge(self, listed: str, item: Assignment, book: Claimant) -> Holding:
        """What the file listed as *listed* is, to *item*: see :func:`holding`."""
        if listed in self.owners:
            mine = self.owners[listed] == item.package
            return Holding.MINE if mine else Holding.ANOTHER
        held = self.vault.held(listed)
        verdict = holding(held, book, self.known)
        unclaimed = verdict is Holding.UNCLAIMED and held.quoted
        if unclaimed and self.contested(listed, item, held.source):
            return Holding.ANOTHER
        return verdict

    def contested(self, listed: str, item: Assignment, source: str | None) -> bool:
        """
        Whether a note nothing claims is wanted by a book besides *item*.

        Such a note -- tagged for no book the run knows, or for none, and
        holding not every one of a book's highlights -- went with its name to
        whichever book came first: the PDF's note, its highlights deleted
        in Books, went to the EPUB for good once that gained one, re-tagged.
        Only one book wanting its name, it is that book's, as a book removed
        from Books and added again is. Two wanting it, either may be a
        guess, and it is handed to neither.

        Wanted by every book of the run that could be given the name: its
        own, and under suffix a numbered one. Whether or not it has a note
        already, so the answer holds still as books gain notes, and a rerun
        decides as the run before it did. A note naming the file its book
        is read from is wanted only by a book read from a file of that name.

        :param listed: The note's name, as the vault lists it.
        :param item: The book that wants it.
        :param source: The file the note names, if any.

        :return: True when *item* is not the one book that wants it.
        """
        if self._wanting is None:
            self._wanting = {}
            for other in self.named:
                for name in _candidates(other, suffix=self.suffix):
                    key = filesystem_key(name)
                    self._wanting.setdefault(key, []).append(other)
        wanting = [
            other
            for other in self._wanting.get(filesystem_key(listed), [])
            if source in (None, _source(other))
        ]
        return [other.package for other in wanting] != [item.package]


def note_names(
    named: Sequence[Assignment],
    *,
    suffix: bool,
    claimants: Mapping[Path, Claimant],
    vault: Vault,
    known: Collection[str] | None,
    library: Mapping[str, Collection[str]] | None = None,
) -> Naming:
    """
    Give each book a note that no other book of the run writes.

    A note's name is its book's with the extension swapped, but a book's name
    is claimed with the extension on: ``Dune.epub`` and ``Dune.pdf`` are two
    names to the run and one note to the vault, and each run wrote one book's
    highlights over the other's. Claimed here as the filesystem compares
    names (:func:`~epubconvert.export.naming.filesystem_key`), so ``dune.pdf``
    beside ``Dune.epub`` loses too on a case-insensitive volume.

    Three passes. A note already a book's -- tagged for it, naming the file
    it is read from, or holding every highlight in the note -- stays its
    own, before anything else is named: a note is the book's it was written
    for, whichever book has highlights today, and a book with none today
    keeps the note tagged for it too. Then every book's own note name, in
    the order given, which is the run's own order, so the same book wins
    each run. Then, under suffix, a number for each book left without one,
    never onto a name another book's file gives it and never onto a file
    already there: a leftover note is never taken over.

    A note nothing claims goes with its name only when one book alone
    wants it (:meth:`_Names.contested`); to two, it is another book's.

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
    :param library: The tags of each package the library lists, by package
        name, highlighted or not.

    :return: Each package's note name, and which of them to refuse.
    """
    names = _Names(named, vault, known, suffix=suffix)
    tagged = {
        item.package: Claimant(frozenset(tags), frozenset(), frozenset())
        for item in named
        if item.package not in claimants
        and (tags := (library or {}).get(item.package.name))
    }
    _reserve(named, {**tagged, **claimants}, names, suffix=suffix)
    _by_source(named, claimants, names, suffix=suffix)
    _by_evidence(named, claimants, names, suffix=suffix)
    for item in named:
        if item.package in names.given:
            continue
        names.given[item.package] = None
        own = Path(item.filename).stem + ".md"
        if not names.free(own):
            continue
        listed = vault.spelling(own)
        book = claimants.get(item.package)
        verdict = names.judge(listed, item, book) if listed and book else None
        if suffix and verdict in (Holding.ANOTHER, Holding.FOREIGN):
            continue
        # The spelling on disk: the book's own on a case-sensitive volume is
        # a second file beside the note it adopted.
        names.give(item, listed or own)
        if verdict is Holding.ANOTHER:
            names.naming.refused.add(item.package)
    if suffix:
        _number(named, names)
    return names.naming


def _number(named: Sequence[Assignment], names: _Names) -> None:
    """
    Number each book left without a name, as ``--on-collision suffix`` asks.

    :param named: Every book of the run with a name, in the run's order.
    :param names: The names given so far, added to in place.
    """
    for item in named:
        if names.given[item.package] is not None:
            continue
        stem = Path(item.filename).stem
        for position in range(2, MAX_NOTE_SUFFIX + 1):
            candidate = _numbered(stem, position)
            if names.free(candidate) and names.vault.spelling(candidate) is None:
                names.give(item, candidate)
                break


def _reserve(
    named: Sequence[Assignment],
    claimants: Mapping[Path, Claimant],
    names: _Names,
    *,
    suffix: bool,
) -> None:
    """
    Give each book the note tagged for it, if one is.

    Looked for among the names the book could be given: its own, and under
    suffix its numbered ones. Every book's tagged note first, and only then
    one the other evidence gives it (:func:`_by_evidence`), so such a note
    never goes to one book while another's tagged note is still to be found.

    A book with no highlights today keeps the note tagged for it. Only the
    books with highlights looked, so without suffix a namesake took the name
    and was refused the note, exiting 1, on exactly the runs its own book
    had nothing to write, and lost the collision, exiting 0, on the others.

    :param named: Every book of the run with a name, in the run's order.
    :param claimants: Each book with highlights, and each book without
        whose tags the library knows.
    :param names: The names given so far, added to in place.
    :param suffix: Whether a book's numbered names are its too.
    """
    for item in named:
        book = claimants.get(item.package)
        if book is None or item.package in names.given:
            continue
        for name in _candidates(item, suffix=suffix):
            listed = names.vault.spelling(name)
            if listed is None or not names.free(listed):
                continue
            held = names.vault.held(listed)
            mine = holding(held, book, names.known) is Holding.MINE
            if mine and held.tag in book.tags:
                names.give(item, listed)
                break


def _by_source(
    named: Sequence[Assignment],
    claimants: Mapping[Path, Claimant],
    names: _Names,
    *,
    suffix: bool,
) -> None:
    """
    Give each book the note naming the file it is read from, if one does.

    A book removed from Books and added again answers to a new asset id, so
    its note's tag names no book the run knows and says nothing. The note
    still names the file its book is read from, and is that book's, as a
    tagged note is, highlights today or not -- unless its tag is another
    known book's, its frontmatter names another edition, or another book
    read from a file of that name wants it too.

    :param named: Every book of the run with a name, in the run's order.
    :param claimants: Each book with highlights.
    :param names: The names given so far, added to in place.
    :param suffix: Whether a book's numbered names are its too.
    """
    for item in named:
        if item.package in names.given:
            continue
        source = _source(item)
        book = claimants.get(item.package)
        for name in _candidates(item, suffix=suffix):
            listed = names.vault.spelling(name)
            if listed is None or not names.free(listed):
                continue
            held = names.vault.held(listed)
            if _names_only(held, source, book, names.known) and not (
                names.contested(listed, item, source)
            ):
                names.give(item, listed)
                break


def _names_only(
    held: Held, source: str, book: Claimant | None, known: Collection[str] | None
) -> bool:
    """Whether a note names the file *source*, and no other book at all."""
    if held.kind is not Holding.UNCLAIMED or held.source != source:
        return False
    if held.tag is not None and (known is None or held.tag in known):
        return False
    return book is None or holding(held, book, known) is not Holding.ANOTHER


def _source(item: Assignment) -> str:
    """The file a book is read from, as a note's marker names it."""
    return disambiguator(item.package.name)


def _by_evidence(
    named: Sequence[Assignment],
    claimants: Mapping[Path, Claimant],
    names: _Names,
    *,
    suffix: bool,
) -> None:
    """
    Give each book the note its highlights say is its own, if one is.

    A note untagged, or tagged for no book the run knows, holding a book's
    highlights. It went to the first book holding any one of them, so two
    editions that share a passage handed one's note to the other for good,
    re-tagged. Every book's claim is gathered first, and a note goes to the
    one book holding every one of its highlights (:func:`holding`). Two
    books holding them all are a guess, and it goes to neither: to both it
    is another book's, so each is numbered past it, or without suffix is
    refused it.

    :param named: Every book of the run with a name, in the run's order.
    :param claimants: Each book with highlights.
    :param names: The names given so far, added to in place.
    :param suffix: Whether a book's numbered names are its too.
    """
    claims: dict[str, list[Path]] = {}
    sought: dict[Path, list[str]] = {}
    for item in named:
        book = claimants.get(item.package)
        if book is None or item.package in names.given:
            continue
        for name in _candidates(item, suffix=suffix):
            listed = names.vault.spelling(name)
            if listed is None or not names.free(listed):
                continue
            held = names.vault.held(listed)
            if holding(held, book, names.known) is Holding.MINE:
                claims.setdefault(listed, []).append(item.package)
                sought.setdefault(item.package, []).append(listed)
    for listed, claim in claims.items():
        names.owners[listed] = claim[0] if len(claim) == 1 else None
    for item in named:
        for listed in sought.get(item.package, ()):
            if names.owners[listed] == item.package:
                names.give(item, listed)
                break


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
