"""
The URL Standard's hosts: domains, IP addresses and the IDNA step.

A URL's host is parsed by rules of its own, section 3 of the WHATWG URL
Standard (https://url.spec.whatwg.org/, read at commit 8e14777 of 2026-09-10).
This module holds them: the host types and their serializer, the IPv4 and IPv6
parsers, and the domain parser with its IDNA step. The host parser that chooses
between them is in :mod:`epubconvert.utils.url`, because its opaque-host branch
checks each code point against the URL code points defined there, and that
module re-exports the host types.

The one deliberate departure from the standard is IDNA. The standard maps
non-ASCII domains with UTS #46, which the standard library does not implement.
ASCII domains -- nearly every domain a book links to -- parse exactly as the
standard says; only an ``xn--`` label's validation error can differ, because
checking one runs its decoded form through the steps below. Non-ASCII labels go
through RFC 3491 nameprep built from the standard library's ``stringprep``
tables, with UTS #46's own exception that ``ß`` and ``ς`` are kept rather than
folded: the one difference the conformance suite exposed, where ``faß.example``
must become ``xn--fa-hia.example``, not ``fass.example``. What remains
different:

- zero-width joiners and non-joiners, which nameprep deletes and UTS #46
  allows only in context;
- code points newer than Unicode 3.2, which nameprep refuses;
- a label that begins with a combining mark, which UTS #46 refuses and
  nameprep accepts;
- the bidirectional rule: RFC 3454's, which nameprep applies, refuses a
  right-to-left label that ends in a digit, and RFC 5893's, which UTS #46
  applies, allows one.

Every parser here appends to the list of validation errors its caller is
collecting, so the errors of a whole URL come out in the order the standard
meets them.
"""

from __future__ import annotations

import re
import stringprep
from dataclasses import dataclass
from typing import NoReturn, TypeAlias
from unicodedata import ucd_3_2_0

__all__ = [
    "EMPTY_HOST",
    "FORBIDDEN_DOMAIN_CODE_POINTS",
    "FORBIDDEN_HOST_CODE_POINTS",
    "Domain",
    "EmptyHost",
    "Host",
    "IPv4Address",
    "IPv6Address",
    "OpaqueHost",
    "ends_in_a_number",
    "parse_domain",
    "parse_ipv4",
    "parse_ipv6",
    "serialize_host",
]

# Validation errors, spelled exactly as the standard's table spells them: its
# IDNA and host-parsing rows. The URL-parsing rows are in url.py.
DOMAIN_TO_ASCII = "domain-to-ASCII"
DOMAIN_PERCENT_ENCODED = "domain-percent-encoded"
HOST_INVALID_CODE_POINT = "host-invalid-code-point"
IPV4_EMPTY_PART = "IPv4-empty-part"
IPV4_TOO_FEW_PARTS = "IPv4-too-few-parts"
IPV4_TOO_MANY_PARTS = "IPv4-too-many-parts"
IPV4_NON_NUMERIC_PART = "IPv4-non-numeric-part"
IPV4_NON_DECIMAL_PART = "IPv4-non-decimal-part"
IPV4_OUT_OF_RANGE_PART = "IPv4-out-of-range-part"
IPV4_NON_ASCII_INPUT = "IPv4-non-ASCII-input"
IPV6_UNCLOSED = "IPv6-unclosed"
IPV6_INVALID_COMPRESSION = "IPv6-invalid-compression"
IPV6_TOO_MANY_PIECES = "IPv6-too-many-pieces"
IPV6_MULTIPLE_COMPRESSION = "IPv6-multiple-compression"
IPV6_INVALID_CODE_POINT = "IPv6-invalid-code-point"
IPV6_TOO_FEW_PIECES = "IPv6-too-few-pieces"
IPV6_PIECE_LEADING_ZERO = "IPv6-piece-leading-zero"
IPV4_IN_IPV6_TOO_MANY_PIECES = "IPv4-in-IPv6-too-many-pieces"
IPV4_IN_IPV6_INVALID_CODE_POINT = "IPv4-in-IPv6-invalid-code-point"
IPV4_IN_IPV6_OUT_OF_RANGE_PART = "IPv4-in-IPv6-out-of-range-part"
IPV4_IN_IPV6_TOO_FEW_PARTS = "IPv4-in-IPv6-too-few-parts"

