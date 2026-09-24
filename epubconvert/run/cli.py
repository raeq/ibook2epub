"""
The command line surface.

Kept apart from the conversion logic so that the argument definitions, which
are long and change often, do not crowd the module that does the work.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence
from pathlib import Path

from .. import __version__
from ..collect.annotations import STDOUT
from ..collect.library import SHELVES
from ..export.catalogue import LIBRARY_FORMATS
from ..export.naming import NAME_PASSTHROUGH, NAME_SOURCES, PORTABLE_MODES, STRIP
from ..utils.defaults import (
    DEFAULT_MAX_EXPORT_FILES,
    DEFAULT_MIN_FREE_MB,
    DEFAULT_OUTPUT,
    discover_source,
)
from .planning import COLLISION_MODES, SKIP, STATUSES

#: Flags that only mean something when books are converted or the shelf is
#: read, each with its spelling. A run that converts nothing -- --library-export,
#: --annotations-only or --annotations-refresh -- refuses every one of them
#: rather than ignoring it, because a flag the user typed that changes nothing
#: is a run doing something other than what was asked, silently. One list for
#: every such mode, so the next conversion flag is not forgotten by one of
#: them: -ar was, and "-ae -ar --match X" refreshed every book on the shelf.
#: --epubcheck comes before --validate, which it implies, so the flag named is
#: the one typed. Judged
#: against the parser's defaults rather than by truthiness, so a flag with a
#: real default -- --min-free, -m -- is caught too; one typed *as* its default
#: is indistinguishable from untyped and passes, which changes nothing.
#: --on-collision, --name-by and --portable-names are absent on purpose: a
#: vault names its notes the way the shelf names its books, so all three
#: shape a convert-nothing run. So is -o, for a different reason: it says
#: where books go, a released version accepted it beside -ao, and it cannot
#: change what an export contains -- refusing it would break a wrapper script
#: to prevent no confusion about the file's contents. The exports name their
#: own destination and say where they wrote it. --min-free is accepted by
#: --annotations-refresh alone, which rebuilds every archive it refreshes on
#: the shelf's own volume; see REFRESH_WRITES.
CONVERSION_ONLY = (
    ("list_only", "--list"),
    ("verify", "--verify"),
    ("covers", "--covers"),
    ("epubcheck", "--epubcheck"),
    ("validate", "--validate"),
    ("refresh", "--refresh"),
    ("skip_incomplete", "--skip-incomplete"),
    ("match", "--match"),
    ("max_export_files", "--max-export-files"),
    ("workers", "--workers"),
    ("min_free", "--min-free"),
    ("no_copy_through", "--no-copy-through"),
    ("no_shuffle", "--no-shuffle"),
)

#: The CONVERSION_ONLY flags --annotations-refresh still has a use for. It
#: rebuilds each archive beside the original, a whole copy of the book on the
#: output volume, and refusing --min-free there left it rebuilding onto a
#: volume already below the floor with no way to say otherwise.
REFRESH_WRITES = frozenset({"min_free"})
#: What neither report consults, since both only read. ``--dry-run`` made
#: each announce a dry-run mode and changed nothing else; ``--annotations-none``
#: states the default of a conversion, and neither converts --
#: ``--library-export`` already refused it for the same reason. Refused rather
#: than let through as harmless: a typed flag the run ignores reads as a
#: choice that changed something.
READ_ONLY_IGNORES = (
    ("dry_run", "--dry-run"),
    ("annotations_none", "--annotations-none"),
)

#: What ``--verify`` never consults. It checks every archive in the output
#: directory, running ``--epubcheck`` when asked (``--validate`` is what it
#: always does, so that one is accepted as saying so). ``--match`` is refused
#: rather than honoured: its help, and every other use of it, names books in
#: the library to *convert*, and under a metadata naming policy an archive on
#: the shelf is not called what its package is. ``--force`` re-exports, and
#: this converts nothing. Nor does it name anything: it opens whatever
#: ``*.epub`` the shelf holds, so the naming flags went by unused, and
#: ``--portable-names romanize`` without its extra even stopped a verify
#: with exit 6 over a policy it never applied.
VERIFY_IGNORES = tuple(
    (held, spelled)
    for held, spelled in CONVERSION_ONLY
    if held not in ("list_only", "verify", "epubcheck", "validate")
) + (
    ("force", "--force"),
    ("portable_names", "--portable-names"),
    ("name_by", "--name-by"),
    ("on_collision", "--on-collision"),
    *READ_ONLY_IGNORES,
)

#: What ``--list`` never consults. It renders the plan, so what shapes a
#: book's status -- ``--match``, ``--force``, ``--refresh``,
#: ``--skip-incomplete``, ``--no-copy-through`` -- is honoured, and
#: ``--workers`` sizes the pool that names the files copied through. What
#: only happens once a book is written, and the cap on how many are, is not:
#: the listing shows every book whatever ``-m`` says.
_WRITING_ONLY = frozenset(
    {"covers", "epubcheck", "validate", "max_export_files", "min_free", "no_shuffle"}
)
LIST_IGNORES = (
    *((held, spelled) for held, spelled in CONVERSION_ONLY if held in _WRITING_ONLY),
    *READ_ONLY_IGNORES,
)

#: How each report is described when it refuses a flag.
REPORTS = {
    "--list": "shows every book's status and converts nothing",
    "--verify": "checks every archive in the output directory and converts nothing",
}


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
    catalogue = parser.add_argument_group(
        "Your library",
        "Taking the catalogue out of Apple Books: what you own, who wrote it, "
        "when you got it, and the collections you sorted it onto. Needs Full "
        "Disk Access on macOS.",
    )
    selection.add_argument(
        "-m",
        "--max-export-files",
        type=int,
        default=DEFAULT_MAX_EXPORT_FILES,
        metavar="N",
        help=(
            "Maximum number of packages to convert; 0=no limit, "
            f"default={DEFAULT_MAX_EXPORT_FILES}. Files copied through are not "
            "counted."
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
        help=(
            "Re-export books even if they are already in the output directory. "
            "With --library-export, replace the file it names."
        ),
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
            "Number of worker threads, for converting books and for copying "
            "PDFs and already-zipped books (default: 4x the CPU count, capped "
            "at 64). The work blocks on iCloud rather than on the CPU, "
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
            "Skip books iCloud has not downloaded, rather than exporting "
            "packages as empty files or downloading PDFs and already-zipped "
            "books. Requires walking every package, which is slow on a cloud "
            "library, so it is off by default."
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
        "--annotations-format",
        choices=("json", "markdown"),
        default="json",
        help=(
            "Shape of the detached export. 'json' is the standards-shaped "
            "document the schema describes. 'markdown' writes one note per "
            "book into a directory, for an Obsidian or Logseq vault; with it, "
            "the argument to -ad and -ao names a DIRECTORY rather than a file. "
            "Embedded annotations (-ae) are always JSON."
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
    catalogue.add_argument(
        "--library-export",
        nargs="?",
        const=STDOUT,
        metavar="FILE",
        help=(
            "Write the library as a file and convert nothing. A Goodreads-format "
            "CSV by default, which The StoryGraph imports directly; see "
            "--library-format. A catalogue rather than a reading history: Apple "
            "keeps titles, authors, collections and acquisition dates, and "
            "little else. Composes with --annotations-only. An existing FILE is "
            "left alone unless --force is given. With no FILE, or with '-', it "
            "goes to standard output."
        ),
    )
    catalogue.add_argument(
        "--library-format",
        choices=LIBRARY_FORMATS,
        default=None,
        help=(
            "Shape of the library export. 'csv', the default, is the Goodreads "
            "export format. 'json' is the canonical record the schema "
            "describes, carrying every collection and every date the database "
            "holds."
        ),
    )
    catalogue.add_argument(
        "--no-isbn",
        action="store_true",
        help=(
            "Do not open any book's package document. That is where the ISBN "
            "comes from, and also the author's sort name and, under --name-by "
            "author-title, the name the book would have on the shelf, so those "
            "are left out too. A tracker "
            "matches a row on its ISBN, so this trades a matched import for "
            "speed: the read costs one per book, where the highlights cost one "
            "per annotated book."
        ),
    )
    catalogue.add_argument(
        "--unknown-shelf",
        choices=SHELVES,
        default=None,
        metavar="SHELF",
        help=(
            "What the CSV's Exclusive Shelf column says for a book the database "
            "says nothing about: 'to-read', 'currently-reading' or 'read'. By "
            "default the column is left blank rather than claiming 'to-read' "
            "for every book merely bought. Some importers require the column; "
            "this is how you choose the claim made on your behalf."
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
    if args.annotations_format == "markdown":
        if not (args.annotations_detached or args.annotations_only):
            # The format governs detached output. With no detached destination
            # there is nothing for it to govern, so the run would convert
            # normally and write no notes without saying so -- a plausible
            # first attempt, failing the way this project refuses to fail.
            parser.error(
                "--annotations-format markdown needs somewhere to write: pass "
                "--annotations-detached DIR, or --annotations-only DIR to skip "
                "converting"
            )
        if STDOUT in (args.annotations_detached, args.annotations_only):
            parser.error(
                "--annotations-format markdown writes one file per book, so it "
                "cannot go to standard output; name a directory"
            )

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
    if args.annotations_none and args.library_export:
        # It states the default of a conversion, and this run converts
        # nothing. Accepted, it read as a choice that changed something.
        parser.error(
            "--library-export converts nothing, so --annotations-none has "
            "nothing to state"
        )
    if args.annotations_refresh and not args.annotations_embedded:
        # -ar walks the shelf. With only -ad it never touches it, which is what
        # -ao already means, so the pair would be two spellings of one thing.
        parser.error(
            "--annotations-refresh needs --annotations-embedded; to write only "
            "the detached file use --annotations-only"
        )


def _check_library_flags(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> None:
    """
    Refuse library flags that contradict each other, or say nothing.

    :param parser: The parser, for reporting the refusal.
    :param args: The parsed arguments.
    """
    if args.library_export == "":
        parser.error("--library-export needs a filename, or - for standard output")
    if not args.library_export:
        # A flag the user typed that changes nothing is a run that does
        # something other than what was asked, silently. --library-format has
        # no default in the parser for exactly this: with one, typing it
        # alone was indistinguishable from not typing it.
        for held, spelled in (
            (args.no_isbn, "--no-isbn"),
            (args.unknown_shelf, "--unknown-shelf"),
            (args.library_format, "--library-format"),
        ):
            if held:
                parser.error(f"{spelled} only applies with --library-export")
        args.library_format = "csv"
        return
    args.library_format = args.library_format or "csv"
    if args.unknown_shelf and args.library_format == "json":
        parser.error(
            "--unknown-shelf fills the CSV's Exclusive Shelf column; the JSON "
            "export records what the database says and nothing else"
        )
    if args.annotations_embedded or args.annotations_detached:
        parser.error(
            "--library-export writes the library instead of converting; it "
            "cannot be combined with --annotations-embedded or "
            "--annotations-detached. To write highlights as well, use "
            "--annotations-only"
        )
    if args.annotations_only and _same_destination(
        args.library_export, args.annotations_only
    ):
        # The library export ran first and, with --force, replaced the
        # highlights file with the catalogue before -ao refused to write it.
        parser.error(
            "--library-export and --annotations-only cannot both go to the same "
            "place; name a different file for one of them"
        )


def _same_destination(first: str, second: str) -> bool:
    """
    Whether two export destinations are one file, or both standard output.

    :param first: A path, or ``-``.
    :param second: A path, or ``-``.

    :return: True when writing both would write one over the other.
    """
    if STDOUT in (first, second):
        return first == second
    # realpath rather than Path.resolve(): on Python 3.10 to 3.12 the latter
    # raises RuntimeError on a symlink loop, out of argument parsing.
    return os.path.realpath(first) == os.path.realpath(second)


def _check_convert_nothing_flags(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> None:
    """
    Refuse conversion flags in a run that converts nothing.

    :param parser: The parser, for reporting the refusal.
    :param args: The parsed arguments.
    """
    if args.library_export:
        mode = "--library-export"
    elif args.annotations_only:
        mode = "--annotations-only"
    elif args.annotations_refresh:
        # A third convert-nothing mode, and it was missing here: it walks the
        # whole shelf and consults none of these, so "-ae -ar --match Alpha"
        # rewrote every archive rather than Alpha's. The two above cannot
        # reach it -- each refuses -ae, which -ar requires.
        mode = "--annotations-refresh"
        if args.force:
            parser.error(
                f"{mode} rewrites an archive's annotations only when they "
                "changed and never converts a book, so --force has nothing to do"
            )
    else:
        return
    if args.force and args.library_export in (None, "", STDOUT):
        # Not in CONVERSION_ONLY: --library-export gives it a second meaning,
        # so it is refused only in the mode that has no use for it.
        parser.error(
            f"{mode} overwrites no file, so --force has nothing to do. An "
            "existing annotation export is merged into rather than replaced, "
            "and standard output has nothing to replace."
        )
    for held, spelled in CONVERSION_ONLY:
        if mode == "--annotations-refresh" and held in REFRESH_WRITES:
            continue
        if getattr(args, held) != parser.get_default(held):
            parser.error(
                f"{mode} reads Apple's container and converts nothing, so "
                f"{spelled} has nothing to do"
            )


def _check_reporting_flags(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> None:
    """
    Refuse a write beside ``--list`` or ``--verify``, which only read, and any
    flag the report never consults.

    :param parser: The parser, for reporting the refusal.
    :param args: The parsed arguments.
    """
    report = "--list" if args.list_only else "--verify" if args.verify else None
    if report is None:
        return
    if args.annotations_refresh:
        # run._run_read_only dispatches -ar before either report, so
        # "--verify -ae -ar" rewrote every archive on the shelf and verified
        # none of them.
        parser.error(
            f"{report} only reads, so it cannot be combined with "
            "--annotations-refresh, which rewrites the shelf"
        )
    # Neither report reads annotations, so "--list -ad FILE" listed the
    # library, wrote no file and exited 0. -ao is not here: it is a
    # convert-nothing mode, and CONVERSION_ONLY already refuses both reports
    # beside it.
    for held, spelled in (
        (args.annotations_embedded, "--annotations-embedded"),
        (args.annotations_detached, "--annotations-detached"),
    ):
        if held:
            parser.error(
                f"{report} only reads, so {spelled} would write nothing; run "
                "the annotation export on its own"
            )
    # Only annotation flags were refused here, so a conversion flag neither
    # report consults went by without a word: "--verify --match X" verified,
    # and exited 7 for, books outside X. Judged against the parser's
    # defaults, as the convert-nothing modes judge them.
    ignored = VERIFY_IGNORES if args.verify else LIST_IGNORES
    for held, spelled in ignored:
        if getattr(args, held) != parser.get_default(held):
            parser.error(f"{report} {REPORTS[report]}, so {spelled} has nothing to do")


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
    _check_reporting_flags(parser, args)
    _check_library_flags(parser, args)
    _check_convert_nothing_flags(parser, args)

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
    # realpath rather than Path.resolve(), as in _same_destination: on Python
    # 3.10 to 3.12 the latter raises RuntimeError on a symlink loop, a
    # traceback and exit 1 where the run reports a missing library itself.
    source = Path(os.path.realpath(args.source_dir))
    output = Path(os.path.realpath(args.output_dir))
    if output == source or output.is_relative_to(source):
        parser.error(
            f"output directory must not be inside the source directory: "
            f"{args.output_dir}"
        )

    return args
