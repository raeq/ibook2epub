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
for every book whose name is on the shelf. Under the policies that name from
the folder it runs only for a book about to be written over an archive
(:func:`epubconvert.run.planning.plan_exports`), which then pays for one source
read too; reporting a book exported still trusts the name there, because
checking that would read every source and every archive on every rerun.

Kept apart from the planner, which decides what to do about the answer.
"""

from __future__ import annotations

import unicodedata
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from ..collect.validate import ValidationError, read_package, usable_identifier


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


def identifier_on_shelf(archive_path: Path) -> str | None:
    """
    Read the usable identifier of an archive already on the shelf.

    :param archive_path: The exported archive.

    :return: Its identifier, or None when it has none or cannot be read.
    """
    try:
        with ZipFile(archive_path) as archive:
            return usable_identifier(read_package(archive))
    except (ValidationError, BadZipFile, OSError):
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
