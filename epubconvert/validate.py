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
from urllib.parse import unquote, urldefrag
from xml.etree import ElementTree
from zipfile import ZIP_STORED, BadZipFile, ZipFile

from .app_logger import logger
from .spec import CONTAINER_PATH, MIMETYPE_CONTENT, MIMETYPE_NAME

CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"
OPF_NS = "http://www.idpf.org/2007/opf"

#: Cap on the *stored* size of XML read from an archive. This bounds the read,
#: not the parse: ElementTree expands internal entities, so a small document
#: can still expand to something far larger, and this limit does not prevent
#: that. It exists to reject implausible files early, not as a memory bound.
MAX_XML_BYTES = 16 * 1024 * 1024

EPUBCHECK = "epubcheck"

#: Href prefixes that name a resource outside the archive. The epub
#: specification allows remote resources in the manifest, and resolving one as
#: an archive path would report a perfectly good book as missing a file.
REMOTE_PREFIXES = ("http://", "https://", "ftp://", "ftps://", "data:", "mailto:")


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


def _read_xml(archive: ZipFile, name: str) -> ElementTree.Element:
    """
    Read and parse one XML member of an archive.

    :param archive: The open archive.
    :param name: Path of the member to read.

    :return: The parsed root element.

    :raises ValidationError: If the member is missing or not valid XML.
    """
    try:
        info = archive.getinfo(name)
    except KeyError as exc:
        raise ValidationError(f"missing {name}") from exc

    if info.file_size > MAX_XML_BYTES:
        raise ValidationError(f"{name} is implausibly large ({info.file_size} bytes)")

    try:
        data = archive.read(name)
    except (BadZipFile, OSError) as exc:
        raise ValidationError(f"could not read {name}: {exc}") from exc

    try:
        return ElementTree.fromstring(data)
    except ElementTree.ParseError as exc:
        raise ValidationError(f"{name} is not valid XML: {exc}") from exc


def find_opf_path(archive: ZipFile) -> str:
    """
    Resolve the package document path from ``META-INF/container.xml``.

    :param archive: The open archive.

    :return: Archive path of the package document.

    :raises ValidationError: If the container is missing, names no rootfile, or
        names one outside the archive.
    """
    root = _read_xml(archive, CONTAINER_PATH)
    for rootfile in root.iter(f"{{{CONTAINER_NS}}}rootfile"):
        full_path = rootfile.get("full-path")
        if full_path:
            return _checked_opf_path(full_path)
    raise ValidationError(f"{CONTAINER_PATH} names no rootfile")


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


def is_remote(href: str) -> bool:
    """
    Report whether an href is a URL rather than an archive member.

    A remote resource is legitimate: the epub specification allows one in the
    manifest. It simply is not expected to be inside the archive, so it must
    not be looked for there.

    :param href: The manifest href to test.

    :return: True if the href is a remote or inline URL.
    """
    return href.lower().startswith(REMOTE_PREFIXES)


def escapes_archive(path: str) -> bool:
    """
    Report whether a path resolves above the archive root.

    :func:`_resolve` normalizes ``..`` segments, but an href carrying more of
    them than the package document has parent directories resolves to a path
    above the archive root, and an absolute path never was inside it. No
    archive member can match either. What makes them worth rejecting rather
    than ignoring is the source side: the cover extractor and the package
    reader join these onto the ``*.epub/`` directory on disk, where they reach
    real files that are no part of the book.

    :param path: An archive path, already normalized.

    :return: True if the path leaves the archive.
    """
    return path.startswith("/") or path == ".." or path.startswith("../")


