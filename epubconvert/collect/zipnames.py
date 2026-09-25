"""
Naming the members of a zip archive as OCF names them, the same on every Python.

The zip format names a member in cp437 unless flag bit 11 says UTF-8, and
Info-ZIP and WinZip each state a UTF-8 name their own way; zipfile reads
neither, and 3.10 and 3.14 disagree about the one field 3.14 does read. What
every reader of an archive calls a member, and whether it opens the archive at
all, is decided here, from the bytes the archive holds.
"""

from __future__ import annotations

import warnings
import zlib
from collections.abc import Iterator
from typing import IO
from zipfile import BadZipFile, ZipFile, ZipInfo

from ..utils.spec import MIMETYPE_BYTES

#: General-purpose flag bit 11: the member's name is encoded in UTF-8.
_UTF8_NAME = 0x800

#: The Info-ZIP Unicode Path extra field's header ID (APPNOTE 4.6.9).
_UNICODE_PATH = 0x7075


def member_name(info: ZipInfo) -> str:
    """
    Name a member of an epub as OCF names it, in UTF-8, flagged or not.

    The zip format reads a name without flag bit 11 as cp437, and zipfile does
    just that. Info-ZIP -- the ``zip -X0``, ``zip -rX9`` recipe for making an
    epub by hand -- writes the UTF-8 bytes of ``第1章.xhtml`` without the flag,
    so zipfile handed back ``τ¼¼1τ½á.xhtml``: --verify called a sound book's
    chapter missing, and a refresh rewrote it under that name, flagged UTF-8,
    renaming it for good. OCF requires UTF-8 names, so an unflagged name is
    read as UTF-8 when its bytes are UTF-8. One that is not was never an epub
    name, and keeps the cp437 reading. ``ZipFile(metadata_encoding=)`` would
    say this once per archive, but it arrived in Python 3.11.

    WinZip, and Info-ZIP on Windows, store such a name unflagged in the OEM
    code page and its UTF-8 name in a Unicode Path extra field (0x7075). That
    field names an unflagged member when its CRC-32 matches the name's bytes,
    as Info-ZIP's unzip reads it: see :func:`_unicode_path`. Never to or from
    ``mimetype``, though: OCF fixes those bytes at offset 30, which a reader
    sniffing the file checks, so a field saying ``mimetype`` passed --verify
    on a first member stored as ``XXXXXXXX``, and one saying ``zzz`` of the
    real ``mimetype`` had a refresh rename it ``zzz``.

    Read from ``orig_filename``, the directory's own name decoded, and never
    ``filename``: Python 3.14 replaces that with the field's name even for a
    flagged name, where 3.10 ignores the field, so one book's member had a
    different name on each. The name ends at a NUL, as zipfile ends
    ``filename``.

    :param info: The member, as the archive's directory describes it.

    :return: Its name.
    """
    name = info.orig_filename.partition("\0")[0]
    raw = _name_bytes(info)
    if raw is None or info.flag_bits & _UTF8_NAME:
        return name
    # The name a Unicode Path field vouches for, if one does, else its own
    # bytes; either is the name only if it is UTF-8.
    field = _unicode_path(info.extra, raw)
    if MIMETYPE_BYTES in {named.partition(b"\0")[0] for named in (raw, field or b"")}:
        field = None  # Never to or from mimetype, as above.
    for stated in (field, raw):
        try:
            if stated is not None:
                return stated.partition(b"\0")[0].decode("utf-8")
        except UnicodeError:
            continue
    return name


def _name_bytes(info: ZipInfo) -> bytes | None:
    """
    Recover the name bytes an archive's directory holds for a member.

    :param info: The member, as the archive's directory describes it.

    :return: The bytes zipfile decoded: as UTF-8 when flagged, else as cp437,
        which maps every byte. None for a ZipInfo made here, not read from an
        archive.
    """
    encoding = "utf-8" if info.flag_bits & _UTF8_NAME else "cp437"
    try:
        return info.orig_filename.encode(encoding)
    except UnicodeError:
        return None


def _unicode_path(extra: bytes, raw: bytes) -> bytes | None:
    """
    Find the UTF-8 name a Unicode Path extra field gives a member's bytes.

    Version 1, then the CRC-32 of the name bytes the header holds, then the
    name in UTF-8 (APPNOTE 4.6.9). A field whose CRC differs was written for
    another name -- a program renamed the member and left the field behind --
    and is ignored, as is one whose name is empty, which Info-ZIP writes to
    say the header's own bytes are the UTF-8 name.

    :param extra: The member's extra fields, from the central directory.
    :param raw: The name bytes the directory holds.

    :return: The name's bytes, or None when no field vouches for *raw*.
    """
    for body in _unicode_fields(extra):
        if _vouches(body, raw):
            return body[5:] or None
    return None


def _vouches(body: bytes, raw: bytes) -> bool:
    """Whether a Unicode Path field's body is version 1 and names *raw*."""
    return (
        len(body) >= 5
        and body[0] == 1
        and int.from_bytes(body[1:5], "little") == zlib.crc32(raw)
    )


def _unicode_fields(extra: bytes) -> Iterator[bytes]:
    """
    Walk a member's extra fields, yielding each Unicode Path field's body.

    :param extra: The member's extra fields, from the central directory.

    :return: The bodies, in order.
    """
    while len(extra) >= 4:
        kind = int.from_bytes(extra[:2], "little")
        size = int.from_bytes(extra[2:4], "little")
        body, extra = extra[4 : 4 + size], extra[4 + size :]
        if kind == _UNICODE_PATH:
            yield body


def open_archive(handle: IO[bytes]) -> ZipFile:
    """
    Open an untrusted archive for reading, the same way on every Python.

    zipfile 3.14 refuses an archive whose Unicode Path field is shorter than
    its version and CRC, or vouches for a member's name in bytes that are not
    UTF-8, where 3.10 opens it and :func:`member_name` fell back without a
    word: --verify exited 7 on one and 0 on the other. So it is refused here
    on every Python, in 3.14's words. 3.14 also warns of an empty field on
    stderr, above the report; only zipfile's own UserWarnings are silenced,
    and only while it reads the directory.

    :param handle: The file, open for reading.

    :return: The open archive.

    :raises BadZipFile: If a Unicode Path field is one 3.14 refuses, and
        whatever else ``ZipFile`` raises.
    """
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=UserWarning, module=r"zipfile\Z")
        archive = ZipFile(handle)  # pylint: disable=consider-using-with
    for info in archive.infolist():
        refused = _corrupt_unicode_path(info)
        if refused is not None:
            archive.close()
            raise BadZipFile(refused)
    return archive


def _corrupt_unicode_path(info: ZipInfo) -> str | None:
    """
    Say how a member's Unicode Path field is corrupt, as zipfile 3.14 says it.

    :param info: The member, as the archive's directory describes it.

    :return: What is wrong, or None when every such field is sound.
    """
    raw = _name_bytes(info)
    refused = "Corrupt unicode path extra field (0x7075)"
    for body in _unicode_fields(info.extra):
        if len(body) < 5:
            return refused
        if raw is not None and _vouches(body, raw):
            try:
                body[5:].decode("utf-8")
            except UnicodeError:
                return f"{refused}: invalid utf-8 bytes"
    return None
