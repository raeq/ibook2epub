"""
Output naming policies.

Two separate decisions are made about every exported book: what to call the
file on disk, and how to decide whether that book has already been exported.
The default policy answers both with the package directory name itself, which
needs no dependencies.

The portable policy answers them with ``disarm``: :func:`sanitize_filename`
produces a name that survives a copy to Windows, exFAT or a Kindle, and
:func:`catalog_key` produces a case- and accent-insensitive identity so the
same book stored under ``The Hobbit.epub`` and ``THE HOBBIT.epub`` is exported
once rather than twice.

The identity is deliberately derived from the *sanitized filename* rather than
from the source directory name. That keeps the output directory the sole
record of completed work: the identity of an already-exported book can be
recomputed by reading its filename back off disk, with no state file.
"""

from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

#: Platforms ``disarm.sanitize_filename`` knows how to target.
Platform = Literal["universal", "windows", "posix"]

try:
    import disarm
except ImportError:  # pragma: no cover - exercised by the no-extra install
    disarm = None  # type: ignore[assignment]

PORTABLE_HINT = (
    "--portable-names needs the 'disarm' package: pip install 'ibook2epub[portable]'"
)

#: Characters that are illegal on Windows/exFAT but legal on APFS and ext4.
#: Present only so the docs and tests can name them; disarm does the removal.
WINDOWS_ILLEGAL = '<>:"/\\|?*'


class PortableNamesUnavailableError(RuntimeError):
    """Raised when portable naming is requested but ``disarm`` is not installed."""


@runtime_checkable
class NamingPolicy(Protocol):
    """Maps a package directory name to an output filename and an identity."""

    #: Short name used in log output.
    label: str

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

    def filename(self, package_name: str) -> str:
        """Return *package_name* unchanged."""
        return package_name

    def identity(self, filename: str) -> str:
        """Return *filename* unchanged."""
        return filename


class PortableNaming:
    """
    Rewrite names for cross-filesystem portability using ``disarm``.

    Spaces are preserved (``separator=" "``) because they are legal on every
    target filesystem; only genuinely illegal characters are replaced. Note
    that ``disarm.sanitize_filename`` also transliterates, so non-Latin titles
    are romanized — ``こころ.epub`` becomes ``kokoro.epub``. That is lossy and
    is the reason this policy is opt-in rather than the default.

    :param platform: Target platform: ``universal``, ``windows`` or ``posix``.
    :param separator: Replacement for illegal characters.

    :raises PortableNamesUnavailableError: If ``disarm`` is not installed.
    """

    label = "portable"

    def __init__(self, platform: Platform = "universal", separator: str = " ") -> None:
        if disarm is None:
            raise PortableNamesUnavailableError(PORTABLE_HINT)
        self.platform: Platform = platform
        self.separator = separator

    def filename(self, package_name: str) -> str:
        """Return a filename safe to copy to *platform*."""
        assert disarm is not None  # noqa: S101 - guarded in __init__
        return disarm.sanitize_filename(
            package_name, separator=self.separator, platform=self.platform
        )

    def identity(self, filename: str) -> str:
        """Return a case- and accent-insensitive key for *filename*."""
        assert disarm is not None  # noqa: S101 - guarded in __init__
        return disarm.catalog_key(filename)


def portable_available() -> bool:
    """
    Report whether portable naming can be used.

    :return: True if ``disarm`` is importable.
    """
    return disarm is not None


def build_policy(portable: bool) -> NamingPolicy:
    """
    Select a naming policy.

    :param portable: Whether the user asked for portable names.

    :return: The policy to hand to the exporter.

    :raises PortableNamesUnavailableError: If portable names were requested
        but ``disarm`` is not installed.
    """
    return PortableNaming() if portable else PassthroughNaming()
