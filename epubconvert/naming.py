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

import hashlib
import unicodedata
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

from .spec import PACKAGE_SUFFIX

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime dependency
    from .validate import Package

try:
    import disarm
except ImportError:  # pragma: no cover - exercised by the no-extra install
    disarm = None  # type: ignore[assignment]

#: Platforms ``disarm.sanitize_filename`` knows how to target.
Platform = Literal["universal", "windows", "posix"]

#: Values accepted by ``--portable-names``. Typed, like the planner's closed
#: sets: as a bare str, ``build_policy("romanise")`` type-checked and fell
#: silently through to the romanizing policy.
PortableMode = Literal["strip", "romanize"]
STRIP: PortableMode = "strip"
ROMANIZE: PortableMode = "romanize"
PORTABLE_MODES = (STRIP, ROMANIZE)

#: Values accepted by ``--name-by``.
NameSource = Literal["passthrough", "author-title"]
NAME_PASSTHROUGH: NameSource = "passthrough"
NAME_AUTHOR_TITLE: NameSource = "author-title"
NAME_SOURCES = (NAME_PASSTHROUGH, NAME_AUTHOR_TITLE)

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


def encode_name(text: str) -> bytes:
    """
    Encode a filename for length measurement, surrogates included.

    ``os.walk`` hands back undecodable filenames with surrogate escapes, and a
    plain ``str.encode("utf-8")`` raises on those. It happened during planning,
    outside any handler, so one badly named directory on a network share aborted
    the whole run with a traceback.

    :param text: The filename to measure.

    :return: Its UTF-8 bytes.
    """
    return text.encode("utf-8", errors="surrogatepass")


def truncate_bytes(text: str, limit: int) -> str:
    """
    Trim text to at most *limit* UTF-8 bytes without splitting a character.

    :param text: The string to trim.
    :param limit: Maximum length in bytes.

    :return: The trimmed string.
    """
    encoded = encode_name(text)
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="surrogatepass")


def split_extension(name: str) -> tuple[str, str]:
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
    stem, extension = split_extension(name)
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

    budget = max_bytes - len(encode_name(extension))
    if budget < 1:
        # An extension that alone exceeds the budget leaves nothing worth
        # preserving; clamp the whole name and accept the loss.
        return truncate_bytes(f"{stem}{extension}", max_bytes)

    if len(encode_name(stem)) > budget:
        stem = truncate_bytes(stem, budget).rstrip(" .") or "_"

    return f"{stem}{extension}"


#: Hex characters in a collision marker. Four bytes of digest distinguishes the
#: dozens of books that ever collide with room to spare, and stays short enough
#: to read past.
DISAMBIGUATOR_CHARS = 8


def disambiguator(identifier: str) -> str:
    """
    Render a stable marker for a book that has to share a name.

    Numbering colliding books by their position meant adding one book renamed
    the others, because a position describes the group rather than the book. A
    digest of the book's own identifier describes only the book, so the marker
    holds still while the library changes around it.

    The digest rather than the identifier itself: real values run to 45
    characters and carry ``:`` and ``/``, which a filename cannot.

    :param identifier: The book's canonical ``dc:identifier``.

    :return: A short alphanumeric marker.
    """
    digest = hashlib.blake2s(
        identifier.encode("utf-8"), digest_size=DISAMBIGUATOR_CHARS // 2
    )
    return digest.hexdigest()


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

    #: Whether :meth:`filename` needs the parsed package document. The planner
    #: skips the per-package read for policies that do not, which is what keeps
    #: a default rerun over thousands of books free of any source-side open.
    needs_metadata: bool

    def filename(self, package_name: str, metadata: Package | None = None) -> str:
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
    needs_metadata = False

    def filename(self, package_name: str, metadata: Package | None = None) -> str:
        """Return *package_name* unchanged."""
        del metadata
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
    needs_metadata = False

    def __init__(self, separator: str = " ") -> None:
        self.separator = separator

    def filename(self, package_name: str, metadata: Package | None = None) -> str:
        """Return a filename safe to copy to any common filesystem."""
        del metadata
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
    needs_metadata = False

    def __init__(self, platform: Platform = "universal", separator: str = " ") -> None:
        if disarm is None:
            raise PortableNamesUnavailableError(PORTABLE_HINT)
        self.platform: Platform = platform
        self.separator = separator

    def filename(self, package_name: str, metadata: Package | None = None) -> str:
        """
        Return a filename safe to copy to *platform*.

        The extension is split off before sanitizing and reattached after, for
        the same reason :func:`strip_unsafe` does it: ``disarm`` cleans the
        whole string, so a title made entirely of illegal characters empties
        the stem and takes the separating dot with it -- ``?.epub`` becomes
        ``epub``. The output directory globs ``*.epub`` to decide what is
        already exported, so that book is re-converted on every run for ever.
        """
        del metadata
        assert disarm is not None  # noqa: S101 - guarded in __init__
        stem, extension = split_extension(package_name)
        cleaned = disarm.sanitize_filename(
            stem, separator=self.separator, platform=self.platform
        )
        cleaned = cleaned.strip(" .") or "_"
        if not extension:
            # Clamped even here: the class declares max_bytes, and
            # planning.suffixed trusts that declaration.
            return truncate_bytes(cleaned, MAX_FILENAME_BYTES).rstrip(" .") or "_"
        budget = MAX_FILENAME_BYTES - len(encode_name(extension))
        if len(encode_name(cleaned)) > budget:
            cleaned = truncate_bytes(cleaned, max(budget, 1)).rstrip(" .") or "_"
        return f"{cleaned}{extension}"

    def identity(self, filename: str) -> str:
        """Return a case- and accent-insensitive key for *filename*."""
        assert disarm is not None  # noqa: S101 - guarded in __init__
        return disarm.catalog_key(filename)


