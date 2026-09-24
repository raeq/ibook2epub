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

import re
import shutil
import stat
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from zipfile import ZIP_STORED, BadZipFile, ZipFile

from ..utils.app_logger import logger
from ..utils.display import printable
from ..utils.opf import Package
from ..utils.spec import MIMETYPE_CONTENT, MIMETYPE_NAME
from .package import (
    UNREADABLE_MEMBER,
    ValidationError,
    disallowed_method,
    read_member,
    read_package,
)

EPUBCHECK = "epubcheck"


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
        if not stat.S_ISREG(path.stat().st_mode):  # Opening a FIFO waits for ever.
            return ["not a regular file"]
        with ZipFile(path) as archive:
            names = archive.namelist()
            members = set(names)
            problems.extend(_check_mimetype(archive, names))
            problems.extend(_check_unique(names))

            # Before anything is inflated: the contents are checked only when
            # every member can be decompressed in bounded memory.
            problems.extend(
                _check_methods(archive) or _check_contents(archive, members)
            )
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


def _check_contents(archive: ZipFile, members: set[str]) -> list[str]:
    """
    Inflate every member, then check what the package document promises.

    :param archive: The open archive, every member stored or deflated.
    :param members: Its member names, built once by the caller.

    :return: A list of problems.
    """
    problems: list[str] = []
    broken = archive.testzip()
    if broken is not None:
        problems.append(f"corrupt member: {printable(broken)}")

    try:
        package = read_package(archive)
    except ValidationError as exc:
        return [*problems, printable(str(exc))]  # names the book's members

    return problems + _check_manifest(members, package)


def _check_unique(names: list[str]) -> list[str]:
    """
    Report every member name the archive holds more than once.

    OCF requires unique names, and readers disagree about a duplicate: some
    take the first local header, some the last directory entry, so one book
    shows different content in each. zipfile merely warns when writing one,
    and the validator passed it, so --verify called such an archive sound.

    Counted rather than searched for: each duplicate was looked up in a list
    of those found so far, so 80,000 names took 6.6 s to check.

    :param names: The archive's member names, in its own order.

    :return: Up to five duplicated names, and a count of the rest.
    """
    repeated = [name for name, count in Counter(names).items() if count > 1]
    problems = [
        f"member name appears more than once: {printable(name)}"
        for name in repeated[:5]
    ]
    if len(repeated) > 5:
        more = len(repeated) - 5
        problems.append(f"...and {more} more member name(s) appearing more than once")
    return problems


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
