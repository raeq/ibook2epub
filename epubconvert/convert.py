"""
Convert Apple iBooks epub packages to zipped epub files.

Apple stores books as ``*.epub/`` directories rather than as epub archives.
This module walks a source directory for those packages and writes a
spec-valid epub file for each one: a zip archive whose first member is an
uncompressed ``mimetype`` entry containing ``application/epub+zip``, followed
by the deflated remainder of the package.

Archives are built to a temporary ``.part`` file and moved into place only on
success, so an interrupted run never leaves a truncated ``.epub`` behind that
a later run would mistake for finished work.
"""

from __future__ import annotations

import argparse
import asyncio
import fnmatch
import os
import shutil
import socket
import tempfile
import threading
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from random import shuffle
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

from . import __version__, app_logger
from .app_logger import logger
from .naming import (
    PORTABLE_MODES,
    STRIP,
    NamingPolicy,
    PassthroughNaming,
    PortableNamesUnavailableError,
    build_policy,
)

try:
    import fcntl

    HAVE_FLOCK = True
except ImportError:  # pragma: no cover - Windows has no fcntl
    HAVE_FLOCK = False

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

# Zip cannot represent a timestamp before 1980; using its floor keeps every
# export byte-identical regardless of when it ran.
ARCHIVE_TIMESTAMP = (1980, 1, 1, 0, 0, 0)

MIMETYPE_NAME = "mimetype"
MIMETYPE_CONTENT = "application/epub+zip"
PACKAGE_SUFFIX = ".epub"
PARTIAL_SUFFIX = ".part"
COMPRESS_LEVEL = 9
ARCHIVE_MODE = 0o644
LOCK_NAME = ".ibook2epub.lock"

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


class OutputLockedError(RuntimeError):
    """Raised when another run already holds the output directory lock."""


@dataclass
class Report:
    """Outcome of an export run."""

    exported: int = 0
    files_written: int = 0
    skipped: int = 0
    failed: int = 0
    planned: int = 0  # Dry-run only: exports that would have been attempted.
    collisions: int = 0  # Distinct packages that share one output name.
    interrupted: bool = False  # The run was stopped with Ctrl-C.


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
            if name.endswith(PACKAGE_SUFFIX):
                found.append(Path(root) / name)
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


