"""
The references inside a book, checked without an external tool.

``--validate`` confirms that every file the package document lists is present.
It does not look inside those files, so a book whose chapters link to pages,
stylesheets and fonts that are missing, undeclared or outside the book passes
it. Seeing those used to take an external validator: epubcheck, which needs a
Java runtime and took a median 2.8 s a book on a 2,798-book shelf checked on
2026-09-10, or epubveri, which on that shelf failed 34 books epubcheck passed.

This module asks the URL questions itself, with :mod:`epubconvert.utils.url`
doing the parsing, and reports them under the message IDs epubcheck uses so its
output can be compared with epubcheck's book for book:

======== ================================================================
RSC-020  a URL string that fails to parse, or is not a valid URL string
RSC-026  a relative URL that leaves the container (EPUB 3.3's two-root test)
RSC-033  a relative URL with a query
RSC-030  a ``file:`` URL
RSC-007  a reference to a file the book does not contain
RSC-008  a reference to a file the book contains but does not declare
RSC-012  a link to a fragment the target document does not define
RSC-001  a manifest item whose file is missing
HTM-025  a link whose scheme is not registered (a warning)
RSC-007w a metadata link whose file is missing (a warning, EPUB 3)
RSC-016  a document that is not well formed, read up to the error
PKG-008  a member that cannot be read, or is too large to check
======== ================================================================

What counts as a reference, and the order of the checks, follow epubcheck 5.3.0
where the specification leaves them open: the elements and attributes it
collects, EPUB 3's additions, only the NCX the spine names, one RSC-001 per
missing file and one RSC-008 per undeclared one, and no fragment check on a
link whose target is outside the spine. The container is modelled as EPUB 3.3
describes, with the two test roots ``https://a.example.org/A/`` and
``https://b.example.org/B/``.

The check is total. A member that cannot be read is reported and skipped, and a
document that stops being well formed contributes the references and ids that
came before the error -- which is what epubcheck does too, so the two agree on
what such a book links to.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from unicodedata import normalize
from xml.parsers import expat
from zipfile import BadZipFile, ZipFile

from ..utils.percent import (
    percent_decode,
    utf8_decode_without_bom,
    utf8_encode,
    utf8_percent_encode,
)
from ..utils.url import URL, Domain, parse
from .validate import MAX_XML_BYTES, ValidationError, find_opf_path

XHTML_NS = "http://www.w3.org/1999/xhtml"
SVG_NS = "http://www.w3.org/2000/svg"
XLINK_HREF = "http://www.w3.org/1999/xlink href"
MATHML_NS = "http://www.w3.org/1998/Math/MathML"
OPF_NS = "http://www.idpf.org/2007/opf"
NCX_NS = "http://www.daisy.org/z3986/2005/ncx/"

XHTML_TYPES = frozenset({"application/xhtml+xml", "text/x-oeb1-document"})
SVG_TYPE = "image/svg+xml"
CSS_TYPE = "text/css"
NCX_TYPE = "application/x-dtbncx+xml"

#: HTML's ASCII whitespace, which a URL attribute may carry at either end.
_ASCII_WHITESPACE = "\t\n\f\r "

#: The schemes epubcheck 5.3.0 treats as registered for HTM-025
#: (``URISchemes.java``), kept identical so the two report the same links.
REGISTERED_SCHEMES = frozenset(
    {
        "aaa", "aaas", "acap", "cap", "cid", "crid", "data", "dav", "dict",
        "dns", "fax", "file", "ftp", "go", "gopher", "h323", "http", "https",
        "icap", "im", "imap", "info", "ipp", "irc", "iris", "iris.beep",
        "iris.xpc", "iris.xpcs", "iris.lwz", "javascript", "ldap", "mailto",
        "mid", "modem", "msrp", "msrps", "mtqp", "mupdate", "news", "nfs",
        "nntp", "opaquelocktoken", "pop", "pres", "rtsp", "service", "shttp",
        "sip", "sips", "snmp", "soap.beep", "soap.beeps", "tag", "tel",
        "telnet", "tftp", "thismessage", "tip", "tv", "urn", "vemmi",
        "xmlrpc.beep", "xmlrpc.beeps", "xmpp", "z39.50r", "z39.50s", "afs",
        "dtn", "iax", "mailserver", "pack", "tn3270", "prospero", "snews",
        "videotex", "wais",
    }
)  # fmt: skip

_ROOT_A = Domain("a.example.org")
_ROOT_B = Domain("b.example.org")
#: What a member name's segment encodes to become a URL path segment: every
#: ASCII code point but the unreserved ones, the sub-delimiters, ":" and "@".
_SEGMENT_ENCODE_SET = frozenset(chr(point) for point in range(0x7F)) - frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~!$&'()*+,;=:@"
)

_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_CSS_URL = re.compile(r"""url\(\s*(?:"([^"]*)"|'([^']*)'|([^)"'\s]*))\s*\)""", re.I)
_CSS_IMPORT = re.compile(r"""@import\s+(?:"([^"]*)"|'([^']*)')""", re.I)
#: Fragments that are not ids: a scheme-based pointer such as ``epubcfi(...)``
#: and the media fragments, which epubcheck also leaves unchecked.
_NOT_AN_ID = re.compile(r"^(?:[A-Za-z][\w.-]*\(.*\)|(?:t|xywh|track|id|xyn|xyr)=)")


