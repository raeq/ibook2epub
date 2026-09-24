"""
Reading a book's package document, whichever shape the book is stored in.

Resolves the package document from ``META-INF/container.xml`` and pulls the
manifest, spine and metadata out of it, from an ``*.epub`` archive or from the
unpacked ``*.epub/`` directory Apple keeps. Everything read here came from the
book, so every read is bounded and every name is checked before it is used.
"""

from __future__ import annotations

import os
import posixpath
import re
import stat
import zlib
from pathlib import Path
from typing import IO, Protocol
from urllib.parse import unquote
from xml.etree import ElementTree
from xml.parsers import expat
from zipfile import (
    ZIP_BZIP2,
    ZIP_DEFLATED,
    ZIP_LZMA,
    ZIP_STORED,
    BadZipFile,
    ZipFile,
    ZipInfo,
)

from ..utils.contained import escapes as escapes_archive
from ..utils.contained import is_remote, open_contained, resolve
from ..utils.display import printable
from ..utils.opf import Package
from ..utils.spec import CONTAINER_PATH

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

#: The compression methods OCF allows an epub's members: stored and deflate.
#: zipfile reads bzip2 and LZMA too, and inflates a whole compressed chunk of
#: either in one call however little is asked for, so no read of one can be
#: bounded. A book never needs them, so a member using one is refused unread.
OCF_METHODS = frozenset({ZIP_STORED, ZIP_DEFLATED})

#: What a refusal calls the methods zipfile reads and OCF does not allow.
_METHOD_NAMES = {ZIP_BZIP2: "bzip2", ZIP_LZMA: "LZMA"}


class ValidationError(Exception):
    """Raised when an archive cannot be validated at all."""


def disallowed_method(info: ZipInfo) -> str | None:
    """
    Name a member's compression method, if it is one OCF does not allow.

    :param info: The member, as the archive's directory describes it.

    :return: The method's name, or None when OCF allows it.
    """
    if info.compress_type in OCF_METHODS:
        return None
    return _METHOD_NAMES.get(
        info.compress_type, f"compression method {info.compress_type}"
    )


def open_member(archive: ZipFile, info: ZipInfo) -> IO[bytes]:
    """
    Open a member of an untrusted archive for streaming, if it can be bounded.

    Streaming in chunks bounds a stored or deflated member, because zipfile
    asks deflate for no more than each chunk. It bounds nothing for bzip2 or
    LZMA, whose decompressors expand a whole compressed chunk in one call.

    :param archive: The open archive.
    :param info: The member to open.

    :return: A binary stream of the member's contents.

    :raises NotImplementedError: If the member uses a method OCF does not
        allow: one of :data:`UNREADABLE_MEMBER`, as zipfile's own refusal of a
        method it does not implement is, so every caller already catches it.
    """
    method = disallowed_method(info)
    if method is not None:
        raise NotImplementedError(
            f"{printable(info.filename)} is compressed with {method}, "
            "which an epub may not use"
        )
    return archive.open(info)


#: What :func:`repeated_entries` says of directory entries sharing a header.
SHARED_HEADER = "members share a local header (possible zip bomb)"


def repeated_entries(archive: ZipFile) -> str | None:
    """
    Report whether the central directory lists any member more than once.

    A directory may list one local header any number of times, and a name
    more than once. zipfile 3.13 and later merely warn about a shared header,
    and every reader that opens entries in turn -- by name or by entry --
    inflates the same member once per listing: a 4 MiB book listing one
    member 200 times was refreshed into 800 MiB. So an archive that repeats
    either is read no further, whichever zipfile is reading it.

    :param archive: The open archive.

    :return: :data:`SHARED_HEADER` if two entries share a local header, a
        description if two share a name, or None if neither does.
    """
    entries = archive.infolist()
    if len({info.header_offset for info in entries}) < len(entries):
        return SHARED_HEADER
    if len({info.filename for info in entries}) < len(entries):
        return "member names appear more than once"
    return None