#: The code points no host may contain.
FORBIDDEN_HOST_CODE_POINTS = frozenset("\x00\t\n\r #/:<>?@[\\]^|")
#: The code points no domain may contain: those, the C0 controls, % and DEL.
FORBIDDEN_DOMAIN_CODE_POINTS = (
    FORBIDDEN_HOST_CODE_POINTS
    | frozenset(chr(point) for point in range(0x20))
    | {"%", "\x7f"}
)

_ASCII_DIGIT = frozenset("0123456789")
_ASCII_HEX = frozenset("0123456789abcdefABCDEF")
_IDNA_DOTS = re.compile("[.。．｡]")
_LDH = re.compile("[a-z0-9-]+")


# --------------------------------------------------------------------- hosts


@dataclass(frozen=True)
class Domain:
    """A domain: non-empty, ASCII, lowercased, punycoded."""

    name: str


@dataclass(frozen=True)
class IPv4Address:
    """An IPv4 address as the 32-bit number the standard defines it to be."""

    value: int


@dataclass(frozen=True)
class IPv6Address:
    """An IPv6 address as its eight 16-bit pieces."""

    pieces: tuple[int, ...]


@dataclass(frozen=True)
class OpaqueHost:
    """The host of a URL that is not special, kept as written but encoded."""

    name: str


@dataclass(frozen=True)
class EmptyHost:
    """The empty host, as in ``file:///``."""


Host: TypeAlias = "Domain | IPv4Address | IPv6Address | OpaqueHost | EmptyHost"
EMPTY_HOST = EmptyHost()


def serialize_host(host: Host) -> str:
    """
    Serialize a host the way the standard's host serializer does.

    :param host: The host to serialize.

    :return: Its ASCII form; IPv6 addresses come bracketed.
    """
    if isinstance(host, IPv4Address):
        return ".".join(str((host.value >> shift) & 0xFF) for shift in (24, 16, 8, 0))
    if isinstance(host, IPv6Address):
        return f"[{_serialize_ipv6(host.pieces)}]"
    if isinstance(host, (Domain, OpaqueHost)):
        return host.name
    return ""


def _compressed_piece_index(pieces: tuple[int, ...]) -> int | None:
    """Find the first longest run of two or more zero pieces, per RFC 5952."""
    longest_index: int | None = None
    longest_size = 1
    found_index: int | None = None
    found_size = 0
    for index, piece in enumerate(pieces):
        if piece != 0:
            if found_size > longest_size:
                longest_index, longest_size = found_index, found_size
            found_index, found_size = None, 0
        else:
            if found_index is None:
                found_index = index
            found_size += 1
    if found_size > longest_size:
        return found_index
    return longest_index


def _serialize_ipv6(pieces: tuple[int, ...]) -> str:
    """Serialize eight pieces with the one compression the standard allows."""
    compress = _compressed_piece_index(pieces)
    output = ""
    ignore_zero = False
    for index, piece in enumerate(pieces):
        if ignore_zero and piece == 0:
            continue
        ignore_zero = False
        if compress == index:
            output += "::" if index == 0 else ":"
            ignore_zero = True
            continue
        output += f"{piece:x}"
        if index != 7:
            output += ":"
    return output


# ---------------------------------------------------------------------- IPv4


def _parse_ipv4_number(text: str) -> tuple[int, bool] | None:
    """The standard's IPv4 number parser: a value and whether it was non-decimal."""
    if not text:
        return None
    radix = 10
    non_decimal = False
    if len(text) >= 2 and text[:2] in ("0x", "0X"):
        text, radix, non_decimal = text[2:], 16, True
    elif len(text) >= 2 and text[0] == "0":
        text, radix, non_decimal = text[1:], 8, True
    if not text:
        return 0, True
    digits = "0123456789abcdef"[:radix]
    if any(character.lower() not in digits for character in text):
        return None
    # int() refuses decimal strings over 4,300 digits. Twelve significant digits
    # in any of these radixes already pass 2**32, more than an address holds.
    if len(text.lstrip("0")) > 12:
        return 1 << 48, non_decimal
    return int(text, radix), non_decimal


