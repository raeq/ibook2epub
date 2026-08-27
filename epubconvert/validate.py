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

This module also holds the OPF reading used elsewhere: resolving the package
document from ``META-INF/container.xml`` and pulling the manifest, spine and
metadata out of it.
"""

from __future__ import annotations

import posixpath
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from urllib.parse import unquote, urldefrag
from xml.etree import ElementTree
from zipfile import ZIP_STORED, BadZipFile, ZipFile

from .app_logger import logger
from .contained import escapes as escapes_archive
from .contained import is_remote, open_contained, resolve
from .spec import CONTAINER_PATH, MIMETYPE_CONTENT, MIMETYPE_NAME

CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"
OPF_NS = "http://www.idpf.org/2007/opf"
DC_NS = "http://purl.org/dc/elements/1.1/"

#: Cap on the *stored* size of XML read from an archive. This bounds the read,
#: not the parse: ElementTree expands internal entities, so a small document
#: can still expand to something far larger, and this limit does not prevent
#: that. It exists to reject implausible files early, not as a memory bound.
MAX_XML_BYTES = 16 * 1024 * 1024

EPUBCHECK = "epubcheck"


@dataclass
class Package:
    """The parts of a package document (OPF) this tool cares about."""

    opf_path: str
    title: str | None = None
    creator: str | None = None
    creator_sort: str | None = None
    identifier: str | None = None
    #: Manifest item id to archive path, already resolved and unquoted.
    manifest: dict[str, str] = field(default_factory=dict)
    #: Manifest ids referenced by the spine, in reading order.
    spine: list[str] = field(default_factory=list)
    #: Manifest id of the cover image, when the package declares one.
    cover_id: str | None = None


#: Values that appear where a ``dc:identifier`` should be but identify nothing.
#: Every one was found in a real 2,805-book library: ``none`` is the identifier
#: for 92 books, ``ISBN`` for three, ``unknown`` for one. Matched case-folded.
JUNK_IDENTIFIERS = frozenset(
    {"none", "null", "isbn", "unknown", "uuid", "calibre", "0"}
)


def usable_identifier(package: Package | None) -> str | None:
    """
    Return the package's identifier, if it identifies anything.

    An identifier is worth using only when it distinguishes this book from
    another. A placeholder the publisher never replaced does the opposite: 92
    books in a surveyed library all claim to be ``none``, so treating that as a
    real value would merge them.

    Uniqueness is *not* checked here, because it cannot be: two books can carry
    the same genuine identifier -- six unrelated technical books in that library
    share one converter's template UUID. Callers that need distinctness have to
    confirm it against the set they are naming.

    :param package: The parsed package document, or None if it was not read.

    :return: The trimmed identifier, or None if there is nothing usable.
    """
    if package is None or not package.identifier:
        return None
    trimmed = package.identifier.strip()
    if not trimmed or trimmed.casefold() in JUNK_IDENTIFIERS:
        return None
    return trimmed


#: Values a converter writes where a title should be. Narrower than
#: :data:`JUNK_IDENTIFIERS` and applied only to titles: 300 books in a surveyed
#: library declare a *creator* of "Unknown", and filing those together under U
#: is a real cataloguing answer, where a title of "none" says less than the
#: folder name the book already has. Matched case-folded, and whole: "None of
#: This Is True" is a real book.
JUNK_TITLES = frozenset({"none", "null", "n/a", "unknown"})


def usable_title(package: Package | None) -> str | None:
    """
    Return the package's title, if it names anything.

    31 books in a surveyed library carry the literal string "none" in every
    metadata field they have, including the one Apple derives its folder name
    from. Treating that as a title made all 31 want ``none.epub``, so they
    collided with each other and were suffixed into a pile.

    :param package: The parsed package document, or None if it was not read.

    :return: The trimmed title, or None if there is nothing usable.
    """
    if package is None or not package.title:
        return None
    trimmed = package.title.strip()
    if not trimmed or trimmed.casefold() in JUNK_TITLES:
        return None
    return trimmed


class ValidationError(Exception):
    """Raised when an archive cannot be validated at all."""


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


class _Members(Protocol):  # pylint: disable=too-few-public-methods
    """
    Reads a named member out of a book, however the book happens to be stored.

    A book arrives in two shapes: an ``*.epub`` archive and the unpacked
    ``*.epub/`` directory Apple keeps. Fetching the bytes genuinely differs --
    one is a zip lookup, the other a guarded open that must refuse a symlink --
    but everything past that is the same rule. Writing that rule twice is what
    made the canonical-identifier defect need fixing at two entry points.
    """

    def read(self, name: str) -> bytes:
        """Return a member's bytes, or raise :class:`ValidationError`."""


