"""
Rendering names that came out of a book.

A package name is input, not fact: it is whatever the publisher or the person
who sideloaded the book put on the directory. Anything shown to a user goes
through here first.

Kept in its own module because both the exporter and the planner display names,
and neither should have to import the other to do it. What a report prints on
standard output goes out through :func:`emit` here too.
"""

from __future__ import annotations

import os
import re
import sys

#: Unicode's bidirectional formatting characters: ALM, LRM and RLM, the
#: embeddings and overrides, and the isolates. None is a control character, so
#: each went to the terminal as it was, and U+202E reverses the rest of the
#: line it is printed on: a name could make the line reporting it read as
#: something else.
_BIDI_CLASS = "\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069"

#: DEL, C1, lone surrogates and the bidi controls: what :func:`_is_control`
#: flags besides C0.
_UNPRINTABLE_ABOVE_C0 = re.compile(f"[\x7f-\x9f\ud800-\udfff{_BIDI_CLASS}]")
_BIDI = re.compile(f"[{_BIDI_CLASS}]")


def printable(name: str, *, keep_bidi: bool = False) -> str:
    """
    Render a name safe to write to a terminal or a log.

    Package names come from the book, so they are input. A name carrying
    ``ESC[2K\r`` erases and rewrites the very line that reports it, which lets
    a sideloaded book decide what the user reads about the run. The name on
    disk is untouched; only what is displayed changes.

    :param name: The name as it appears on disk.
    :param keep_bidi: Leave the bidi controls as they are, for text that is
        data rather than a report: in a Hebrew or Arabic title a right-to-left
        mark is part of how it is written, and escaping it corrupts the title
        wherever the file is imported.

    :return: The name with C0, DEL, C1, lone surrogates and bidi controls
        escaped: ``\\xNN`` up to U+00FF and ``\\uNNNN`` above it, since a
        ``\\x`` escape takes only two digits.
    """
    return "".join(
        _escaped(char)
        if _is_control(char) and not (keep_bidi and _BIDI.match(char))
        else char
        for char in name
    )


def _escaped(char: str) -> str:
    """
    Spell one character as an escape.

    :param char: The character.

    :return: ``\\x`` and two hex digits, or ``\\u`` and four above U+00FF.
    """
    code = ord(char)
    return f"\\x{code:02x}" if code <= 0xFF else f"\\u{code:04x}"


def printable_json(document: str) -> str:
    """
    Render a JSON document safe to print, without changing what it decodes to.

    ``json.dumps`` escapes C0 but, told to keep titles readable with
    ``ensure_ascii=False``, writes DEL, C1, lone surrogates and the bidi
    controls through as they are: a C1 ``CSI`` then steers the terminal, a
    U+202E reverses the line, and a surrogate -- which
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


def emit(text: str) -> None:
    """
    Print a report's text on standard output, and stop quietly if nobody reads.

    ``--list | head`` is how a long listing gets read, and the pipe closing
    ended it in a BrokenPipeError traceback and exit 1. The reader closing it
    is saying they have seen enough, not reporting an error, so the run goes
    on to its own exit code: a ``--verify`` still exits 7 for a damaged shelf.

    :param text: What to print; a newline is added.
    """
    try:
        print(text, flush=True)
    except BrokenPipeError:
        # Standard output is pointed at the null device, so neither the next
        # line nor the interpreter's flush at exit can raise again.
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        os.close(devnull)


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

    :return: True for C0, DEL, C1, lone surrogates and the bidi controls.
    """
    if _BIDI.match(char):
        return True
    code = ord(char)
    if 0xD800 <= code <= 0xDFFF:
        # A lone surrogate, which os.walk returns for an undecodable filename.
        # It survives every control-character test and then makes the log
        # handler raise UnicodeEncodeError while emitting the record, so the
        # line is lost -- the same names encode_name exists to survive.
        return True
    return code < 0x20 or code == 0x7F or 0x80 <= code <= 0x9F
