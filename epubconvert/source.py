"""
Inspection of source packages before conversion.

Two conditions make a package impossible to convert into a readable book, and
both are silent: a DRM-protected purchase produces a valid-looking archive
nobody can open, and a book that iCloud has not downloaded produces one with
empty files inside. Detecting them turns a failure discovered days later on a
Kindle into a line in the run's output.

Detection only. Nothing here removes or circumvents protection; a protected
book is reported and skipped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .app_logger import logger

ENCRYPTION_PATH = "META-INF/encryption.xml"
SINF_PATH = "META-INF/sinf.xml"

#: Encryption algorithms that are *not* DRM. Both obfuscate embedded fonts, a
#: routine publishing practice; the book itself reads perfectly. Treating
#: these as DRM would wrongly skip a large slice of a normal library — in one
#: real 2,805-book library, every package carrying encryption.xml used the
#: Adobe font algorithm and none was protected.
FONT_OBFUSCATION_ALGORITHMS = frozenset(
    {
        "http://www.idpf.org/2008/embedding",
        "http://ns.adobe.com/pdf/enc#RC",
    }
)

_ALGORITHM = re.compile(r'Algorithm="([^"]+)"')

#: macOS marks a file whose contents live only in the cloud.
SF_DATALESS = 0x40000000

MAX_ENCRYPTION_BYTES = 1024 * 1024


@dataclass(frozen=True)
class SourceStatus:
    """What inspection found out about one package."""

    drm: bool = False
    incomplete: bool = False
    reason: str | None = None

    @property
    def convertible(self) -> bool:
        """Whether this package can be turned into a readable book."""
        return not (self.drm or self.incomplete)


def encryption_algorithms(package: Path) -> set[str]:
    """
    Read the algorithms declared in a package's ``encryption.xml``.

    :param package: The ``*.epub/`` package directory.

    :return: The algorithm URIs found, empty if the file is absent.
    """
    path = package / ENCRYPTION_PATH
    try:
        if not path.is_file() or path.stat().st_size > MAX_ENCRYPTION_BYTES:
            return set()
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.debug("Could not read %s: %s", path, exc)
        return set()
    return set(_ALGORITHM.findall(text))


def has_drm(package: Path) -> tuple[bool, str | None]:
    """
    Report whether a package is protected by DRM.

    The presence of ``encryption.xml`` alone is **not** evidence of DRM: it is
    also how font obfuscation is declared. The algorithm decides.

    :param package: The ``*.epub/`` package directory.

    :return: Whether it is protected, and a short reason when it is.
    """
    if (package / SINF_PATH).is_file():
        return True, "FairPlay protected (META-INF/sinf.xml)"

    algorithms = encryption_algorithms(package)
    protecting = algorithms - FONT_OBFUSCATION_ALGORITHMS
    if protecting:
        return True, f"encrypted with {sorted(protecting)[0]}"

    return False, None


def has_dataless_files(package: Path) -> bool:
    """
    Report whether any file in the package is an undownloaded iCloud stub.

    This walks the package, which is the expensive operation on a cloud
    library, so callers should only ask when the user opted in.

    :param package: The ``*.epub/`` package directory.

    :return: True if at least one file has no local contents.
    """
    try:
        for path in package.rglob("*"):
            if not path.is_file():
                continue
            flags = getattr(path.stat(), "st_flags", 0)
            if flags & SF_DATALESS:
                return True
    except OSError as exc:
        logger.debug("Could not inspect %s: %s", package, exc)
    return False


def inspect_package(package: Path, check_incomplete: bool = False) -> SourceStatus:
    """
    Decide whether a package can be converted into a readable book.

    :param package: The ``*.epub/`` package directory.
    :param check_incomplete: Also look for undownloaded iCloud stubs, which
        requires walking the package.

    :return: What was found.
    """
    protected, reason = has_drm(package)
    if protected:
        return SourceStatus(drm=True, reason=reason)

    if check_incomplete and has_dataless_files(package):
        return SourceStatus(incomplete=True, reason="not downloaded from iCloud")

    return SourceStatus()
