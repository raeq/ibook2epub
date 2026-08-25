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
from .planning import COLLISION_MODES, SKIP, STATUSES


def build_parser() -> argparse.ArgumentParser:
    """
    Build the command line parser.

    :return: The configured argument parser.
    """
    parser = argparse.ArgumentParser(
        prog="ibook2epub",
        description="Convert Apple iBooks epub packages to zipped epub files.",
    )
    # Grouped because twenty-three flags in one flat list is a wall. The help
    # text itself was already good; only its container was the problem.
    selection = parser.add_argument_group(
        "Choosing books", "Which books this run considers."
    )
    output = parser.add_argument_group(
        "Naming and output", "Where books go and what they are called."
    )
    planning = parser.add_argument_group(
        "Deciding what to do", "What the run does, or reports without doing."
    )
    integrity = parser.add_argument_group(
        "Checking the result", "Verifying archives and protecting the volume."
    )
    logging = parser.add_argument_group(
        "Output and logging", "How much the run says, and where."
    )
    selection.add_argument(
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
    output.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Path of the output directory; created if it does not exist.",
    )
    selection.add_argument(
        "-s",
        "--source-dir",
        type=Path,
        default=None,
        help=(
            "Path of the source directory containing *.epub/ packages. "
            "Defaults to whichever known iBooks location holds books."
        ),
    )
    planning.add_argument(
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
    planning.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Re-export books even if they are already in the output directory.",
    )
    selection.add_argument(
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
            "Number of compression threads (default: 4x the CPU count, "
            "capped at 64). The work blocks on iCloud rather than on the CPU, "
            "so raising this well past the CPU count is what helps; 48-64 is "
            "reasonable for a cloud library."
        ),
    )
    output.add_argument(
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
    planning.add_argument(
        "--list",
        action="store_true",
        dest="list_only",
        help=(
            f"List every *.epub/ package with its status "
            f"({', '.join(STATUSES)}) and exit without converting anything. "
            f"Anything in the source that is not a package is counted, not "
            f"listed."
        ),
    )
    planning.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="With --list, emit machine-readable JSON instead of a table.",
    )
    planning.add_argument(
        "--refresh",
        action="store_true",
        help=(
            "Re-export a book when its source directory is newer than the "
            "exported file. Compares directory timestamps, so a book "
            "re-downloaded in place may not be noticed; use --force for that."
        ),
    )
    planning.add_argument(
        "--skip-incomplete",
        action="store_true",
        help=(
            "Skip books iCloud has not downloaded, which would otherwise "
            "export as empty files. Requires walking every package, which is "
            "slow on a cloud library, so it is off by default."
        ),
    )
    output.add_argument(
        "--on-collision",
        choices=COLLISION_MODES,
        default=SKIP,
        help=(
            "What to do when two books want the same output name: 'skip' "
            "exports only the first, 'suffix' keeps both by appending ' (2)'."
        ),
    )
    integrity.add_argument(
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
    output.add_argument(
        "--no-copy-through",
        action="store_true",
        help=(
            "Do not copy already-valid .epub files and .pdf files to the "
            "output directory. They are copied by default, because a real "
            "library holds both Apple's package folders and books that "
            "arrived already zipped, and exporting only the first produces "
            "half a shelf."
        ),
    )
    output.add_argument(
        "--covers",
        action="store_true",
        help="Also write each book's cover image beside its epub file.",
    )
    integrity.add_argument(
        "--validate",
        action="store_true",
        help=(
            "Check each archive before it is moved into place: zip integrity, "
            "the mimetype entry, and that every file the package document "
            "lists is really present. A book that fails is not written, so it "
            "is retried on the next run."
        ),
    )
    integrity.add_argument(
        "--epubcheck",
        action="store_true",
        help=(
            "Also run the external 'epubcheck' tool on each archive. Implies "
            "--validate and requires epubcheck on PATH."
        ),
    )
    integrity.add_argument(
        "--verify",
        action="store_true",
        help=(
            "Check the archives already in the output directory and report "
            "any that are damaged, then exit without converting anything."
        ),
    )
    selection.add_argument(
        "--no-shuffle",
        action="store_true",
        help=(
            "Take the first N books still needing export, in sorted order, "
            "instead of a random selection when --max-export-files applies."
        ),
    )
    logging.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Increase log verbosity; -v for debug, -vv for trace.",
    )
    logging.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Only log warnings and errors.",
    )
    logging.add_argument(
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

    # Silently ignoring a flag the user typed is worse than refusing it: the
    # run does something other than what was asked and says nothing.
    if args.quiet and args.verbose:
        parser.error("--quiet and --verbose contradict each other")

    if args.as_json and not args.list_only:
        parser.error("--json only applies with --list")

    if args.list_only and args.verify:
        parser.error("--list and --verify cannot be combined")

    if args.epubcheck:
        args.validate = True

    args.source_auto = args.source_dir is None
    if args.source_auto:
        args.source_dir = discover_source()

    # --verify only reads the output directory. Requiring an iBooks library
    # for it would stop anyone checking a shelf of exported books on a machine
    # that never had one.

    # The environment -- whether directories exist, whether tools are
    # installed -- is checked in run.main, which can give each failure its own
    # exit code. parser.error always exits 2, and putting environment checks
    # here is what made five unrelated conditions indistinguishable.

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
