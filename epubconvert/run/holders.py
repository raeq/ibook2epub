"""
Whose book an archive already on the shelf holds.

A name is not a book. With no state file, a book whose name is on the shelf
was taken for exported -- but the name is only what the planner computes now,
and a file keeps the book it was written for. A run narrowed by ``--match``
names only the books it selected, so a book alone in it takes a name another
edition's archive already has; and once the edition holding a name is deleted
from the library, the next edition takes the name. Each was reported exported
by the other book's archive and never written, and ``--refresh`` or
``--force`` wrote it over that archive, which for a deleted book can be the
last copy. formal/RerunPlanner.tla found all three.

So when the book has a usable identifier, the archive's identifier is read and
compared. Only when both are usable: a placeholder such as ``none``, or an
archive that cannot be read, says nothing about which book it is, and the name
is trusted as before.

Reading one archive's identifier measured 0.15 ms for a 4-member book and
1.38 ms for a 504-member one, on Linux 6.18 with the archives in the page
cache: between 0.4 s and 3.9 s over a 2,800-book shelf, against no reads at
all. Under a policy that already reads each source's package document it runs
once per run for every archive under a name the plan gave a book, whole
library included under ``--match``: the orphan check
(:func:`epubconvert.run.orphans.find_orphans`) and the plan
(:func:`epubconvert.run.planning.plan_exports`) each ask, and the second is
answered from what the first read. Asked apart, they read the shelf twice: 400
opens for a no-op rerun over 200 books. Under the policies that name from the
folder it runs only for a book about to be written over an archive, which then
pays for one source read too; reporting a book exported still trusts the name
there, because checking that would read every source and every archive on
every rerun. The one exception is a file found under another spelling of the
book's name, which may be the book's own archive after a rename by case; that
rare case pays for one source read (:func:`foreign`).

Kept apart from the planner, which decides what to do about the answer.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Collection, Container
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from ..collect.identifiers import usable_identifier
from ..collect.package import (
    ValidationError,
    read_archive_package,
    read_package_dir,
)
from ..collect.source import is_evicted


def same_identity(first: str, second: str) -> bool:
    """
    Report whether two identities name one book, whatever their normal form.

    Compared through NFC, like the lookup that finds a file on the shelf: HFS+
    hands names back decomposed, and compared raw a book's own archive read
    back from there was another book's for ever -- a collision no ``--refresh``
    or ``--force`` could get past. Canonically equivalent names render alike
    and are one file wherever the filesystem normalizes, so they cannot be two
    books.

    :param first: One identity, as a naming policy computes it.
    :param second: The other.

    :return: True if the two are the same identity.
    """
    return unicodedata.normalize("NFC", first) == unicodedata.normalize("NFC", second)


#: Archives whose identifier is remembered. A shelf surveyed at 2,800 books
#: fits many times over; an entry is a path and a short string.
REMEMBERED = 1 << 15


def identifier_on_shelf(archive_path: Path) -> str | None:
    """
    Read the usable identifier of an archive already on the shelf.

    Remembered for as long as the file is unchanged. Keyed on what ``stat``
    says rather than the path alone, because the run writes between asking
    twice: files copied through land after the orphan check and before the
    plan, and every write renames a new file into place, which changes the
    inode even when the size and a coarse timestamp do not.

    :param archive_path: The exported archive.

    :return: Its identifier, or None when it has none or cannot be read.
    """
    try:
        status = archive_path.stat()
    except OSError:
        return None
    return _identifier_of(
        archive_path, (status.st_ino, status.st_mtime_ns, status.st_size)
    )


@lru_cache(maxsize=REMEMBERED)
def _identifier_of(archive_path: Path, _stamp: tuple[int, int, int]) -> str | None:
    """Read an archive's identifier; *_stamp* only keys what is remembered."""
    try:
        return usable_identifier(read_archive_package(archive_path))
    except ValidationError:
        return None


def holds_another_book(found: Path, identifier: str | None) -> str | None:
    """
    Explain why the archive under a book's name holds another book, if it does.

    :param found: The archive on the shelf under this book's name.
    :param identifier: This book's usable identifier, or None.

    :return: The reason to report, or None when the archive may be this book.
    """
    if identifier is None:
        return None
    return _held_by(found, identifier_on_shelf(found), identifier)


