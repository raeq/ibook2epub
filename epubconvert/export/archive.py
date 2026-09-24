"""
Finding source packages, and writing one out as an epub archive.

The two halves of the mechanical work: locating the ``*.epub/`` directories
Apple leaves behind, and turning one of them into a zip archive the epub
specification accepts. Neither half knows anything about runs, reports or
concurrency -- :mod:`epubconvert.run.convert` supplies those.
"""

from __future__ import annotations

import errno
import itertools
import json
import os
import shutil
import stat
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path, PurePosixPath
from typing import Any
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

from ..collect.annotations import EMBEDDED_PATH, embedded_json, index_by_book
from ..collect.package import open_member, read_member
from ..collect.validate import ArchiveInvalidError, ValidationOptions
from ..utils.app_logger import logger
from ..utils.contained import contains, open_contained
from ..utils.display import printable
from ..utils.spec import CONTAINER_PATH, MIMETYPE_CONTENT, MIMETYPE_NAME, PACKAGE_SUFFIX

# Zip cannot represent a timestamp before 1980; using its floor keeps every
# export byte-identical regardless of when it ran.
ARCHIVE_TIMESTAMP = (1980, 1, 1, 0, 0, 0)

#: Marks a half-written archive. The prefix matters as much as the suffix:
#: the sweep in :func:`epubconvert.run.convert.sweep_partials` deletes what it
#: matches, and a bare ``*.part`` glob also matches a browser's in-progress
#: download or a user's own file sitting in the output directory.
PARTIAL_PREFIX = ".ibook2epub-"
#: Appended to every temporary this tool writes.
PARTIAL_SUFFIX = ".part"

#: Suffixes worth taking along verbatim. A real library holds both forms --
#: Apple's package directories, and books that arrived already zipped or as
#: PDFs -- and converting only the first produced a partial shelf whose
#: summary read complete.
COPYABLE_SUFFIXES = frozenset({PACKAGE_SUFFIX, ".pdf"})
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
#: question, answered by :func:`file_mode` from the user's umask.
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


def _shown(exc: OSError, fallback: Path) -> str:
    """
    Name the path an ``os.walk`` error is about, safe to log.

    ``exc.filename`` is a directory name off the disk, so it is input: raw, a
    name carrying ``ESC[2K`` erased the warning reporting it, and one holding
    an undecodable byte made the log file's handler raise on the surrogate.

    :param exc: The error ``os.walk`` handed to ``onerror``.
    :param fallback: The directory being walked, if the error names none.

    :return: The name, escaped.
    """
    return printable(os.fsdecode(exc.filename or fallback))


def collect_copyable(source_dir: Path) -> list[Path]:
    """
    Find files worth copying to the shelf unchanged.

    A ``*.epub`` **file** rather than a directory is a book that arrived
    already zipped; a ``*.pdf`` is a book this tool has nothing to do to. Both
    belong on the shelf the run produces, and neither needs converting.

    Only real files: the same trust rule every other reader here uses, so a
    symlink out of the library is not followed.

    :param source_dir: The directory to search.

    :return: Files to copy, sorted by path.
    """
    found: list[Path] = []
    resolved = source_dir.resolve()

    def on_error(exc: OSError) -> None:
        logger.warning("Could not scan %s: %s", _shown(exc, source_dir), exc)

    for root, dirs, files in os.walk(source_dir, onerror=on_error):
        directory = Path(root)
        # A package's own contents are never copy-through candidates.
        dirs[:] = [name for name in dirs if not name.endswith(PACKAGE_SUFFIX)]
        for name in files:
            path = directory / name
            if PurePosixPath(name).suffix.lower() not in COPYABLE_SUFFIXES:
                continue
            if not contains(source_dir, path, resolved_root=resolved):
                logger.warning(
                    "Skipped symlink %s in %s",
                    printable(name),
                    printable(source_dir.name),
                )
                continue
            # A FIFO named *.epub was kept, and reading it waited for ever.
            if not _is_regular(path):
                logger.warning("Skipped %s: not a regular file", printable(name))
                continue
            found.append(path)

    found.sort()
    return found


