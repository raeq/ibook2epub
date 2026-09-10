"""
The URL Standard's parser, for the URL strings a book writes.

A book names its stylesheets, its images, its fonts and its links with URL
strings, and every question a checker asks of one -- is it well formed, does
it stay inside the book, which file does it name -- depends on reading it the
way a reading system will. This module implements the WHATWG URL Standard's
basic URL parser and URL serializer (https://url.spec.whatwg.org/, read at
commit 8e14777 of 2026-09-10) with the standard library alone.

It exists because ad-hoc URL handling gets real books wrong. On a 2,798-book
shelf checked on 2026-09-10, one validator read ``kindle:embed:0002?mime=image/jpg``
as a relative file path and reported a missing file in 137 books, rejected an
``href`` with a trailing space that HTML allows, and failed to match
``#Vi%C3%A8le`` to ``id="Vièle"``. Each of those questions has one answer in
the standard, and a parser that follows its state machine gets it without a
special case.

Two departures from the standard, both deliberate:

- **IDNA.** Non-ASCII domains go through RFC 3491 nameprep rather than
  UTS #46, which the standard library does not implement.
  :mod:`epubconvert.utils.urlhost`, which holds the host types and parsers,
  says where the two differ.
- **Encoding.** Only UTF-8. The legacy ``encoding`` argument exists for HTML
  documents in other encodings; EPUB requires UTF-8 or UTF-16 content, which
  reaches this module as text.

The parser is total: :func:`parse` never raises. It returns a :class:`Parsed`
holding either a :class:`URL` or failure, together with the validation errors
the standard names, in the order the parser met them. Percent-encoding and the
UTF-8 steps it rests on are in :mod:`epubconvert.utils.percent`.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum, auto

from .percent import (
    C0_CONTROL_PERCENT_ENCODE_SET,
    FRAGMENT_PERCENT_ENCODE_SET,
    PATH_PERCENT_ENCODE_SET,
    QUERY_PERCENT_ENCODE_SET,
    SPECIAL_QUERY_PERCENT_ENCODE_SET,
    USERINFO_PERCENT_ENCODE_SET,
    is_hex_pair_at,
    percent_decode,
    to_scalar_value_string,
    utf8_decode_without_bom,
    utf8_encode,
    utf8_percent_encode_code_point,
)
from .urlhost import (
    DOMAIN_PERCENT_ENCODED,
    EMPTY_HOST,
    FORBIDDEN_HOST_CODE_POINTS,
    HOST_INVALID_CODE_POINT,
    IPV4_NON_ASCII_INPUT,
    IPV6_UNCLOSED,
    Domain,
    EmptyHost,
    Host,
    IPv4Address,
    IPv6Address,
    OpaqueHost,
    ends_in_a_number,
    parse_domain,
    parse_ipv4,
    parse_ipv6,
    serialize_host,
)

__all__ = [
    "SPECIAL_SCHEMES",
    "URL",
    "Domain",
    "EmptyHost",
    "Host",
    "IPv4Address",
    "IPv6Address",
    "OpaqueHost",
    "Parsed",
    "parse",
    "serialize_host",
]

#: The special schemes and their default ports.
SPECIAL_SCHEMES: dict[str, int | None] = {
    "ftp": 21,
    "file": None,
    "http": 80,
    "https": 443,
    "ws": 80,
    "wss": 443,
}

# Validation errors, spelled exactly as the standard's table spells them, so a
# report can be matched against the standard without a translation table. These
# are its URL-parsing rows; the IDNA and host-parsing rows are in urlhost.
INVALID_URL_UNIT = "invalid-URL-unit"
SPECIAL_SCHEME_MISSING_FOLLOWING_SOLIDUS = "special-scheme-missing-following-solidus"
MISSING_SCHEME_NON_RELATIVE_URL = "missing-scheme-non-relative-URL"
INVALID_REVERSE_SOLIDUS = "invalid-reverse-solidus"
INVALID_CREDENTIALS = "invalid-credentials"
HOST_MISSING = "host-missing"
PORT_OUT_OF_RANGE = "port-out-of-range"
PORT_INVALID = "port-invalid"
FILE_INVALID_WINDOWS_DRIVE_LETTER = "file-invalid-Windows-drive-letter"
FILE_INVALID_WINDOWS_DRIVE_LETTER_HOST = "file-invalid-Windows-drive-letter-host"

_ASCII_ALPHA = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
_ASCII_DIGIT = frozenset("0123456789")
_ASCII_ALNUM = _ASCII_ALPHA | _ASCII_DIGIT
_SCHEME_CODE_POINTS = _ASCII_ALNUM | frozenset("+-.")
_C0_CONTROL_OR_SPACE = "".join(chr(point) for point in range(0x21))
_TAB_OR_NEWLINE = re.compile("[\t\n\r]")
_URL_ASCII = _ASCII_ALNUM | frozenset("!$&'()*+,-./:;=?@_~")

# Runs of code points a state takes in one step. Appending one code point at a
# time copies the whole string on every append, which made a book whose cover
# is a 1.5 MB data: URL take 24 seconds; a crafted host a few megabytes long,
# far longer.
_SCHEME_RUN = re.compile("[A-Za-z0-9+.-]*")
_DIGIT_RUN = re.compile("[0-9]*")
_AUTHORITY_RUN = re.compile(r"[^@/?#]*")
_SPECIAL_AUTHORITY_RUN = re.compile(r"[^@/\\?#]*")
_HOST_RUN = re.compile(r"[^:\[\]/?#]*")
_SPECIAL_HOST_RUN = re.compile(r"[^:\[\]/\\?#]*")
_FILE_HOST_RUN = re.compile(r"[^/\\?#]*")
_PATH_RUN = re.compile(r"[^/?#]*")
_SPECIAL_PATH_RUN = re.compile(r"[^/\\?#]*")
_OPAQUE_PATH_RUN = re.compile(r"[^?#]*")
_QUERY_RUN = re.compile(r"[^#]*")


# ---------------------------------------------------------------------- URL


@dataclass
class URL:
    """
    A URL record: the parsed form the standard calls a URL.

    ``path`` is a list of segments, or a single string when the URL has an
    opaque path (``mailto:``, ``kindle:embed:0002``). The getters mirror the
    standard's URL API so a result can be compared with a browser's.
    """

    scheme: str = ""
    username: str = ""
    password: str = ""
    host: Host | None = None
    port: int | None = None
    path: list[str] | str = field(default_factory=list)
    query: str | None = None
    fragment: str | None = None

    @property
    def special(self) -> bool:
        """Whether the scheme is one of the six special schemes."""
        return self.scheme in SPECIAL_SCHEMES

    @property
    def opaque_path(self) -> bool:
        """Whether the path is a single opaque string."""
        return isinstance(self.path, str)

    def serialize(self, *, exclude_fragment: bool = False) -> str:
        """
        Serialize the URL, as the standard's URL serializer does.

        :param exclude_fragment: Leave the fragment off.

        :return: The URL's ASCII serialization.
        """
        output = f"{self.scheme}:"
        if self.host is not None:
            output += "//"
            if self.username or self.password:
                output += self.username
                if self.password:
                    output += f":{self.password}"
                output += "@"
            output += serialize_host(self.host)
            if self.port is not None:
                output += f":{self.port}"
        elif isinstance(self.path, list) and len(self.path) > 1 and self.path[0] == "":
            output += "/."
        output += self.pathname
        if self.query is not None:
            output += f"?{self.query}"
        if not exclude_fragment and self.fragment is not None:
            output += f"#{self.fragment}"
        return output

    @property
    def href(self) -> str:
        """The full serialization."""
        return self.serialize()

    @property
    def protocol(self) -> str:
        """The scheme followed by ``:``."""
        return f"{self.scheme}:"

    @property
    def hostname(self) -> str:
        """The serialized host, or the empty string when there is none."""
        return "" if self.host is None else serialize_host(self.host)

    @property
    def host_and_port(self) -> str:
        """The API's ``host``: the hostname and, when there is one, the port."""
        if self.host is None:
            return ""
        return self.hostname if self.port is None else f"{self.hostname}:{self.port}"

    @property
    def port_string(self) -> str:
        """The API's ``port``: the port as a string, or the empty string."""
        return "" if self.port is None else str(self.port)

    @property
    def pathname(self) -> str:
        """The serialized path."""
        if isinstance(self.path, str):
            return self.path
        return "".join(f"/{segment}" for segment in self.path)

    @property
    def search(self) -> str:
        """``?`` and the query, or the empty string for none or an empty one."""
        return f"?{self.query}" if self.query else ""

    @property
    def hash(self) -> str:
        """``#`` and the fragment, or the empty string for none or an empty one."""
        return f"#{self.fragment}" if self.fragment else ""

    @property
    def origin(self) -> str:
        """
        The serialized origin: ``scheme://host[:port]``, or ``null``.

        A ``blob:`` URL takes the origin of the URL in its path when that is
        an ``http``, ``https`` or ``file`` URL. Every scheme that is not
        special, and ``file``, has an opaque origin, which serializes as
        ``null``; the standard leaves ``file`` to the implementation and advises
        opaque when in doubt.
        """
        if self.scheme == "blob":
            inner = parse(self.pathname).url
            if inner is not None and inner.scheme in ("http", "https", "file"):
                return inner.origin
            return "null"
        if self.scheme in ("ftp", "http", "https", "ws", "wss"):
            return f"{self.scheme}://{self.host_and_port}"
        return "null"

    def copy_path(self) -> list[str] | str:
        """A clone of the path, as the parser takes from a base URL."""
        return list(self.path) if isinstance(self.path, list) else self.path


