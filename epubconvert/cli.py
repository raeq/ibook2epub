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
from .annotations import STDOUT
from .defaults import (
    DEFAULT_MAX_EXPORT_FILES,
    DEFAULT_MIN_FREE_MB,
    DEFAULT_OUTPUT,
    discover_source,
)
from .naming import NAME_PASSTHROUGH, NAME_SOURCES, PORTABLE_MODES, STRIP
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
    # Their own group. They are four independent choices about one subject,
    # and reading them next to --portable-names would suggest they interact.
    annotations = parser.add_argument_group(
        "Your highlights and notes",
        "Taking annotations out of Apple Books. Any of these needs Full Disk "
        "Access on macOS. None is the default: a conversion touches Apple's "
        "container only when asked to.",
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
    annotations.add_argument(
        "-an",
        "--annotations-none",
        action="store_true",
        help=(
            "Do not touch annotations. The default, stated so a script can be "
            "explicit about it."
        ),
    )
    annotations.add_argument(
        "-ae",
        "--annotations-embedded",
        action="store_true",
        help=(
            "Write each book's annotations into it at META-INF/annotations.json, "
            "so they travel with the book to whatever reads it next."
        ),
    )
    annotations.add_argument(
        "-ad",
        "--annotations-detached",
        nargs="?",
        const=STDOUT,
        metavar="FILE",
        help=(
            "Write one file for the whole library. Survives losing the books, "
            "which is what taking them with you means. A rerun merges into it "
            "rather than replacing it. With no FILE, or with '-', it goes to "
            "standard output and everything else goes to standard error."
        ),
    )
    annotations.add_argument(
        "-ao",
        "--annotations-only",
        nargs="?",
        const=STDOUT,
        metavar="FILE",
        help=(
            "Write the detached file and nothing else: no conversion, no "
            "shelf. For somebody who wants their highlights and not three "
            "thousand epub files. With no FILE, or with '-', it goes to "
            "standard output."
        ),
    )
    annotations.add_argument(
        "-ar",
        "--annotations-refresh",
        action="store_true",
        help=(
            "Update annotations for books already converted, and convert "
            "nothing. A library is converted once and annotated for years "
            "afterwards; this picks up new highlights without rewriting every "
            "archive."
        ),
    )
    output.add_argument(
        "--name-by",
        choices=NAME_SOURCES,
        default=NAME_PASSTHROUGH,
        help=(
            "Where an output name comes from: 'passthrough' uses the package "
            "folder name, 'author-title' uses the book's own dc:title and "
            "dc:creator to write 'Author - Title.epub'. Composes with "
            "--portable-names. Adopting it renames every book already "
            "exported; the old files are reported as orphans, not deleted."
        ),
    )
    output.add_argument(
        "--on-collision",
        choices=COLLISION_MODES,
        default=SKIP,
        help=(
            "What to do when two books want the same output name: 'skip' "
            "exports only the first, 'suffix' keeps both. A suffixed book is "
            "marked with a digest of its own dc:identifier, so adding another "
            "book later does not rename it; books whose identifier is missing "
            "or shared fall back to ' (2)', which does move."
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


def _check_annotation_flags(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> None:
    """
    Refuse annotation flags that contradict each other, or say nothing.

    :param parser: The parser, for reporting the refusal.
    :param args: The parsed arguments.
    """
    # An empty FILE is falsy, so every check below it and every use of it read
    # as "not asked for": "-ad ''" silently ran an ordinary conversion.
    for flag in ("annotations_detached", "annotations_only"):
        if getattr(args, flag) == "":
            spelled = "--" + flag.replace("_", "-")
            parser.error(f"{spelled} needs a filename, or - for standard output")

    if args.annotations_only and (
        args.annotations_embedded or args.annotations_detached
    ):
        parser.error(
            "--annotations-only writes the detached file instead of converting; "
            "it cannot be combined with --annotations-embedded or "
            "--annotations-detached"
        )
    if args.annotations_none and (
        args.annotations_embedded or args.annotations_detached or args.annotations_only
    ):
        parser.error("--annotations-none contradicts the other annotation flags")
    if args.annotations_refresh and not args.annotations_embedded:
        # -ar walks the shelf. With only -ad it never touches it, which is what
        # -ao already means, so the pair would be two spellings of one thing.
        parser.error(
            "--annotations-refresh needs --annotations-embedded; to write only "
            "the detached file use --annotations-only"
        )


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

    _check_annotation_flags(parser, args)

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