class _ArchiveMembers:  # pylint: disable=too-few-public-methods
    """Members of an epub archive."""

    def __init__(self, archive: ZipFile) -> None:
        self.archive = archive

    def read(self, name: str) -> bytes:
        """
        Return a member's bytes, refusing an implausibly large one.

        The size is taken from the central directory before anything is
        inflated, so a zip bomb is refused rather than expanded.
        """
        try:
            info = self.archive.getinfo(name)
        except KeyError as exc:
            raise ValidationError(f"missing {name}") from exc

        if info.file_size > MAX_XML_BYTES:
            raise ValidationError(
                f"{name} is implausibly large ({info.file_size} bytes)"
            )
        try:
            return self.archive.read(name)
        except (BadZipFile, OSError) as exc:
            raise ValidationError(f"could not read {name}: {exc}") from exc


class _DirectoryMembers:  # pylint: disable=too-few-public-methods
    """Members of an unpacked package directory."""

    def __init__(self, root: Path) -> None:
        self.root = root
        # Resolved once: resolving walks every component, and it does not
        # change across a package.
        self.resolved_root = root.resolve()

    def read(self, name: str) -> bytes:
        """
        Return a member's bytes, refusing a link out of the package.

        Both documents are named by the book and read straight off disk, so a
        symlinked package document let a book choose any file the user could
        read as its own metadata.
        """
        path = resolve(self.root, name, resolved_root=self.resolved_root)
        if path is None:
            raise ValidationError(f"{name} is not a readable file")
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise ValidationError(f"missing {name}") from exc
        if size > MAX_XML_BYTES:
            raise ValidationError(f"{name} is implausibly large ({size} bytes)")
        try:
            with open_contained(path) as handle:
                return handle.read()
        except OSError as exc:
            raise ValidationError(f"could not read {name}: {exc}") from exc


def _element(members: _Members, name: str) -> ElementTree.Element:
    """
    Read a member and parse it as XML.

    :param members: The book being read.
    :param name: Path of the member within the book.

    :return: The parsed root element.

    :raises ValidationError: If the member is missing or not valid XML.
    """
    data = members.read(name)
    if _declares_entities(data):
        raise ValidationError(f"{name} declares XML entities, which are not allowed")
    try:
        return ElementTree.fromstring(data)
    except ElementTree.ParseError as exc:
        raise ValidationError(f"{name} is not valid XML: {exc}") from exc


def _declares_entities(data: bytes) -> bool:
    """
    Report whether a document declares XML entities.

    A package document is attacker-controlled, and an entity declaration lets a
    small file expand into a large one. :data:`MAX_XML_BYTES` cannot see it: the
    cap measures the file, and the expansion happens after it is read.

    expat has capped the amplification factor since 2.4, so a current Python
    already refuses the classic attack. That protection is implicit, silent and
    version-dependent, and this tool supports Python 3.10 and newer. The rule is
    stated here so it belongs to the tool rather than to whichever expat the
    interpreter was built against.

    Only the ``DOCTYPE`` declaration is scanned, because that is the one place
    an entity may be declared. Scanning the whole document would refuse a book
    that merely writes about entities in its title.

    :param data: The raw bytes of the document.

    :return: True if a ``DOCTYPE`` internal subset declares any entity.
    """
    start = data.find(b"<!DOCTYPE")
    if start < 0:
        return False

    # Walk to the declaration's own ">", ignoring any inside the "[...]"
    # internal subset. An unterminated declaration falls out at end of input
    # and is treated as declaring whatever it contains, which fails closed.
    depth = 0
    index = start
    while index < len(data):
        char = data[index : index + 1]
        if char == b"[":
            depth += 1
        elif char == b"]":
            depth -= 1
        elif char == b">" and depth <= 0:
            break
        index += 1
    return b"<!ENTITY" in data[start:index]


