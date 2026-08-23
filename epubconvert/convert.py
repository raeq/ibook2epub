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
import os
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from random import shuffle
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

from . import app_logger
from .app_logger import logger
from .naming import (
    NamingPolicy,
    PassthroughNaming,
    PortableNamesUnavailableError,
    build_policy,
)

DEFAULT_SOURCE = (
    Path.home() / "Library/Mobile Documents/iCloud~com~apple~iBooks/Documents"
)
DEFAULT_OUTPUT = Path.home() / "Books"
DEFAULT_MAX_EXPORT_FILES = 5

MIMETYPE_NAME = "mimetype"
MIMETYPE_CONTENT = "application/epub+zip"
PACKAGE_SUFFIX = ".epub"
PARTIAL_SUFFIX = ".part"
COMPRESS_LEVEL = 9

# Apple-specific bookkeeping that must not be copied into the exported epub.
# ``mimetype`` is excluded here because it is written separately, uncompressed
# and first, as the epub specification requires.
EXCLUDED_NAMES = frozenset({MIMETYPE_NAME, ".DS_Store"})
EXCLUDED_SUFFIXES = frozenset({".plist"})
EXCLUDED_PREFIXES = ("bookmarks",)


@dataclass
class Report:
    """Outcome of an export run."""

    exported: int = 0
    files_written: int = 0
    skipped: int = 0
    failed: int = 0
    planned: int = 0  # Dry-run only: exports that would have been attempted.
    collisions: int = 0  # Distinct packages that share one output name.


def is_excluded(name: str) -> bool:
    """
    Report whether a package member should be left out of the epub.

    :param name: The bare file name (not a path) to test.

    :return: True if the file must not be copied into the archive.
    """
    return (
        name in EXCLUDED_NAMES
        or Path(name).suffix in EXCLUDED_SUFFIXES
        or name.startswith(EXCLUDED_PREFIXES)
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

    try:
        for root, dirs, _files in os.walk(source_dir):
            descend = []
            for name in dirs:
                if name.endswith(PACKAGE_SUFFIX):
                    found.append(Path(root) / name)
                else:
                    descend.append(name)
            dirs[:] = descend
    except OSError as exc:
        logger.error("Could not scan %s: %s", source_dir, exc)
        return []

    found.sort()
    logger.debug("Found %d epub package(s) under %s", len(found), source_dir)
    return found


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
    partial = target_archive.parent / (target_archive.name + PARTIAL_SUFFIX)
    file_count = 0

    try:
        with ZipFile(partial, "w", ZIP_DEFLATED) as archive:
            # The mimetype entry must come first and must be stored, not deflated.
            archive.writestr(MIMETYPE_NAME, MIMETYPE_CONTENT, compress_type=ZIP_STORED)

            for path in sorted(source_dir.rglob("*")):
                if not path.is_file():
                    continue
                if is_excluded(path.name):
                    logger.trace("Skipped object: <%s>", path.name)
                    continue
                arcname = path.relative_to(source_dir).as_posix()
                archive.write(path, arcname, compresslevel=COMPRESS_LEVEL)
                file_count += 1

        partial.replace(target_archive)
    except BaseException:
        # Leave no partial archive behind, so the "already exported" check
        # stays a reliable record of completed work.
        partial.unlink(missing_ok=True)
        raise

    return file_count


async def _export_one(
    pool: ThreadPoolExecutor, package: Path, target: Path
) -> int | None:
    """
    Export one package in a worker thread, absorbing any failure.

    :param pool: The executor running the compression work.
    :param package: The package directory to compress.
    :param target: The epub file to create.

    :return: The file count on success, or None if the export failed.
    """
    loop = asyncio.get_running_loop()
    try:
        file_count = await loop.run_in_executor(pool, zip_package, package, target)
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-except
        logger.error("Failed to export %s: %s", package.name, exc)
        return None

    logger.info("Exported %s (%d files)", target.name, file_count)
    return file_count


def _plan_exports(
    packages: Sequence[Path],
    output_dir: Path,
    policy: NamingPolicy,
    dry_run: bool,
    report: Report,
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
    on_disk = {
        policy.identity(existing.name)
        for existing in output_dir.glob(f"*{PACKAGE_SUFFIX}")
    }
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
    dry_run: bool = False,
    max_workers: int | None = None,
    naming: NamingPolicy | None = None,
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

    :return: A report of what was exported, skipped and failed.
    """
    policy = naming if naming is not None else PassthroughNaming()
    report = Report()
    pending = _plan_exports(packages, output_dir, policy, dry_run, report)

    if report.skipped:
        logger.info("Skipped %d already-exported file(s).", report.skipped)
    if report.collisions:
        logger.warning(
            "%d package(s) skipped because another book claims the same output name.",
            report.collisions,
        )

    if not pending:
        return report

    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="zip") as pool:
        results = await asyncio.gather(
            *(_export_one(pool, package, target) for package, target in pending)
        )

    for file_count in results:
        if file_count is None:
            report.failed += 1
        else:
            report.exported += 1
            report.files_written += file_count

    return report


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
        default=DEFAULT_SOURCE,
        help="Path of the source directory containing *.epub/ packages.",
    )
    parser.add_argument(
        "-d",
        "--dry-run",
        action="store_true",
        help="Report what would be exported without writing anything.",
    )
    parser.add_argument(
        "-p",
        "--portable-names",
        action="store_true",
        help=(
            "Rewrite output names so they survive a copy to Windows, exFAT or "
            "a Kindle, and treat case/accent variants of a title as the same "
            "book. Romanizes non-Latin titles, and renames files an earlier "
            "run already exported. Needs the 'disarm' extra."
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

    if not args.source_dir.is_dir():
        parser.error(f"source directory does not exist: {args.source_dir}")

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

    if randomise:
        shuffle(selected)
    selected = selected[:max_export_files]
    logger.info("Limiting activity to a maximum of %d epub file(s).", max_export_files)
    return selected


def format_summary(report: Report, output_dir: Path, dry_run: bool) -> str:
    """
    Render a one-line human readable summary of a run.

    :param report: The report to render.
    :param output_dir: The directory the run targeted.
    :param dry_run: Whether the run was a dry run.

    :return: The summary line.
    """
    if dry_run:
        summary = (
            f"Dry run: would export {report.planned} epub file(s) to "
            f"{output_dir} (skipped {report.skipped} already present"
        )
        if report.collisions:
            summary += f", {report.collisions} name collision(s)"
        return summary + ")."

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
    return summary + "."


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

    logger.info("Examining source: %s", args.source_dir)
    logger.info("Writing output to: %s", args.output_dir)
    if args.portable_names:
        logger.info("Portable naming enabled (non-Latin titles are romanized).")
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

    packages = collect_package_dirs(args.source_dir)
    if not packages:
        logger.warning("No *.epub packages found under %s", args.source_dir)

    selected = select_packages(
        packages, args.max_export_files, randomise=not args.no_shuffle
    )

    report = asyncio.run(
        export_packages(selected, args.output_dir, dry_run=args.dry_run, naming=policy)
    )

    print(format_summary(report, args.output_dir, args.dry_run))
    logger.debug("Ending the convert application.")

    return 1 if report.failed else 0
