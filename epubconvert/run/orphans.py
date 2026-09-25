"""
Finding the archives on the shelf that no book in the library claims.

Split from :mod:`epubconvert.run.planning` when that module reached the line
limit. The planner names the books and places them on the shelf; this module
asks it where each book is, and reports the files no book is placed at. It
decides nothing a run acts on: an orphan is reported, never deleted.
"""

from __future__ import annotations

from collections.abc import Container, Sequence
from pathlib import Path

from ..export.naming import filesystem_key
from ..utils.policy import Assignment, NamingPolicy
from .claims import MARKED, NUMBERED, shelf_files, shelf_names
from .holders import (
    holds_another_book,
    identifier_on_shelf,
    marker_on_shelf,
    same_identity,
    written_for,
)
from .placing import Existing, Shelf, place, read_shelf
from .planning import ORPHAN, SKIP, CollisionMode, Decision, assign_names


def find_orphans(
    output_dir: Path,
    policy: NamingPolicy,
    packages: Sequence[Path],
    on_collision: CollisionMode = SKIP,
    *,
    assigned: Sequence[Assignment] | None = None,
    unopened: Container[Path] = frozenset(),
    copied: bool = True,
) -> list[Path]:
    """
    Find archives on the shelf that no book in the library claims.

    The library has always been seen richly -- five statuses, reasons, tallies
    -- and the output directory not at all. An archive left behind by a book
    deleted from the library, or by adopting a naming policy that renames
    everything, sits there for ever: ``--verify`` blesses it because it is a
    sound archive, and ``--list`` only ever looked at sources.

    Asks the planner for the names rather than deriving them, so a book that
    took a ``" (2)"`` suffix is not reported as abandoning the name it holds.
    And a file is claimed only when the planner would call it that book's:
    one holding another book is the plan's collision, and counting it as
    claimed hid the archive of a book deleted from the library, which can be
    its last copy. Under ``--name-by author-title`` that reads each claimed
    archive's identifier, as planning does (:mod:`epubconvert.run.holders`),
    and a book that moved on to its marked name claims that file
    (:func:`epubconvert.run.placing.place`).

    Yet a file under a name the plan gave a book, holding another book of the
    library, is that other book's. In skip mode the Ace edition, exported
    alone and then outsorted by an added 1965 edition, loses the name and is a
    collision; its archive, the only copy, was listed here as claimed by
    nothing -- the list a person reviews before deleting. An archive under a
    name no book wants, such as one left by adopting a renaming policy, stays
    an orphan: its book is written under the new name. A book that lost its
    name claims the file under the name it wanted, when that file is of its
    identity and may be its book: under a policy that names from the folder
    there is no identifier to go by, so ``b/dune.epub``, exported alone and
    then outsorted by an added ``a/Dune.epub`` that a case-insensitive
    filesystem gives the same file, had its only archive listed here.

    An archive whose marker names its source is judged by that alone: one
    written from a source no book of the library has is an orphan, whatever
    book its name or its identifier might be taken for, and so is one of a
    book the plan names elsewhere, as an archive left under an old name is.
    The archive of a book the plan gives no name may be its only one, and is
    not listed. That reads the last bytes of each file no book is placed at,
    and of no other.

    Nothing is deleted, here or anywhere. The never-deletes stance is
    deliberate; the gap was that nothing would say either.

    :param output_dir: Directory holding exported files.
    :param policy: Naming policy supplying filenames and identities.
    :param packages: **Every** package in the library, not the subset this run
        is looking at -- ``--match`` narrows a run, not the shelf.
    :param on_collision: The collision mode, so suffixed names are recognised.
    :param assigned: The names already given, when the caller has them, the
        files copied through included
        (:func:`~epubconvert.run.copynames.claim_copies`): a copy claims
        the file it is placed at, as a package does.
    :param unopened: The books not to open for their identifier.
    :param copied: Whether the run copies the files it takes along. A file
        a copy left unopened cannot tell from its own
        (:attr:`~epubconvert.utils.policy.Assignment.unverified`) is kept
        from the list only where the copy is reported, and says why: under
        ``--no-copy-through`` nothing else named it, and a deleted book's
        archive left the list without a word.

    :return: Archives no book accounts for, sorted by path.
    """
    if assigned is None:
        assigned = assign_names(
            packages,
            policy,
            on_collision,
            shelf=shelf_names(output_dir),
            unopened=unopened,
        )
    shelf = read_shelf(output_dir, policy, assigned, unopened=unopened)
    claimed = _unopened_forms(assigned, frozenset(packages), shelf)
    for item in assigned:
        clash = place(item, shelf).clash
        if item.unverified and not copied:
            continue
        if clash is None and not item.filename:
            clash = _held_by_loser(item, shelf)
        if clash is not None:
            claimed.add(filesystem_key(clash.identity))

    live = {item.identifier for item in assigned if item.identifier}
    nameless = {item.source for item in assigned if item.source and not item.filename}
    return sorted(
        found
        for found in shelf_files(output_dir)
        if (key := filesystem_key(policy.identity(found.name))) not in claimed
        and _unclaimed(found, key in shelf.spoken, (live, nameless), shelf)
    )