def _opf_path(members: _Members) -> str:
    """
    Resolve the package document path from ``META-INF/container.xml``.

    A container may list several rootfiles; one without a ``full-path`` is not
    the one we want and is not a reason to give up.

    :param members: The book being read.

    :return: Path of the package document within the book.

    :raises ValidationError: If the container is missing, names no rootfile, or
        names one outside the book.
    """
    root = _element(members, CONTAINER_PATH)
    for rootfile in root.iter(f"{{{CONTAINER_NS}}}rootfile"):
        full_path = rootfile.get("full-path")
        if full_path:
            # Checked for both shapes. The directory reader joins the result
            # onto a real directory, so a rootfile of "/etc/passwd" or
            # "../../.." would be opened rather than merely missed.
            return _checked_opf_path(full_path)
    raise ValidationError(f"{CONTAINER_PATH} names no rootfile")


def find_opf_path(archive: ZipFile) -> str:
    """
    Resolve the package document path of an archive.

    :param archive: The open archive.

    :return: Archive path of the package document.

    :raises ValidationError: If the container is missing, names no rootfile, or
        names one outside the archive.
    """
    return _opf_path(_ArchiveMembers(archive))


def _checked_opf_path(full_path: str) -> str:
    """
    Normalize a declared rootfile path and refuse one that leaves the archive.

    :param full_path: The ``full-path`` attribute from ``container.xml``.

    :return: The normalized archive path.

    :raises ValidationError: If the path points outside the archive.
    """
    normalized = posixpath.normpath(full_path)
    if escapes_archive(normalized):
        raise ValidationError(
            f"{CONTAINER_PATH} names a rootfile outside the package: {full_path}"
        )
    return normalized


def _resolve(base: str, href: str) -> str:
    """
    Resolve a manifest href against the package document's directory.

    The result is normalized, because a package document in ``OEBPS/`` may
    legitimately reach a sibling directory with ``../``. Leaving those segments
    in place produces a path no archive member ever matches, which would make
    ``--validate`` reject a perfectly good book.

    :param base: Archive path of the package document.
    :param href: The href to resolve.

    :return: The archive path the href points at.
    """
    target, _ = urldefrag(href)
    target = unquote(target)
    if not target:
        return target
    directory = posixpath.dirname(base)
    if not directory:
        return posixpath.normpath(target)
    return posixpath.normpath(posixpath.join(directory, target))


def read_package(archive: ZipFile) -> Package:
    """
    Parse the package document of an epub archive.

    :param archive: The open archive.

    :return: The package metadata, manifest and spine.

    :raises ValidationError: If the package document is missing or unparsable.
    """
    return _package(_ArchiveMembers(archive))


def _package(members: _Members) -> Package:
    """
    Find and parse a book's package document, whatever shape the book is in.

    :param members: The book being read.

    :return: The package metadata, manifest and spine.

    :raises ValidationError: If the package document is missing or unparsable.
    """
    opf_path = _opf_path(members)
    return _package_from_root(_element(members, opf_path), opf_path)


def _canonical_identifier(root: ElementTree.Element) -> str | None:
    """
    Return the identifier the package document declares as its own.

    A book may carry several ``dc:identifier`` elements -- a retail ASIN, an
    ISBN, a converter's UUID -- and the spec names the canonical one through
    the ``unique-identifier`` IDREF on ``<package>``. Publishers commonly list
    the retail id first, so taking document order returned the wrong value for
    798 of 2,805 books in a real library.

    Falling back to the first is still needed. In the same library 17 books
    point the attribute at an id that is not in the document and 2 omit the
    attribute, and an identifier of some sort beats none.

    :param root: The parsed OPF root element.

    :return: The canonical identifier, or None if the book declares none.
    """
    found = [
        element
        for element in root.iter(f"{{{DC_NS}}}identifier")
        if element.text and element.text.strip()
    ]
    if not found:
        return None
    named = root.get("unique-identifier")
    if named:
        for element in found:
            if element.get("id") == named:
                return (element.text or "").strip()
    return (found[0].text or "").strip()