def zip_package(source_dir: Path, target_archive: Path) -> int:
    """
    Write a single package directory out as a spec-valid epub archive.

    This is a blocking function, intended to be handed to a worker thread. The
    archive is assembled under a temporary name and only moved to
    ``target_archive`` once it is complete.

    :param source_dir: The ``*.epub/`` package directory to compress.
    :param target_archive: The path of the epub file to create.

    :return: The number of package files stored, excluding ``mimetype``.
    """
    # Deriving the temporary name from the target overflows the filesystem's
    # per-component limit when the target is already at it: 255 bytes plus
    # ".part" is 260. Take a short unique name in the same directory instead,
    # which keeps the closing replace atomic and can never be too long.
    handle, partial_name = tempfile.mkstemp(
        dir=target_archive.parent, suffix=PARTIAL_SUFFIX
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

            for path in sorted(source_dir.rglob("*")):
                if not path.is_file():
                    continue
                if is_excluded(path.name, at_root=path.parent == source_dir):
                    logger.trace("Skipped object: <%s>", path.name)
                    continue
                arcname = path.relative_to(source_dir).as_posix()
                entry = _entry(arcname, ZIP_DEFLATED)
                with path.open("rb") as source, archive.open(entry, "w") as target:
                    shutil.copyfileobj(source, target)
                file_count += 1

        partial.replace(target_archive)
    except BaseException:
        # Leave no partial archive behind, so the "already exported" check
        # stays a reliable record of completed work.
        partial.unlink(missing_ok=True)
        raise

    return file_count


#: Guards the shared Report and progress counter, which worker threads update.
_REPORT_LOCK = threading.Lock()


class _Progress:  # pylint: disable=too-few-public-methods
    """Counts finished books so each log line shows how far along the run is."""

    def __init__(self, total: int) -> None:
        self.total = total
        self.done = 0

    def tick(self) -> str:
        """Advance the counter and render it as ``[12/240]``."""
        self.done += 1
        return f"[{self.done}/{self.total}]"


def _zip_and_record(
    package: Path, target: Path, report: Report, progress: _Progress
) -> None:
    """
    Export one package and record the outcome, all in the worker thread.

    The bookkeeping deliberately happens here rather than in the awaiting
    coroutine. On Ctrl-C the event loop is torn down and those coroutines
    never resume, so counting there would report a book as not exported while
    its file was already atomically in place. Recording in the same thread
    that did the work keeps the summary consistent with the directory.

    :param package: The package directory to compress.
    :param target: The epub file to create.
    :param report: Report to record the outcome in.
    :param progress: Shared counter for the ``[n/total]`` prefix.
    """
    try:
        file_count = zip_package(package, target)
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-except
        with _REPORT_LOCK:
            report.failed += 1
            marker = progress.tick()
        logger.error("%s Failed to export %s: %s", marker, package.name, exc)
        return

    with _REPORT_LOCK:
        report.exported += 1
        report.files_written += file_count
        marker = progress.tick()
    logger.info("%s Exported %s (%d files)", marker, target.name, file_count)


async def _export_one(
    pool: ThreadPoolExecutor,
    package: Path,
    target: Path,
    report: Report,
    progress: _Progress,
) -> None:
    """
    Hand one package to a worker thread.

    :param pool: The executor running the compression work.
    :param package: The package directory to compress.
    :param target: The epub file to create.
    :param report: Report to record the outcome in.
    :param progress: Shared counter for the ``[n/total]`` prefix.
    """
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(pool, _zip_and_record, package, target, report, progress)


def _plan_exports(
    packages: Sequence[Path],
    output_dir: Path,
    policy: NamingPolicy,
    *,
    dry_run: bool,
    report: Report,
    force: bool = False,
) -> list[tuple[Path, Path]]:
    """
    Decide which packages to export and under what names.

    Books already present in *output_dir* are recognised by recomputing their
    identity from the filenames on disk, so no state file is needed. Two
    packages that want the same output name are a collision: the first wins
    and the second is reported rather than silently overwritten.

    :param packages: Package directories to consider.
    :param output_dir: Directory the epub files are written into.
    :param policy: Naming policy supplying filenames and identities.
    :param dry_run: When True, plan the work but queue nothing.
    :param report: Report to accumulate skip, collision and plan counts into.

    :return: The (package, target) pairs to export.
    """
    pending: list[tuple[Path, Path]] = []

    # Missing directories glob to nothing, which is what a dry run wants.
    # --force pretends the output directory is empty so everything re-exports.
    on_disk = (
        set()
        if force
        else {
            policy.identity(existing.name)
            for existing in output_dir.glob(f"*{PACKAGE_SUFFIX}")
        }
    )
    claimed: set[str] = set()

    for package in packages:
        filename = policy.filename(package.name)
        target = output_dir / filename
        key = policy.identity(filename)

        if key in on_disk:
            logger.info("Already exported, skipping: %s", package.name)
            report.skipped += 1
            continue

        if key in claimed:
            logger.warning(
                "Name collision, skipping: %s would also export to %s",
                package,
                target.name,
            )
            report.collisions += 1
            continue

        claimed.add(key)

        if dry_run:
            logger.info("Would export: %s -> %s", package, target)
            report.planned += 1
            continue

        logger.debug("Queued for export: %s", package)
        pending.append((package, target))

    return pending


async def export_packages(
    packages: Sequence[Path],
    output_dir: Path,
    *,
    dry_run: bool = False,
    max_workers: int | None = None,
    naming: NamingPolicy | None = None,
    force: bool = False,
    report: Report | None = None,
) -> Report:
    """
    Export a batch of packages concurrently.

    Whether a book has already been exported is decided by its *identity*
    under the naming policy, not by an exact filename match. Identities of
    completed work are recomputed by reading the output directory, so that
    directory remains the sole record of what has been converted and no state
    file is needed.

    :param packages: Package directories to export.
    :param output_dir: Directory to write the epub files into.
    :param dry_run: When True, report what would happen without writing.
    :param max_workers: Size of the compression thread pool.
    :param naming: Naming policy; defaults to :class:`PassthroughNaming`.
    :param force: Re-export books already present in the output directory.
    :param report: Report to accumulate into. Pass one to retain the partial
        counts if the run is interrupted.

    :return: A report of what was exported, skipped and failed.
    """
    policy = naming if naming is not None else PassthroughNaming()
    report = report if report is not None else Report()
    pending = _plan_exports(
        packages, output_dir, policy, dry_run=dry_run, report=report, force=force
    )

    if report.skipped:
        logger.info("Skipped %d already-exported file(s).", report.skipped)
    if report.collisions:
        logger.warning(
            "%d package(s) skipped because another book claims the same output name.",
            report.collisions,
        )

    if not pending:
        return report

    progress = _Progress(len(pending))
    pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="zip")
    try:
        await asyncio.gather(
            *(
                _export_one(pool, package, target, report, progress)
                for package, target in pending
            )
        )
    finally:
        # cancel_futures drops books that have not started, so an interrupt
        # does not wait for the whole queued backlog. wait=True still joins
        # the handful already being written: they finish, replace atomically,
        # and record themselves, which keeps the summary honest about what is
        # on disk.
        pool.shutdown(wait=True, cancel_futures=True)

    return report


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


