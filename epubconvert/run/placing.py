"""
Where a book is on the shelf, as the planner decides it.

A name is not a book (:mod:`epubconvert.run.holders`), so every caller that
needs a book's archive asks here rather than joining its assigned name onto
the output directory: the plan, the orphan check, and the annotation routes,
which went to ``output_dir / assignment.filename`` and so rewrote another
book's archive with ``-ar``, and never found a book that had moved on to its
marked name.
"""

from __future__ import annotations

import unicodedata
from collections import Counter
from collections.abc import Collection, Container, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import NamedTuple

from ..collect.identifiers import usable_identifier
from ..collect.package import ValidationError, read_package_dir
from ..export.naming import filesystem_key
from ..utils.policy import Assignment, NamingPolicy
from .claims import MAX_SUFFIX, shelf_files, suffixed
from .holders import (
    declares_one,
    foreign,
    holds_another_book,
    same_identity,
    written_for,
)


@dataclass(frozen=True)
class Existing:
    """An archive already in the output directory, and whose book it is."""

    path: Path
    identity: str


@dataclass
class Shelf:
    """The archives already on the shelf, and the names a plan has spoken for."""

    policy: NamingPolicy
    #: Each archive, keyed as the filesystem sees its name.
    existing: dict[str, Existing]
    #: Keys of every name the plan assigned, and of each name a book moved on
    #: to, so no two books of one plan are placed at one file.
    spoken: set[str]
    #: The identity of every book in the plan, NFC-normalized, so a file of
    #: another spelling is known for a namesake's (holders.foreign).
    live: frozenset[str] = field(default_factory=frozenset)
    #: The books not to open for their identifier (holders.Unopened).
    unopened: Container[Path] = frozenset()
    #: How many books of the plan have each source, which an archive's
    #: marker names (:func:`~epubconvert.run.holders.written_for`).
    sources: Counter[str] = field(default_factory=Counter)


class Place(NamedTuple):
    """Where a book is recognised or written, or why it has nowhere."""

    #: The name, or ``""`` when the book has none.
    filename: str
    #: The book's own archive under that name, if one is there.
    clash: Existing | None
    #: Why the book has no name, when it has none.
    reason: str | None


def read_shelf(
    output_dir: Path,
    policy: NamingPolicy,
    assigned: Sequence[Assignment],
    *,
    unopened: Container[Path] = frozenset(),
) -> Shelf:
    """
    Read the archives already on the shelf, for a plan to place books against.

    :param output_dir: Directory holding exported files.
    :param policy: Naming policy supplying identities.
    :param assigned: The plan's names.
    :param unopened: The books not to open for their identifier.

    :return: The shelf, with every assigned name spoken for.
    """
    # Missing directories glob to nothing, which is what a dry run wants.
    # Keyed through the same fold the name assignment uses. Folding one and
    # not the other meant a book already exported under a different case was
    # never recognised, and was re-exported on every run for ever.
    existing = {
        filesystem_key(policy.identity(found.name)): Existing(
            path=found, identity=policy.identity(found.name)
        )
        for found in shelf_files(output_dir)
    }
    spoken = {filesystem_key(item.identity) for item in assigned if item.filename}
    live = frozenset(unicodedata.normalize("NFC", item.identity) for item in assigned)
    sources = Counter(item.source for item in assigned if item.source)
    return Shelf(policy, existing, spoken, live, unopened, sources)


def place(assignment: Assignment, shelf: Shelf) -> Place:
    """
    Settle the file a book is recognised by or written to.

    Its assigned name, unless the archive there is another book's
    (:mod:`epubconvert.run.holders`). Under ``--on-collision suffix`` it then
    moves on to the first position of its marked name that no book of this
    plan is named and no other book's archive holds. It used to be a collision
    on every run for ever -- a book alone in a run, or left alone by a deleted
    edition, takes the plain name the other edition's archive has -- though
    suffix mode exists to keep both. formal/RerunPlanner.tla found it. The
    marker is a digest of the book's own identifier, so the next run finds it
    in the same place, and the other archive is still never written over.

    :param assignment: The book's assigned name, identity and identifier.
    :param shelf: The shelf and the plan's names, updated in place.

    :return: Where the book goes, or why it has nowhere.
    """
    if not assignment.filename:
        return Place("", None, assignment.reason)
    clash = shelf.existing.get(filesystem_key(assignment.identity))
    reason = _foreign_to(clash, assignment.identity, assignment, shelf)
    if reason is None:
        return Place(assignment.filename, clash, None)
    if assignment.marked:
        budget = getattr(shelf.policy, "max_bytes", 0)
        for position in range(1, MAX_SUFFIX + 1):
            candidate = suffixed(assignment.marked, position, budget)
            identity = shelf.policy.identity(candidate)
            key = filesystem_key(identity)
            clash = shelf.existing.get(key)
            if key not in shelf.spoken and (
                _foreign_to(clash, identity, assignment, shelf) is None
            ):
                shelf.spoken.add(key)
                return Place(candidate, clash, None)
    return Place("", None, reason)