def read_member(archive: ZipFile, info: ZipInfo, limit: int) -> bytes | None:
    """
    Decompress a member of an untrusted archive, but never more than *limit*.

    ``info.file_size`` is what the central directory declares, not a bound.
    ``ZipFile.read`` decompresses the whole stream before the two are ever
    compared -- deflate up to 1 GiB a call, bzip2 and LZMA without limit -- so
    a 2.4 KB archive declaring a 180-byte member made it allocate 1.9 GiB.

    :param archive: The open archive.
    :param info: The member to read.
    :param limit: The most it may hold.

    :return: Its bytes, or None if it holds more than *limit*.

    :raises NotImplementedError: If the member uses a method OCF does not
        allow.
    """
    with open_member(archive, info) as handle:
        data = handle.read(limit + 1)
    return None if len(data) > limit else data


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

        The size the central directory declares is checked before anything is
        inflated, and the read is bounded as well, because that declaration
        is the book's own claim about itself.
        """
        try:
            info = self.archive.getinfo(name)
        except KeyError as exc:
            raise ValidationError(f"missing {name}") from exc

        method = disallowed_method(info)
        if method is not None:
            raise ValidationError(
                f"{name} is compressed with {method}, which an epub may not use"
            )
        if info.file_size > MAX_XML_BYTES:
            raise ValidationError(
                f"{name} is implausibly large ({info.file_size} bytes)"
            )
        try:
            data = read_member(self.archive, info, MAX_XML_BYTES)
        except UNREADABLE_MEMBER as exc:
            raise ValidationError(f"could not read {name}: {exc}") from exc
        if data is None:
            raise ValidationError(f"{name} is larger than it declares")
        return data


class _DirectoryMembers:  # pylint: disable=too-few-public-methods
    """Members of an unpacked package directory."""

    def __init__(self, root: Path) -> None:
        self.root = root
        # Resolved once: resolving walks every component, and it does not
        # change across a package.
        try:
            self.resolved_root = root.resolve()
        except (RuntimeError, ValueError) as exc:
            # Python 3.10 to 3.12 raise RuntimeError, not OSError, for a
            # symlink loop, and a NUL in the path -- Apple's untyped ZPATH can
            # hold one -- raises ValueError. Every reader of a package catches
            # ValidationError, so either took the whole library export,
            # annotation export or naming pass down with this one package.
            # Translated here, at the one place a package directory is opened.
            raise ValidationError(f"{printable(root.name)} cannot be resolved") from exc

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
            info = path.stat()
        except OSError as exc:
            raise ValidationError(f"missing {name}") from exc
        # A FIFO here passed every check and stats at size 0; opening it then
        # waited for a writer for ever. open_contained refuses one on the
        # descriptor too, which is what holds if the name is swapped after
        # this check.
        if not stat.S_ISREG(info.st_mode):
            raise ValidationError(f"could not read {name}: not a regular file")
        if info.st_size > MAX_XML_BYTES:
            raise ValidationError(f"{name} is implausibly large ({info.st_size} bytes)")
        try:
            with open_contained(path) as handle:
                # Bounded, because the size above was measured before the open
                # and a file can grow in between.
                data = handle.read(MAX_XML_BYTES + 1)
        except OSError as exc:
            raise ValidationError(f"could not read {name}: {exc}") from exc
        if len(data) > MAX_XML_BYTES:
            raise ValidationError(f"{name} is implausibly large (grew while read)")
        return data


def _element(members: _Members, name: str) -> ElementTree.Element:
    """
    Read a member and parse it as XML.

    :param members: The book being read.
    :param name: Path of the member within the book.

    :return: The parsed root element.

    :raises ValidationError: If the member is missing or not valid XML.
    """
    data = members.read(name)
    try:
        return parse_xml(data)
    except EntityDeclarationError as exc:
        raise ValidationError(f"{name} {exc}") from exc
    except ElementTree.ParseError as exc:
        raise ValidationError(f"{name} is not valid XML: {exc}") from exc


class EntityDeclarationError(ElementTree.ParseError):
    """Raised for a document from a book that declares an XML entity."""


def parse_xml(data: bytes) -> ElementTree.Element:
    """
    Parse a document from a book, however it is refused.

    A malformed document raises ParseError, but expat refuses a declared
    multi-byte encoding -- Shift_JIS, EUC-JP, UTF-32 -- with ValueError, and
    Python an encoding it does not know with LookupError. Callers caught
    ParseError alone, so one Japanese book's ``encoding="Shift_JIS"`` ended the
    whole run. Every reader of a book's XML parses through here, so there is
    one exception to catch.

    A document that declares an entity is refused here too, for the same
    reason: the rule was applied beside only two of the readers, and the
    encryption declaration, parsed by the third, went without it.

    :param data: The raw bytes of the document.

    :return: The root element.

    :raises ElementTree.ParseError: If the document cannot be parsed, for
        whatever reason; :class:`EntityDeclarationError`, one of them, if it
        declares an entity.
    """
    if _declares_entities(data):
        raise EntityDeclarationError("declares XML entities, which are not allowed")
    try:
        return ElementTree.fromstring(data)
    except (ValueError, LookupError) as exc:
        raise ElementTree.ParseError(f"unreadable encoding: {exc}") from exc


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

    The parse stops at the first declaration. Parsed to the end, it expanded
    every reference it met before the answer was given, so the one check
    meant to spare the tool an expansion performed it, and a billion-laughs
    document was stopped only by expat's own limit where it has one.

    A malformed document is left alone here and refused by the parse that
    follows, which reports it better.

    :param data: The raw bytes of the document.

    :return: True if the document declares any entity.
    """
    parser = expat.ParserCreate()
    parser.EntityDeclHandler = _stop_at_declaration
    try:
        parser.Parse(data, True)
    except _EntityDeclaredError:
        return True
    except (expat.ExpatError, ValueError, LookupError):
        # An encoding expat refuses is malformed for this purpose too, and
        # raised LookupError from here, ahead of the parse that reports it.
        return False
    return False


