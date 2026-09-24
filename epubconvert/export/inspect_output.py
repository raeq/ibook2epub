"""
Reading the output directory, and writing beside it.

Writing the output directory is not the same as trusting it. Two of these
operations read it back: checking that exported archives are still sound, and
measuring the space left to write into.

The third, :func:`extract_cover`, writes *into* it, and deliberately takes its
bytes from the source package rather than from the archive just written. It
lives here because it is about what ends up beside a book, not about how the
book itself is built.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..collect.package import ValidationError, read_package_dir
from ..collect.validate import ValidationOptions
from ..utils.app_logger import logger
from ..utils.contained import is_free, open_contained, resolve
from ..utils.display import printable
from ..utils.spec import PACKAGE_SUFFIX
from .archive import PARTIAL_PREFIX, PARTIAL_SUFFIX, file_mode

#: Extensions a cover may be written under: the EPUB 3.3 core media types for
#: images (GIF, JPEG, PNG, SVG, WebP), so every reader can show what lands
#: beside a book. Never :data:`~epubconvert.utils.spec.PACKAGE_SUFFIX`, which
#: would make the cover a second book -- or, on a case-insensitive volume, the
#: book itself.
COVER_SUFFIXES = frozenset({".gif", ".jpeg", ".jpg", ".png", ".svg", ".webp"})

#: Guards the "cannot measure free space" warning so it is said once per
#: process rather than once per sampling interval.
_warned_about_free_space: set[bool] = set()


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
        # said, because this silently turns --min-free into a no-op on exactly
        # the removable and network volumes it exists for. Said once per run:
        # the caller samples this on a cadence, so an unguarded warning would
        # repeat for the life of the run.
        if _warned_about_free_space:
            return 1 << 30
        _warned_about_free_space.add(True)
        logger.warning(
            "Cannot measure free space on %s (%s); --min-free is not enforced.",
            printable(str(path)),
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
        href = (
            described.manifest.get(described.cover_id) if described.cover_id else None
        )
        if not href:
            return None
        source = resolve(package, href, resolved_root=package.resolve())
        if source is None or not source.is_file():
            logger.debug(
                "No cover for %s: %r is not a readable file inside the package",
                printable(target_archive.name),
                href,
            )
            return None

        cover = _cover_name(target_archive, href)
        if cover is None:
            return None

        # Streamed rather than read whole: one copy per worker, and the pool
        # is now sized for blocking work, so a 5 MB cover at 64 workers is a
        # 320 MB transient this does not need.
        #
        # Through open_contained like every other reader. shutil.copyfile opens
        # its source with a plain open(), which follows a symlink -- so
        # resolving the path and then copying it reopened the check-then-open
        # window O_NOFOLLOW exists to close, in the one reader that did not use
        # it.
        #
        # Into a partial and then published, never written under its final
        # name. A cover is only written when its name is free, so one left
        # truncated by a full disk was never rewritten: every later run saw the
        # name taken.
        if not _write_new(source, cover):
            logger.debug(
                "Not writing cover for %s: %s was taken while copying",
                printable(target_archive.name),
                printable(cover.name),
            )
            return None
    except (OSError, ValueError, ValidationError) as exc:
        # The reason can quote the book's own words -- a rootfile path out of
        # container.xml, which XML lets carry a C1 CSI and a CR -- so it is
        # escaped as the name is.
        logger.debug(
            "No cover for %s: %s", printable(target_archive.name), printable(str(exc))
        )
        return None

    return cover


def _cover_name(target_archive: Path, href: str) -> Path | None:
    """
    Choose the name a cover is written under, beside its book.

    :param target_archive: The exported epub file the cover sits beside.
    :param href: The cover's href, out of the book's own manifest.

    :return: A free name with an image suffix, or None if there is none.
    """
    # with_suffix() *replaces* the extension, so a cover href ending in
    # ".epub" would resolve to the archive itself and overwrite the book
    # with image bytes. Build the name from the stem instead, and refuse
    # any path that is not a new file beside the archive.
    #
    # The suffix is the book's choice, so it is lower-cased and held to
    # the image types a reader must support. Refusing only the exact
    # archive name was case-sensitive: "cover.EPUB" wrote Book.EPUB beside
    # Book.epub, one file on the case-insensitive volume the shelf is
    # copied to, and any other suffix put a file of the book's choosing
    # -- ".html", ".exe" -- into the output directory.
    suffix = (Path(href).suffix or ".jpg").lower()
    if suffix not in COVER_SUFFIXES:
        logger.debug(
            "Not writing cover for %s: %s is not an image suffix",
            printable(target_archive.name),
            printable(suffix),
        )
        return None
    cover = target_archive.parent / f"{target_archive.stem}{suffix}"
    if not is_free(cover):
        logger.debug(
            "Not writing cover for %s: %s is taken",
            printable(target_archive.name),
            printable(cover.name),
        )
        return None
    return cover


def _write_new(source: Path, target: Path) -> bool:
    """
    Copy *source* to a name that must not exist yet, all at once or not at all.

    The copy goes to a partial in the same directory and is published with
    ``os.link``, which refuses a name that is already taken -- by a file, or
    by a symlink it would otherwise follow -- so the window between the caller's
    :func:`~epubconvert.utils.contained.is_free` check and the write cannot
    overwrite anything. The partial is removed whatever happens.

    :param source: The file to copy, opened without following a symlink.
    :param target: The name to publish it under.

    :return: True if written, False if the name was taken in the meantime.

    :raises OSError: If the copy or the publishing failed for another reason.
    """
    handle, partial_name = tempfile.mkstemp(
        dir=target.parent, prefix=PARTIAL_PREFIX, suffix=PARTIAL_SUFFIX
    )
    os.close(handle)
    partial = Path(partial_name)
    try:
        partial.chmod(file_mode())
        with open_contained(source) as reading, partial.open("wb") as writing:
            shutil.copyfileobj(reading, writing)
        try:
            os.link(partial, target)
        except FileExistsError:
            return False
        except OSError:
            # FAT and exFAT have no hard links, and they are the volumes a
            # shelf is most often copied to. There the check is repeated and
            # the partial renamed: the run lock already keeps this tool's own
            # runs out, so the narrower guarantee is lost only against some
            # other program writing the same name in the same instant.
            if not is_free(target):
                return False
            partial.replace(target)
        return True
    finally:
        partial.unlink(missing_ok=True)


def _entries(directory: Path) -> list[Path]:
    """
    List a directory, or nothing when it cannot be listed, as a glob would.

    :param directory: The directory.

    :return: Its entries.
    """
    try:
        return list(directory.iterdir())
    except OSError:
        return []


def verify_output(
    output_dir: Path, epubcheck: bool = False
) -> tuple[int, int, list[str]]:
    """
    Check the archives already sitting in the output directory.

    The output directory is the record of completed work, but nothing ever
    re-reads that record, so a damaged export stays invisible. This reads it
    back.

    :param output_dir: Directory holding exported epub files.
    :param epubcheck: Also run the external epubcheck tool.

    :return: How many archives were checked, how many were damaged, and the
        names of the damaged ones so a caller can name them in its advice.
    """
    options = ValidationOptions(enabled=True, epubcheck=epubcheck)
    # Files only, as the planner reads the shelf: a directory of this name was
    # reported damaged, and a FIFO froze the whole check. The extension in any
    # case: a book copied through keeps the name it arrived with, and a glob
    # for "*.epub" never read "Foo.EPUB". A partial is never a finished book.
    archives = sorted(
        found
        for found in _entries(output_dir)
        if found.suffix.lower() == PACKAGE_SUFFIX
        and not found.name.startswith(PARTIAL_PREFIX)
        and found.is_file()
    )
    damaged = 0
    broken: list[str] = []

    # Pooled, unlike the sequential loop this replaced: measured 2.06x on 100
    # archives at four threads. Capped at eight because the namelist and
    # ElementTree work is pure Python, and past that the GIL makes it slower
    # rather than faster -- so this is deliberately not the export pool's size.
    with ThreadPoolExecutor(
        max_workers=min(8, os.cpu_count() or 4), thread_name_prefix="verify"
    ) as pool:
        results = list(pool.map(options.check, archives))

    for position, (archive, problems) in enumerate(
        zip(archives, results, strict=True), start=1
    ):
        if problems:
            damaged += 1
            broken.append(archive.name)
            logger.error(
                "[%d/%d] %s is damaged: %s",
                position,
                len(archives),
                printable(archive.name),
                "; ".join(problems[:3]),
            )
        else:
            logger.debug(
                "[%d/%d] %s is sound", position, len(archives), printable(archive.name)
            )

    return len(archives), damaged, broken