@dataclass(frozen=True)
class Parsed:
    """
    What the parser made of a string.

    ``url`` is None when parsing failed. ``errors`` lists the validation
    errors met on the way, by the standard's names; a string is a *valid URL
    string* exactly when it parses with none.
    """

    url: URL | None
    errors: tuple[str, ...]

    @property
    def failed(self) -> bool:
        """Whether the parser returned failure."""
        return self.url is None

    @property
    def valid(self) -> bool:
        """Whether the input was a valid URL string: parsed, with no error."""
        return self.url is not None and not self.errors


# --------------------------------------------------------- URL code points


def _is_url_code_point(code_point: str) -> bool:
    """Whether a code point may appear in a valid URL string unencoded."""
    if code_point in _URL_ASCII:
        return True
    value = ord(code_point)
    if value < 0xA0 or value > 0x10FFFD or 0xD800 <= value <= 0xDFFF:
        return False
    return not (0xFDD0 <= value <= 0xFDEF or value & 0xFFFE == 0xFFFE)


# --------------------------------------------------------- the host parser


def _parse_opaque_host(text: str, errors: list[str]) -> Host | None:
    """The standard's opaque-host parser."""
    if any(character in FORBIDDEN_HOST_CODE_POINTS for character in text):
        errors.append(HOST_INVALID_CODE_POINT)
        return None
    _check_url_units(text, errors)
    if not text:
        return EMPTY_HOST
    return OpaqueHost(
        "".join(
            utf8_percent_encode_code_point(character, C0_CONTROL_PERCENT_ENCODE_SET)
            for character in text
        )
    )