def ends_in_a_number(text: str) -> bool:
    """
    Whether the last label is a number, which makes the host an IPv4 address.

    :param text: An ASCII domain.

    :return: True when the IPv4 parser, not the domain, decides the host.
    """
    parts = text.split(".")
    if parts[-1] == "":
        if len(parts) == 1:
            return False
        parts.pop()
    last = parts[-1]
    if last and all(character in _ASCII_DIGIT for character in last):
        return True
    return _parse_ipv4_number(last) is not None


def parse_ipv4(text: str, errors: list[str]) -> IPv4Address | None:
    """
    The standard's IPv4 parser.

    :param text: An ASCII domain that ends in a number.
    :param errors: Where validation errors go.

    :return: The address, or None for failure.
    """
    parts = text.split(".")
    if parts[-1] == "":
        errors.append(IPV4_EMPTY_PART)
        if len(parts) > 1:
            parts.pop()
    if len(parts) < 4:
        errors.append(IPV4_TOO_FEW_PARTS)
    if len(parts) > 4:
        errors.append(IPV4_TOO_MANY_PARTS)
        return None
    numbers: list[int] = []
    for part in parts:
        result = _parse_ipv4_number(part)
        if result is None:
            errors.append(IPV4_NON_NUMERIC_PART)
            return None
        if result[1]:
            errors.append(IPV4_NON_DECIMAL_PART)
        numbers.append(result[0])
    if any(number > 255 for number in numbers):
        errors.append(IPV4_OUT_OF_RANGE_PART)
    if any(number > 255 for number in numbers[:-1]):
        return None
    if numbers[-1] >= 256 ** (5 - len(numbers)):
        return None
    value = numbers[-1]
    for counter, number in enumerate(numbers[:-1]):
        value += number * 256 ** (3 - counter)
    return IPv4Address(value)


# ---------------------------------------------------------------------- IPv6


class _FailureError(Exception):
    """Raised inside the IPv6 parser where the standard returns failure."""