class MetadataNaming:
    """
    Name a book after what the book says about itself.

    Every other policy derives the name from the package directory, which is
    already a valid filename and already unique on disk. This one derives it
    from ``dc:title`` and ``dc:creator``, which are neither, so the composed
    name is always sanitized and clamped even when the wrapped policy would
    not bother.

    The author is ``creator_sort`` when the book supplies a sort name in either
    EPUB dialect, and ``creator`` **verbatim** otherwise. It is never
    rearranged: in a surveyed 2,805-book library 26% of books supply no sort
    name, and among those the raw ``dc:creator`` text is sometimes already
    inverted (``Patterson, James``), so a split-on-last-space rule would turn
    it into ``James, Patterson``. A shelf therefore mixes both conventions.
    That is visible and correct, where guessing would be invisible and wrong.

    Books with no creator are named by title alone rather than falling back to
    the directory name, because in the same library 77% of directory names are
    already the title.

    :param inner: Policy supplying the sanitizing and the identity. Defaults to
        the standard-library :class:`StripNaming`, which is what makes a
        generated name safe to write.
    """

    label = "author-title"
    max_bytes = MAX_FILENAME_BYTES
    needs_metadata = True

    def __init__(self, inner: NamingPolicy | None = None) -> None:
        self.inner: NamingPolicy = StripNaming() if inner is None else inner

    def filename(self, package_name: str, metadata: Package | None = None) -> str:
        """
        Return ``Author - Title.epub``, falling back as the metadata allows.

        :param package_name: The package directory name, used when the book
            declares no title or could not be parsed at all.
        :param metadata: The parsed package document, or None if it could not
            be read.

        :return: The name to write this book under.
        """
        composed = compose_metadata_name(package_name, metadata)
        return self.inner.filename(composed)

    def identity(self, filename: str) -> str:
        """Return the wrapped policy's key for *filename*."""
        return self.inner.identity(filename)


def compose_metadata_name(package_name: str, metadata: Package | None) -> str:
    """
    Build the raw ``Author - Title`` name, before any sanitizing.

    Kept separate from the policy so the composition rules can be tested and
    reasoned about without a filesystem in the way.

    :param package_name: Fallback when the book declares no title.
    :param metadata: The parsed package document, or None.

    :return: A candidate filename, extension included. Not yet safe to write.
    """
    if metadata is None or not metadata.title:
        return package_name

    _, extension = split_extension(package_name)
    title = metadata.title.strip()
    author = (metadata.creator_sort or metadata.creator or "").strip()
    stem = f"{author} - {title}" if author else title
    return f"{stem}{extension or PACKAGE_SUFFIX}"


def build_policy(
    portable: PortableMode | None, name_by: NameSource = NAME_PASSTHROUGH
) -> NamingPolicy:
    """
    Select a naming policy.

    The two settings are orthogonal and compose. ``--name-by`` decides where
    the name comes from; ``--portable-names`` decides how it is cleaned. Asking
    for author-title names on a Kindle-bound shelf wants both.

    :param portable: ``None`` for passthrough, ``"strip"`` for the standard
        library policy, or ``"romanize"`` for the ``disarm`` one.
    :param name_by: ``"passthrough"`` to name after the package directory, or
        ``"author-title"`` to name after the package document.

    :return: The policy to hand to the exporter.

    :raises PortableNamesUnavailableError: If romanizing was requested but
        ``disarm`` is not installed.
    """
    if portable is None:
        inner: NamingPolicy = PassthroughNaming()
    elif portable == STRIP:
        inner = StripNaming()
    else:
        inner = PortableNaming()

    if name_by == NAME_AUTHOR_TITLE:
        # Passthrough imposes no cleaning and no byte budget, because its names
        # come from the filesystem and are already valid. A composed name is
        # not, so it always gets the standard-library cleaning at minimum.
        return MetadataNaming(None if portable is None else inner)
    return inner