def _check_url_units(text: str, errors: list[str]) -> None:
    """Record invalid-URL-unit for a code point or a ``%`` a valid string lacks."""
    for index, character in enumerate(text):
        if character == "%":
            if not is_hex_pair_at(text, index + 1):
                errors.append(INVALID_URL_UNIT)
        elif not _is_url_code_point(character):
            errors.append(INVALID_URL_UNIT)


def _parse_host(text: str, is_opaque: bool, errors: list[str]) -> Host | None:
    """The standard's host parser."""
    if text.startswith("["):
        if not text.endswith("]"):
            errors.append(IPV6_UNCLOSED)
            return None
        return parse_ipv6(text[1:-1], errors)
    if is_opaque:
        return _parse_opaque_host(text, errors)
    if any(
        character == "%" and is_hex_pair_at(text, index + 1)
        for index, character in enumerate(text)
    ):
        errors.append(DOMAIN_PERCENT_ENCODED)
    domain = utf8_decode_without_bom(percent_decode(utf8_encode(text)))
    ascii_domain = parse_domain(domain, errors)
    if ascii_domain is None:
        return None
    if ends_in_a_number(ascii_domain):
        if not domain.isascii():
            errors.append(IPV4_NON_ASCII_INPUT)
        return parse_ipv4(ascii_domain, errors)
    return Domain(ascii_domain)