class _IPv6Reader:  # pylint: disable=too-few-public-methods
    """The standard's IPv6 parser, one pointer walking the input."""

    def __init__(self, text: str, errors: list[str]) -> None:
        self.text = text
        self.errors = errors
        self.pointer = 0
        self.address = [0] * 8
        self.piece_index = 0
        self.compress: int | None = None

    def _char(self, offset: int = 0) -> str | None:
        position = self.pointer + offset
        return self.text[position] if position < len(self.text) else None

    def _at(self, characters: frozenset[str]) -> bool:
        """Whether the code point at the pointer is one of *characters*."""
        return self.pointer < len(self.text) and self.text[self.pointer] in characters

    def _fail(self, error: str) -> NoReturn:
        self.errors.append(error)
        raise _FailureError

    def read(self) -> IPv6Address:
        """Parse the whole input; failure raises :class:`_FailureError`."""
        if self._char() == ":":
            if self._char(1) != ":":
                self._fail(IPV6_INVALID_COMPRESSION)
            self.pointer += 2
            self.piece_index += 1
            self.compress = self.piece_index
        while self._char() is not None:
            if self._piece():
                break
        return self._finish()

    def _piece(self) -> bool:
        """Read one piece; True when an IPv4 address ended the input."""
        if self.piece_index == 8:
            self._fail(IPV6_TOO_MANY_PIECES)
        if self._char() == ":":
            if self.compress is not None:
                self._fail(IPV6_MULTIPLE_COMPRESSION)
            self.pointer += 1
            self.piece_index += 1
            self.compress = self.piece_index
            return False
        value = length = 0
        while length < 4 and self._at(_ASCII_HEX):
            value = value * 0x10 + int(self.text[self.pointer], 16)
            self.pointer += 1
            length += 1
        if self._char() == ".":
            self._ipv4_tail(length)
            return True
        if self._char() == ":":
            self.pointer += 1
            if self._char() is None:
                self._fail(IPV6_INVALID_CODE_POINT)
        elif self._char() is not None:
            self._fail(IPV6_INVALID_CODE_POINT)
        if length > 1 and value < 0x10 ** (length - 1):
            self.errors.append(IPV6_PIECE_LEADING_ZERO)
        self.address[self.piece_index] = value
        self.piece_index += 1
        return False

    def _ipv4_tail(self, length: int) -> None:
        """Read the IPv4 address that ends the input into the last two pieces."""
        if length == 0:
            self._fail(IPV4_IN_IPV6_INVALID_CODE_POINT)
        self.pointer -= length
        if self.piece_index > 6:
            self._fail(IPV4_IN_IPV6_TOO_MANY_PIECES)
        numbers_seen = 0
        while self._char() is not None:
            if numbers_seen > 0:
                if self._char() == "." and numbers_seen < 4:
                    self.pointer += 1
                else:
                    self._fail(IPV4_IN_IPV6_INVALID_CODE_POINT)
            part = self._ipv4_part()
            self.address[self.piece_index] = (
                self.address[self.piece_index] * 0x100 + part
            )
            numbers_seen += 1
            if numbers_seen in (2, 4):
                self.piece_index += 1
        if numbers_seen != 4:
            self._fail(IPV4_IN_IPV6_TOO_FEW_PARTS)

    def _ipv4_part(self) -> int:
        """Read one decimal part of that address: no leading zero, at most 255."""
        if not self._at(_ASCII_DIGIT):
            self._fail(IPV4_IN_IPV6_INVALID_CODE_POINT)
        part: int | None = None
        while self._at(_ASCII_DIGIT):
            number = int(self.text[self.pointer])
            if part == 0:
                self._fail(IPV4_IN_IPV6_INVALID_CODE_POINT)
            part = number if part is None else part * 10 + number
            if part > 255:
                self._fail(IPV4_IN_IPV6_OUT_OF_RANGE_PART)
            self.pointer += 1
        return part or 0

    def _finish(self) -> IPv6Address:
        """Apply the compression, or refuse too few pieces."""
        if self.compress is not None:
            swaps = self.piece_index - self.compress
            piece_index = 7
            while piece_index != 0 and swaps > 0:
                other = self.compress + swaps - 1
                self.address[piece_index], self.address[other] = (
                    self.address[other],
                    self.address[piece_index],
                )
                piece_index -= 1
                swaps -= 1
        elif self.piece_index != 8:
            self._fail(IPV6_TOO_FEW_PIECES)
        return IPv6Address(tuple(self.address))


def parse_ipv6(text: str, errors: list[str]) -> IPv6Address | None:
    """
    The standard's IPv6 parser.

    :param text: What is between the brackets.
    :param errors: Where validation errors go.

    :return: The address, or None for failure.
    """
    try:
        return _IPv6Reader(text, errors).read()
    except _FailureError:
        return None


# ------------------------------------------------------------ domains, IDNA


#: The two characters UTS #46 keeps where IDNA 2003 folds them to ``ss`` and
#: ``σ``. Folding them was the one conformance case this module failed.
_KEPT_BY_UTS46 = frozenset("ßς")

_PROHIBITED = (
    stringprep.in_table_c12,
    stringprep.in_table_c22,
    stringprep.in_table_c3,
    stringprep.in_table_c4,
    stringprep.in_table_c5,
    stringprep.in_table_c6,
    stringprep.in_table_c7,
    stringprep.in_table_c8,
    stringprep.in_table_c9,
    stringprep.in_table_a1,
)