def _held_by(found: Path, holder: str | None, identifier: str) -> str | None:
    """Explain a mismatch between the shelf's identifier and this book's."""
    if holder is None or holder == identifier:
        return None
    return f"{found.name} holds another book, {holder}; this book is {identifier}"


@dataclass(frozen=True)
class Unopened:
    """
    The books a run must not open to identify, because opening one downloads it.

    Asked as a container: ``book in unopened``.
    """

    #: Files copied through that are left unopened: evicted, under
    #: ``--skip-incomplete`` or ``--no-copy-through``.
    files: frozenset[Path] = frozenset()
    #: ``--skip-incomplete``: a package iCloud has evicted is left unopened
    #: too, told by one stat when it is asked about (source.is_evicted).
    packages: bool = False

    def __contains__(self, book: object) -> bool:
        """Whether *book* is not to be opened."""
        if book in self.files:
            return True
        return self.packages and isinstance(book, Path) and is_evicted(book)


def foreign(
    found: Path,
    found_identity: str,
    identity: str,
    identifier: str | None,
    *,
    source: Path | None = None,
    live: Collection[str] = frozenset(),
    unopened: Container[Path] = frozenset(),
) -> str | None:
    """
    Explain why a file matching a book's name on the filesystem is not its own.

    Two ways it can be. The filesystem key answers a looser question than
    identity, so two different books can share it; and even a file of this
    book's own identity may hold another book (:func:`holds_another_book`).

    A file of another identity under the same key is not always another
    book's, though. Rename ``b/dune.epub`` to ``b/Dune.epub`` and its archive
    is found under the old spelling, where the default policy, comparing
    names exactly, called it another book's for ever: a collision with its
    own archive, or in suffix mode a second copy beside it and the first
    listed as an orphan. So when *source* is given the book's identifier is
    read, if the plan has not, and compared with the file's -- one read, paid
    only for a file of another spelling. When neither identifier says, the
    name is trusted unless another book of the library has the file's exact
    identity: a book renamed by case keeps its archive, and a namesake that
    is still in the library keeps its own.

    :param found: The archive occupying this book's filename.
    :param found_identity: That archive's identity, from its name on disk.
    :param identity: This book's identity.
    :param identifier: This book's usable identifier, or None.
    :param source: The book's own package or file, to read its identifier
        from when the file is of another identity. Without it such a file
        is always another book's.
    :param live: The identity of every book in the plan, NFC-normalized.
    :param unopened: Books not to open for their identifier, because opening
        one downloads it: *source* is judged as one whose identifier says
        nothing. The read ignored ``--skip-incomplete``, and downloaded the
        book the flag exists to leave where it is.

    :return: The reason to report, or None when the file may be this book's.
    """
    if same_identity(found_identity, identity):
        return holds_another_book(found, identifier)
    taken = f"{found.name} already holds this name"
    if source is None:
        return taken
    if identifier is None and source not in unopened:
        identifier = source_identifier(source)
    holder = identifier_on_shelf(found) if identifier is not None else None
    if holder is not None and identifier is not None:
        # Read once: holds_another_book would stat the archive again.
        return _held_by(found, holder, identifier)
    return taken if unicodedata.normalize("NFC", found_identity) in live else None


def source_identifier(source: Path) -> str | None:
    """
    Read the usable identifier of a book in the library.

    Remembered while the book is unchanged, as :func:`identifier_on_shelf`
    remembers an archive's: a run and ``--list`` each place the library
    three times -- to settle the copies, for the plan and for the orphan
    check -- and a book renamed by case had its package document read at
    each. A package directory is keyed on its own ``stat``, which changes as
    entries are added or removed; within one run nothing rewrites it.

    :param source: A package directory, or a file copied through.

    :return: Its identifier, or None when it has none or cannot be read, as
        for a PDF.
    """
    try:
        status = source.stat()
    except OSError:
        return None
    return _source_identifier_of(
        source, (status.st_ino, status.st_mtime_ns, status.st_size)
    )


@lru_cache(maxsize=REMEMBERED)
def _source_identifier_of(source: Path, _stamp: tuple[int, int, int]) -> str | None:
    """Read a book's identifier; *_stamp* only keys what is remembered."""
    try:
        if source.is_dir():
            return usable_identifier(read_package_dir(source))
        return usable_identifier(read_archive_package(source))
    except (ValidationError, OSError):
        return None