def _is_regular(path: Path) -> bool:
    """Report whether *path* is a regular file, without following a link."""
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def copy_through(source: Path, target: Path) -> None:
    """
    Put a file on the shelf without touching its bytes.

    Written to a temporary and moved into place, like every other write here,
    so an interrupted run never leaves a half-copied file that a later run
    mistakes for finished work.

    :param source: The file to copy.
    :param target: Where it should land.
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
        partial.replace(target)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise


def count_ignored(source_dir: Path, packages: Sequence[Path]) -> int:
    """
    Count things in the source that were not converted and never mentioned.

    A real library holds both forms -- Apple's ``*.epub/`` package directories
    and books that were sideloaded already zipped -- plus whatever else lives
    beside them. Anything that is not a package was passed over in silence:
    not skipped, not counted, not listed, so a partial export read as a
    complete one.

    Counted, not converted. Copying an already-valid archive through is a
    feature with its own decisions to make, not something to do by surprise.

    :param source_dir: The directory that was searched.
    :param packages: The packages that were found, which are not ignored.

    :return: How many entries were passed over.
    """
    found = {package.resolve() for package in packages}
    ignored = 0

    def on_error(exc: OSError) -> None:
        logger.debug("Could not count entries in %s: %s", _shown(exc, source_dir), exc)

    for root, dirs, files in os.walk(source_dir, onerror=on_error):
        directory = Path(root)
        # Not descended into: a package's own contents are not "ignored".
        dirs[:] = [name for name in dirs if (directory / name).resolve() not in found]
        ignored += len(files)

    return ignored


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
        logger.warning("Could not scan %s: %s", _shown(exc, source_dir), exc)

    for root, dirs, _files in os.walk(source_dir, onerror=on_error):
        descend = []
        for name in dirs:
            candidate = Path(root) / name
            # A package is a directory the walk owns, never a redirection. A
            # symlink named *.epub was accepted here and its whole target
            # zipped into the shelf. The rule lives in one place; see
            # :mod:`epubconvert.utils.contained` for why it is not restated here.
            if not contains(Path(root), candidate):
                # Only a *.epub link is a skipped book; anything else is a
                # linked directory the walk simply does not follow, and
                # warning about it on a library of symlinked shelves is noise.
                if name.endswith(PACKAGE_SUFFIX):
                    logger.warning(
                        "Ignoring symlinked package %s", printable(str(candidate))
                    )
                else:
                    logger.debug(
                        "Not following symlinked directory %s",
                        printable(str(candidate)),
                    )
                continue
            if name.endswith(PACKAGE_SUFFIX):
                found.append(candidate)
            else:
                descend.append(name)
        dirs[:] = descend

    found.sort()
    logger.debug(
        "Found %d epub package(s) under %s", len(found), printable(str(source_dir))
    )
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


def _store(archive: ZipFile, path: Path, arcname: str) -> None:
    """
    Stream one package file into the archive under a normalized entry.

    :param archive: The archive being assembled.
    :param path: The file to store, opened without following a symlink.
    :param arcname: Its path inside the archive.
    """
    with open_contained(path) as source:
        member = entry(arcname, compression_for(arcname))
        _size_ahead(member, os.fstat(source.fileno()).st_size)
        with archive.open(member, "w") as target:
            shutil.copyfileobj(source, target)


def _size_ahead(member: ZipInfo, size: int) -> None:
    """
    Tell zipfile how large a member will be before it is streamed in.

    ``ZipFile.open(info, "w")`` chooses between 32-bit and ZIP64 headers from
    ``info.file_size`` *before* a byte is written, and a fresh ZipInfo says 0.
    So every member was given 32-bit headers, and the first one past 2 GiB --
    a long audiobook track, a video -- raised RuntimeError when it closed: the
    book could never be exported. The size recorded here is only that
    decision's input; zipfile overwrites it with the count it actually wrote.

    Below zipfile's threshold (5% under 2 GiB) the decision comes out as it
    did with 0, so every ordinary book keeps its bytes.

    :param member: The entry about to be opened for writing.
    :param size: The source file's size in bytes.
    """
    member.file_size = size


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


def _embed_annotations(
    archive: ZipFile,
    annotations: Sequence[dict[str, object]] | None,
    stored: set[str],
) -> int:
    """
    Store this book's annotations inside the archive, if it has any.

    The W3C work puts an embedded annotation set at
    :data:`~epubconvert.collect.annotations.EMBEDDED_PATH` and requires no entry for it
    in ``container.xml`` or the package manifest: the location is the contract.

    Written after the members rather than before, so it cannot displace the
    mimetype entry, which must come first and stored.

    :param archive: The archive being assembled.
    :param annotations: This book's annotations, or None.
    :param stored: Names written so far, added to in place.

    :return: How many members were added, which is one or none.
    """
    if not annotations:
        return 0
    archive.writestr(
        entry(EMBEDDED_PATH, compression_for(EMBEDDED_PATH)),
        embedded_json(list(annotations)),
    )
    stored.add(EMBEDDED_PATH)
    return 1


def zip_package(
    source_dir: Path,
    target_archive: Path,
    validation: ValidationOptions | None = None,
    annotations: Sequence[dict[str, object]] | None = None,
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
    :param annotations: This book's annotations, stored in the same pass, or
        None. Embedding here rather than rebuilding the finished archive
        afterwards halves the writing: the archive was being serialised once
        without them and once with.

    :return: The number of members stored, excluding ``mimetype``. Counts the
        embedded annotation set, which is not a package file.

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
    file_count = 0

    try:
        # Inside the guard: a failure here used to leak a .part per book,
        # because the handler that unlinks it starts below.
        partial.chmod(file_mode())
        with ZipFile(
            partial, "w", ZIP_DEFLATED, compresslevel=COMPRESS_LEVEL
        ) as archive:
            # The mimetype entry must come first and must be stored, not deflated.
            archive.writestr(entry(MIMETYPE_NAME, ZIP_STORED), MIMETYPE_CONTENT)

            stored: set[str] = set()
            for path in _members(source_dir):
                if is_excluded(path.name, at_root=path.parent == source_dir):
                    logger.trace("Excluded from archive: %s", printable(path.name))
                    continue
                arcname = path.relative_to(source_dir).as_posix()
                if annotations and arcname == EMBEDDED_PATH:
                    # Replaced, not stored twice. A sideloaded package can
                    # arrive carrying its own set, and copying it before
                    # _embed_annotations wrote the name again left two members
                    # called that: zip allows it, the OCF does not, and readers
                    # disagree about which one they see. Replacing is what
                    # replace_annotations does to an archive already on the
                    # shelf, so a fresh export and a refresh agree.
                    logger.trace("Replaced by this run's annotations: %s", arcname)
                    continue
                _store(archive, path, arcname)
                stored.add(arcname)
                file_count += 1

            file_count += _embed_annotations(archive, annotations, stored)

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


#: The process umask, read once at import. Reading it requires *setting* it --
#: there is no query-only call -- so doing that per book from up to 64 workers
#: let one thread observe another's zeroed window and write a world-writable
#: book, and could leave the process umask at 0 for everything afterwards.
#: Import happens before any thread exists, and this tool never changes it.
UMASK = os.umask(0)
os.umask(UMASK)


def file_mode() -> int:
    """
    Return the mode an exported file should carry, per the user's umask.

    :return: 0o666 with the umask applied.
    """
    return 0o666 & ~UMASK


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

    A symlink anywhere in the package **fails the export**. Skipping one is
    not safe: ``os.walk`` does not descend into a symlinked directory, so a
    package whose whole content tree is a link contributed nothing at all, and
    the archive -- holding a real ``container.xml`` at the root -- passed
    :func:`assert_is_a_book` and was recorded as finished work. Refusing the
    book is the only answer that cannot silently lose content.

    :param source_dir: The package directory to enumerate.

    :return: The files to store, sorted, symlinks excluded.

    :raises OSError: If any directory under the package cannot be read.
    """

    def on_error(exc: OSError) -> None:
        raise exc

    found: list[Path] = []
    resolved = source_dir.resolve()
    for root, dirs, files in os.walk(source_dir, onerror=on_error):
        directory = Path(root)
        # Directories as well as files. os.walk quietly declines to descend a
        # symlinked directory, which is exactly how a package could contribute
        # nothing and still be reported as exported.
        for name in dirs + files:
            path = directory / name
            if not contains(source_dir, path, resolved_root=resolved):
                raise OSError(
                    errno.ELOOP,
                    f"symlink in package: {printable(name)}",
                    str(path),
                )
            if path.is_file():
                found.append(path)

    found.sort()
    return found


