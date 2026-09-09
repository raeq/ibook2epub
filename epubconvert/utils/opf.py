"""
What a package document says, once it has been read.

A plain holder, kept apart from the module that fills it in. Every layer
handles one: the reader builds it, the naming policies ask it for a title and
a creator, the catalogue asks it for an identifier. Held in ``extract`` it
made the vocabulary of naming depend on the reader, which is backwards.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Package:
    """The parts of a package document (OPF) this tool cares about."""

    opf_path: str
    title: str | None = None
    creator: str | None = None
    creator_sort: str | None = None
    identifier: str | None = None
    #: Manifest item id to archive path, already resolved and unquoted.
    manifest: dict[str, str] = field(default_factory=dict)
    #: Manifest ids referenced by the spine, in reading order.
    spine: list[str] = field(default_factory=list)
    #: Manifest id of the cover image, when the package declares one.
    cover_id: str | None = None