def _sort_name(root: ElementTree.Element, creator: ElementTree.Element) -> str | None:
    """
    Find the inverted form of a creator's name, in either EPUB dialect.

    EPUB2 put it in an ``opf:file-as`` attribute on ``dc:creator``. EPUB3
    deprecated that and moved it to a ``<meta refines="#id"
    property="file-as">`` element. Apple's library is overwhelmingly EPUB3, so
    reading only the attribute left 301 of 2,793 books with a creator named in
    display order -- "Yuval Noah Harari - Sapiens" -- under a policy whose only
    purpose is sorting by author.

    The attribute wins when a book somehow carries both, which keeps books that
    already had a sort name naming exactly as they did. No book in the surveyed
    library has both.

    The *first* refining element wins. One publisher gave the author and the
    illustrator the same ``id``, so ``#creator`` carries two sort names and
    taking the last produced the illustrator's.

    :param root: The parsed OPF root element.
    :param creator: The ``dc:creator`` element being described.

    :return: The sort name, or None if the book supplies none.
    """
    attribute = creator.get(f"{{{OPF_NS}}}file-as")
    if attribute and attribute.strip():
        return attribute.strip()

    creator_id = creator.get("id")
    if not creator_id:
        return None

    target = f"#{creator_id}"
    for meta in root.iter(f"{{{OPF_NS}}}meta"):
        if meta.get("refines") != target or meta.get("property") != "file-as":
            continue
        if meta.text and meta.text.strip():
            return meta.text.strip()
    return None


def _package_from_root(root: ElementTree.Element, opf_path: str) -> Package:
    """
    Build a :class:`Package` from a parsed package document.

    :param root: The parsed OPF root element.
    :param opf_path: Path of the package document, used to resolve hrefs.

    :return: The package metadata, manifest and spine.
    """
    package = Package(opf_path=opf_path)

    title = root.find(".//{http://purl.org/dc/elements/1.1/}title")
    if title is not None and title.text:
        package.title = title.text.strip()

    creator = root.find(f".//{{{DC_NS}}}creator")
    if creator is not None and creator.text:
        package.creator = creator.text.strip()
        # Publishers usually supply an inverted form; it beats guessing.
        package.creator_sort = _sort_name(root, creator)

    package.identifier = _canonical_identifier(root)

    for item in root.iter(f"{{{OPF_NS}}}item"):
        item_id = item.get("id")
        href = item.get("href")
        # A remote resource is not expected in the archive, so recording it
        # would only produce a false "manifest item is not in the archive".
        if item_id and href and not is_remote(href):
            # A fragment-only href ("#toc") resolves to nothing. Recording it
            # made --validate report a sound book as missing a member, so the
            # archive was never written and the book was retried for ever.
            resolved = _resolve(opf_path, href)
            if resolved:
                package.manifest[item_id] = resolved
        if item_id and "cover-image" in (item.get("properties") or ""):
            package.cover_id = item_id

    for itemref in root.iter(f"{{{OPF_NS}}}itemref"):
        idref = itemref.get("idref")
        if idref:
            package.spine.append(idref)

    if package.cover_id is None:
        for meta in root.iter(f"{{{OPF_NS}}}meta"):
            if meta.get("name") == "cover":
                package.cover_id = meta.get("content")
                break

    return package