def index_by_package(
    found: list[dict[str, Any]],
    packages: Sequence[Path],
    *,
    copyable: Sequence[Path],
    quiet: bool = False,
) -> dict[str, list[dict[str, Any]]]:
    """
    Index annotations by book, leaving out any whose book cannot be told apart.

    An annotation records its book's package *name*, not its path, because the
    path runs through the reader's home directory. Two directories with the
    same name in different places are therefore indistinguishable to
    :func:`~epubconvert.collect.annotations.for_book`, and matching by name gave
    each of them the other's highlights. Only the ``-ar`` refresh checked: the
    conversion, a vault of notes and the stranded-highlight warning each built
    an index of their own and gave one highlight to both books. Every one of
    them builds it here now.

    A book that arrived already zipped answers to a name too: ``b/Foo.epub``
    the file and ``a/Foo.epub/`` the package are one key. Only package
    directories were counted, so the zipped book's highlights were embedded in
    the package's archive and written into its vault note. Every caller passes
    the library's copyable files as well, whether or not this run copies them:
    the file is in the library either way, and so are its highlights.

    :param found: Every annotation collected.
    :param packages: Every package the run knows about, which is the only
        place the paths are known.
    :param copyable: Every file in the library that
        :func:`collect_copyable` finds. Keyword-only and required, so a new
        caller cannot leave the zipped books out by omission, which is how the
        defect above was written.
    :param quiet: Leave the warning to a caller that has already given it.

    :return: What :func:`~epubconvert.collect.annotations.index_by_book`
        builds, less every name more than one book answers to.
    """
    index = index_by_book(found)
    seen: dict[str, Path] = {}
    ambiguous: set[str] = set()
    for book in itertools.chain(packages, copyable):
        if seen.setdefault(book.name, book) != book:
            ambiguous.add(book.name)
    for name in sorted(ambiguous & index.keys()):
        dropped = index.pop(name)
        if not quiet:
            logger.warning(
                "Skipped %d annotation(s) for %s: more than one package or "
                "already-zipped book in the library has that name, so which "
                "book they belong to cannot be told apart.",
                len(dropped),
                printable(name),
            )
    return index