def _unclaimed(
    found: Path,
    spoken: bool,
    books: tuple[Container[str], Container[str]],
    shelf: Shelf,
) -> bool:
    """
    Decide whether a file no book is placed at is an orphan.

    :param found: The file.
    :param spoken: Whether its name is one the plan gave a book.
    :param books: The identifiers of the books of the library, and the
        sources of those the plan gives no name.
    :param shelf: The shelf, with the sources of the books of the library.

    :return: True when no book of the library may be the one it holds.
    """
    live, nameless = books
    marked = marker_on_shelf(found) if shelf.sources else None
    # A marker naming a source two books share names neither: the file is
    # judged as one written before markers (holders.written_for).
    if marked is not None and shelf.sources[marked] < 2:
        return marked not in nameless
    return not (live and spoken and identifier_on_shelf(found) in live)


def _unopened_forms(
    assigned: Sequence[Assignment], packages: Container[Path], shelf: Shelf
) -> set[str]:
    """
    Find the numbered and marked files of the names of packages left unopened.

    Under ``--skip-incomplete`` an evicted package's identifier is not read,
    so nothing says which numbered or marked file of its name is its own
    (:func:`~epubconvert.run.claims.kept_numbers`): it takes its plain name,
    and its own archive under a number was listed as an orphan. Any of them
    may be its own, as the file under the name a book lost may be
    (:func:`_held_by_loser`), so none is listed.

    :param assigned: Every book's name.
    :param packages: The library's packages, of the books in *assigned*.
    :param shelf: The archives already present; *shelf.unopened* says which
        books are left unopened.

    :return: The filesystem keys of those files.
    """
    forms: dict[str, list[str]] = {}
    for key in shelf.existing:
        plain = _plain(key)
        if plain != key:
            forms.setdefault(plain, []).append(key)
    if not forms:
        return set()
    return {
        key
        for item in assigned
        if item.package in packages
        and (keys := forms.get(_plain(filesystem_key(item.identity))))
        and item.package in shelf.unopened
        for key in keys
    }


def _plain(key: str) -> str:
    """A filesystem key less any digest marker and ``" (n)"``."""
    found = MARKED.fullmatch(key) or NUMBERED.fullmatch(key)
    return key if found is None else found["stem"] + (found["extension"] or "")


def _held_by_loser(item: Assignment, shelf: Shelf) -> Existing | None:
    """
    Find the archive a book that lost its name may still hold.

    :param item: The book, with no name and the identity of the one it wanted.
    :param shelf: The archives already present.

    :return: The archive under that name when it can be this book's, as the
        plan would judge a book of that name: of its identity, not holding
        another book by identifier, and not a file the claim pass found is
        not a copy's own.
    """
    found = shelf.existing.get(filesystem_key(item.identity))
    if (
        found is None
        or item.not_own
        or not same_identity(found.identity, item.identity)
    ):
        return None
    # The file under a name two books want: its marker says whose it is.
    marked = written_for(found.path, item.source, shelf.sources)
    if marked is not None:
        return found if marked else None
    return found if holds_another_book(found.path, item.identifier) is None else None


def orphan_decisions(
    orphans: Sequence[Path], assigned: Sequence[Assignment] = ()
) -> list[Decision]:
    """
    Render orphans as decisions so one listing can carry both.

    :param orphans: Archives no book accounts for.
    :param assigned: The names the orphans were found against, so one a
        file copied through may be the copy of, unopened, says so
        (:func:`find_orphans`).

    :return: One decision per orphan.
    """
    doubted = {
        item.filename: item.package
        for item in assigned
        if item.unverified and item.filename
    }
    return [
        Decision(
            path,
            ORPHAN,
            path,
            reason=(
                f"{doubted[path.name].name} is not downloaded from iCloud; "
                "cannot tell whether this is its copy"
                if path.name in doubted
                else "no book in the library claims this name"
            ),
        )
        for path in orphans
    ]
