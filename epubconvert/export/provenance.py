"""
The mark an archive carries of the book it was written from.

There is no state file, and a name on the shelf is not a book
(:mod:`epubconvert.run.holders`). A book's identifier tells two books apart
only where each declares a usable one of its own: 92 books in a surveyed
library claim to be ``none``, and 52 more share a real value, so two books
of one name were told apart by the name alone. One could be reported
exported from the other's file and never written, and ``--force`` or
``--refresh`` wrote it over the other's archive (formal/README.md,
``Unidentifiable``).

So every archive this tool writes names its source in the zip archive
comment: ``ibook2epub/1 src=<digest>``. The comment sits after the central
directory, outside the OCF container, where readers ignore it and nothing
moves ``mimetype`` from the start. The version number lets it grow: a reader
takes the ``src`` field of any version it finds and ignores the others.

The key is the book's path in the library, relative to the library's root,
digested as a marked name digests an identifier
(:func:`~epubconvert.export.naming.disambiguator`) and as a note's ``src=``
is: ``a/Dune.epub`` and ``b/Dune.epub`` are two books. Folded as
:func:`~epubconvert.export.naming.filesystem_key` folds a name -- NFC and
Unicode case folding -- because the library lives on a volume that is
case-insensitive by default: a folder renamed by case is the same folder
there, and a book that lost its own file to the rename would be written
again under a number and its archive listed as an orphan, on every run until
someone noticed. The fold errs towards "its own" only for two books whose
paths differ by case alone, which a case-insensitive volume cannot hold.
Where a case-sensitive one does, a marker naming their one source is no
evidence for either: it neither gives the file to one of them nor refuses it
to both, and their names and identifiers decide, as before markers. Once one
of them leaves the library, its archives name the other's source, and are
judged as a deleted book's are where another book was added at its path.

Apple's asset id is not in it: a conversion reads the library directory,
never Apple's database, and a run that has no asset id would write
archives that name less than those of a run that has.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

from .naming import disambiguator, filesystem_key

#: The marker's version. A reader accepts any from 1 up.
VERSION = 1

#: The marker as the archive comment holds it: this tool, a version, then
#: space-separated fields of a lowercase key and a plain value, one of them
#: ``src``. Anything else -- a comment another tool wrote, a control
#: character, a stray space -- is no marker, so nothing read here can reach a
#: terminal or a report except the hex digest.
MARKER = re.compile(
    rb"ibook2epub/(?P<version>[1-9][0-9]{0,2})"
    rb"(?P<fields>(?: [a-z]{1,16}=[0-9A-Za-z._-]{1,64}){1,8})"
)
#: The ``src`` field, among a marker's fields.
SOURCE_FIELD = re.compile(rb" src=(?P<digest>[0-9a-f]{8,64})(?= |$)")

#: Where the end-of-central-directory record starts, and how long it is less
#: its comment. The comment's length is its last two bytes.
_END_SIGNATURE = b"PK\x05\x06"
_END_SIZE = 22
#: The most of a comment worth reading for a marker. A marker is short; a
#: longer comment is another tool's, and not read past this.
MAX_MARKER_BYTES = 512
#: How many of a file's last bytes are read for its marker: the end record
#: and a comment as long as a marker is read, and no more, by either reader
#: (:func:`read_source`, and the one that reads the identifier too).
TAIL_BYTES = _END_SIZE + MAX_MARKER_BYTES


def source_of(package: Path, library: Path | None) -> str | None:
    """
    Digest a book's path in the library, as its archives' marker names it.

    :param package: The book's package directory.
    :param library: The library's root, the directory the run walks.

    :return: The digest, or None when there is no library to be relative
        to, or the book is not in it.
    """
    if library is None:
        return None
    try:
        relative = package.relative_to(library)
    except ValueError:
        return None
    return disambiguator(filesystem_key(relative.as_posix()))


def stamp(source: str) -> bytes:
    """
    Render the marker that names *source*.

    :param source: A digest from :func:`source_of`.

    :return: The archive comment.
    """
    return bytes(f"ibook2epub/{VERSION} src={source}", "ascii")


def parse(comment: bytes) -> str | None:
    """
    Find the source a marker names.

    :param comment: An archive comment.

    :return: The digest, or None when the comment is no marker.
    """
    found = MARKER.fullmatch(comment)
    if found is None:
        return None
    field = SOURCE_FIELD.search(found["fields"])
    return None if field is None else str(field["digest"], "ascii")


def read_source(archive: Path) -> str | None:
    """
    Read the source an archive's marker names, from the end of the file.

    Only the end of the central directory is read, one short read after a
    seek, never the directory itself: ``ZipFile`` parses every entry of it to
    offer the comment, and the planner asks for this where it trusted a name
    for free.

    :param archive: A file on the shelf.

    :return: The digest, or None when it carries no marker or cannot be
        read.
    """
    try:
        descriptor = os.open(archive, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode):
            # A FIFO would be waited on for ever.
            return None
        span = min(status.st_size, TAIL_BYTES)
        os.lseek(descriptor, status.st_size - span, os.SEEK_SET)
        tail = os.read(descriptor, span)
    except OSError:
        return None
    finally:
        os.close(descriptor)
    return from_tail(tail)


def from_tail(tail: bytes) -> str | None:
    """
    Find the source the marker in a file's last bytes names.

    The one rule for both readers: the comment of an end record that ends the
    file, no byte after it, within the last :data:`TAIL_BYTES`. ``ZipFile``
    finds a comment wherever the last end record is, with anything after it,
    so a file with bytes appended named its source to one reader and not the
    other.

    :param tail: The file's last :data:`TAIL_BYTES` bytes, or all of it when
        it is shorter.

    :return: The digest, or None when they end in no marker.
    """
    return parse(_comment(tail))


def _comment(tail: bytes) -> bytes:
    """
    Find the archive comment in the last bytes of a zip archive.

    :param tail: The file's last bytes, the end of the central directory
        among them.

    :return: The comment, or nothing when no end record ends the file there.
    """
    at = tail.rfind(_END_SIGNATURE)
    while at >= 0:
        record = tail[at : at + _END_SIZE]
        if len(record) == _END_SIZE:
            length = int.from_bytes(record[20:22], "little")
            if at + _END_SIZE + length == len(tail):
                return tail[at + _END_SIZE :]
        at = tail.rfind(_END_SIGNATURE, 0, at)
    return b""