def filter_packages(packages: Sequence[Path], pattern: str | None) -> list[Path]:
    """
    Narrow the package list to those matching a user pattern.

    A pattern with no glob metacharacter matches anywhere in the name, so
    ``--match hobbit`` finds ``The Hobbit.epub``. Anything else is treated as
    a glob against the whole name. Matching is case-insensitive.

    :param packages: Discovered package directories.
    :param pattern: The user's pattern, or None to keep everything.

    :return: The packages that matched.
    """
    if pattern is None:
        return list(packages)

    needle = pattern.lower()
    if not any(char in needle for char in "*?["):
        needle = f"*{needle}*"

    matched = [p for p in packages if fnmatch.fnmatch(p.name.lower(), needle)]
    logger.info(
        "Matched %d of %d package(s) against %r", len(matched), len(packages), pattern
    )
    return matched


def count_pending(
    packages: Sequence[Path], output_dir: Path, policy: NamingPolicy
) -> int:
    """
    Count distinct books that are not yet in the output directory.

    This is what makes the batching workflow legible: with the default cap,
    a run can report how much of the library is still to do.

    :param packages: All packages under consideration.
    :param output_dir: Directory the epub files are written into.
    :param policy: Naming policy supplying filenames and identities.

    :return: The number of books still to export.
    """
    on_disk = {
        policy.identity(existing.name)
        for existing in output_dir.glob(f"*{PACKAGE_SUFFIX}")
    }
    wanted = {policy.identity(policy.filename(p.name)) for p in packages}
    return len(wanted - on_disk)


@contextmanager
def output_lock(output_dir: Path) -> Iterator[None]:
    """
    Hold an advisory lock on the output directory for the duration of a run.

    Rerun safety invites scheduling this from cron or launchd, where two runs
    can overlap. Both would read the output directory before either wrote to
    it and export the same books; ``os.replace`` keeps that from corrupting
    anything, but it wastes the work and confuses the logs.

    On platforms without ``fcntl`` (Windows) this is a no-op.

    :param output_dir: Directory to lock.

    :raises OutputLockedError: If another run already holds the lock.
    """
    if not HAVE_FLOCK:  # pragma: no cover - exercised only on Windows
        yield
        return

    path = output_dir / LOCK_NAME
    # Opened "r+" where possible so a failed lock can still read the holder's
    # details; "w" would truncate them before we got to report them.
    handle = path.open("r+") if path.exists() else path.open("w")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise OutputLockedError(
                f"another ibook2epub run is already using {output_dir} "
                f"({_read_lock_holder(handle)})"
            ) from exc

        # The PID is recorded for diagnostics only. It is deliberately NOT
        # used to detect or remove an orphaned lock: flock is released by the
        # kernel when the holder dies, even on SIGKILL, so a leftover lock
        # file is inert. Checking liveness by PID would add a PID-reuse race
        # to solve a problem that does not exist.
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()} host={socket.gethostname()}\n")
        handle.flush()
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        handle.close()


def _read_lock_holder(handle: object) -> str:
    """
    Describe whoever currently holds the lock, for the error message.

    :param handle: The open lock file.

    :return: A short description, or a fallback when nothing is readable.
    """
    try:
        handle.seek(0)  # type: ignore[attr-defined]
        details = handle.read().strip()  # type: ignore[attr-defined]
    except OSError:  # pragma: no cover - unreadable lock file
        return "holder unknown"
    return details or "holder unknown"


