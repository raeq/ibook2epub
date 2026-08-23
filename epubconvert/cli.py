"""
The command line surface.

Kept apart from the conversion logic so that the argument definitions, which
are long and change often, do not crowd the module that does the work.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .defaults import (
    DEFAULT_MAX_EXPORT_FILES,
    DEFAULT_MIN_FREE_MB,
    DEFAULT_OUTPUT,
    discover_source,
)
from .naming import PORTABLE_MODES, STRIP
from .planning import COLLISION_MODES, SKIP
from .validate import epubcheck_available


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
        "--list",
        action="store_true",
        dest="list_only",
        help=(
            "List every book with its status (pending, exported, collision, "
            "drm, incomplete) and exit without converting anything."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="With --list, emit machine-readable JSON instead of a table.",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help=(
            "Re-export a book when its source directory is newer than the "
            "exported file. Compares directory timestamps, so a book "
            "re-downloaded in place may not be noticed; use --force for that."
        ),
    )
    parser.add_argument(
        "--skip-incomplete",
        action="store_true",
        help=(
            "Skip books iCloud has not downloaded, which would otherwise "
            "export as empty files. Requires walking every package, which is "
            "slow on a cloud library, so it is off by default."
        ),
    )
    parser.add_argument(
        "--on-collision",
        choices=COLLISION_MODES,
        default=SKIP,
        help=(
            "What to do when two books want the same output name: 'skip' "
            "exports only the first, 'suffix' keeps both by appending ' (2)'."
        ),
    )
    parser.add_argument(
        "--min-free",
        type=int,
        default=DEFAULT_MIN_FREE_MB,
        metavar="MB",
        help=(
            "Stop before the output volume drops below this many megabytes, "
            f"default={DEFAULT_MIN_FREE_MB}. 0 disables the check. Useful "
            "when writing to an SD card or a Kindle."
        ),
    )
    parser.add_argument(
        "--covers",
        action="store_true",
        help="Also write each book's cover image beside its epub file.",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help=(
            "Check each archive before it is moved into place: zip integrity, "
            "the mimetype entry, and that every file the package document "
            "lists is really present. A book that fails is not written, so it "
            "is retried on the next run."
        ),
    )
    parser.add_argument(
        "--epubcheck",
        action="store_true",
        help=(
            "Also run the external 'epubcheck' tool on each archive. Implies "
            "--validate and requires epubcheck on PATH."
        ),
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help=(
            "Check the archives already in the output directory and report "
            "any that are damaged, then exit without converting anything."
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

    if args.epubcheck:
        args.validate = True
        if not epubcheck_available():
            parser.error("--epubcheck needs the 'epubcheck' tool on PATH")

    args.source_auto = args.source_dir is None
    if args.source_auto:
        args.source_dir = discover_source()

    # --verify only reads the output directory. Requiring an iBooks library
    # for it would stop anyone checking a shelf of exported books on a machine
    # that never had one.
    if not args.verify and not args.source_dir.is_dir():
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
