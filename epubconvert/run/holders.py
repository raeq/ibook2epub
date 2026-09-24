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
(:func:`epubconvert.run.planning.find_orphans`) and the plan
(:func:`epubconvert.run.planning.plan_exports`) each ask, and the second is
answered from what the first read. Asked apart, they read the shelf twice: 400
opens for a no-op rerun over 200 books. Under the policies that name from the
folder it runs only for a book about to be written over an archive, which then
pays for one source read too; reporting a book exported still trusts the name
there, because checking that would read every source and every archive on
every rerun.

Kept apart from the planner, which decides what to do about the answer.
"""

from __future__ import annotations

import unicodedata
from functools import lru_cache
from pathlib import Path

from ..collect.identifiers import usable_identifier
from ..collect.package import ValidationError, read_archive_package


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
    holder = identifier_on_shelf(found)
    if holder is None or holder == identifier:
        return None
    return f"{found.name} holds another book, {holder}; this book is {identifier}"


def foreign(
    found: Path, found_identity: str, identity: str, identifier: str | None
) -> str | None:
    """
    Explain why a file matching a book's name on the filesystem is not its own.

    Two ways it can be. The filesystem key answers a looser question than
    identity, so two different books can share it; and even a file of this
    book's own identity may hold another book (:func:`holds_another_book`).

    :param found: The archive occupying this book's filename.
    :param found_identity: That archive's identity, from its name on disk.
    :param identity: This book's identity.
    :param identifier: This book's usable identifier, or None.

    :return: The reason to report, or None when the file may be this book's.
    """
    if not same_identity(found_identity, identity):
        return f"{found.name} already holds this name"
    return holds_another_book(found, identifier)