class Kind(Enum):
    """What a reference is, which decides the checks it gets."""

    HYPERLINK = auto()
    CITE = auto()
    LINK = auto()
    RESOURCE = auto()
    URL_ONLY = auto()
    MANIFEST = auto()


@dataclass(frozen=True)
class Reference:
    """One URL string found in a book, and where it was found."""

    text: str
    kind: Kind
    source: str
    line: int | None
    #: An ``a`` or ``area`` link, or an SVG ``a``: the only places HTM-025
    #: looks at, as in epubcheck.
    link_element: bool = False


@dataclass(frozen=True)
class Finding:
    """A problem, under the message ID epubcheck gives the same problem."""

    code: str
    severity: str
    path: str
    line: int | None
    message: str


@dataclass
class _Item:
    member: str
    media_type: str


#: The markup attributes that hold URLs, by element: (attribute, kind).
_XHTML_REFERENCES: dict[str, tuple[tuple[str, Kind], ...]] = {
    "a": (("href", Kind.HYPERLINK),),
    "area": (("href", Kind.HYPERLINK),),
    "img": (("src", Kind.RESOURCE),),
    "object": (("data", Kind.RESOURCE),),
}
#: What EPUB 3 content documents add.
_XHTML3_REFERENCES: dict[str, tuple[tuple[str, Kind], ...]] = {
    "iframe": (("src", Kind.RESOURCE),),
    "script": (("src", Kind.RESOURCE),),
    "embed": (("src", Kind.RESOURCE),),
    "input": (("src", Kind.RESOURCE),),
    "audio": (("src", Kind.RESOURCE),),
    "video": (("src", Kind.RESOURCE), ("poster", Kind.RESOURCE)),
    "source": (("src", Kind.RESOURCE),),
    "track": (("src", Kind.RESOURCE),),
    "blockquote": (("cite", Kind.CITE),),
    "q": (("cite", Kind.CITE),),
    "ins": (("cite", Kind.CITE),),
    "del": (("cite", Kind.CITE),),
}
#: SVG, where epubcheck reads ``xlink:href`` and never a bare ``href``.
_SVG_REFERENCES: dict[str, Kind] = {
    "a": Kind.HYPERLINK,
    "use": Kind.RESOURCE,
    "image": Kind.RESOURCE,
    "font-face-uri": Kind.RESOURCE,
}


def _encode_segment(segment: str) -> str:
    """Percent-encode a member name's segment for use in a content URL."""
    return utf8_percent_encode(segment, _SEGMENT_ENCODE_SET)


