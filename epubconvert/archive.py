"""
Finding source packages, and writing one out as an epub archive.

The two halves of the mechanical work: locating the ``*.epub/`` directories
Apple leaves behind, and turning one of them into a zip archive the epub
specification accepts. Neither half knows anything about runs, reports or
concurrency -- :mod:`epubconvert.convert` supplies those.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

from .app_logger import logger
from .spec import CONTAINER_PATH, MIMETYPE_CONTENT, MIMETYPE_NAME, PACKAGE_SUFFIX
from .validate import ArchiveInvalidError, ValidationOptions

# Zip cannot represent a timestamp before 1980; using its floor keeps every
# export byte-identical regardless of when it ran.
ARCHIVE_TIMESTAMP = (1980, 1, 1, 0, 0, 0)

#: Marks a half-written archive. The prefix matters as much as the suffix:
#: the sweep in :func:`epubconvert.convert.sweep_partials` deletes what it
#: matches, and a bare ``*.part`` glob also matches a browser's in-progress
#: download or a user's own file sitting in the output directory.
PARTIAL_PREFIX = ".ibook2epub-"
PARTIAL_SUFFIX = ".part"
COMPRESS_LEVEL = 9
ARCHIVE_MODE = 0o644

# Filesystem junk, never book content, so excluded wherever it appears.
EXCLUDED_ANYWHERE = frozenset({".DS_Store"})

# Apple bookkeeping, which only ever sits at the package root. These patterns
# must NOT be applied deeper: a chapter legitimately named ``bookmarks.xhtml``,
# a ``.plist`` data asset, or a file called ``mimetype`` inside ``OEBPS/`` are
# all real content, and dropping them corrupts the book. ``mimetype`` is listed
# because the root copy is rewritten separately, uncompressed and first, as the
# epub specification requires.
EXCLUDED_ROOT_NAMES = frozenset({MIMETYPE_NAME})
EXCLUDED_ROOT_SUFFIXES = frozenset({".plist"})
EXCLUDED_ROOT_PREFIXES = ("bookmarks",)


def is_excluded(name: str, *, at_root: bool) -> bool:
    """
    Report whether a package member should be left out of the epub.

    The Apple bookkeeping patterns apply only at the package root. Applying
    them at every depth silently drops real content — a chapter file named
    ``bookmarks.xhtml`` or a ``.plist`` asset under ``OEBPS/`` — which
    produces an archive that readers reject for a missing spine item.

    :param name: The bare file name (not a path) to test.
    :param at_root: Whether the file sits directly in the package directory.

    :return: True if the file must not be copied into the archive.
    """
    if name in EXCLUDED_ANYWHERE:
        return True
    if not at_root:
        return False
    return (
        name in EXCLUDED_ROOT_NAMES
        or Path(name).suffix in EXCLUDED_ROOT_SUFFIXES
        or name.startswith(EXCLUDED_ROOT_PREFIXES)
    )


def collect_package_dirs(source_dir: Path) -> list[Path]:
    """
    Find every ``*.epub/`` package directory beneath the source directory.

    The walk does not descend into a package once it has been found, so files
    inside a package can never be mistaken for packages themselves. Results are
    full paths, which keeps nested packages addressable; earlier versions
    returned bare directory names and silently broke on anything that was not
    a direct child of the source directory.

    :param source_dir: The directory to search.

    :return: Package directories, sorted by path.
    """
    found: list[Path] = []

    def on_error(exc: OSError) -> None:
        # os.walk swallows scandir failures unless onerror is supplied, so an
        # unreadable directory would otherwise be skipped in total silence.
        logger.warning("Could not scan %s: %s", exc.filename or source_dir, exc)

    for root, dirs, _files in os.walk(source_dir, onerror=on_error):
        descend = []
        for name in dirs:
            entry = Path(root) / name
            # A package is a directory the walk owns, never a redirection. A
            # symlink named *.epub was accepted here and its whole target
            # zipped into the shelf, which turned "convert my books" into
            # "copy that directory somewhere shareable".
            if entry.is_symlink():
                logger.warning("Ignoring symlinked package %s", entry)
                continue
            if name.endswith(PACKAGE_SUFFIX):
                found.append(entry)
            else:
                descend.append(name)
        dirs[:] = descend

    found.sort()
    logger.debug("Found %d epub package(s) under %s", len(found), source_dir)
    return found


def _entry(arcname: str, compress_type: int) -> ZipInfo:
    """
    Build a zip entry with normalized metadata.

    Zip members carry a modification time and permission bits, so exporting
    the same book twice would otherwise produce different bytes every time.
    Pinning both makes re-exports byte-identical, which lets backups dedup,
    stops rsync re-copying unchanged books, and allows outputs to be compared
    by hash.

    :param arcname: Path of the member inside the archive.
    :param compress_type: ``ZIP_STORED`` or ``ZIP_DEFLATED``.

    :return: The prepared entry.
    """
    entry = ZipInfo(arcname, date_time=ARCHIVE_TIMESTAMP)
    entry.compress_type = compress_type
    entry.external_attr = ARCHIVE_MODE << 16
    return entry


def zip_package(
    source_dir: Path,
    target_archive: Path,
    validation: ValidationOptions | None = None,
) -> int:
    """
    Write a single package directory out as a spec-valid epub archive.

    This is a blocking function, intended to be handed to a worker thread. The
    archive is assembled under a temporary name and only moved to
    ``target_archive`` once it is complete.

    Validation, when asked for, runs against the temporary file *before* the
    move. A book that fails therefore leaves nothing in the output directory
    and will be attempted again on the next run, rather than being recorded as
    finished work.

    :param source_dir: The ``*.epub/`` package directory to compress.
    :param target_archive: The path of the epub file to create.
    :param validation: Checks to run before the archive is moved into place.

    :return: The number of package files stored, excluding ``mimetype``.

    :raises ArchiveInvalidError: If validation was requested and failed.
    """
    # Deriving the temporary name from the target overflows the filesystem's
    # per-component limit when the target is already at it: 255 bytes plus
    # ".part" is 260. Take a short unique name in the same directory instead,
    # which keeps the closing replace atomic and can never be too long.
    handle, partial_name = tempfile.mkstemp(
        dir=target_archive.parent, prefix=PARTIAL_PREFIX, suffix=PARTIAL_SUFFIX
    )
    os.close(handle)
    partial = Path(partial_name)
    # mkstemp creates 0600; exported books should be readable like any other
    # file the user writes.
    partial.chmod(ARCHIVE_MODE)
    file_count = 0

    try:
        with ZipFile(
            partial, "w", ZIP_DEFLATED, compresslevel=COMPRESS_LEVEL
        ) as archive:
            # The mimetype entry must come first and must be stored, not deflated.
            archive.writestr(_entry(MIMETYPE_NAME, ZIP_STORED), MIMETYPE_CONTENT)

            stored: set[str] = set()
            for path in _members(source_dir):
                if is_excluded(path.name, at_root=path.parent == source_dir):
                    logger.trace("Excluded from archive: %s", path.name)
                    continue
                arcname = path.relative_to(source_dir).as_posix()
                entry = _entry(arcname, ZIP_DEFLATED)
                with path.open("rb") as source, archive.open(entry, "w") as target:
                    shutil.copyfileobj(source, target)
                stored.add(arcname)
                file_count += 1

        # An archive holding only the mimetype we wrote ourselves is a valid
        # zip and not a book. Without this the empty case -- a package that was
        # never downloaded, or one deleted between planning and writing --
        # lands in the output directory and is recorded as finished work that
        # no rerun ever retries.
        if not file_count:
            raise ArchiveInvalidError(target_archive.name, ["package holds no files"])
        if CONTAINER_PATH not in stored:
            raise ArchiveInvalidError(
                target_archive.name, [f"package has no {CONTAINER_PATH}"]
            )

        if validation is not None:
            problems = validation.check(partial)
            if problems:
                raise ArchiveInvalidError(target_archive.name, problems)

        partial.replace(target_archive)
    except BaseException:
        # Leave no partial archive behind, so the "already exported" check
        # stays a reliable record of completed work.
        partial.unlink(missing_ok=True)
        raise

    return file_count


def _members(source_dir: Path) -> list[Path]:
    """
    List the files to store, in a fixed order, refusing to be misdirected.

    Two departures from a plain ``rglob``, both of which cost a book its
    integrity when left out:

    ``os.walk`` is given an ``onerror`` that re-raises, so an unreadable
    subdirectory fails the export instead of contributing nothing. ``rglob``
    swallows that error, and the archive was written without the missing
    content, reported as a success, and recorded as completed work -- so
    repairing the permissions and rerunning skipped the book.

    Symlinks are skipped. ``os.walk`` does not descend into a symlinked
    directory, but it does list symlinked *files*, and opening one reads
    whatever it points at. A book that ships a link to a file outside itself
    would otherwise have that file's bytes copied into the archive under an
    innocuous name, and travel wherever the shelf is copied.

    :param source_dir: The package directory to enumerate.

    :return: The files to store, sorted, symlinks excluded.

    :raises OSError: If any directory under the package cannot be read.
    """

    def on_error(exc: OSError) -> None:
        raise exc

    found: list[Path] = []
    for root, _dirs, files in os.walk(source_dir, onerror=on_error):
        directory = Path(root)
        for name in files:
            path = directory / name
            if path.is_symlink():
                logger.warning("Skipped symlink %s in %s", name, source_dir.name)
                continue
            if path.is_file():
                found.append(path)

    found.sort()
    return found