def contained_file(package: Path, href: str, root: Path | None = None) -> Path | None:
    """
    Resolve a manifest href to a real file inside a package, or refuse it.

    Two checks, because neither covers the other: :func:`escapes_archive`
    rejects the ``../`` and absolute paths :func:`_resolve` preserves, and the
    resolved comparison catches a symlink that sits inside the package but
    leads out of it.

    This lives beside :func:`escapes_archive` rather than beside its caller so
    that the whole containment rule is in one place. Any future reader that
    turns a manifest entry into a path on disk -- fonts, thumbnails, metadata
    sidecars -- should come through here rather than reinvent half of it.

    :param package: The ``*.epub/`` package directory.
    :param href: An archive path from the manifest.

    :return: The file to read, or None if there is nothing safe to read.
    """
    if escapes_archive(href):
        logger.debug("%r escapes the package %s", href, package.name)
        return None

    # The caller may already hold the resolved root; it does not change across
    # a package, and resolving it walks every path component each time.
    resolved_root = root if root is not None else package.resolve()
    source = package / href
    try:
        inside = source.resolve().is_relative_to(resolved_root)
    except (OSError, ValueError):
        # A NUL byte in the href, or a resolve on a broken mount. The
        # documented contract is "None if there is nothing safe to read", and
        # this function invites callers who will not expect a raise.
        logger.debug("%r could not be resolved inside %s", href, package.name)
        return None
    if not inside:
        logger.debug("%r resolves outside the package %s", href, package.name)
        return None

    return source if source.is_file() else None


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
    Parse the package document of an archive.

    :param archive: The open archive.

    :return: The package metadata, manifest and spine.

    :raises ValidationError: If the package document is missing or unparsable.
    """
    opf_path = find_opf_path(archive)
    return _package_from_root(_read_xml(archive, opf_path), opf_path)


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

    creator = root.find(".//{http://purl.org/dc/elements/1.1/}creator")
    if creator is not None and creator.text:
        package.creator = creator.text.strip()
        # Publishers usually supply an inverted form here; it beats guessing.
        package.creator_sort = creator.get(f"{{{OPF_NS}}}file-as")

    identifier = root.find(".//{http://purl.org/dc/elements/1.1/}identifier")
    if identifier is not None and identifier.text:
        package.identifier = identifier.text.strip()

    for item in root.iter(f"{{{OPF_NS}}}item"):
        item_id = item.get("id")
        href = item.get("href")
        # A remote resource is not expected in the archive, so recording it
        # would only produce a false "manifest item is not in the archive".
        if item_id and href and not is_remote(href):
            package.manifest[item_id] = _resolve(opf_path, href)
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
    container = package / CONTAINER_PATH
    try:
        root = ElementTree.fromstring(_read_capped(container))
    except OSError as exc:
        raise ValidationError(f"missing {CONTAINER_PATH}") from exc
    except ElementTree.ParseError as exc:
        raise ValidationError(f"{CONTAINER_PATH} is not valid XML: {exc}") from exc

    opf_path = ""
    for rootfile in root.iter(f"{{{CONTAINER_NS}}}rootfile"):
        opf_path = rootfile.get("full-path") or ""
        if opf_path:
            break
    if not opf_path:
        raise ValidationError(f"{CONTAINER_PATH} names no rootfile")
    # Unlike the archive reader, this one joins the result onto a real
    # directory, so a rootfile of "/etc/passwd" or "../../.." would be opened
    # rather than merely missed.
    opf_path = _checked_opf_path(opf_path)

    try:
        opf_root = ElementTree.fromstring(_read_capped(package / opf_path))
    except OSError as exc:
        raise ValidationError(f"missing {opf_path}") from exc
    except ElementTree.ParseError as exc:
        raise ValidationError(f"{opf_path} is not valid XML: {exc}") from exc

    return _package_from_root(opf_root, opf_path)


def _read_capped(path: Path) -> bytes:
    """
    Read an XML document off disk, refusing an implausible one.

    The archive reader has enforced :data:`MAX_XML_BYTES` since it was written;
    this side had no cap at all, and it is the untrusted one -- these bytes come
    straight out of the ``*.epub/`` directory. Measured, a 105 MB container
    document parsed happily at 338 MB resident.

    :param path: The document to read.

    :return: Its bytes.

    :raises ValidationError: If it is missing or implausibly large.
    """
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ValidationError(f"missing {path.name}") from exc
    if size > MAX_XML_BYTES:
        raise ValidationError(f"{path.name} is implausibly large ({size} bytes)")
    return path.read_bytes()


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
    except (NotImplementedError, RuntimeError, ValueError) as exc:
        # zipfile raises NotImplementedError for a compression method it does
        # not implement and RuntimeError for an encrypted member. --verify is
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