def _foreign_to(
    clash: Existing | None, identity: str, assignment: Assignment, shelf: Shelf
) -> str | None:
    """Say why *clash* is not this book's archive; None if free or its own."""
    if clash is None:
        return None
    marked = _marked(clash, identity, assignment, shelf)
    if marked is not None:
        # The marker names a source: stronger than a name, and than an
        # identifier two books may share. Identifiers that both declare and
        # differ still say another book, whatever path it was written from.
        if not marked:
            return f"{clash.path.name} was written for another book"
        return holds_another_book(clash.path, assignment.identifier)
    reason = foreign(
        clash.path,
        clash.identity,
        identity,
        assignment.identifier,
        source=assignment.package,
        live=shelf.live,
        unopened=shelf.unopened,
    )
    if reason is None and assignment.not_own:
        # Settled by the claim pass, which read the sizes: a PDF has no
        # identifier for foreign to go by.
        return f"{clash.path.name} already holds this name"
    if reason is None and _declares_none(assignment, shelf):
        # The one read this costs is the one that gave the marker above.
        return declares_one(clash.path)
    return reason


def _declares_none(assignment: Assignment, shelf: Shelf) -> bool:
    """
    Say whether a book is known to declare no usable identifier.

    Known under a policy that read each package document to name it, for a
    package it named; a policy that names from the folder read nothing, and a
    file copied through is judged by its bytes.

    :param assignment: The book.
    :param shelf: The shelf, and the policy it is read under.

    :return: True when the book's identifier was read and there is none.
    """
    return (
        assignment.identifier is None
        and assignment.source is not None
        and not assignment.unnamed
        and bool(getattr(shelf.policy, "needs_metadata", False))
    )


def _marked(
    clash: Existing, identity: str, assignment: Assignment, shelf: Shelf
) -> bool | None:
    """
    Read what the marker of the archive under a book's name says of it.

    Read only where the name alone was trusted, or the identifiers are read
    anyway, so a rerun over a shelf of identified books reads nothing it did
    not: where the plan read the book's identifier, the marker comes with the
    archive's, from the one open (holders.marker_with_identifier); where it
    knows the book declares none, that one open is the one read the book
    pays; and a file of another spelling of its name, which may be its own
    after a rename by case, pays for a read of the file's last bytes rather
    than, first, of the book's package document. A book named from its
    folder is otherwise trusted by its name, as before: checking that would
    read every archive on every rerun. Two books of one name are told apart
    when the names are claimed (:mod:`epubconvert.run.telling`), and before
    a write (:func:`~epubconvert.run.planning.plan_exports`).

    :param clash: The archive under the book's name.
    :param identity: The identity of that name.
    :param assignment: The book.
    :param shelf: The shelf.

    :return: True when the marker names this book, False when another, None
        when it was not read or says nothing.
    """
    if assignment.source is None:
        return None
    if assignment.identifier is not None or _declares_none(assignment, shelf):
        return written_for(clash.path, assignment.source, shelf.sources, read=True)
    if not same_identity(clash.identity, identity):
        return written_for(clash.path, assignment.source, shelf.sources)
    return None