# ----------------------------------------------------------------- the parser


class _State(Enum):
    SCHEME_START = auto()
    SCHEME = auto()
    NO_SCHEME = auto()
    SPECIAL_RELATIVE_OR_AUTHORITY = auto()
    PATH_OR_AUTHORITY = auto()
    RELATIVE = auto()
    RELATIVE_SLASH = auto()
    SPECIAL_AUTHORITY_SLASHES = auto()
    SPECIAL_AUTHORITY_IGNORE_SLASHES = auto()
    AUTHORITY = auto()
    HOST = auto()
    PORT = auto()
    FILE = auto()
    FILE_SLASH = auto()
    FILE_HOST = auto()
    PATH_START = auto()
    PATH = auto()
    OPAQUE_PATH = auto()
    QUERY = auto()
    FRAGMENT = auto()


class _FailureError(Exception):
    """Raised inside the machine when the parser returns failure."""


def _is_windows_drive_letter(text: str) -> bool:
    return len(text) == 2 and text[0] in _ASCII_ALPHA and text[1] in ":|"


def _is_normalized_windows_drive_letter(text: str) -> bool:
    return len(text) == 2 and text[0] in _ASCII_ALPHA and text[1] == ":"


def _starts_with_windows_drive_letter(text: str) -> bool:
    return _is_windows_drive_letter(text[:2]) and (len(text) == 2 or text[2] in "/\\?#")


def _is_single_dot(segment: str) -> bool:
    return segment == "." or segment.lower() == "%2e"


def _is_double_dot(segment: str) -> bool:
    return segment == ".." or segment.lower() in (".%2e", "%2e.", "%2e%2e")


