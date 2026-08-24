"""
Output naming policies.

Two separate decisions are made about every exported book: what to call the
file on disk, and how to decide whether that book has already been exported.
The default policy answers both with the package directory name itself, which
needs no dependencies.

Two portable policies rewrite names so they survive a copy to Windows, exFAT
or a Kindle:

``strip``
    Standard library only. Replaces the characters those filesystems reject,
    handles reserved device names, and clamps the result to a byte budget.
    Nearly loss-free, so it is what a bare ``--portable-names`` selects.

``romanize``
    Uses ``disarm``. Everything ``strip`` does, plus transliteration and
    accent- and case-insensitive identity, at the cost of turning
    ``こころ.epub`` into ``kokoro.epub``.

The identity is deliberately derived from the *sanitized filename* rather than
from the source directory name. That keeps the output directory the sole
record of completed work: the identity of an already-exported book can be
recomputed by reading its filename back off disk, with no state file.
"""

from __future__ import annotations

import unicodedata
from typing import Literal, Protocol, runtime_checkable

try:
    import disarm
except ImportError:  # pragma: no cover - exercised by the no-extra install
    disarm = None  # type: ignore[assignment]

#: Platforms ``disarm.sanitize_filename`` knows how to target.
Platform = Literal["universal", "windows", "posix"]

#: Values accepted by ``--portable-names``.
STRIP = "strip"
ROMANIZE = "romanize"
PORTABLE_MODES = (STRIP, ROMANIZE)

PORTABLE_HINT = (
    f"--portable-names={ROMANIZE} needs the 'disarm' package: "
    f"pip install 'ibook2epub[portable]'"
)

#: Characters that are illegal on Windows and exFAT but legal on APFS and ext4.
WINDOWS_ILLEGAL = '<>:"/\\|?*'

#: Names Windows reserves as devices, whatever the extension.
RESERVED_STEMS = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{digit}" for digit in range(1, 10)}
    | {f"LPT{digit}" for digit in range(1, 10)}
)

#: The per-component limit on ext4, exFAT and APFS.
MAX_FILENAME_BYTES = 255


class PortableNamesUnavailableError(RuntimeError):
    """Raised when portable naming is requested but ``disarm`` is not installed."""


def filesystem_key(filename: str) -> str:
    """
    Return the key by which the *filesystem* will consider two names equal.

    A naming policy decides whether two books are the same book. This decides
    whether two names are the same *file*, which is a different question and
    the one that governs whether a write destroys another write. macOS
    formats APFS and HFS+ case-insensitively by default, so ``Book.epub`` and
    ``BOOK.epub`` are one file there while :class:`PassthroughNaming` gives
    them distinct identities. Two workers then replaced onto the same path: one
    book was lost, the run reported both as exported, and no rerun ever
    converged.

    Normalization is applied as well as case folding, because a name that has
    lived on HFS+ is stored decomposed while the same name typed fresh is
    composed.

    :param filename: The candidate output filename.

    :return: A key equal for any two names the filesystem cannot tell apart.
    """
    return unicodedata.normalize("NFC", filename).casefold()


