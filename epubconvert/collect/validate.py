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
import re
import shutil
import subprocess
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import unquote, urldefrag
from xml.etree import ElementTree
from xml.parsers import expat
from zipfile import ZIP_STORED, BadZipFile, ZipFile

from ..utils.app_logger import logger
from ..utils.contained import escapes as escapes_archive
from ..utils.contained import is_remote, open_contained, resolve
from ..utils.display import printable
from ..utils.opf import Package
from ..utils.spec import CONTAINER_PATH, MIMETYPE_CONTENT, MIMETYPE_NAME

# CPython builds lzma only where liblzma is present, and zipfile imports it
# only when a member needs it. Without it no member can raise LZMAError, so
# there is nothing to catch -- but importing it unconditionally would stop
# this module, and every command, from loading at all.
try:
    from lzma import LZMAError
except ImportError:
    _LZMA_ERRORS: tuple[type[Exception], ...] = ()
else:
    _LZMA_ERRORS = (LZMAError,)

CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"
OPF_NS = "http://www.idpf.org/2007/opf"
DC_NS = "http://purl.org/dc/elements/1.1/"

#: Cap on the *stored* size of XML read from an archive. This bounds the read,
#: not the parse: ElementTree expands internal entities, so a small document
#: can still expand to something far larger, and this limit does not prevent
#: that. It exists to reject implausible files early, not as a memory bound.
MAX_XML_BYTES = 16 * 1024 * 1024

#: What reading a damaged member can raise. A bad CRC is a BadZipFile, but a
#: damaged compressed stream raises out of its decompressor instead: zlib.error
#: for deflate and lzma.LZMAError for LZMA, neither of them a BadZipFile or an
#: OSError (bzip2's is an OSError). zipfile also raises NotImplementedError for
#: a method it does not implement, RuntimeError for an encrypted member, and
#: EOFError when a member holds less data than the directory declares. Every
#: caller that must survive a damaged archive catches this one set, so the next
#: such error is added in one place (#21).
UNREADABLE_MEMBER: tuple[type[Exception], ...] = (
    BadZipFile,
    OSError,
    EOFError,
    NotImplementedError,
    RuntimeError,
    ValueError,
    zlib.error,
    *_LZMA_ERRORS,
)

EPUBCHECK = "epubcheck"


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


#: A UUID, whatever case it is written in.
_UUID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

#: The prefixes publishers put in front of the value itself. ``urn:`` scheme
#: names are case-insensitive by RFC 8141, and this library holds both
#: ``urn:isbn:`` and ``URN:ISBN:``.
_IDENTIFIER_PREFIX = re.compile(r"(?i)^(urn:)?(isbn|uuid|ean)[:\s]+")


def canonical_identifier(value: str) -> str:
    """
    Render an identifier the one way its book would always have written it.

    An identifier is a matching key, and it is only a key if the same book
    yields the same string. It does not: a surveyed 2,805-book library writes
    one ISBN six ways -- bare, ``urn:isbn:``, ``URN:ISBN:``, hyphenated,
    ``ISBN 978...``, and ``urn:ean:`` -- across 1,092 books, and one UUID two
    ways across 1,448. Two copies of the same book catalogued by different
    tools therefore did not match each other.

    Only what can be *verified* is normalised. An ISBN is recognised by its
    check digit, never by counting digits: 68 identifiers in that library are
    10 or 13 digits and fail it, and a digit count would have relabelled every
    one of them. An ISBN-10 becomes the ISBN-13 meaning the same book, which is
    exact arithmetic rather than a guess. Anything unrecognised is returned
    exactly as it came in, because the specification says this field is opaque
    and reshaping an opaque string is a claim about it.

    :param value: The identifier the package document declares.

    :return: The canonical form, or *value* unchanged when it is neither a
        valid ISBN nor a UUID.
    """
    body = _IDENTIFIER_PREFIX.sub("", value.strip())
    if _UUID.fullmatch(body):
        return f"urn:uuid:{body.lower()}"

    digits = re.sub(r"[-\s]", "", body)
    if _is_isbn13(digits):
        return f"urn:isbn:{digits}"
    if _is_isbn10(digits):
        return f"urn:isbn:{_as_isbn13(digits)}"
    return value.strip()


#: The prefix a canonical ISBN carries.
ISBN_URN = "urn:isbn:"


def isbn13_of(identifier: object) -> str | None:
    """
    Take the bare ISBN out of a canonical identifier, when it is one.

    Judged by check digit, never by prefix. :func:`canonical_identifier`
    leaves an identifier it cannot verify exactly as declared, so ``urn:isbn:``
    in front of something is not evidence that an ISBN follows: 68 identifiers
    in a surveyed library are ten or thirteen digits and fail their check, and
    a tracker handed one of those matches the wrong book or none. Derived by
    stripping the prefix rather than looked up again, so it cannot disagree
    with the field it came from. About 41% of books have one: 1,108
    ``urn:isbn`` against 1,448 ``urn:uuid`` in that library.

    :param identifier: The canonical identifier, or None.

    :return: The thirteen digits, or None when the book is identified some
        other way or its ISBN does not verify.
    """
    if not isinstance(identifier, str) or not identifier.startswith(ISBN_URN):
        return None
    digits = identifier[len(ISBN_URN) :]
    return digits if _is_isbn13(digits) else None


