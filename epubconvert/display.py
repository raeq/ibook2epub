"""
Rendering names that came out of a book.

A package name is input, not fact: it is whatever the publisher or the person
who sideloaded the book put on the directory. Anything shown to a user goes
through here first.

Kept in its own module because both the exporter and the planner display names,
and neither should have to import the other to do it.
"""

from __future__ import annotations


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
