"""
Percent-encoding, and the UTF-8 steps the URL Standard rests it on.

The URL Standard defines percent-encoding over UTF-8 bytes (section 1.3 of
https://url.spec.whatwg.org/, read at commit 8e14777 of 2026-09-10) and borrows
UTF-8 encode and UTF-8 decode without BOM from the Encoding Standard. This is
the one place the URL modules turn text into bytes or bytes into text, and none
of it can raise: text is made a scalar value string first, so a lone surrogate
becomes U+FFFD rather than an error, and decoding replaces what is malformed.
"""

from __future__ import annotations

import re

__all__ = [
    "C0_CONTROL_PERCENT_ENCODE_SET",
    "FRAGMENT_PERCENT_ENCODE_SET",
    "PATH_PERCENT_ENCODE_SET",
    "QUERY_PERCENT_ENCODE_SET",
    "SPECIAL_QUERY_PERCENT_ENCODE_SET",
    "USERINFO_PERCENT_ENCODE_SET",
    "is_hex_pair_at",
    "percent_decode",
    "percent_decode_string",
    "to_scalar_value_string",
    "utf8_decode_without_bom",
    "utf8_encode",
    "utf8_percent_encode",
    "utf8_percent_encode_code_point",
]

_ASCII_HEX = frozenset("0123456789abcdefABCDEF")
_SURROGATE = re.compile("[\ud800-\udfff]")

# The percent-encode sets. Every set also holds all code points above U+007E;
# that half is a comparison in utf8_percent_encode_code_point, not a set.
C0_CONTROL_PERCENT_ENCODE_SET = frozenset(chr(point) for point in range(0x20)) | {
    "\x7f"
}
FRAGMENT_PERCENT_ENCODE_SET = C0_CONTROL_PERCENT_ENCODE_SET | frozenset(' "<>`')
QUERY_PERCENT_ENCODE_SET = C0_CONTROL_PERCENT_ENCODE_SET | frozenset(' "#<>')
SPECIAL_QUERY_PERCENT_ENCODE_SET = QUERY_PERCENT_ENCODE_SET | {"'"}
PATH_PERCENT_ENCODE_SET = QUERY_PERCENT_ENCODE_SET | frozenset("?^`{}")
USERINFO_PERCENT_ENCODE_SET = PATH_PERCENT_ENCODE_SET | frozenset("/:;=@[\\]|")


def to_scalar_value_string(text: str) -> str:
    """
    The Infra Standard's conversion to a scalar value string.

    :param text: Any string.

    :return: The string with each lone surrogate replaced by U+FFFD, which
        UTF-8 can represent.
    """
    return _SURROGATE.sub("\ufffd", text)


def utf8_encode(text: str) -> bytes:
    """
    The Encoding Standard's UTF-8 encode, which never raises.

    :param text: Any string; a lone surrogate is encoded as U+FFFD.

    :return: Its UTF-8 bytes.
    """
    return to_scalar_value_string(text).encode("utf-8")


def utf8_decode_without_bom(data: bytes) -> str:
    """
    The Encoding Standard's UTF-8 decode without BOM, which never raises.

    A leading byte order mark is kept as U+FEFF and a malformed sequence
    becomes U+FFFD.

    :param data: The bytes to decode.

    :return: The text.
    """
    return data.decode("utf-8", "replace")


def utf8_percent_encode_code_point(code_point: str, encode_set: frozenset[str]) -> str:
    """
    UTF-8 percent-encode a code point using a percent-encode set.

    :param code_point: One code point.
    :param encode_set: The ASCII code points to encode. Everything above U+007E
        is encoded as well, as in every set the standard defines.

    :return: The code point, or its UTF-8 bytes percent-encoded.
    """
    if code_point > "\x7e" or code_point in encode_set:
        return "".join(f"%{byte:02X}" for byte in utf8_encode(code_point))
    return code_point


def utf8_percent_encode(text: str, encode_set: frozenset[str]) -> str:
    """
    UTF-8 percent-encode a string using a percent-encode set.

    :param text: The text to encode.
    :param encode_set: As for :func:`utf8_percent_encode_code_point`.

    :return: The encoded text.
    """
    return "".join(
        utf8_percent_encode_code_point(character, encode_set) for character in text
    )


def percent_decode(data: bytes) -> bytes:
    """
    Percent-decode a byte sequence; a ``%`` not followed by two hex digits stays.

    :param data: The bytes to decode.

    :return: The decoded bytes.
    """
    output = bytearray()
    index = 0
    length = len(data)
    while index < length:
        byte = data[index]
        if byte == 0x25 and _is_hex_pair(data, index + 1):
            output.append(int(data[index + 1 : index + 3], 16))
            index += 3
            continue
        output.append(byte)
        index += 1
    return bytes(output)


def percent_decode_string(text: str) -> str:
    """
    Percent-decode a string, as the URL Standard section 1.3 defines it.

    UTF-8 encode, percent-decode the bytes, then UTF-8 decode without a BOM.
    Written here rather than at the two call sites because this module is the
    one place the URL modules turn text into bytes or bytes into text, and the
    surrogate policy that choice implies should be stated once.

    :param text: The text to decode.

    :return: The decoded text.
    """
    if "%" not in text and text.isascii():
        # The round trip cannot change plain ASCII without a "%": utf8_encode
        # only rewrites lone surrogates and percent_decode only "%" escapes,
        # and this rules out both. Worth the test because the reference checker
        # runs it per path segment of every reference -- 80,146 times on a
        # synthetic 200-chapter book -- and member names are almost all this.
        return text
    return utf8_decode_without_bom(percent_decode(utf8_encode(text)))


def _is_hex_pair(data: bytes, start: int) -> bool:
    """Whether two ASCII hex digits sit at *start* in *data*."""
    pair = data[start : start + 2]
    return len(pair) == 2 and all(chr(byte) in _ASCII_HEX for byte in pair)


def is_hex_pair_at(text: str, start: int) -> bool:
    """
    Whether two ASCII hex digits sit at *start* in *text*.

    :param text: The text to look in.
    :param start: Where the pair would begin: just after a ``%``, which the pair
        makes a percent-encoded byte.

    :return: True for a pair of hex digits.
    """
    pair = text[start : start + 2]
    return len(pair) == 2 and all(character in _ASCII_HEX for character in pair)