def _decode_segment(segment: str) -> str:
    """Percent-decode a URL path segment or fragment back to text."""
    return utf8_decode_without_bom(percent_decode(utf8_encode(segment)))


def _fragment_id(fragment: str, svg: bool) -> str | None:
    """The id a fragment names, or None when it names something else."""
    fragment = fragment.split(":~:", 1)[0]
    if not fragment or _NOT_AN_ID.match(fragment):
        return None
    if svg:
        fragment = fragment.split("&", 1)[0]
    return _decode_segment(fragment) or None


def _css_references(text: str, source: str, first_line: int) -> Iterator[Reference]:
    """Every ``url()`` and ``@import`` in a stylesheet, with its line."""
    blanked = _CSS_COMMENT.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)
    for pattern in (_CSS_URL, _CSS_IMPORT):
        for match in pattern.finditer(blanked):
            value = next((group for group in match.groups() if group is not None), "")
            if not value.strip() or value.lstrip().startswith("#"):
                continue
            line = first_line + blanked.count("\n", 0, match.start())
            yield Reference(value, Kind.RESOURCE, source, line)


class _Book:
    """One archive, read once: its members, manifest, spine, ids and references."""

    def __init__(self, archive: ZipFile) -> None:
        self.archive = archive
        self.members = {
            normalize("NFC", name): name
            for name in archive.namelist()
            if not name.endswith("/")
        }
        self.findings: list[Finding] = []
        self.references: list[Reference] = []
        self.ids: dict[str, set[str]] = {}
        self.manifest: dict[str, _Item] = {}
        self.spine: set[str] = set()
        self.epub3 = False
        self.opf_path = ""
        self._spine_ids: list[str] = []
        self._manifest_ids: dict[str, str] = {}
        self._reported_once: set[tuple[str, str]] = set()
        self.toc_id: str | None = None
        self._bases: dict[tuple[str, str], URL] = {}

    # -- reading

    def _report(
        self, code: str, severity: str, path: str, line: int | None, message: str
    ) -> None:
        self.findings.append(Finding(code, severity, path, line, message))

    def _read(self, member: str) -> bytes | None:
        name = self.members.get(normalize("NFC", member))
        if name is None:
            return None
        try:
            if self.archive.getinfo(name).file_size > MAX_XML_BYTES:
                self._report("PKG-008", "error", member, None, "too large to check")
                return None
            return self.archive.read(name)
        except (BadZipFile, OSError, EOFError, RuntimeError, ValueError) as exc:
            self._report("PKG-008", "error", member, None, f"could not read: {exc}")
            return None

    def _content_url(self, root: Domain, member: str) -> URL:
        """A member's URL under one test root; each is built once per book."""
        key = (root.name, member)
        url = self._bases.get(key)
        if url is None:
            top = "A" if root == _ROOT_A else "B"
            segments = [_encode_segment(segment) for segment in member.split("/")]
            url = URL(scheme="https", host=root, path=[top, *segments])
            self._bases[key] = url
        return url

    def _member_of(self, text: str, base_member: str) -> str | None:
        """The member a manifest href names, or None for one outside the book."""
        parsed = parse(
            text.strip(_ASCII_WHITESPACE), self._content_url(_ROOT_A, base_member)
        )
        url = parsed.url
        if url is None or url.host != _ROOT_A or not isinstance(url.path, list):
            return None
        if not url.path or url.path[0] != "A":
            return None
        return "/".join(_decode_segment(segment) for segment in url.path[1:])

    def _parse_xml(self, member: str, data: bytes, handler: _Handler) -> None:
        parser = expat.ParserCreate(namespace_separator=" ")
        parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
        handler.parser = parser
        parser.StartElementHandler = handler.start
        parser.EndElementHandler = handler.end
        parser.CharacterDataHandler = handler.text
        try:
            parser.Parse(data, True)
        except expat.ExpatError as exc:
            self._report(
                "RSC-016",
                "fatal",
                member,
                exc.lineno,
                f"not well-formed: {expat.ErrorString(exc.code)}",
            )

    def read_package(self) -> bool:
        """Read the package document; False when there is none to read."""
        try:
            self.opf_path = find_opf_path(self.archive)
        except ValidationError as exc:
            self._report("RSC-001", "fatal", "META-INF/container.xml", None, str(exc))
            return False
        data = self._read(self.opf_path)
        if data is None:
            if normalize("NFC", self.opf_path) not in self.members:
                self._report(
                    "RSC-001", "fatal", self.opf_path, None, "package document missing"
                )
            return False
        self._parse_xml(self.opf_path, data, _PackageHandler(self, self.opf_path))
        self.spine = {self.manifest_path(item_id) for item_id in self._spine_ids} - {""}
        return True

    def manifest_path(self, item_id: str) -> str:
        """The member a manifest id names, or "" for an id it does not hold."""
        return self._manifest_ids.get(item_id, "")

    def add_spine_item(self, idref: str) -> None:
        """Note a spine entry; ids resolve once the whole manifest is read."""
        self._spine_ids.append(idref)

    def add_item(self, item_id: str | None, href: str, media_type: str) -> None:
        """Declare a manifest item; an href outside the book declares nothing."""
        member = self._member_of(href, self.opf_path)
        if member is None:
            return
        self.manifest[normalize("NFC", member)] = _Item(member, media_type)
        if item_id:
            self._manifest_ids[item_id] = normalize("NFC", member)

    def read_documents(self) -> None:
        """Collect the references and ids of every document the book declares."""
        # Only the NCX the spine names is read, as epubcheck reads it: any other
        # is a stray file no reading system opens.
        toc = self.manifest_path(self.toc_id or "")
        for key, item in list(self.manifest.items()):
            if item.media_type in XHTML_TYPES or item.media_type == SVG_TYPE:
                data = self._read(item.member)
                if data is not None:
                    self._parse_xml(
                        item.member, data, _MarkupHandler(self, item.member)
                    )
            elif item.media_type == NCX_TYPE and key == toc:
                data = self._read(item.member)
                if data is not None:
                    self._parse_xml(item.member, data, _NcxHandler(self, item.member))
            elif item.media_type == CSS_TYPE:
                data = self._read(item.member)
                if data is not None:
                    text = utf8_decode_without_bom(data).removeprefix("\ufeff")
                    self.references.extend(_css_references(text, item.member, 1))

    # -- checking

    def check(self) -> None:
        """Check every reference collected."""
        for reference in self.references:
            self._check(reference)

    def _check(self, reference: Reference) -> None:
        text = reference.text.strip(_ASCII_WHITESPACE)
        if not text or (reference.kind is Kind.HYPERLINK and text == "."):
            return
        where = (reference.source, reference.line)
        first = parse(text, self._content_url(_ROOT_A, reference.source))
        if first.url is None:
            reason = first.errors[-1] if first.errors else "failure"
            self._report(
                "RSC-020", "error", *where, f'"{text}" is not a valid URL ({reason})'
            )
            return
        if first.errors:
            reasons = ", ".join(dict.fromkeys(first.errors))
            self._report(
                "RSC-020", "error", *where, f'"{text}" is not a valid URL ({reasons})'
            )
        url = first.url
        # Checked after parsing, as epubcheck does, so a file: URL that is not a
        # valid URL string is reported as both.
        if url.scheme == "file":
            self._report("RSC-030", "error", *where, f'"{text}" is a file: URL')
            return
        if url.host != _ROOT_A:
            if reference.link_element and url.scheme not in REGISTERED_SCHEMES:
                self._report(
                    "HTM-025",
                    "warning",
                    *where,
                    f'"{text}" uses an unregistered scheme',
                )
            return
        second = parse(text, self._content_url(_ROOT_B, reference.source)).url
        if second is None or second.host != _ROOT_B:
            return
        if (
            not isinstance(url.path, list)
            or not isinstance(second.path, list)
            or url.path[:1] != ["A"]
            or second.path[:1] != ["B"]
        ):
            self._report(
                "RSC-026", "error", *where, f'"{text}" leaks outside the container'
            )
            return
        if url.query is not None:
            self._report("RSC-033", "error", *where, f'"{text}" has a query')
        target = "/".join(_decode_segment(segment) for segment in url.path[1:])
        self._check_target(reference, target, url.fragment, where)

    def _check_target(
        self,
        reference: Reference,
        target: str,
        fragment: str | None,
        where: tuple[str, int | None],
    ) -> None:
        key = normalize("NFC", target)
        present = key in self.members
        if reference.kind is Kind.MANIFEST:
            # Once a file, as epubcheck reports it, however many items name it.
            if not present and ("RSC-001", key) not in self._reported_once:
                self._reported_once.add(("RSC-001", key))
                self._report(
                    "RSC-001", "error", *where, f'File "{target}" could not be found'
                )
            return
        if reference.kind is Kind.URL_ONLY:
            return
        item = self.manifest.get(key)
        # A declared file the book lacks is as missing as an undeclared one.
        if item is None or not present:
            self._absent(reference, target, key, present, where)
            return
        if (
            reference.kind is not Kind.HYPERLINK
            or not fragment
            or key not in self.spine
        ):
            return
        is_svg = item.media_type == SVG_TYPE
        if not is_svg and item.media_type not in XHTML_TYPES:
            return
        wanted = _fragment_id(fragment, is_svg)
        if wanted is not None and wanted not in self.ids.get(key, set()):
            self._report(
                "RSC-012",
                "error",
                *where,
                f'fragment "{wanted}" is not defined in {target}',
            )

    def _absent(
        self,
        reference: Reference,
        target: str,
        key: str,
        present: bool,
        where: tuple[str, int | None],
    ) -> None:
        """A file the book lacks (RSC-007), or holds but does not declare (RSC-008)."""
        if reference.kind is Kind.LINK:
            if not present and self.epub3:
                self._report(
                    "RSC-007w", "warning", *where, f'"{target}" could not be found'
                )
            return
        if not present:
            self._report(
                "RSC-007",
                "error",
                *where,
                f'Referenced resource "{target}" could not be found in the EPUB',
            )
        elif ("RSC-008", key) not in self._reported_once:
            self._reported_once.add(("RSC-008", key))
            self._report(
                "RSC-008",
                "error",
                *where,
                f'Referenced resource "{target}" is not declared in the manifest',
            )