# One method per state of the standard's state machine, so the count of public
# methods is the standard's count of states.
class _Machine:  # pylint: disable=too-many-instance-attributes,too-many-public-methods
    """
    The basic URL parser's state machine, one method per state.

    Each method handles the code point under the pointer, exactly as the
    standard's state of the same name does; ``None`` stands for EOF. Failure is
    raised as :class:`_FailureError` and turned into a value by :func:`parse`, so
    the parser as a whole stays total.
    """

    def __init__(self, text: str, base: URL | None, errors: list[str]) -> None:
        self.text = text
        self.base = base
        self.errors = errors
        self.url = URL()
        self.state = _State.SCHEME_START
        self.buffer = ""
        self.pointer = 0
        self.at_sign_seen = False
        self.inside_brackets = False
        self.password_token_seen = False

    # -- helpers

    def _remaining_starts_with(self, prefix: str) -> bool:
        return self.text.startswith(prefix, self.pointer + 1)

    def _error(self, name: str) -> None:
        self.errors.append(name)

    def _fail(self, name: str) -> None:
        self.errors.append(name)
        raise _FailureError(name)

    def _check_unit_at(self, index: int) -> None:
        """Record invalid-URL-unit if the code point at *index* is not one."""
        character = self.text[index]
        if character == "%":
            if not is_hex_pair_at(self.text, index + 1):
                self._error(INVALID_URL_UNIT)
        elif not _is_url_code_point(character):
            self._error(INVALID_URL_UNIT)

    def _unit(self, index: int, encode_set: frozenset[str]) -> str:
        """Check the code point at *index* and return it percent-encoded."""
        self._check_unit_at(index)
        return utf8_percent_encode_code_point(self.text[index], encode_set)

    def _take(self, run: re.Pattern[str]) -> tuple[int, int]:
        """
        Take the run of code points *run* matches at the pointer, in one step.

        :return: The run's start and end. The pointer is left on its last code
            point, where the main loop expects it, and at least the code point
            under the pointer is taken, so the loop always moves on.
        """
        start = self.pointer
        match = run.match(self.text, start)
        end = max(match.end() if match else start, start + 1)
        self.pointer = end - 1
        return start, end

    def _base(self) -> URL:
        if self.base is None:  # pragma: no cover - the states guarantee a base
            raise _FailureError("no base")
        return self.base

    def _inherit_authority(self) -> None:
        base = self._base()
        self.url.username = base.username
        self.url.password = base.password
        self.url.host = base.host
        self.url.port = base.port

    def _shorten(self) -> None:
        path = self.url.path
        if not isinstance(path, list):  # pragma: no cover - asserted by the standard
            return
        if (
            self.url.scheme == "file"
            and len(path) == 1
            and _is_normalized_windows_drive_letter(path[0])
        ):
            return
        if path:
            path.pop()

    def _host(self, text: str) -> Host:
        host = _parse_host(text, not self.url.special, self.errors)
        if host is None:
            raise _FailureError("host")
        return host

    def _path_list(self) -> list[str]:
        path = self.url.path
        if not isinstance(path, list):  # pragma: no cover - states keep it a list
            raise _FailureError("opaque path in path state")
        return path

    # -- the run

    def run(self) -> URL:
        """Run the machine to the end of the input and return the URL."""
        handlers = _HANDLERS
        length = len(self.text)
        while True:
            character = self.text[self.pointer] if self.pointer < length else None
            handlers[self.state](self, character)
            if self.pointer >= length:
                return self.url
            self.pointer += 1

    # -- the states

    def scheme_start(self, c: str | None) -> None:
        """Scheme start state."""
        if c is not None and c in _ASCII_ALPHA:
            self.buffer += c.lower()
            self.state = _State.SCHEME
        else:
            self.state = _State.NO_SCHEME
            self.pointer -= 1

    def scheme(self, c: str | None) -> None:
        """Scheme state."""
        if c is not None and c in _SCHEME_CODE_POINTS:
            start, end = self._take(_SCHEME_RUN)
            self.buffer += self.text[start:end].lower()
            return
        if c != ":":
            self.buffer = ""
            self.state = _State.NO_SCHEME
            self.pointer = -1
            return
        self.url.scheme = self.buffer
        self.buffer = ""
        if self.url.scheme == "file":
            if not self._remaining_starts_with("//"):
                self._error(SPECIAL_SCHEME_MISSING_FOLLOWING_SOLIDUS)
            self.state = _State.FILE
        elif (
            self.url.special
            and self.base is not None
            and self.base.scheme == self.url.scheme
        ):
            self.state = _State.SPECIAL_RELATIVE_OR_AUTHORITY
        elif self.url.special:
            self.state = _State.SPECIAL_AUTHORITY_SLASHES
        elif self._remaining_starts_with("/"):
            self.state = _State.PATH_OR_AUTHORITY
            self.pointer += 1
        else:
            self.url.path = ""
            self.state = _State.OPAQUE_PATH

    def no_scheme(self, c: str | None) -> None:
        """No scheme state."""
        base = self.base
        if base is None or (base.opaque_path and c != "#"):
            self._fail(MISSING_SCHEME_NON_RELATIVE_URL)
            return
        if base.opaque_path:
            self.url.scheme = base.scheme
            self.url.path = base.copy_path()
            self.url.query = base.query
            self.url.fragment = ""
            self.state = _State.FRAGMENT
        elif base.scheme != "file":
            self.state = _State.RELATIVE
            self.pointer -= 1
        else:
            self.state = _State.FILE
            self.pointer -= 1

    def special_relative_or_authority(self, c: str | None) -> None:
        """Special relative or authority state."""
        if c == "/" and self._remaining_starts_with("/"):
            self.state = _State.SPECIAL_AUTHORITY_IGNORE_SLASHES
            self.pointer += 1
        else:
            self._error(SPECIAL_SCHEME_MISSING_FOLLOWING_SOLIDUS)
            self.state = _State.RELATIVE
            self.pointer -= 1

    def path_or_authority(self, c: str | None) -> None:
        """Path or authority state."""
        if c == "/":
            self.state = _State.AUTHORITY
        else:
            self.state = _State.PATH
            self.pointer -= 1

    def relative(self, c: str | None) -> None:
        """Relative state."""
        base = self._base()
        self.url.scheme = base.scheme
        if c == "/":
            self.state = _State.RELATIVE_SLASH
        elif self.url.special and c == "\\":
            self._error(INVALID_REVERSE_SOLIDUS)
            self.state = _State.RELATIVE_SLASH
        else:
            self._inherit_authority()
            self.url.path = base.copy_path()
            self.url.query = base.query
            if c == "?":
                self.url.query = ""
                self.state = _State.QUERY
            elif c == "#":
                self.url.fragment = ""
                self.state = _State.FRAGMENT
            elif c is not None:
                self.url.query = None
                self._shorten()
                self.state = _State.PATH
                self.pointer -= 1

    def relative_slash(self, c: str | None) -> None:
        """Relative slash state."""
        if self.url.special and c in ("/", "\\"):
            if c == "\\":
                self._error(INVALID_REVERSE_SOLIDUS)
            self.state = _State.SPECIAL_AUTHORITY_IGNORE_SLASHES
        elif c == "/":
            self.state = _State.AUTHORITY
        else:
            self._inherit_authority()
            self.state = _State.PATH
            self.pointer -= 1

    def special_authority_slashes(self, c: str | None) -> None:
        """Special authority slashes state."""
        if c == "/" and self._remaining_starts_with("/"):
            self.pointer += 1
        else:
            self._error(SPECIAL_SCHEME_MISSING_FOLLOWING_SOLIDUS)
            self.pointer -= 1
        self.state = _State.SPECIAL_AUTHORITY_IGNORE_SLASHES

    def special_authority_ignore_slashes(self, c: str | None) -> None:
        """Special authority ignore slashes state."""
        if c not in ("/", "\\"):
            self.state = _State.AUTHORITY
            self.pointer -= 1
        else:
            self._error(SPECIAL_SCHEME_MISSING_FOLLOWING_SOLIDUS)

    def _ends_authority(self, c: str | None) -> bool:
        return c is None or c in "/?#" or (self.url.special and c == "\\")

    def authority(self, c: str | None) -> None:
        """Authority state."""
        if c == "@":
            self._error(INVALID_CREDENTIALS)
            if self.at_sign_seen:
                self.buffer = f"%40{self.buffer}"
            self.at_sign_seen = True
            for code_point in self.buffer:
                if code_point == ":" and not self.password_token_seen:
                    self.password_token_seen = True
                    continue
                encoded = utf8_percent_encode_code_point(
                    code_point, USERINFO_PERCENT_ENCODE_SET
                )
                if self.password_token_seen:
                    self.url.password += encoded
                else:
                    self.url.username += encoded
            self.buffer = ""
        elif self._ends_authority(c):
            if self.at_sign_seen and not self.buffer:
                self._fail(HOST_MISSING)
            self.pointer -= len(self.buffer) + 1
            self.buffer = ""
            self.state = _State.HOST
        elif c is not None:
            run = _SPECIAL_AUTHORITY_RUN if self.url.special else _AUTHORITY_RUN
            start, end = self._take(run)
            self.buffer += self.text[start:end]

    def host(self, c: str | None) -> None:
        """Host state and hostname state."""
        if c == ":" and not self.inside_brackets:
            if not self.buffer:
                self._fail(HOST_MISSING)
            self.url.host = self._host(self.buffer)
            self.buffer = ""
            self.state = _State.PORT
        elif self._ends_authority(c):
            self.pointer -= 1
            if self.url.special and not self.buffer:
                self._fail(HOST_MISSING)
            self.url.host = self._host(self.buffer)
            self.buffer = ""
            self.state = _State.PATH_START
        elif c is not None and c in "[]:":
            if c == "[":
                self.inside_brackets = True
            if c == "]":
                self.inside_brackets = False
            self.buffer += c
        elif c is not None:
            run = _SPECIAL_HOST_RUN if self.url.special else _HOST_RUN
            start, end = self._take(run)
            self.buffer += self.text[start:end]

    def port(self, c: str | None) -> None:
        """Port state."""
        if c is not None and c in _ASCII_DIGIT:
            start, end = self._take(_DIGIT_RUN)
            self.buffer += self.text[start:end]
        elif self._ends_authority(c):
            if self.buffer:
                # int() refuses decimal strings over 4,300 digits, and a port of
                # more than five significant digits is out of range anyway.
                digits = self.buffer.lstrip("0") or "0"
                number = int(digits) if len(digits) <= 5 else 0x10000
                if number > 0xFFFF:
                    self._fail(PORT_OUT_OF_RANGE)
                default = SPECIAL_SCHEMES.get(self.url.scheme)
                self.url.port = None if number == default else number
                self.buffer = ""
            self.state = _State.PATH_START
            self.pointer -= 1
        else:
            self._fail(PORT_INVALID)

    def file(self, c: str | None) -> None:
        """File state."""
        self.url.scheme = "file"
        self.url.host = EMPTY_HOST
        if c in ("/", "\\"):
            if c == "\\":
                self._error(INVALID_REVERSE_SOLIDUS)
            self.state = _State.FILE_SLASH
            return
        base = self.base
        if base is None or base.scheme != "file":
            self.state = _State.PATH
            self.pointer -= 1
            return
        self.url.host = base.host
        self.url.path = base.copy_path()
        self.url.query = base.query
        if c == "?":
            self.url.query = ""
            self.state = _State.QUERY
        elif c == "#":
            self.url.fragment = ""
            self.state = _State.FRAGMENT
        elif c is not None:
            self.url.query = None
            if not _starts_with_windows_drive_letter(self.text[self.pointer :]):
                self._shorten()
            else:
                self._error(FILE_INVALID_WINDOWS_DRIVE_LETTER)
                self.url.path = []
            self.state = _State.PATH
            self.pointer -= 1

    def file_slash(self, c: str | None) -> None:
        """File slash state."""
        if c in ("/", "\\"):
            if c == "\\":
                self._error(INVALID_REVERSE_SOLIDUS)
            self.state = _State.FILE_HOST
            return
        base = self.base
        if base is not None and base.scheme == "file":
            self.url.host = base.host
            base_path = base.path
            if (
                not _starts_with_windows_drive_letter(self.text[self.pointer :])
                and isinstance(base_path, list)
                and base_path
                and _is_normalized_windows_drive_letter(base_path[0])
            ):
                self._path_list().append(base_path[0])
        self.state = _State.PATH
        self.pointer -= 1

    def file_host(self, c: str | None) -> None:
        """File host state."""
        if c is not None and c not in "/\\?#":
            start, end = self._take(_FILE_HOST_RUN)
            self.buffer += self.text[start:end]
            return
        self.pointer -= 1
        if _is_windows_drive_letter(self.buffer):
            self._error(FILE_INVALID_WINDOWS_DRIVE_LETTER_HOST)
            self.state = _State.PATH
        elif not self.buffer:
            self.url.host = EMPTY_HOST
            self.state = _State.PATH_START
        else:
            host = self._host(self.buffer)
            self.url.host = EMPTY_HOST if host == Domain("localhost") else host
            self.buffer = ""
            self.state = _State.PATH_START

    def path_start(self, c: str | None) -> None:
        """Path start state."""
        if self.url.special:
            if c == "\\":
                self._error(INVALID_REVERSE_SOLIDUS)
            self.state = _State.PATH
            if c not in ("/", "\\"):
                self.pointer -= 1
        elif c == "?":
            self.url.query = ""
            self.state = _State.QUERY
        elif c == "#":
            self.url.fragment = ""
            self.state = _State.FRAGMENT
        elif c is not None:
            self.state = _State.PATH
            if c != "/":
                self.pointer -= 1

    def path(self, c: str | None) -> None:
        """Path state."""
        slash = c == "/" or (self.url.special and c == "\\")
        if not (c is None or slash or c in "?#"):
            run = _SPECIAL_PATH_RUN if self.url.special else _PATH_RUN
            start, end = self._take(run)
            self.buffer += "".join(
                self._unit(index, PATH_PERCENT_ENCODE_SET)
                for index in range(start, end)
            )
            return
        if self.url.special and c == "\\":
            self._error(INVALID_REVERSE_SOLIDUS)
        segments = self._path_list()
        if _is_double_dot(self.buffer):
            self._shorten()
            if not slash:
                segments.append("")
        elif _is_single_dot(self.buffer):
            if not slash:
                segments.append("")
        else:
            if (
                self.url.scheme == "file"
                and not segments
                and _is_windows_drive_letter(self.buffer)
            ):
                self.buffer = f"{self.buffer[0]}:"
            segments.append(self.buffer)
        self.buffer = ""
        if c == "?":
            self.url.query = ""
            self.state = _State.QUERY
        elif c == "#":
            self.url.fragment = ""
            self.state = _State.FRAGMENT

    def opaque_path(self, c: str | None) -> None:
        """Opaque path state."""
        path = self.url.path
        if not isinstance(path, str):  # pragma: no cover - set by the scheme state
            raise _FailureError("list path in opaque path state")
        if c == "?":
            self.url.query = ""
            self.state = _State.QUERY
        elif c == "#":
            self.url.fragment = ""
            self.state = _State.FRAGMENT
        elif c is not None:
            start, end = self._take(_OPAQUE_PATH_RUN)
            # A space is encoded only where a query or a fragment follows it.
            followed = end < len(self.text)
            parts: list[str] = []
            for index in range(start, end):
                if self.text[index] == " ":
                    self._error(INVALID_URL_UNIT)
                    parts.append("%20" if followed and index == end - 1 else " ")
                else:
                    parts.append(self._unit(index, C0_CONTROL_PERCENT_ENCODE_SET))
            self.url.path = path + "".join(parts)

    def query(self, c: str | None) -> None:
        """Query state."""
        if c is None or c == "#":
            encode_set = (
                SPECIAL_QUERY_PERCENT_ENCODE_SET
                if self.url.special
                else QUERY_PERCENT_ENCODE_SET
            )
            encoded = "".join(
                utf8_percent_encode_code_point(character, encode_set)
                for character in self.buffer
            )
            self.url.query = (self.url.query or "") + encoded
            self.buffer = ""
            if c == "#":
                self.url.fragment = ""
                self.state = _State.FRAGMENT
            return
        start, end = self._take(_QUERY_RUN)
        for index in range(start, end):
            self._check_unit_at(index)
        self.buffer += self.text[start:end]

    def fragment(self, c: str | None) -> None:
        """Fragment state: the rest of the input, taken in one step."""
        if c is None:
            return
        start, end = self.pointer, len(self.text)
        self.pointer = end - 1
        self.url.fragment = (self.url.fragment or "") + "".join(
            self._unit(index, FRAGMENT_PERCENT_ENCODE_SET)
            for index in range(start, end)
        )


