"""
What a naming policy provides, and what it produces.

The vocabulary only. Every policy that implements this lives in
:mod:`epubconvert.export.naming`, and everything that writes a file reads an
:class:`Assignment`, but the two modules that *read* Apple's databases also
have to state what a book will be called on the shelf. Held here, they can
say so without importing the module that decides how a run is named, which
is the wrong way round: collecting cannot depend on export.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..utils.opf import Package


@dataclass(frozen=True)
class Assignment:
    """
    One package's output name, and why it has none when it has none.

    Here rather than with the planner that fills it in: a name and the reason
    a package has none is what a policy produces, and everything that writes
    a file needs to read one without also depending on how the run was
    planned.
    """

    package: Path
    #: The name to write, or ``""`` when the package lost a collision.
    filename: str
    identity: str
    #: What holds the name instead, set only when *filename* is empty.
    reason: str | None = None
    #: Named from metadata that declared no creator. Reported, not fatal.
    authorless: bool = False
    #: The policy wanted metadata and got none it could name from, so the
    #: package directory name was used. Reported, not fatal.
    from_folder: bool = False
    #: The book's usable dc:identifier, when naming read its package document.
    #: What tells this book from another that wants the same name.
    identifier: str | None = None
    #: Under ``--on-collision suffix``, the name marked with a digest of
    #: *identifier* that the book moves on to when the archive under its own
    #: name holds another book. None when there is no identifier to digest.
    marked: str | None = None


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