def _ascii_digits(text: str) -> bool:
    """
    Whether *text* is ASCII digits, and only those.

    ``str.isdigit`` is not that test. It accepts a superscript two, which
    ``int`` then refuses, so an identifier of superscript digits stopped the
    run with a ValueError; and it accepts Arabic-Indic digits, which ``int``
    reads, so such an identifier was written back as an ISBN in those digits.
    """
    return text.isascii() and text.isdigit()


def _isbn13_sum(digits: str) -> int:
    """ISO 2108's ISBN-13 weighting: the digits weigh 1 and 3 alternately."""
    return sum((1 if i % 2 == 0 else 3) * int(c) for i, c in enumerate(digits))


def _isbn10_sum(body: str) -> int:
    """ISO 2108's ISBN-10 weighting of the nine body digits: 10 down to 2."""
    return sum((10 - i) * int(c) for i, c in enumerate(body))


def _is_isbn13(digits: str) -> bool:
    """Whether *digits* is thirteen digits carrying a valid check digit."""
    if len(digits) != 13 or not _ascii_digits(digits):
        return False
    return _isbn13_sum(digits) % 10 == 0


def _is_isbn10(digits: str) -> bool:
    """Whether *digits* is ten characters carrying a valid check digit."""
    if len(digits) != 10 or not _ascii_digits(digits[:9]):
        return False
    if not (_ascii_digits(digits[9]) or digits[9] in "Xx"):
        return False
    check = 10 if digits[9] in "Xx" else int(digits[9])
    return (_isbn10_sum(digits[:9]) + check) % 11 == 0


def isbn10_of(isbn13: str | None) -> str | None:
    """
    Derive the ISBN-10 that means the same book, when one exists.

    The inverse of :func:`_as_isbn13`, kept beside it so the two weightings
    cannot drift apart. Only a ``978`` ISBN-13 has one; ``979`` books were
    never given an ISBN-10. Written because Goodreads exports both columns and
    an importer may match on either.

    :param isbn13: Thirteen digits, already verified, or None.

    :return: The ten characters, or None, including for anything that is not
        thirteen ASCII digits: a prefix test alone made ``"978"`` into ``"0"``
        and raised ValueError on ``"978abc..."``.
    """
    if (
        isbn13 is None
        or len(isbn13) != 13
        or not _ascii_digits(isbn13)
        or not isbn13.startswith("978")
    ):
        return None
    body = isbn13[3:12]
    check = (11 - _isbn10_sum(body) % 11) % 11
    return body + ("X" if check == 10 else str(check))


def _as_isbn13(digits: str) -> str:
    """
    Convert a valid ISBN-10 to the ISBN-13 naming the same book.

    :param digits: Ten characters, already checked.

    :return: Thirteen digits.
    """
    body = f"978{digits[:9]}"
    return f"{body}{(10 - _isbn13_sum(body) % 10) % 10}"


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
        except UNREADABLE_MEMBER as exc:
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

    Asked of the parser rather than worked out by reading the bytes. Two
    hand-written scans of the ``DOCTYPE`` declaration were defeated in turn --
    first by a ``SYSTEM`` identifier containing ``>``, then by a comment holding
    a decoy ``<!DOCTYPE`` -- because each had to re-derive where the declaration
    starts and ends. expat already knows, so it is asked.

    A malformed document is left alone here and refused by the parse that
    follows, which reports it better.

    :param data: The raw bytes of the document.

    :return: True if the document declares any entity.
    """
    parser = expat.ParserCreate()
    declared: list[int] = []
    parser.EntityDeclHandler = lambda *_args: declared.append(1)
    try:
        parser.Parse(data, True)
    except expat.ExpatError:
        return False
    return bool(declared)


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
        # A list of values separated by XML white space, so a whole value: the
        # substring test took "not-cover-image" and "x:cover-image" for covers.
        # Not str.split(), which also splits on a no-break space and the rest
        # of Unicode's white space, inside what XML reads as one value.
        properties = _XML_WHITESPACE.split(item.get("properties") or "")
        if item_id and "cover-image" in properties:
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
    except UNREADABLE_MEMBER as exc:
        # Everything else a damaged member raises: BadZipFile and OSError are
        # caught above, each with its own wording. --verify is the one command
        # whose job is finding damage, so it must report a hostile archive
        # rather than die on it and check nothing further.
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


#: A line in which epubcheck reports a problem that fails a book.
_EPUBCHECK_FAILURE = re.compile(r"\b(?:ERROR|FATAL)\b")

#: What separates the values of an XML list attribute (XML 1.0, production 3).
_XML_WHITESPACE = re.compile(r"[ \t\r\n]+")


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

    try:
        completed = subprocess.run(  # noqa: S603 - fixed executable, no shell
            [executable, str(path)],
            capture_output=True,
            text=True,
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
            printable(path.name),
            exc,
        )
        return []

    if completed.returncode == 0:
        return []

    # Both streams: a JVM writes notices such as "Picked up JAVA_TOOL_OPTIONS"
    # to stderr, which hid every diagnostic on stdout when only one was read.
    # FATAL is epubcheck's most severe level, and was dropped with the rest.
    output = f"{completed.stderr}\n{completed.stdout}".splitlines()
    errors = [line.strip() for line in output if _EPUBCHECK_FAILURE.search(line)]
    logger.debug("epubcheck exited %d for %s", completed.returncode, path.name)
    return errors[:10] or [f"epubcheck failed with exit code {completed.returncode}"]