_HANDLERS: dict[_State, Callable[[_Machine, str | None], None]] = {
    _State.SCHEME_START: _Machine.scheme_start,
    _State.SCHEME: _Machine.scheme,
    _State.NO_SCHEME: _Machine.no_scheme,
    _State.SPECIAL_RELATIVE_OR_AUTHORITY: _Machine.special_relative_or_authority,
    _State.PATH_OR_AUTHORITY: _Machine.path_or_authority,
    _State.RELATIVE: _Machine.relative,
    _State.RELATIVE_SLASH: _Machine.relative_slash,
    _State.SPECIAL_AUTHORITY_SLASHES: _Machine.special_authority_slashes,
    _State.SPECIAL_AUTHORITY_IGNORE_SLASHES: _Machine.special_authority_ignore_slashes,
    _State.AUTHORITY: _Machine.authority,
    _State.HOST: _Machine.host,
    _State.PORT: _Machine.port,
    _State.FILE: _Machine.file,
    _State.FILE_SLASH: _Machine.file_slash,
    _State.FILE_HOST: _Machine.file_host,
    _State.PATH_START: _Machine.path_start,
    _State.PATH: _Machine.path,
    _State.OPAQUE_PATH: _Machine.opaque_path,
    _State.QUERY: _Machine.query,
    _State.FRAGMENT: _Machine.fragment,
}


def parse(text: str, base: URL | None = None) -> Parsed:
    """
    Parse a URL string, as the standard's basic URL parser does.

    :param text: The string, as a book wrote it.
    :param base: The URL to resolve a relative string against, if any.

    :return: The URL, or failure, and the validation errors met on the way.
    """
    errors: list[str] = []
    scalar = to_scalar_value_string(text)
    stripped = scalar.strip(_C0_CONTROL_OR_SPACE)
    if stripped != scalar:
        errors.append(INVALID_URL_UNIT)
    if _TAB_OR_NEWLINE.search(stripped):
        errors.append(INVALID_URL_UNIT)
        stripped = _TAB_OR_NEWLINE.sub("", stripped)
    try:
        url = _Machine(stripped, base, errors).run()
    except _FailureError:
        return Parsed(None, tuple(errors))
    return Parsed(url, tuple(errors))