def settled(
    assigned: Sequence[Assignment],
    output_dir: Path,
    policy: NamingPolicy,
    *,
    unopened: Container[Path] = frozenset(),
) -> list[Assignment]:
    """
    Rename each assignment to where :func:`place` puts it.

    For what reads a book's name rather than its archive: a vault note is named
    after the book's file on the shelf, and a file copied through is written
    where the plan places it. A note named from the assignment followed a book
    that had moved on to its marked name back to the plain one -- another
    book's note.

    :param assigned: Names, in the order the plan gives them.
    :param output_dir: Directory holding exported files.
    :param policy: The naming policy the names came from.
    :param unopened: The books not to open for their identifier.

    :return: Each assignment, renamed, or with no name and the reason.
    """
    shelf = read_shelf(output_dir, policy, assigned, unopened=unopened)
    result = []
    for item in assigned:
        filename, clash, reason = place(item, shelf)
        if clash is not None:
            # The book's own archive, found under another spelling of its
            # name: the file, not the name, is what a note is named after and
            # what a copy is checked against. Kept as assigned, -ar wrote
            # Dune.md where the conversion route wrote dune.md, and on a
            # case-sensitive volume the copy was written again as Dune.epub.
            filename = clash.path.name
        if filename == item.filename:
            result.append(item)
        elif filename:
            result.append(
                replace(item, filename=filename, identity=policy.identity(filename))
            )
        else:
            result.append(replace(item, filename="", reason=reason))
    return result


def placed(
    assigned: Sequence[Assignment],
    output_dir: Path,
    policy: NamingPolicy,
    *,
    writing: bool = False,
    only: Collection[Path] | None = None,
    unopened: Container[Path] = frozenset(),
) -> dict[Path, Path | None]:
    """
    Find the archive on the shelf that is each book's own.

    The plan's own answer: a book the plan would report exported is found at
    the file it would report, a book whose name holds another book's archive
    is found at the marked name it moved on to, or nowhere. Each archive's
    identifier is read as the plan reads it, and what one of them read is not
    read again while the file is unchanged.

    :param assigned: The whole library's names, from
        :func:`epubconvert.run.planning.assign_names`, in the order it gives
        them, so each book moves on to the name the plan moves it to.
    :param output_dir: Directory holding exported files.
    :param policy: The naming policy the names came from.
    :param writing: The caller is about to write over the archive. A policy
        that names from the folder read no identifier, so the name is trusted,
        as the plan trusts it for a book it reports exported; before a write
        the book's own identifier is read and compared, as it is before
        ``--refresh`` or ``--force`` writes.
    :param only: The books the caller will write, when not all of them are:
        only these pay for the comparison *writing* asks for. Every book is
        still placed, because where one moves on depends on the books before
        it; ``-ae -ar`` compared all 2,000 books of a shelf to rewrite the one
        with a highlight, 8.7x slower than before the comparison.
    :param unopened: The books not to open for their identifier, as the plan
        leaves them (holders.Unopened): judged as a book whose identifier says
        nothing, as the plan judges it. Under ``--skip-incomplete``, ``-ar``
        read an evicted package's document before the write, and so
        downloaded it.

    :return: Each package, and its own archive or None when it has none.
    """
    shelf = read_shelf(output_dir, policy, assigned, unopened=unopened)
    unread = writing and not getattr(policy, "needs_metadata", False)
    found: dict[Path, Path | None] = {}
    for item in assigned:
        clash = place(item, shelf).clash
        compared = only is None or item.package in only
        if (
            clash is not None
            and unread
            and compared
            and item.package not in unopened
            and _holds_another(clash.path, item, shelf)
        ):
            clash = None
        found[item.package] = clash.path if clash is not None else None
    return found


def _holds_another(found: Path, item: Assignment, shelf: Shelf) -> bool:
    """
    Say, before a write, whether an archive holds another book than *item*.

    As the plan asks before ``--refresh`` or ``--force`` writes
    (:func:`~epubconvert.run.planning.plan_exports`): its marker, then the
    identifiers.

    :param found: The archive the book is placed at.
    :param item: The book, named from its folder.
    :param shelf: The shelf.

    :return: True when the archive is another book's.
    """
    marked = written_for(found, item.source, shelf.sources)
    if marked is False:
        return True
    # A marker naming this book does not excuse the comparison: a book
    # deleted and another added at its path, whose identifiers differ.
    identifier = _identifier_of(item.package)
    if identifier is None and marked is None and item.source is not None:
        return declares_one(found) is not None
    return holds_another_book(found, identifier) is not None


def _identifier_of(package: Path) -> str | None:
    """Read a package's usable identifier, or None when it cannot be read."""
    try:
        return usable_identifier(read_package_dir(package))
    except (ValidationError, OSError):
        return None