def read_package_dir(package: Path) -> Package:
    """
    Parse the package document of an unpacked ``*.epub/`` directory.

    Reading from the source directory avoids re-opening and re-inflating an
    archive that was just written, which on a cloud-backed library doubles the
    read work per book for no benefit.

    :param package: The package directory.

    :return: The package metadata, manifest and spine.

    :raises ValidationError: If the container or package document is missing
        or unparsable.
    """
    return _package(_DirectoryMembers(package))


def validate_archive(path: Path) -> list[str]:
    """
    Check one exported archive and describe anything wrong with it.

    :param path: The epub file to check.

    :return: A list of problems; empty means the archive is sound.
    """
    problems: list[str] = []

    try:
        with ZipFile(path) as archive:
            names = archive.namelist()
            members = set(names)
            problems.extend(_check_mimetype(archive, names))

            broken = archive.testzip()
            if broken is not None:
                problems.append(f"corrupt member: {broken}")

            try:
                package = read_package(archive)
            except ValidationError as exc:
                problems.append(str(exc))
                return problems

            problems.extend(_check_manifest(members, package))
    except BadZipFile as exc:
        return [f"not a readable zip archive: {exc}"]
    except OSError as exc:
        return [f"could not open: {exc}"]
    except (EOFError, NotImplementedError, RuntimeError, ValueError) as exc:
        # zipfile raises NotImplementedError for a compression method it does
        # not implement, RuntimeError for an encrypted member, and EOFError
        # when a member holds less data than the directory declares. --verify is
        # the one command whose job is finding damage, so it must report a
        # hostile archive rather than die on it and check nothing further.
        return [f"unreadable archive: {exc}"]

    return problems


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
    if names[0] != MIMETYPE_NAME:
        problems.append(f"first member is {names[0]!r}, not 'mimetype'")
        if MIMETYPE_NAME not in names:
            return problems

    info = archive.getinfo(MIMETYPE_NAME)
    if info.compress_type != ZIP_STORED:
        problems.append("mimetype is compressed; it must be stored")
    # The specification fixes this member's length exactly, so a declared size
    # that differs settles it without reading anything. --verify runs over
    # files this tool may not have written, and a member declaring 512 MiB was
    # otherwise materialised in full to be compared against 20 bytes.
    if info.file_size != len(MIMETYPE_CONTENT) or (
        archive.read(MIMETYPE_NAME) != MIMETYPE_CONTENT
    ):
        problems.append("mimetype does not contain 'application/epub+zip'")

    return problems


def _check_manifest(members: set[str], package: Package) -> list[str]:
    """
    Check that everything the package document promises is present.

    :param members: The archive's member names, built once by the caller.
    :param package: The parsed package document.

    :return: A list of problems.
    """
    problems: list[str] = []

    if not package.manifest:
        problems.append(f"{package.opf_path} declares no manifest items")

    missing = sorted(
        f"{item_id} -> {href}"
        for item_id, href in package.manifest.items()
        if href not in members
    )
    for entry in missing[:5]:
        problems.append(f"manifest item is not in the archive: {entry}")
    if len(missing) > 5:
        problems.append(f"...and {len(missing) - 5} more missing manifest item(s)")

    dangling = sorted(set(package.spine) - set(package.manifest))
    for idref in dangling[:5]:
        problems.append(f"spine references unknown manifest id: {idref}")
    if len(dangling) > 5:
        problems.append(f"...and {len(dangling) - 5} more dangling spine id(s)")

    if not package.spine:
        problems.append(f"{package.opf_path} declares no spine")

    return problems


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

    :return: A list of problems; empty means epubcheck was happy.
    """
    executable = shutil.which(EPUBCHECK)
    if executable is None:
        return ["epubcheck is not on PATH"]

    try:
        completed = subprocess.run(  # noqa: S603 - fixed executable, no shell
            [executable, str(path)],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return [f"epubcheck could not be run: {exc}"]

    if completed.returncode == 0:
        return []

    output = (completed.stderr or completed.stdout).strip().splitlines()
    errors = [line.strip() for line in output if "ERROR" in line]
    logger.debug("epubcheck exited %d for %s", completed.returncode, path.name)
    return errors[:10] or [f"epubcheck failed with exit code {completed.returncode}"]
