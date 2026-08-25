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

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from xml.etree import ElementTree

from .app_logger import logger
from .contained import resolve

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

#: The element whose ``Algorithm`` attribute decides whether a package is
#: protected. Matched by local name, because packages differ in how they bind
#: the xmlenc namespace. Other elements (``DigestMethod``,
#: ``CanonicalizationMethod``) carry an ``Algorithm`` attribute too, and
#: reading those would report a readable book as DRM-protected.
ENCRYPTION_METHOD = "EncryptionMethod"

#: The element that declares something is encrypted at all. Counted separately
#: because a block carrying no ``EncryptionMethod`` still means protection.
ENCRYPTED_DATA = "EncryptedData"

#: macOS marks a file whose contents live only in the cloud.
SF_DATALESS = 0x40000000

MAX_ENCRYPTION_BYTES = 1024 * 1024


class UnreadableEncryptionError(Exception):
    """Raised when a package declares encryption that cannot be interpreted."""


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

    The file is parsed rather than pattern-matched: an attribute value may be
    single-quoted, which a text scan would miss and report a protected book as
    readable, and elements other than ``EncryptionMethod`` carry an
    ``Algorithm`` attribute that must not be mistaken for one.

    :param package: The ``*.epub/`` package directory.

    :return: The encryption algorithm URIs found, empty if the file is absent.

    :raises UnreadableEncryptionError: If the file exists but cannot be read
        or parsed. This deliberately does not fail open: a truncated
        encryption.xml -- a realistic state for a partly synced iCloud file --
        must not be read as "no protection", or a protected book is exported
        as an unopenable archive that no rerun will retry.
    """
    path = resolve(package, ENCRYPTION_PATH)
    if path is None:
        # A symlink here would let the book choose which file answers "is this
        # protected", which is the question that decides whether it exports.
        raise UnreadableEncryptionError(f"{ENCRYPTION_PATH} is not a readable file")
    if not path.is_file():
        return set()

    try:
        if path.stat().st_size > MAX_ENCRYPTION_BYTES:
            raise UnreadableEncryptionError(f"{ENCRYPTION_PATH} is implausibly large")
        data = path.read_bytes()
    except OSError as exc:
        raise UnreadableEncryptionError(f"could not read {ENCRYPTION_PATH}") from exc

    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError as exc:
        raise UnreadableEncryptionError(f"{ENCRYPTION_PATH} is not valid XML") from exc

    algorithms: set[str] = set()
    blocks = 0
    for element in root.iter():
        local = element.tag.rpartition("}")[2]
        if local == ENCRYPTED_DATA:
            blocks += 1
        if local != ENCRYPTION_METHOD:
            continue
        algorithm = element.get("Algorithm")
        if algorithm:
            algorithms.add(algorithm)

    # A block that names no algorithm is still a declaration that something is
    # encrypted; XML Encryption allows the algorithm to travel out of band.
    # Returning an empty set read as "no protection", so the book exported as
    # an unopenable archive and was recorded as finished work. Every other
    # unreadable state here fails closed; this one failed open.
    if blocks and not algorithms:
        raise UnreadableEncryptionError(
            f"{ENCRYPTION_PATH} declares encryption but names no algorithm"
        )
    return algorithms


def has_drm(package: Path) -> tuple[bool, str | None]:
    """
    Report whether a package is protected by DRM.

    The presence of ``encryption.xml`` alone is **not** evidence of DRM: it is
    also how font obfuscation is declared. The algorithm decides.

    :param package: The ``*.epub/`` package directory.

    :return: Whether it is protected, and a short reason when it is.
    """
    sinf = resolve(package, SINF_PATH)
    if sinf is None or sinf.is_file():
        return True, "FairPlay protected (META-INF/sinf.xml)"

    try:
        algorithms = encryption_algorithms(package)
    except UnreadableEncryptionError as exc:
        # Treated as protected rather than readable: exporting a book whose
        # protection could not be ruled out produces a broken archive that is
        # then recorded as finished work.
        logger.debug("Assuming %s is protected: %s", package.name, exc)
        return True, f"encryption could not be checked ({exc})"

    protecting = algorithms - FONT_OBFUSCATION_ALGORITHMS
    if protecting:
        return True, f"encrypted with {sorted(protecting)[0]}"

    return False, None


@lru_cache(maxsize=1)
def dataless_detection_available() -> bool:
    """
    Report whether this platform can tell a cloud stub from a real file.

    ``st_flags`` exists only on BSD-derived systems. Everywhere else the
    ``SF_DATALESS`` test degrades to ``0 & flag``, so ``--skip-incomplete``
    found nothing while the help text promised the check and the user paid a
    full walk of every package for it.

    :return: True if ``os.stat_result`` carries ``st_flags``.
    """
    return hasattr(os.stat_result, "st_flags")


def has_dataless_files(package: Path) -> bool:
    """
    Report whether any file in the package is an undownloaded iCloud stub.

    This walks the package, which is the expensive operation on a cloud
    library, so callers should only ask when the user opted in. ``os.walk``
    already separates files from directories, which spares a second stat per
    entry that a ``rglob`` plus ``is_file`` would cost.

    :param package: The ``*.epub/`` package directory.

    :return: True if at least one file has no local contents.
    """

    if not dataless_detection_available():
        logger.warning(
            "Cannot detect undownloaded files on this platform; "
            "--skip-incomplete has no effect."
        )
        return False

    def on_error(exc: OSError) -> None:
        logger.debug("Could not inspect %s: %s", exc.filename or package, exc)

    for root, _dirs, files in os.walk(package, onerror=on_error):
        directory = Path(root)
        for name in files:
            try:
                stat = (directory / name).stat()
            except OSError as exc:  # pragma: no cover - racing removal
                logger.debug("Could not stat %s: %s", name, exc)
                continue
            if getattr(stat, "st_flags", 0) & SF_DATALESS:
                return True
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
