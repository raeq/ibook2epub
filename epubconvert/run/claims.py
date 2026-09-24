"""
Which output names a run has spoken for, and what to say to a book that lost.

The bookkeeping :func:`epubconvert.run.planning.assign_names` settles names
with, kept apart from the rules it applies.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..collect.validate import usable_identifier
from ..export.naming import filesystem_key

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..utils.opf import Package


class Claims:
    """
    Which output names are spoken for, and where each search left off.

    Two sets rather than one. ``identity`` answers whether two books are the
    same book; ``filesystem_key`` answers whether two names are the same
    *file*, which on a case-insensitive volume is a looser question and the one
    that decides whether a write destroys another write.

    The resume positions exist because a colliding group that exhausted
    ``MAX_SUFFIX`` made every later member retry all 99 candidates, recomputing
    identity each time, before losing.
    """

    def __init__(self) -> None:
        self.identities: set[str] = set()
        self.paths: set[str] = set()
        self.positions: dict[str, int] = {}
        self.holders: dict[str, str] = {}

    def resume(self, group: str) -> int:
        """Return the first position worth trying for this group."""
        return self.positions.get(group, 1)

    def take(self, group: str, position: int, key: str, candidate: str) -> bool:
        """Claim a candidate if both its identity and its path are free."""
        path_key = filesystem_key(candidate)
        if key in self.identities or path_key in self.paths:
            return False
        self.identities.add(key)
        self.paths.add(path_key)
        self.positions[group] = position + 1
        self.holders.setdefault(group, candidate)
        return True

    def holder(self, group: str) -> str | None:
        """Return the name that took this group, if anything did."""
        return self.holders.get(group)

    def exhaust(self, group: str, limit: int) -> None:
        """Record that this group has no positions left to try."""
        self.positions[group] = limit + 1


def lost_to(holder: str | None, metadata: Package | None) -> str:
    """
    Explain which book holds the name, and say what this one is.

    Naming the winner turns "another book already claims this name" into
    something a person can act on, and the loser's identifier is what tells
    them whether the two are the same book stored twice or genuinely different
    editions. In a surveyed library both cases are common.

    :param holder: The filename that took the name, if one did.
    :param metadata: The losing package's document, if it was read.

    :return: The reason to record on the decision.
    """
    reason = (
        f"{holder} already holds this name"
        if holder
        else "another book already claims this name"
    )
    identifier = usable_identifier(metadata)
    return f"{reason}; this book is {identifier}" if identifier else reason
