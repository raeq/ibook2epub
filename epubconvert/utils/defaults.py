"""
Where books come from and where they go by default.

Held apart from both the CLI and the conversion logic because both need them
and neither should import the other.
"""

from __future__ import annotations

from pathlib import Path

from .app_logger import logger
from .spec import PACKAGE_SUFFIX

# Apple has moved the library between releases, so probe both known homes
# rather than assuming one and reporting an empty library on the other.
SOURCE_CANDIDATES = (
    Path.home() / "Library/Mobile Documents/iCloud~com~apple~iBooks/Documents",
    Path.home()
    / "Library/Containers/com.apple.BKAgentService/Data/Documents/iBooks/Books",
)
DEFAULT_SOURCE = SOURCE_CANDIDATES[0]
DEFAULT_OUTPUT = Path.home() / "Books"
DEFAULT_MAX_EXPORT_FILES = 5

#: Refuse to keep writing once the output volume falls below this, in MiB.
DEFAULT_MIN_FREE_MB = 128


def discover_source() -> Path:
    """
    Pick the iBooks library location that actually holds books.

    Apple has used more than one home for the library, and a single hardcoded
    default silently reports an empty library on a machine using the other
    one. Prefer a candidate that contains packages; fall back to one that at
    least exists.

    :return: The best source directory found.
    """
    existing = [candidate for candidate in SOURCE_CANDIDATES if candidate.is_dir()]

    for candidate in existing:
        try:
            if any(
                child.name.endswith(PACKAGE_SUFFIX) for child in candidate.iterdir()
            ):
                return candidate
        except OSError as exc:  # pragma: no cover - unreadable candidate
            logger.debug("Could not inspect %s: %s", candidate, exc)

    return existing[0] if existing else DEFAULT_SOURCE