# ------------------------------------------------------------------ handlers


class _Handler:
    """Receives expat's events for one document."""

    def __init__(self, book: _Book, member: str) -> None:
        self.book = book
        self.member = member
        self.parser: expat.XMLParserType | None = None

    def _line(self) -> int | None:
        return None if self.parser is None else self.parser.CurrentLineNumber

    def _add(self, text: str, kind: Kind, *, link_element: bool = False) -> None:
        self.book.references.append(
            Reference(text, kind, self.member, self._line(), link_element)
        )

    def start(self, name: str, attributes: dict[str, str]) -> None:
        """An element opened."""

    def end(self, name: str) -> None:
        """An element closed."""

    def text(self, data: str) -> None:
        """Character data."""


class _PackageHandler(_Handler):
    """The package document: manifest, spine, guide and metadata links."""

    def start(self, name: str, attributes: dict[str, str]) -> None:
        namespace, _, local = name.rpartition(" ")
        if namespace != OPF_NS:
            return
        if local == "package":
            self.book.epub3 = attributes.get("version", "").startswith("3")
        elif local == "item" and "href" in attributes:
            href = attributes["href"]
            self._add(href, Kind.MANIFEST)
            self.book.add_item(
                attributes.get("id"), href, attributes.get("media-type", "")
            )
        elif local == "spine":
            self.book.toc_id = attributes.get("toc")
        elif local == "itemref" and attributes.get("idref"):
            self.book.add_spine_item(attributes["idref"])
        elif local == "reference" and "href" in attributes:
            # A guide entry is a link into the book, fragment and all: epubcheck
            # checks its fragment as it does a content document's.
            self._add(attributes["href"], Kind.HYPERLINK)
        elif local == "link" and "href" in attributes and self.book.epub3:
            self._add(attributes["href"], Kind.LINK)