def _same_annotations(
    held: bytes | None, annotations: Sequence[dict[str, object]]
) -> bool:
    """
    Whether an archive already carries exactly these annotations.

    :param held: The embedded document as it stands, or None if there is none.
    :param annotations: What it should carry.

    :return: True if a rewrite would change nothing.
    """
    if held is None:
        return not annotations
    try:
        loaded = json.loads(held)
    except (ValueError, RecursionError):
        # Unreadable, so replacing it is the point rather than a no-op. The
        # member comes from a package directory that arrived from Apple or a
        # sideload, so it is not assumed to be JSON, let alone an object:
        # deeply nested input raises RecursionError, which is not a ValueError.
        return False
    stored = loaded.get("annotations") if isinstance(loaded, dict) else None
    return bool(stored == list(annotations))


#: The most of an embedded annotation set a refresh reads to compare it. The
#: archive may not be one this tool wrote, and its declared size is no bound.
#: A set larger than this is replaced rather than read, as a malformed one is.
MAX_EMBEDDED_BYTES = 64 * 1024 * 1024


class NoRoomError(Exception):
    """
    Raised when a rebuild was due and the volume is below the floor.

    Not an ``OSError``: a refresh treats those as one damaged book and goes on
    to the next, and a full volume is the same answer for every book after it.
    """