def build_parser() -> argparse.ArgumentParser:
    """
    Build the command line parser.

    :return: The configured argument parser.
    """
    parser = argparse.ArgumentParser(
        prog="ibook2epub",
        description="Convert Apple iBooks epub packages to zipped epub files.",
    )
    parser.add_argument(
        "-m",
        "--max-export-files",
        type=int,
        default=DEFAULT_MAX_EXPORT_FILES,
        metavar="N",
        help=(
            "Maximum number of epub files to export, "
            f"default={DEFAULT_MAX_EXPORT_FILES}, 0=no limit."
        ),
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Path of the output directory; created if it does not exist.",
    )
    parser.add_argument(
        "-s",
        "--source-dir",
        type=Path,
        default=None,
        help=(
            "Path of the source directory containing *.epub/ packages. "
            "Defaults to whichever known iBooks location holds books."
        ),
    )
    parser.add_argument(
        "-d",
        "--dry-run",
        action="store_true",
        help="Report what would be exported without writing anything.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Re-export books even if they are already in the output directory.",
    )
    parser.add_argument(
        "--match",
        default=None,
        metavar="PATTERN",
        help=(
            "Only convert books whose name matches PATTERN. A pattern without "
            "wildcards matches anywhere in the name, so --match hobbit finds "
            "'The Hobbit.epub'; otherwise it is a glob. Case-insensitive."
        ),
    )
    parser.add_argument(
        "-w",
        "--workers",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Number of compression threads. The work is I/O bound on a cloud "
            "library, so a value above the CPU count often helps."
        ),
    )
    parser.add_argument(
        "-p",
        "--portable-names",
        nargs="?",
        const=STRIP,
        default=None,
        choices=PORTABLE_MODES,
        metavar="MODE",
        help=(
            "Rewrite output names so they survive a copy to Windows, exFAT or "
            "a Kindle. 'strip' (the default when -p is given alone) removes "
            "the characters those filesystems reject and needs no extra "
            "packages. 'romanize' also transliterates non-Latin titles and "
            "folds accents when deciding whether a book is already exported, "
            "and needs the 'disarm' extra. Either mode renames books an "
            "earlier run already exported."
        ),
    )
    parser.add_argument(
        "--no-shuffle",
        action="store_true",
        help=(
            "Take the first N packages in sorted order instead of a random "
            "selection when --max-export-files applies."
        ),
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Increase log verbosity; -v for debug, -vv for trace.",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Only log warnings and errors.",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=None,
        metavar="PATH",
        help="Also write log records to this file.",
    )
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """
    Parse and validate command line arguments.

    :param argv: Argument list, defaulting to ``sys.argv[1:]``.

    :return: The parsed arguments.
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.max_export_files < 0:
        parser.error("--max-export-files must be 0 or greater")

    if args.workers is not None and args.workers < 1:
        parser.error("--workers must be 1 or greater")

    args.source_auto = args.source_dir is None
    if args.source_auto:
        args.source_dir = discover_source()

    if not args.source_dir.is_dir():
        parser.error(f"source directory does not exist: {args.source_dir}")

    # Writing into the tree being scanned pollutes the next run: temporary
    # files land mid-scan and finished exports look like source packages.
    source = args.source_dir.resolve()
    output = args.output_dir.resolve()
    if output == source or output.is_relative_to(source):
        parser.error(
            f"output directory must not be inside the source directory: "
            f"{args.output_dir}"
        )

    return args


def select_packages(
    packages: Sequence[Path], max_export_files: int, randomise: bool = True
) -> list[Path]:
    """
    Apply the export cap to the discovered packages.

    :param packages: All discovered package directories.
    :param max_export_files: The cap, where 0 means no limit.
    :param randomise: Choose the capped subset at random.

    :return: The packages to export.
    """
    selected = list(packages)

    if not max_export_files:
        logger.info("All %d epub package(s) will be processed.", len(selected))
        return selected

    if len(selected) <= max_export_files:
        logger.info("All %d epub package(s) will be processed.", len(selected))
        return selected

    if randomise:
        shuffle(selected)
    selected = selected[:max_export_files]
    logger.info(
        "Limiting activity to %d of %d epub package(s).",
        max_export_files,
        len(packages),
    )
    return selected


def format_summary(
    report: Report, output_dir: Path, dry_run: bool, remaining: int | None = None
) -> str:
    """
    Render a one-line human readable summary of a run.

    :param report: The report to render.
    :param output_dir: The directory the run targeted.
    :param dry_run: Whether the run was a dry run.
    :param remaining: Books still to convert after this run, if known.

    :return: The summary line.
    """
    if dry_run:
        summary = (
            f"Dry run: would export {report.planned} epub file(s) to "
            f"{output_dir} (skipped {report.skipped} already present"
        )
        if report.collisions:
            summary += f", {report.collisions} name collision(s)"
        summary += ")."
        if remaining:
            summary += f" {remaining} remaining."
        return summary

    summary = (
        f"Exported {report.exported} epub file(s) "
        f"({report.files_written} member files) to {output_dir}"
    )
    if report.skipped:
        summary += f", skipped {report.skipped}"
    if report.collisions:
        summary += f", {report.collisions} name collision(s)"
    if report.failed:
        summary += f", failed {report.failed}"
    summary += "."
    if report.interrupted:
        summary = f"Interrupted. {summary}"
    if remaining:
        summary += f" {remaining} remaining; rerun to continue."
    return summary


def _run_export(args: argparse.Namespace, policy: NamingPolicy) -> tuple[Report, int]:
    """
    Collect, select and export, under the output directory lock.

    :param args: Parsed command line arguments.
    :param policy: The naming policy in force.

    :return: The run's report, and the number of books still to convert.

    :raises OutputLockedError: If another run holds the output lock.
    """
    packages = filter_packages(collect_package_dirs(args.source_dir), args.match)
    if not packages:
        logger.warning("No matching *.epub packages found under %s", args.source_dir)

    pending_before = count_pending(packages, args.output_dir, policy)
    selected = select_packages(
        packages, args.max_export_files, randomise=not args.no_shuffle
    )

    # Held here rather than inside export_packages so the partial counts
    # survive a Ctrl-C.
    report = Report()

    # A dry run writes nothing, so it needs no lock and must not create one.
    lock = nullcontext() if args.dry_run else output_lock(args.output_dir)
    with lock:
        try:
            asyncio.run(
                export_packages(
                    selected,
                    args.output_dir,
                    dry_run=args.dry_run,
                    max_workers=args.workers,
                    naming=policy,
                    force=args.force,
                    report=report,
                )
            )
        except KeyboardInterrupt:
            # Stopping is a normal way to end a long run: every finished book
            # is already complete and atomically in place, so a rerun simply
            # continues.
            report.interrupted = True

    return report, max(0, pending_before - report.exported)


def main(argv: Sequence[str] | None = None) -> int:
    """
    Run the conversion from the command line.

    :param argv: Argument list, defaulting to ``sys.argv[1:]``.

    :return: A process exit code; non-zero if any export failed.
    """
    args = parse_args(argv)

    verbosity = 0 if args.quiet else 1 + args.verbose
    app_logger.configure(verbosity=verbosity, log_file=args.log_file)

    try:
        policy = build_policy(args.portable_names)
    except PortableNamesUnavailableError as exc:
        logger.critical("%s", exc)
        return 2

    logger.debug("Naming policy: %s", policy.label)
    if args.source_auto:
        logger.info("Using discovered iBooks library: %s", args.source_dir)
    else:
        logger.info("Examining source: %s", args.source_dir)
    logger.info("Writing output to: %s", args.output_dir)
    if args.portable_names == STRIP:
        logger.info("Portable naming: stripping characters other filesystems reject.")
    elif args.portable_names:
        logger.info(
            "Portable naming: romanizing (non-Latin titles are transliterated)."
        )
    if args.dry_run:
        logger.info(
            "Running in dry-run mode. No file system modifications will be performed."
        )

    if not args.dry_run:
        try:
            args.output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.critical("Could not create output directory: %s", exc)
            return 1

    try:
        report, remaining = _run_export(args, policy)
    except OutputLockedError as exc:
        logger.critical("%s", exc)
        return 3

    print(format_summary(report, args.output_dir, args.dry_run, remaining))
    logger.debug("Ending the convert application.")

    if report.interrupted:
        return 130  # Conventional exit code for termination by SIGINT.
    return 1 if report.failed else 0
