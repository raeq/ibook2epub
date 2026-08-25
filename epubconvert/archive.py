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
from pathlib import Path, PurePosixPath
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

from .app_logger import logger
from .contained import contains
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
#: Appended to every temporary this tool writes.
PARTIAL_SUFFIX = ".part"
#: Deflate level for text members. Measured on a 3 MB book: level 9 costs 3.2x
#: the CPU of level 6 for 0.6% less size, and bare zlib on the same text is
#: 4.8x for 1.6%. Level 6 is zlib's default and the level worth paying for.
#: This was inert until :func:`entry` assigned it -- a prebuilt ZipInfo makes
#: ``ZipFile(compresslevel=)`` a no-op.
COMPRESS_LEVEL = 6

#: Extensions whose bytes are already entropy-coded. Deflating them scans the
#: data to save nothing: measured 3.8x faster to store, for +0.02% size. The
#: rule is a pure function of the name, so exports stay byte-identical.
STORED_SUFFIXES = frozenset(
    {
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".webp",
        ".avif",
        ".mp3",
        ".m4a",
        ".mp4",
        ".m4v",
        ".ogg",
        ".opus",
        ".woff",
        ".woff2",
        ".otf",
        ".ttf",
        ".zip",
        ".gz",
    }
)
#: The permission bits recorded *inside* the zip for each member. Fixed rather
#: than taken from the umask, because it is archive metadata and re-exports
#: must stay byte-identical. What the exported file itself gets is a separate
#: question, answered by :func:`_file_mode` from the user's umask.
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
            candidate = Path(root) / name
            # A package is a directory the walk owns, never a redirection. A
            # symlink named *.epub was accepted here and its whole target
            # zipped into the shelf. The rule lives in one place; see
            # :mod:`epubconvert.contained` for why it is not restated here.
            if not contains(Path(root), candidate):
                logger.warning("Ignoring symlinked package %s", candidate)
                continue
            if name.endswith(PACKAGE_SUFFIX):
                found.append(candidate)
            else:
                descend.append(name)
        dirs[:] = descend

    found.sort()
    logger.debug("Found %d epub package(s) under %s", len(found), source_dir)
    return found


def entry(arcname: str, compress_type: int) -> ZipInfo:
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
    member = ZipInfo(arcname, date_time=ARCHIVE_TIMESTAMP)
    member.compress_type = compress_type
    member.external_attr = ARCHIVE_MODE << 16
    # Assigned here rather than on the ZipFile: open() consults the archive's
    # compresslevel only when it builds the ZipInfo itself, so handing it a
    # prebuilt one silently discarded the setting.
    _set_level(member, COMPRESS_LEVEL)
    return member


def _set_level(member: ZipInfo, level: int) -> None:
    """
    Record the deflate level on a member.

    ``ZipInfo._compresslevel`` is private CPython API, and the only way to
    apply a level to a prebuilt entry: ``ZipFile(compresslevel=)`` is consulted
    only when ``open()`` constructs the ZipInfo itself. The attribute has
    carried this name since 3.7 and is what the public constructor sets.

    :param member: The entry to annotate.
    :param level: The zlib level to record.
    """
    setattr(member, "_compresslevel", level)  # noqa: B010


def level_of(member: ZipInfo) -> int | None:
    """
    Report the deflate level recorded on a member.

    Exists so tests can assert the level was applied without reaching into
    private CPython API themselves.

    :param member: The entry to inspect.

    :return: The recorded level, or None if none was set.
    """
    return getattr(member, "_compresslevel", None)


def compression_for(arcname: str) -> int:
    """
    Choose how a member should be stored.

    :param arcname: The member's path inside the archive.

    :return: ``ZIP_STORED`` for already-compressed media, else ``ZIP_DEFLATED``.
    """
    if PurePosixPath(arcname).suffix.lower() in STORED_SUFFIXES:
        return ZIP_STORED
    return ZIP_DEFLATED


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
    # file the user writes -- which means like the umask says, not 0644
    # regardless. A user running with `umask 077` still got world-readable
    # books. ARCHIVE_MODE stays as the zip entry's recorded mode, which is
    # metadata and rightly fixed for byte-identical re-exports.
    partial.chmod(_file_mode())
    file_count = 0

    try:
        with ZipFile(
            partial, "w", ZIP_DEFLATED, compresslevel=COMPRESS_LEVEL
        ) as archive:
            # The mimetype entry must come first and must be stored, not deflated.
            archive.writestr(entry(MIMETYPE_NAME, ZIP_STORED), MIMETYPE_CONTENT)

            stored: set[str] = set()
            for path in _members(source_dir):
                if is_excluded(path.name, at_root=path.parent == source_dir):
                    logger.trace("Excluded from archive: %s", path.name)
                    continue
                arcname = path.relative_to(source_dir).as_posix()
                member = entry(arcname, compression_for(arcname))
                with path.open("rb") as source, archive.open(member, "w") as target:
                    shutil.copyfileobj(source, target)
                stored.add(arcname)
                file_count += 1

        assert_is_a_book(target_archive.name, stored)

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


def _file_mode() -> int:
    """
    Return the mode an exported file should carry, per the user's umask.

    :return: 0o666 with the umask applied.
    """
    mask = os.umask(0)
    os.umask(mask)
    return 0o666 & ~mask


def assert_is_a_book(name: str, stored: set[str]) -> None:
    """
    Refuse to hand back an archive that is not a book.

    **This is the choke point.** The output directory is the tool's only record
    of completed work, so anything that reaches it is recorded as finished and
    no rerun retries it. Every silent-success defect this project has had ended
    here: an unreadable subdirectory that contributed nothing, a package
    deleted between planning and writing, a package that was never downloaded,
    a symlinked directory holding somebody else's files. In each case the run
    wrote a structurally valid zip, reported an export, and permanently
    recorded a book that was wrong or missing.

    A valid zip is not the bar. The bar is the two things every epub has: the
    container document that says where the package document lives, and at least
    one member besides the ``mimetype`` this function's caller wrote itself.

    Deliberately cheap and unconditional -- ``--validate`` is the thorough
    check and it is off by default, so this is what protects the invariant on
    an ordinary run.

    :param name: The archive's name, for the error message.
    :param stored: Arc names written from the package, excluding ``mimetype``.

    :raises ArchiveInvalidError: If the archive is not a book.
    """
    if not stored:
        raise ArchiveInvalidError(name, ["package holds no files"])
    if CONTAINER_PATH not in stored:
        raise ArchiveInvalidError(name, [f"package has no {CONTAINER_PATH}"])


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
    resolved = source_dir.resolve()
    for root, _dirs, files in os.walk(source_dir, onerror=on_error):
        directory = Path(root)
        for name in files:
            path = directory / name
            if not contains(source_dir, path, resolved_root=resolved):
                logger.warning("Skipped symlink %s in %s", name, source_dir.name)
                continue
            if path.is_file():
                found.append(path)

    found.sort()
    return found