def replace_annotations(
    target_archive: Path,
    annotations: Sequence[dict[str, object]],
    *,
    room: Callable[[], bool] | None = None,
) -> bool:
    """
    Swap the embedded annotation set of an archive already on the shelf.

    A zip member cannot be replaced in place, so the archive is rebuilt beside
    itself and moved over the original -- the same temporary-then-replace path
    a conversion uses, so an interrupted refresh leaves the old archive intact
    rather than a half-written one.

    Every other member is copied across verbatim, and ``mimetype`` keeps its
    place and its lack of compression: it is the one member whose position the
    specification fixes.

    :param target_archive: The archive to refresh.
    :param annotations: The annotations this book should now carry.
    :param room: Asked once a rebuild is known to be due, just before the
        copy is started beside the original. Asking earlier refused a refresh
        that had nothing to write, and a shelf already up to date on a full
        volume reported a failure.

    :return: True if the archive was rewritten, False if it already said this.

    :raises NoRoomError: If *room* said there is no room for the copy.
    """
    # An empty set is not an instruction to delete. A package that arrived
    # carrying its own annotations lost them silently when this run happened
    # to have none for that book, and the shelf is the only record there is.
    if not annotations:
        return False

    partial: Path | None = None
    target_archive, mode = _what_to_replace(target_archive)
    try:
        with ZipFile(target_archive) as reading:
            names = reading.namelist()
            held = (
                read_member(reading, reading.getinfo(EMBEDDED_PATH), MAX_EMBEDDED_BYTES)
                if EMBEDDED_PATH in names
                else None
            )

            # Compared before the members are read, not after. Every member was
            # being decompressed into memory to reach a comparison that only
            # looks at this one small blob: 7.86 MB of peak allocation on a
            # 6.4 MB book, to decide against rewriting it.
            if _same_annotations(held, annotations):
                return False
            if room is not None and not room():
                raise NoRoomError(target_archive.name)

            members = [
                info for info in reading.infolist() if info.filename != EMBEDDED_PATH
            ]
            handle, temporary = tempfile.mkstemp(
                dir=target_archive.parent, prefix=PARTIAL_PREFIX, suffix=PARTIAL_SUFFIX
            )
            os.close(handle)
            partial = Path(temporary)
            _rebuild(reading, members, partial, embedded_json(list(annotations)))
            # After the rebuild, as write_atomically does: a book the user
            # made read-only would otherwise make its own partial unwritable.
            partial.chmod(mode)

        # Replaced once the original is closed rather than while it is still
        # being read, which a platform that locks open files refuses.
        assert_is_a_book(
            target_archive.name,
            {info.filename for info in members} | {EMBEDDED_PATH},
        )
        partial.replace(target_archive)
    except BaseException:
        if partial is not None:
            partial.unlink(missing_ok=True)
        raise
    return True