class _NcxHandler(_Handler):
    """The NCX, whose ``content src`` links are hyperlinks."""

    def start(self, name: str, attributes: dict[str, str]) -> None:
        namespace, _, local = name.rpartition(" ")
        if namespace == NCX_NS and local == "content" and "src" in attributes:
            self._add(attributes["src"], Kind.HYPERLINK)


class _MarkupHandler(_Handler):
    """XHTML and SVG documents: references, ids and inline CSS."""

    def __init__(self, book: _Book, member: str) -> None:
        super().__init__(book, member)
        self.ids = book.ids.setdefault(normalize("NFC", member), set())
        self.style_line: int | None = None
        self.style_text: list[str] = []

    def start(self, name: str, attributes: dict[str, str]) -> None:
        if "id" in attributes:
            self.ids.add(attributes["id"])
        if "style" in attributes:
            self.book.references.extend(
                _css_references(attributes["style"], self.member, self._line() or 1)
            )
        namespace, _, local = name.rpartition(" ")
        if namespace == XHTML_NS:
            self._xhtml(local, attributes)
        elif namespace == SVG_NS and XLINK_HREF in attributes:
            kind = _SVG_REFERENCES.get(local)
            if kind is not None:
                self._add(attributes[XLINK_HREF], kind, link_element=local == "a")
        elif (
            namespace == MATHML_NS
            and local == "math"
            and self.book.epub3
            and "altimg" in attributes
        ):
            self._add(attributes["altimg"], Kind.RESOURCE)

    def _xhtml(self, local: str, attributes: dict[str, str]) -> None:
        if local == "style":
            self.style_line = self._line()
            self.style_text = []
            return
        if local == "link" and "href" in attributes:
            rel = attributes.get("rel", "").lower().split()
            kind = Kind.RESOURCE if "stylesheet" in rel else Kind.URL_ONLY
            self._add(attributes["href"], kind)
            return
        table = dict(_XHTML_REFERENCES)
        if self.book.epub3:
            table.update(_XHTML3_REFERENCES)
            if local in ("img", "source") and "srcset" in attributes:
                for candidate in attributes["srcset"].split(","):
                    words = candidate.split()
                    if words:
                        self._add(words[0], Kind.RESOURCE)
        for attribute, kind in table.get(local, ()):
            if attribute in attributes:
                self._add(
                    attributes[attribute], kind, link_element=local in ("a", "area")
                )

    def end(self, name: str) -> None:
        if self.style_line is not None and name == f"{XHTML_NS} style":
            text = "".join(self.style_text)
            self.book.references.extend(
                _css_references(text, self.member, self.style_line)
            )
            self.style_line = None

    def text(self, data: str) -> None:
        if self.style_line is not None:
            self.style_text.append(data)


# -------------------------------------------------------------------- entry


@dataclass
class Report:
    """The findings for one book, and how many references were checked."""

    findings: list[Finding] = field(default_factory=list)
    references: int = 0


def check_archive(archive: ZipFile) -> Report:
    """
    Check the URL references inside an open epub archive.

    :param archive: The archive.

    :return: The findings, in the order the book's documents were read.
    """
    book = _Book(archive)
    if book.read_package():
        book.read_documents()
        book.check()
    return Report(book.findings, len(book.references))


def check_file(path: Path) -> Report:
    """
    Check the URL references inside an epub file.

    :param path: The ``.epub`` file.

    :return: The findings; an archive that cannot be opened is one finding.
    """
    try:
        with ZipFile(path) as archive:
            return check_archive(archive)
    except (BadZipFile, OSError, EOFError, RuntimeError, ValueError) as exc:
        return Report(
            [Finding("PKG-008", "fatal", path.name, None, f"unreadable: {exc}")]
        )
