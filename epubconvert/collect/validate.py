"""
Structural validation of exported epub archives.

Checks the properties a reader actually depends on: the archive opens, the
``mimetype`` entry is first and stored, the container points at a package
document, and every file the package document promises is really present.

That last check is the one that matters most here. A converter that silently
drops a content file still produces a structurally valid *zip*; what breaks is
the book, when a reader follows a manifest entry to a file that is not there.
Exactly that bug shipped in this project once, and this module is what catches
it.

Reading the package document itself lives in :mod:`.package`, and judging its
identifiers in :mod:`.identifiers`.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from zipfile import ZIP_STORED, BadZipFile, ZipFile

from ..utils.app_logger import logger
from ..utils.display import printable
from ..utils.opf import Package
from ..utils.spec import MIMETYPE_CONTENT, MIMETYPE_NAME, fold_name
from .package import (
    SHARED_HEADER,
    UNREADABLE_MEMBER,
    ValidationError,
    disallowed_method,
    open_member,
    open_regular,
    read_member,
    read_package,
    repeated_entries,
)

EPUBCHECK = "epubcheck"

#: How much a CRC check inflates before the ratio below is asked about.
#: Reading in chunks bounds the memory a check costs, not the time: a 4 MB
#: book declaring 4 GiB was inflated in full, 5 s of --verify apiece. Under
#: this, a check costs at most about 1.3 s here, whatever the book declares.
MAX_CHECKED_BYTES = 1024**3

#: Past :data:`MAX_CHECKED_BYTES`, how many times its own size a book may
#: declare and still have its members checked. A large real book is large
#: because of its pictures, sound and video, which are stored rather than
#: deflated, so it declares about what it holds: even a 2 GB illustrated or
#: video book is checked. Text deflates three- or fourfold, and nothing a
#: book is made of deflates thirty-twofold over a gigabyte; deflate itself
#: reaches a thousandfold, which is what a bomb uses.
MAX_CHECKED_RATIO = 32


class ArchiveInvalidError(Exception):
    """Raised when a freshly written archive fails its checks."""

    def __init__(self, name: str, problems: list[str]) -> None:
        self.name = name
        self.problems = problems
        shown = "; ".join(problems[:3])
        extra = f" (+{len(problems) - 3} more)" if len(problems) > 3 else ""
        super().__init__(f"{shown}{extra}")


@dataclass(frozen=True)
class ValidationOptions:
    """How thoroughly to check an archive after writing it."""

    enabled: bool = False
    epubcheck: bool = False

    def check(self, path: Path) -> list[str]:
        """
        Run the configured checks over *path*.

        :param path: The archive to check.

        :return: A list of problems; empty means it passed.
        """
        if not self.enabled:
            return []
        problems = validate_archive(path)
        if problems or not self.epubcheck:
            return problems
        return run_epubcheck(path)


def validate_archive(path: Path) -> list[str]:
    """
    Check one exported archive and describe anything wrong with it.

    :param path: The epub file to check.

    :return: A list of problems; empty means the archive is sound.
    """
    problems: list[str] = []

    try:
        # Judged on the descriptor, not a stat of the name: a FIFO swapped in
        # between the two was opened for reading, and waited for ever.
        with open_regular(path) as handle, ZipFile(handle) as archive:
            size = os.fstat(handle.fileno()).st_size
            names = archive.namelist()
            members = set(names)
            problems.extend(_check_mimetype(archive, names))
            problems.extend(_check_unique(names))
            repeated = repeated_entries(archive)
            if repeated == SHARED_HEADER:
                problems.append(repeated)

            # Before anything is inflated: the contents are checked only when
            # every member can be decompressed in bounded memory, and once.
            methods = _check_methods(archive)
            problems.extend(methods)
            if not methods and repeated is None:
                problems.extend(_check_contents(archive, members, size))
    except ValidationError as exc:
        return [str(exc)]
    except BadZipFile as exc:
        return [f"not a readable zip archive: {exc}"]
    except OSError as exc:
        return [f"could not open: {exc}"]
    except UNREADABLE_MEMBER as exc:
        # Everything else a damaged member raises: BadZipFile and OSError are
        # caught above, each with its own wording. --verify is the one command
        # whose job is finding damage, so it must report a hostile archive
        # rather than die on it and check nothing further.
        return [f"unreadable archive: {exc}"]

    return problems


def _check_contents(archive: ZipFile, members: set[str], size: int) -> list[str]:
    """
    Inflate every member, then check what the package document promises.

    Every member is inflated only if together they declare no more than the
    book is worth checking: see :data:`MAX_CHECKED_BYTES`. The declaration
    bounds the inflating, since zipfile reads a member no further than the
    size it declares. Past that, the book is reported rather than spending
    minutes on it, and what the package document promises is still checked.

    :param archive: The open archive, every member stored or deflated.
    :param members: Its member names, built once by the caller.
    :param size: The size of the archive itself, in bytes.

    :return: A list of problems.
    """
    problems: list[str] = []
    declared = sum(info.file_size for info in archive.infolist())
    if declared > max(MAX_CHECKED_BYTES, MAX_CHECKED_RATIO * size):
        problems.append(
            f"too large to check its members: they declare {declared} bytes, "
            f"more than {MAX_CHECKED_RATIO} times the {size} bytes the book holds"
        )
    else:
        broken = _first_corrupt(archive)
        if broken is not None:
            problems.append(f"corrupt member: {printable(broken)}")

    try:
        package = read_package(archive)
    except ValidationError as exc:
        return [*problems, printable(str(exc))]  # names the book's members

    return problems + _check_manifest(members, package)


def _first_corrupt(archive: ZipFile) -> str | None:
    """
    Inflate every member once and check it against its recorded CRC.

    What ``testzip()`` did, but by entry rather than by name: it opened each
    entry by name, so every entry repeating a name inflated the last of them
    again. Read in chunks, so no member is ever held whole.

    :param archive: The open archive, every member stored or deflated, and
        none listed twice.

    :return: The first member whose contents do not match its CRC, or None.
    """
    for info in archive.infolist():
        try:
            with open_member(archive, info) as handle:
                # zipfile checks the CRC itself once a stream is read to its
                # end, and raises BadZipFile if it differs, as testzip() did.
                while handle.read(_CHUNK_BYTES):
                    pass
        except BadZipFile:
            return info.filename
    return None


#: How much of a member :func:`_first_corrupt` inflates at a time.
_CHUNK_BYTES = 1024 * 1024


def _check_unique(names: list[str]) -> list[str]:
    """
    Report every member name the archive holds more than once, or as good as.

    OCF requires unique names, and readers disagree about a duplicate: some
    take the first local header, some the last directory entry, so one book
    shows different content in each. zipfile merely warns when writing one,
    and the validator passed it, so --verify called such an archive sound.

    Counted rather than searched for: each duplicate was looked up in a list
    of those found so far, so 80,000 names took 6.6 s to check.

    :param names: The archive's member names, in its own order.

    :return: Up to five duplicated names, and a count of the rest; then the
        same for names that differ only by case or normalization.
    """
    counted = Counter(names)
    repeated = [name for name, count in counted.items() if count > 1]
    problems = [
        f"member name appears more than once: {printable(name)}"
        for name in repeated[:5]
    ]
    if len(repeated) > 5:
        more = len(repeated) - 5
        problems.append(f"...and {more} more member name(s) appearing more than once")
    return problems + _check_folded(counted)


def _check_folded(counted: Counter[str]) -> list[str]:
    """
    Report distinct member names that differ only by case or normalization.

    OCF requires names to stay unique after full case folding and NFC, since
    a reader unpacking the book onto APFS, HFS+ or NTFS writes both to one
    file, and which one survives is up to the order it writes them in.

    :param counted: Each distinct member name, so an exact duplicate, which
        :func:`_check_unique` reports, is not reported again here.

    :return: Up to five colliding groups named, and a count of the rest.
    """
    groups: dict[str, list[str]] = {}
    for name in counted:
        groups.setdefault(fold_name(name), []).append(name)
    colliding = [sorted(group) for group in groups.values() if len(group) > 1]
    problems = [
        "member names differ only by case or Unicode normalization: "
        + ", ".join(_distinguished(group))
        for group in colliding[:5]
    ]
    if len(colliding) > 5:
        problems.append(
            f"...and {len(colliding) - 5} more member name(s) "
            "differing only by case or normalization"
        )
    return problems


def _distinguished(names: list[str]) -> list[str]:
    """
    Render names safe to print, and told apart even where they look alike.

    A composed ``é`` and an ``e`` with a combining accent print the same, and
    a report naming one file twice explains nothing, so names that would are
    spelled with every character past ASCII escaped.

    :param names: Distinct names.

    :return: Their renderings, in the same order.
    """
    shown = [printable(name) for name in names]
    if len({unicodedata.normalize("NFC", name) for name in shown}) == len(shown):
        return shown
    return [ascii(name)[1:-1] for name in names]


def _check_methods(archive: ZipFile) -> list[str]:
    """
    Report every member compressed with a method OCF does not allow.

    :param archive: The open archive.

    :return: Up to five members named, and a count of the rest.
    """
    disallowed = [
        f"member is compressed with {method}, which an epub may not use: "
        f"{printable(info.filename)}"
        for info in archive.infolist()
        if (method := disallowed_method(info)) is not None
    ]
    if len(disallowed) > 5:
        more = len(disallowed) - 5
        return [
            *disallowed[:5],
            f"...and {more} more member(s) compressed a way OCF forbids",
        ]
    return disallowed


def _check_mimetype(archive: ZipFile, names: list[str]) -> list[str]:
    """
    Check the ``mimetype`` entry the epub specification mandates.

    :param archive: The open archive.
    :param names: Its member names, built once by the caller.

    :return: A list of problems.
    """
    problems: list[str] = []

    if not names:
        return ["archive is empty"]
    # First by position in the file, never by the central directory's order:
    # that index is written last, in whatever order the writer chose. OCF
    # requires mimetype *physically* first, since a reader identifies an epub
    # by the bytes at offset 0. Judged by the index, an archive listing
    # mimetype first while storing it later passed, and a sound one storing it
    # first but listing it later was reported damaged. zipfile reports offsets
    # from the start of the file, so bytes prepended to it are caught too.
    first = min(archive.infolist(), key=lambda member: member.header_offset)
    if first.filename != MIMETYPE_NAME:
        problems.append(f"first member is {first.filename!r}, not 'mimetype'")
        if MIMETYPE_NAME not in names:
            return problems

    info = archive.getinfo(MIMETYPE_NAME)
    if first.filename == MIMETYPE_NAME and info.header_offset != 0:
        problems.append(
            f"mimetype is stored at byte {info.header_offset}, not first in the file"
        )
    stored = info.compress_type == ZIP_STORED
    if not stored:
        problems.append("mimetype is compressed; it must be stored")
    # The specification fixes this member's length exactly, so a declared size
    # that differs settles it without reading anything. --verify runs over
    # files this tool may not have written, and a member declaring 512 MiB was
    # otherwise materialised in full to be compared against 20 bytes. Nor is
    # the declaration a bound, so only a stored member is read, and only as
    # far as that length: a compressed one declaring 20 bytes inflated whole.
    if info.file_size != len(MIMETYPE_CONTENT) or (
        stored and read_member(archive, info, len(MIMETYPE_CONTENT)) != MIMETYPE_CONTENT
    ):
        problems.append("mimetype does not contain 'application/epub+zip'")

    return problems


def _check_manifest(members: set[str], package: Package) -> list[str]:
    """
    Check that everything the package document promises is present.

    :param members: The archive's member names, built once by the caller.
    :param package: The parsed package document.

    :return: Problems, the book's ids and hrefs escaped: ``%1B`` decodes to ESC.
    """
    problems: list[str] = []

    if not package.manifest:
        problems.append(f"{printable(package.opf_path)} declares no manifest items")

    missing = sorted(
        printable(f"{item_id} -> {href}")
        for item_id, href in package.manifest.items()
        if href not in members
    )
    for entry in missing[:5]:
        problems.append(f"manifest item is not in the archive: {entry}")
    if len(missing) > 5:
        problems.append(f"...and {len(missing) - 5} more missing manifest item(s)")

    dangling = sorted(map(printable, set(package.spine) - set(package.manifest)))
    for idref in dangling[:5]:
        problems.append(f"spine references unknown manifest id: {idref}")
    if len(dangling) > 5:
        problems.append(f"...and {len(dangling) - 5} more dangling spine id(s)")

    if not package.spine:
        problems.append(f"{printable(package.opf_path)} declares no spine")

    return problems


#: A line in which epubcheck reports a problem that fails a book.
_EPUBCHECK_FAILURE = re.compile(r"\b(?:ERROR|FATAL)\b")


def epubcheck_available() -> bool:
    """
    Report whether the external ``epubcheck`` tool is on PATH.

    :return: True if it can be run.
    """
    return shutil.which(EPUBCHECK) is not None


def run_epubcheck(path: Path, timeout: int = 120) -> list[str]:
    """
    Run the external ``epubcheck`` validator over an archive.

    This is a much stricter check than the structural one, and is only
    attempted when the user asks for it.

    :param path: The epub file to check.
    :param timeout: Seconds to allow before giving up.

    :return: A list of problems; empty means epubcheck was happy, or that it
        could not run at all, which is logged rather than blamed on the book.
    """
    executable = shutil.which(EPUBCHECK)
    if executable is None:
        return ["epubcheck is not on PATH"]
    name = printable(path.name)

    try:
        completed = subprocess.run(  # noqa: S603 - fixed executable, no shell
            [executable, str(path)],
            capture_output=True,
            # A JVM writes in its own locale's encoding, not necessarily this
            # one's, and a strict decode raised UnicodeDecodeError out of here
            # for a member named in ISO-8859-1.
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # Not a verdict on the book. Reported as a problem, it kept the book
        # out of the output directory -- so a book slower to check than the
        # timeout was retried and never written, on every run -- and made
        # --verify count a sound archive as damaged.
        logger.warning(
            "epubcheck could not check %s, so it was not checked: %s",
            name,
            exc,
        )
        return []

    if completed.returncode == 0:
        return []

    # Both streams: a JVM writes notices such as "Picked up JAVA_TOOL_OPTIONS"
    # to stderr, which hid every diagnostic on stdout when only one was read.
    # FATAL is epubcheck's most severe level, and was dropped with the rest.
    output = f"{completed.stderr}\n{completed.stdout}".splitlines()
    failing = [line for line in output if _EPUBCHECK_FAILURE.search(line)]
    errors = [printable(line.strip()) for line in failing]  # they name its members
    logger.debug("epubcheck exited %d for %s", completed.returncode, name)
    return errors[:10] or [f"epubcheck failed with exit code {completed.returncode}"]