def _rebuild(
    reading: ZipFile, members: list[ZipInfo], partial: Path, embedded: str
) -> None:
    """
    Copy archive members into a new archive, one stream at a time, and embed
    an annotation set after them.

    Streamed rather than read into a list first: that held the whole book in
    memory, so refreshing a 300 MB book peaked at 300 MB, and the MemoryError
    a large one raised is not an error a refresh reports and moves past. Each
    member keeps its compression and gets the same normalized entry a fresh
    export gives it, so a refreshed book is byte-identical to one exported
    with the same annotations in the first place.

    :param reading: The archive being refreshed, open for reading.
    :param members: The members to carry across, in order.
    :param partial: The new archive to write.
    :param embedded: The annotation document to store after the members.
    """
    with ZipFile(partial, "w", ZIP_DEFLATED, compresslevel=COMPRESS_LEVEL) as writing:
        for info in members:
            member = entry(info.filename, info.compress_type)
            _size_ahead(member, info.file_size)
            with (
                open_member(reading, info) as source,
                writing.open(member, "w") as target,
            ):
                shutil.copyfileobj(source, target)
        writing.writestr(entry(EMBEDDED_PATH, compression_for(EMBEDDED_PATH)), embedded)


def write_atomically(target: Path, text: str) -> None:
    """
    Replace a file's contents, or leave the old contents alone.

    ``write_text`` truncates before it writes, so a failure partway through --
    a full disk, a Ctrl-C -- left the export as a prefix of itself, which is
    neither the old file nor the new one. It is also not valid JSON, so every
    later run then refused to write to that path at all. The export is the
    artifact the merge machinery exists to protect; this is the same
    temporary-then-replace path :func:`~epubconvert.export.archive.zip_package` uses.

    A replace swaps in a new file, so three things the old one carried are
    carried across deliberately:

    - **Its mode.** Every rerun wrote the partial at the umask's mode, so an
      export the user had made 0600 became readable by everyone again.
    - **Its being a link.** The replace landed on the link itself, so a link
      into a synced folder became a regular file here and the synced copy went
      stale without a word. A link is now written through: the partial goes
      beside the file it resolves to, so the rename stays atomic there.
    - **Its contents, durably.** Without an fsync before the rename, a crash
      just after it can leave the name on a file whose data never reached the
      disk -- the old contents gone and the new ones empty.

    :param target: The file to replace.
    :param text: What it should hold.

    :raises OSError: If it could not be written. The old file survives.
    """
    target, mode = _what_to_replace(target)
    handle, temporary = tempfile.mkstemp(
        dir=target.parent, prefix=PARTIAL_PREFIX, suffix=PARTIAL_SUFFIX
    )
    os.close(handle)
    partial = Path(temporary)
    try:
        partial.write_text(text, encoding="utf-8")
        _sync(partial)
        # After the write, not before: a target the user made read-only would
        # otherwise make its own partial unwritable.
        partial.chmod(mode)
        partial.replace(target)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise


def _what_to_replace(target: Path) -> tuple[Path, int]:
    """
    Find the file a replace should land on, and the mode it should keep.

    Resolved unconditionally: a plain path resolves to itself, give or take a
    linked parent, and a link resolves to the file the user meant, so the
    replace lands there and the link survives. Shared by every path that
    replaces a file the user may have set up: the refresh of a book on the
    shelf once reset its mode and replaced a linked entry, after the notes and
    exports had been fixed for exactly that.

    :param target: The path about to be replaced.

    :return: The file to replace, and the permission bits to give the new one.
    """
    resolved = Path(os.path.realpath(target))
    try:
        # Permission bits only: a setuid or sticky bit is not something to
        # reproduce.
        return resolved, stat.S_IMODE(resolved.stat().st_mode) & 0o777
    except FileNotFoundError:
        return resolved, file_mode()


def _sync(path: Path) -> None:
    """
    Push a written file's data to the disk.

    ``fsync`` flushes the file, not the descriptor it is called on, so a fresh
    descriptor on a file that has just been written and closed is enough.

    :param path: The file to flush.
    """
    descriptor = os.open(path, os.O_RDWR)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
