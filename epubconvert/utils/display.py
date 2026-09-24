"""
Rendering names that came out of a book.

A package name is input, not fact: it is whatever the publisher or the person
who sideloaded the book put on the directory. Anything shown to a user goes
through here first.

Kept in its own module because both the exporter and the planner display names,
and neither should have to import the other to do it.
"""

from __future__ import annotations

import re

#: DEL, C1 and lone surrogates: what :func:`_is_control` flags besides C0.
_UNPRINTABLE_ABOVE_C0 = re.compile("[\x7f-\x9f\ud800-\udfff]")


def printable(name: str) -> str:
    """
    Render a name safe to write to a terminal or a log.

    Package names come from the book, so they are input. A name carrying
    ``ESC[2K\r`` erases and rewrites the very line that reports it, which lets
    a sideloaded book decide what the user reads about the run. The name on
    disk is untouched; only what is displayed changes.

    :param name: The name as it appears on disk.

    :return: The name with C0, DEL, C1 and lone surrogates escaped.

    """
    return "".join(
        char if not _is_control(char) else f"\\x{ord(char):02x}" for char in name
    )


def printable_json(document: str) -> str:
    """
    Render a JSON document safe to print, without changing what it decodes to.

    ``json.dumps`` escapes C0 but, told to keep titles readable with
    ``ensure_ascii=False``, writes DEL, C1 and lone surrogates through as they
    are: a C1 ``CSI`` then steers the terminal, and a surrogate -- which
    ``os.walk`` returns for an undecodable filename -- makes printing the
    document raise UnicodeEncodeError. Each is written as the ``\\uXXXX``
    escape JSON already has for it instead, so a reader decodes the very same
    name and can still open the file. A surrogate that ``os.walk`` hands back
    is always a low one, so no two escaped here recombine into a pair.

    :param document: JSON text, as ``json.dumps`` produced it.

    :return: The same document with those characters escaped.
    """
    # Everything _is_control flags above C0, which json.dumps has escaped.
    return _UNPRINTABLE_ABOVE_C0.sub(
        lambda found: f"\\u{ord(found.group()):04x}", document
    )


def collapse(value: object) -> str:
    """
    Render a value as one line.

    ``usable_title`` trims but leaves internal newlines alone, so a title
    carrying one used to split a note's ``#`` heading and drop its second line
    into the body unguarded; in a CSV it would have started a new row.

    :param value: Whatever the book or the reader supplied.

    :return: The value with its whitespace collapsed to single spaces.
    """
    return " ".join(str(value).split())


def _is_control(char: str) -> bool:
    """
    Report whether a character cannot safely be written out.

    :param char: The character to test.

    :return: True for C0, DEL, C1 and lone surrogates.
    """
    code = ord(char)
    if 0xD800 <= code <= 0xDFFF:
        # A lone surrogate, which os.walk returns for an undecodable filename.
        # It survives every control-character test and then makes the log
        # handler raise UnicodeEncodeError while emitting the record, so the
        # line is lost -- the same names encode_name exists to survive.
        return True
    return code < 0x20 or code == 0x7F or 0x80 <= code <= 0x9F