def _nameprep(label: str) -> str | None:
    """
    RFC 3491 nameprep, keeping ``ß`` and ``ς`` as UTS #46 does.

    Mapping (table B.1 deleted, B.2 case-folded), NFKC under Unicode 3.2, then
    the prohibited-output and bidirectional checks of RFC 3454. Unassigned code
    points are refused, as ToASCII refuses them when AllowUnassigned is false.

    :return: The prepared label, or None if nameprep refuses it.
    """
    mapped = "".join(
        ""
        if stringprep.in_table_b1(character)
        else character
        if character in _KEPT_BY_UTS46
        else stringprep.map_table_b2(character)
        for character in label
    )
    prepared = ucd_3_2_0.normalize("NFKC", mapped)
    if any(check(character) for character in prepared for check in _PROHIBITED):
        return None
    right_to_left = [stringprep.in_table_d1(character) for character in prepared]
    if any(right_to_left) and (
        any(stringprep.in_table_d2(character) for character in prepared)
        or not right_to_left[0]
        or not right_to_left[-1]
    ):
        return None
    return prepared


def _punycode_encode(label: str) -> str:
    """The RFC 3492 punycode of a prepared label, as ASCII text."""
    return label.encode("punycode").decode("ascii")


def _punycode_decode(label: str) -> str:
    """What a punycode string of letters, digits and hyphens encodes."""
    return label.encode("ascii").decode("punycode")


def _to_ascii_label(label: str) -> str | None:
    """Map one non-ASCII label to its ASCII form: nameprep, then punycode."""
    prepared = _nameprep(label)
    if prepared is None or not prepared:
        return None
    if prepared.isascii():
        return prepared
    return "xn--" + _punycode_encode(prepared)


def _relaxed_to_ascii(domain: str) -> str | None:
    """Domain to ASCII with relaxed flags; the stdlib stand-in for UTS #46."""
    labels: list[str] = []
    for label in _IDNA_DOTS.split(domain):
        if not label or label.isascii():
            labels.append(label.lower())
            continue
        converted = _to_ascii_label(label)
        if converted is None:
            return None
        labels.append(converted)
    return ".".join(labels)


def _punycode_label_ok(label: str) -> bool:
    """Whether an ``xn--`` label decodes and re-encodes to itself."""
    try:
        decoded = _punycode_decode(label[4:])
    except UnicodeError:
        return False
    if not decoded or decoded.isascii():
        return False
    return _to_ascii_label(decoded) == label


def _strict_to_ascii_ok(domain: str) -> bool:
    """
    Whether ToASCII with the strict flags would succeed.

    CheckHyphens, UseSTD3ASCIIRules and VerifyDnsLength: letters, digits and
    hyphens only, no hyphen at either end or in positions 3 and 4 of a label
    that is not punycode, labels of 1 to 63 octets and a name of at most 253.
    A failure here is the ``domain-to-ASCII`` validation error, which is how a
    92-character label in a real book's link is reported without failing the
    parse.
    """
    ascii_domain = _relaxed_to_ascii(domain)
    if ascii_domain is None:
        return False
    labels = ascii_domain.split(".")
    if len(labels) > 1 and labels[-1] == "":
        labels.pop()
    if len(".".join(labels)) > 253:
        return False
    for label in labels:
        if not 1 <= len(label) <= 63 or not _LDH.fullmatch(label):
            return False
        if label.startswith("xn--"):
            if not _punycode_label_ok(label):
                return False
            continue
        if label[0] == "-" or label[-1] == "-" or label[2:4] == "--":
            return False
    return True


def parse_domain(domain: str, errors: list[str]) -> str | None:
    """
    The standard's domain parser, with beStrict false as the host parser uses it.

    :param domain: The percent-decoded host.
    :param errors: Where the ``domain-to-ASCII`` validation error goes.

    :return: The ASCII domain, or None for failure.
    """
    if not _strict_to_ascii_ok(domain):
        errors.append(DOMAIN_TO_ASCII)
    if domain.isascii():
        result: str | None = domain.lower()
    else:
        result = _relaxed_to_ascii(domain)
        if result is None:
            return None
    if not result or any(
        character in FORBIDDEN_DOMAIN_CODE_POINTS for character in result
    ):
        return None
    return result