class _EntityDeclaredError(Exception):
    """Raised out of expat to stop a parse at an entity declaration."""


def _stop_at_declaration(*_args: object) -> None:
    """
    Stop the parse: an entity has been declared, and nothing more is needed.

    :raises _EntityDeclaredError: Always. expat abandons the parse and ``Parse``
        raises it, before any content, and so any reference, is reached.
    """
    raise _EntityDeclaredError


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

    A query is not part of the path either. Left on, ``ch1.xhtml?x=1`` was
    looked for as a member of that name and reported missing. It is split off
    before percent-decoding, so a member whose name holds a ``?`` -- written
    ``%3F`` in the href -- is still found.

    :param base: Archive path of the package document.
    :param href: The href to resolve.

    :return: The archive path the href points at.
    """
    # By hand: urldefrag parses the URL, and raised ValueError for " //[x".
    target = unquote(href.partition("#")[0].partition("?")[0])
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


def read_archive_package(path: Path) -> Package:
    """
    Open an ``*.epub`` archive and parse its package document.

    The one way to read an already-zipped book's metadata, so opening the
    archive fails the same way reading it does. zipfile raises more than
    BadZipFile from its constructor: UnicodeDecodeError for a name flagged
    UTF-8 that is not, NotImplementedError for a zip version it does not
    implement. Three readers caught ValidationError, BadZipFile and OSError
    each, so either took down a ``--name-by author-title`` run.

    :param path: The archive.

    :return: The package metadata, manifest and spine.

    :raises ValidationError: If the archive cannot be opened or read, or its
        package document is missing or unparsable -- an OSError included, as
        every caller treats a file it cannot open as one that cannot describe
        itself. So is anything but a regular file.
    """
    try:
        # zipfile leaves a stream it was handed open, so it is closed here.
        with _open_regular(path) as handle, ZipFile(handle) as archive:
            return read_package(archive)
    except UNREADABLE_MEMBER as exc:
        raise ValidationError(printable(str(exc))) from exc


def _open_regular(path: Path) -> IO[bytes]:
    """
    Open a file for reading, refusing anything that is not a regular file.

    A FIFO named ``*.epub`` in the library was opened for reading, which waits
    for a writer, so a ``--name-by author-title`` run -- ``--list`` included --
    hung for ever. Opened without blocking and judged on the descriptor, so a
    name swapped after a check cannot slip one in. Not ``open_contained``: that
    refuses a hard link too, and a book on the shelf may legitimately be one.

    :param path: The file to open.

    :return: A binary stream, positioned at the start.

    :raises ValidationError: If it is not a regular file.
    :raises OSError: If it cannot be opened.
    """
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValidationError("not a regular file")
        os.set_blocking(descriptor, True)
    except BaseException:
        os.close(descriptor)
        raise
    return os.fdopen(descriptor, "rb")


def _package(members: _Members) -> Package:
    """
    Find and parse a book's package document, whatever shape the book is in.

    :param members: The book being read.

    :return: The package metadata, manifest and spine.

    :raises ValidationError: If the package document is missing or unparsable.
    """
    opf_path = _opf_path(members)
    try:
        return _package_from_root(_element(members, opf_path), opf_path)
    except ValueError as exc:  # Every reader catches ValidationError, not this.
        raise ValidationError(f"unreadable package: {printable(str(exc))}") from exc


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


#: What separates the values of an XML list attribute (XML 1.0, production 3).
_XML_WHITESPACE = re.compile(r"[ \t\r\n]+")