def truncate_bytes(text: str, limit: int) -> str:
    """
    Trim text to at most *limit* UTF-8 bytes without splitting a character.

    :param text: The string to trim.
    :param limit: Maximum length in bytes.

    :return: The trimmed string.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="ignore")


def _split_extension(name: str) -> tuple[str, str]:
    """
    Split a filename into its stem and its extension.

    The split happens *before* any cleaning. Cleaning the whole name and
    splitting afterwards lets the trailing-dot strip eat the dot that
    separates the extension, whenever the stem cleans away to nothing: a book
    titled ``?`` gives ``?.epub`` -> ``.epub`` -> ``epub``. That name has no
    extension for ``*.epub`` to match, so the output directory stops
    recognising the book as exported and every rerun exports it again.

    :param name: The filename to split.

    :return: The stem, and the extension including its leading dot. The
        extension is empty when the name has none.
    """
    stem, dot, extension = name.rpartition(".")
    if not dot or not extension:
        return name, ""
    return stem, f".{extension}"


def _replace_illegal(text: str, separator: str) -> str:
    """
    Swap out the characters other filesystems reject and collapse whitespace.

    :param text: The text to clean.
    :param separator: Replacement for illegal characters.

    :return: The cleaned text.
    """
    replaced = "".join(
        separator if char in WINDOWS_ILLEGAL or ord(char) < 32 else char
        for char in text
    )
    return " ".join(replaced.split())


def strip_unsafe(
    name: str, *, separator: str = " ", max_bytes: int = MAX_FILENAME_BYTES
) -> str:
    """
    Make a filename safe for Windows, exFAT and ext4 without romanizing it.

    Non-Latin scripts are preserved: every filesystem in play stores them
    correctly, so the portability problem is the ASCII metacharacters, the
    reserved device names, and the length — not the script.

    The extension is carried through untouched by the stem's cleaning, so a
    title made entirely of illegal characters yields ``_.epub`` rather than a
    name the ``*.epub`` glob can no longer see.

    :param name: The candidate filename.
    :param separator: Replacement for illegal characters.
    :param max_bytes: Byte budget for the result.

    :return: A filename safe to write on any of the target filesystems.
    """
    stem, extension = _split_extension(name)
    stem = _replace_illegal(stem, separator)
    extension = _replace_illegal(extension, separator)

    # Windows silently drops a trailing dot or space, so a name ending in one
    # would not round-trip. Only the stem is stripped: stripping the assembled
    # name would take the separating dot with it.
    stem = stem.strip(" .")
    if extension:
        extension = f".{extension.strip(' .')}"

    if stem.upper() in RESERVED_STEMS:
        stem = f"_{stem}"

    if not stem:
        stem = "_"

    budget = max_bytes - len(extension.encode("utf-8"))
    if budget < 1:
        # An extension that alone exceeds the budget leaves nothing worth
        # preserving; clamp the whole name and accept the loss.
        return truncate_bytes(f"{stem}{extension}", max_bytes)

    if len(stem.encode("utf-8")) > budget:
        stem = truncate_bytes(stem, budget).rstrip(" .") or "_"

    return f"{stem}{extension}"


@runtime_checkable
class NamingPolicy(Protocol):
    """Maps a package directory name to an output filename and an identity."""

    #: Short name used in log output.
    label: str

    #: Byte budget for a generated name, or 0 for no clamping. Passthrough
    #: deliberately imposes none: its names come from the source directory and
    #: are already valid, and truncating one would break the round trip that
    #: rerun safety depends on.
    max_bytes: int

    def filename(self, package_name: str) -> str:
        """Return the name to write this package under."""

    def identity(self, filename: str) -> str:
        """Return the key that decides whether this book is already exported."""


class PassthroughNaming:
    """
    Use the package directory name unchanged.

    This is the default and has no dependencies. Identity is the filename, so
    two books are the same book only if their names match exactly.
    """

    label = "passthrough"
    max_bytes = 0

    def filename(self, package_name: str) -> str:
        """Return *package_name* unchanged."""
        return package_name

    def identity(self, filename: str) -> str:
        """Return *filename* unchanged."""
        return filename


class StripNaming:
    """
    Remove characters other filesystems reject, preserving the script.

    Standard library only. Identity folds case, so ``The Hobbit.epub`` and
    ``THE HOBBIT.epub`` are one book; unlike :class:`PortableNaming` it does
    not fold accents, because doing so without transliterating is not
    something the standard library offers.

    :param separator: Replacement for illegal characters.
    """

    label = "portable(strip)"
    max_bytes = MAX_FILENAME_BYTES

    def __init__(self, separator: str = " ") -> None:
        self.separator = separator

    def filename(self, package_name: str) -> str:
        """Return a filename safe to copy to any common filesystem."""
        return strip_unsafe(package_name, separator=self.separator)

    def identity(self, filename: str) -> str:
        """Return a case-insensitive key for *filename*."""
        return filename.casefold()


class PortableNaming:
    """
    Rewrite names for portability using ``disarm``, including transliteration.

    Spaces are preserved (``separator=" "``) because they are legal on every
    target filesystem. Note that ``disarm.sanitize_filename`` transliterates,
    so non-Latin titles are romanized — ``こころ.epub`` becomes
    ``kokoro.epub``. That is lossy, which is why :class:`StripNaming` is what
    a bare ``--portable-names`` selects.

    :param platform: Target platform: ``universal``, ``windows`` or ``posix``.
    :param separator: Replacement for illegal characters.

    :raises PortableNamesUnavailableError: If ``disarm`` is not installed.
    """

    label = "portable(romanize)"
    max_bytes = MAX_FILENAME_BYTES

    def __init__(self, platform: Platform = "universal", separator: str = " ") -> None:
        if disarm is None:
            raise PortableNamesUnavailableError(PORTABLE_HINT)
        self.platform: Platform = platform
        self.separator = separator

    def filename(self, package_name: str) -> str:
        """
        Return a filename safe to copy to *platform*.

        The extension is split off before sanitizing and reattached after, for
        the same reason :func:`strip_unsafe` does it: ``disarm`` cleans the
        whole string, so a title made entirely of illegal characters empties
        the stem and takes the separating dot with it -- ``?.epub`` becomes
        ``epub``. The output directory globs ``*.epub`` to decide what is
        already exported, so that book is re-converted on every run for ever.
        """
        assert disarm is not None  # noqa: S101 - guarded in __init__
        stem, extension = _split_extension(package_name)
        cleaned = disarm.sanitize_filename(
            stem, separator=self.separator, platform=self.platform
        )
        cleaned = cleaned.strip(" .") or "_"
        if not extension:
            return cleaned
        budget = MAX_FILENAME_BYTES - len(extension.encode("utf-8"))
        if len(cleaned.encode("utf-8")) > budget:
            cleaned = truncate_bytes(cleaned, max(budget, 1)).rstrip(" .") or "_"
        return f"{cleaned}{extension}"

    def identity(self, filename: str) -> str:
        """Return a case- and accent-insensitive key for *filename*."""
        assert disarm is not None  # noqa: S101 - guarded in __init__
        return disarm.catalog_key(filename)


def build_policy(portable: str | None) -> NamingPolicy:
    """
    Select a naming policy.

    :param portable: ``None`` for passthrough, ``"strip"`` for the standard
        library policy, or ``"romanize"`` for the ``disarm`` one.

    :return: The policy to hand to the exporter.

    :raises PortableNamesUnavailableError: If romanizing was requested but
        ``disarm`` is not installed.
    """
    if portable is None:
        return PassthroughNaming()
    if portable == STRIP:
        return StripNaming()
    return PortableNaming()
