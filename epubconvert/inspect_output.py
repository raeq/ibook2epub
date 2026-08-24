"""
Looking at the output directory rather than writing to it.

Writing the output directory is not the same as trusting it. Two of these
operations read it back: checking that exported archives are still sound, and
measuring the space left to write into.

The third, :func:`extract_cover`, writes *into* it, and deliberately takes its
bytes from the source package rather than from the archive just written. It
lives here because it is about what ends up beside a book, not about how the
book itself is built.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from .app_logger import logger
from .spec import PACKAGE_SUFFIX
from .validate import (
    ValidationError,
    ValidationOptions,
    contained_file,
    read_package_dir,
)


def free_megabytes(path: Path) -> int:
    """
    Report free space on the volume holding *path*, in MiB.

    :param path: A path on the volume to measure.

    :return: Free space in MiB, or a large number if it cannot be determined.
    """
    try:
        return shutil.disk_usage(path).free // (1024 * 1024)
    except OSError as exc:
        # Permissive, so a volume we cannot measure never blocks work -- but
        # said once, because this silently turns --min-free into a no-op on
        # exactly the removable and network volumes it exists for.
        logger.warning(
            "Cannot measure free space on %s (%s); --min-free is not enforced.",
            path,
            exc,
        )
        return 1 << 30


def extract_cover(package: Path, target_archive: Path) -> Path | None:
    """
    Write a book's cover image beside its exported archive.

    The image is read from the source package rather than from the archive
    that was just written, which avoids re-inflating a book to recover bytes
    that were sitting uncompressed on disk a moment earlier.

    Writing the image is a convenience, never the point of the run, so every
    failure is swallowed. The book itself is already complete and atomically
    in place by this point; letting a full disk or a rejected filename escape
    from here would abort the run and lose the counts for books that had
    already succeeded.

    The href is a value out of the book's own package document, so it is not
    trusted to stay inside the package: a manifest declaring
    ``href="../../../secret"`` as the cover image would otherwise have this
    function read that file and write its bytes into the output directory,
    where they travel on to whatever device the shelf is copied to.

    :param package: The source ``*.epub/`` package directory.
    :param target_archive: The exported epub file the cover sits beside.

    :return: The cover file written, or None if none could be written.
    """
    try:
        described = read_package_dir(package)
        if not described.cover_id:
            return None
        href = described.manifest.get(described.cover_id)
        if not href:
            return None
        source = contained_file(package, href)
        if source is None:
            logger.debug(
                "No cover for %s: %r is not a readable file inside the package",
                target_archive.name,
                href,
            )
            return None

        # with_suffix() *replaces* the extension, so a cover href ending in
        # ".epub" would resolve to the archive itself and overwrite the book
        # with image bytes. Build the name from the stem instead, and refuse
        # any path that is not a new file beside the archive.
        suffix = Path(href).suffix or ".jpg"
        cover = target_archive.parent / f"{target_archive.stem}{suffix}"
        if cover == target_archive or cover.exists():
            logger.debug(
                "Not writing cover for %s: %s is taken",
                target_archive.name,
                cover.name,
            )
            return None

        cover.write_bytes(source.read_bytes())
    except (OSError, ValueError, ValidationError) as exc:
        logger.debug("No cover for %s: %s", target_archive.name, exc)
        return None

    return cover


def verify_output(output_dir: Path, epubcheck: bool = False) -> tuple[int, int]:
    """
    Check the archives already sitting in the output directory.

    The output directory is the record of completed work, but nothing ever
    re-reads that record, so a damaged export stays invisible. This reads it
    back.

    :param output_dir: Directory holding exported epub files.
    :param epubcheck: Also run the external epubcheck tool.

    :return: The number of archives checked, and the number found damaged.
    """
    options = ValidationOptions(enabled=True, epubcheck=epubcheck)
    archives = sorted(output_dir.glob(f"*{PACKAGE_SUFFIX}"))
    damaged = 0

    for position, archive in enumerate(archives, start=1):
        problems = options.check(archive)
        if problems:
            damaged += 1
            logger.error(
                "[%d/%d] %s is damaged: %s",
                position,
                len(archives),
                archive.name,
                "; ".join(problems[:3]),
            )
        else:
            logger.debug("[%d/%d] %s is sound", position, len(archives), archive.name)

    return len(archives), damaged
