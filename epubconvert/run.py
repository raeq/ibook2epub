"""
Driving one run from the command line.

Everything between parsing arguments and returning an exit code: what a run
announces before it starts, the read-only ``--list`` and ``--verify`` branches,
and the export itself under the output directory lock.

Held apart from :mod:`epubconvert.convert` so the exporter can be used as a
library without argparse in the call chain.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import sys
import tempfile
from collections.abc import Sequence
from contextlib import nullcontext
from pathlib import Path
from typing import Any
from zipfile import BadZipFile

from . import app_logger, exits
from .annotations import (
    STDOUT,
    AnnotationsUnavailableError,
)
from .annotations import build_document as build_annotation_document
from .annotations import collect as collect_annotations
from .annotations import for_book as annotations_for_book
from .annotations import index_by_book as index_annotations
from .annotations import merge as merge_annotations
from .app_logger import logger
from .archive import (
    PARTIAL_PREFIX,
    PARTIAL_SUFFIX,
    collect_copyable,
    collect_package_dirs,
    copy_through,
    count_ignored,
    file_mode,
    replace_annotations,
)
from .cli import parse_args
from .convert import (
    ExportOptions,
    OutputLockedError,
    Report,
    cap_exports,
    count_pending_decisions,
    exit_code,
    export_planned,
    filter_packages,
    format_summary,
    output_lock,
    progress_for,
    sweep_partials,
)
from .defaults import SOURCE_CANDIDATES
from .display import printable
from .inspect_output import verify_output
from .naming import (
    NamingPolicy,
    PortableNamesUnavailableError,
    PortableNaming,
    StripNaming,
    build_policy,
)
from .planning import (
    Assignment,
    CollisionMode,
    PlanOptions,
    assign_names,
    copy_target_name,
    find_orphans,
    orphan_decisions,
    plan_exports,
    render_listing,
)
from .validate import ArchiveInvalidError, ValidationOptions, epubcheck_available


def _log_preamble(args: argparse.Namespace, policy: NamingPolicy) -> None:
    """
    Say what this run is about to do, before it does it.

    :param args: Parsed command line arguments.
    :param policy: The naming policy in force.
    """
    logger.debug("Naming policy: %s", policy.label)
    if args.source_auto:
        logger.info("Using discovered iBooks library: %s", args.source_dir)
    else:
        logger.info("Examining source: %s", args.source_dir)
    logger.info("Writing output to: %s", args.output_dir)
    # Keyed off the policy object rather than re-derived from the raw argument.
    # Two independent statements of one fact drift apart the moment
    # build_policy's mapping changes, and the debug line above is the one that
    # would still be right.
    if isinstance(policy, StripNaming):
        logger.info("Portable naming: stripping characters other filesystems reject.")
    elif isinstance(policy, PortableNaming):
        logger.info(
            "Portable naming: romanizing (non-Latin titles are transliterated)."
        )
    if args.dry_run:
        logger.info(
            "Running in dry-run mode. No file system modifications will be performed."
        )


def _shared_names(
    packages: Sequence[Path],
    discovered: Sequence[Path],
    policy: NamingPolicy,
    on_collision: CollisionMode,
) -> list[Assignment] | None:
    """
    Name every package once, when both callers want the same answer.

    Planning and orphan detection each name a set of packages, and naming reads
    a package document per book under a metadata policy -- so computing it in
    both places read every book twice. Measured on a real 2,805-book library:
    5,610 reads for one listing.

    They want different sets whenever ``--match`` or ``-m`` narrows the
    selection: the shelf is judged against the whole library, the run against
    the subset. Sharing only when the two lists agree keeps that distinction,
    at the cost of the saving on a filtered run, which is the smaller run
    anyway.

    :param packages: What this run will convert.
    :param discovered: Every package in the library.
    :param policy: The naming policy in force.
    :param on_collision: The collision mode in force.

    :return: The shared assignment, or None when the callers disagree and each
        must name its own set.
    """
    if list(packages) != list(discovered):
        return None
    return assign_names(discovered, policy, on_collision)


def _run_listing(args: argparse.Namespace, policy: NamingPolicy) -> int:
    """
    Render the plan without converting anything.

    :param args: Parsed command line arguments.
    :param policy: The naming policy in force.

    :return: A process exit code.
    """
    discovered = collect_package_dirs(args.source_dir)
    copyable = [] if args.no_copy_through else collect_copyable(args.source_dir)
    packages = filter_packages(discovered, args.match)
    shared = _shared_names(packages, discovered, policy, args.on_collision)
    decisions = plan_exports(
        packages, args.output_dir, policy, _plan_options(args), assigned=shared
    )
    # Orphans come from the whole library, not this run's filtered subset:
    # --match narrows a run, not the shelf. Files copied through claim their
    # names too, or the shelf would report what this run just put there.
    orphans = orphan_decisions(
        find_orphans(
            args.output_dir,
            policy,
            discovered,
            args.on_collision,
            claimed_extra=[copy_target_name(path, policy) for path in copyable],
            assigned=shared,
        )
    )
    print(render_listing(decisions + orphans, args.as_json))
    ignored = count_ignored(args.source_dir, discovered) - len(copyable)
    if ignored and not args.as_json:
        print(f"{ignored} ignored (not books)")
    return 0


def _gather_annotations(
    args: argparse.Namespace, policy: NamingPolicy
) -> list[dict[str, Any]] | None:
    """
    Read the reader's highlights, if this run wants any.

    :param args: Parsed command line arguments.
    :param policy: The naming policy, so each book can say what its file on
        the shelf is called.

    :return: The annotations, or None when the run asked for none or they
        could not be read. A failure here does not stop a conversion: the books
        are the point, and the highlights are an extra.
    """
    if not (args.annotations_embedded or args.annotations_detached):
        return None
    try:
        return collect_annotations(policy=policy)
    except AnnotationsUnavailableError as exc:
        logger.error("Could not read annotations: %s", exc)
        return None


def _write_detached(found: list[dict[str, Any]], destination: str) -> int:
    """
    Write the one-file-per-library export.

    To a file, a rerun merges into what is there. To standard output there is
    nothing to merge into -- a pipe is not a file to add to -- so the whole set
    is emitted and nothing is said about what changed.

    :param found: What was read from Apple.
    :param destination: A path, or ``-`` for standard output.

    :return: A process exit code.
    """
    if destination == STDOUT:
        document = json.dumps(
            build_annotation_document(found), indent=2, ensure_ascii=False
        )
        try:
            print(document)
        except BrokenPipeError:
            # "-ao - | head" and "| less" then q are how this flag's own help
            # text says to use it. Closing the pipe is the reader saying they
            # have seen enough, not an error to report. The descriptor is
            # replaced so the interpreter's shutdown flush cannot raise again.
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return exits.SUCCESS

    target = Path(destination)
    try:
        existing = _existing_annotations(target)
    except AnnotationsUnavailableError as exc:
        logger.critical("%s", exc)
        return exits.NO_OUTPUT

    merged, tally = merge_annotations(existing, found)
    try:
        _write_atomically(
            target,
            json.dumps(build_annotation_document(merged), indent=2, ensure_ascii=False)
            + "\n",
        )
    except OSError as exc:
        logger.critical("Could not write %s: %s", printable(str(target)), exc)
        return exits.NO_OUTPUT

    logger.info("Wrote %d annotation(s) to %s", len(merged), printable(str(target)))
    if existing is not None:
        logger.info(
            "%d added, %d updated, %d unchanged, %d kept (no longer in Books)",
            tally["added"],
            tally["updated"],
            tally["unchanged"],
            tally["kept"],
        )
    return exits.SUCCESS


def _write_atomically(target: Path, text: str) -> None:
    """
    Replace a file's contents, or leave the old contents alone.

    ``write_text`` truncates before it writes, so a failure partway through --
    a full disk, a Ctrl-C -- left the export as a prefix of itself, which is
    neither the old file nor the new one. It is also not valid JSON, so every
    later run then refused to write to that path at all. The export is the
    artifact the merge machinery exists to protect; this is the same
    temporary-then-replace path :func:`~epubconvert.archive.zip_package` uses.

    :param target: The file to replace.
    :param text: What it should hold.

    :raises OSError: If it could not be written. The old file survives.
    """
    handle, temporary = tempfile.mkstemp(
        dir=target.parent, prefix=PARTIAL_PREFIX, suffix=PARTIAL_SUFFIX
    )
    os.close(handle)
    partial = Path(temporary)
    try:
        partial.chmod(file_mode())
        partial.write_text(text, encoding="utf-8")
        partial.replace(target)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise


def _existing_annotations(target: Path) -> dict[str, Any] | None:
    """
    Read back an export so a rerun can add to it rather than replace it.

    A file that is there and is not an export of this shape is refused rather
    than overwritten or treated as empty. Either would throw away annotations
    the reader may no longer be able to get back out of Books.

    "Not the document I expect" is one condition, not two. Refusing only
    malformed JSON meant a file holding a valid JSON *list* fell through to
    "nothing to merge into" and was silently replaced.

    :param target: The file about to be written.

    :return: The document, or None when there is nothing to merge into.

    :raises AnnotationsUnavailableError: If it is there and is not one of ours.
    """
    if not target.exists():
        return None
    try:
        if target.stat().st_size > MAX_EXPORT_BYTES:
            raise ValueError(f"larger than {MAX_EXPORT_BYTES} bytes")
        loaded = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as exc:
        # RecursionError is neither: deeply nested JSON raises it out of
        # json.loads, and it used to escape as a traceback.
        raise AnnotationsUnavailableError(
            f"{target.name} is already there and could not be read ({exc}); "
            "move it aside rather than have this overwrite it"
        ) from exc
    if not isinstance(loaded, dict) or not isinstance(loaded.get("annotations"), list):
        raise AnnotationsUnavailableError(
            f"{target.name} is already there and is not an annotation export; "
            "move it aside rather than have this overwrite it"
        )
    return loaded


#: How large an export this will read back to merge into. Generous next to a
#: real one -- 400 annotations is under half a megabyte -- and finite, so a
#: file that is not an export cannot be read into memory whole before the
#: check that would have refused it.
MAX_EXPORT_BYTES = 256 * 1024 * 1024


def _annotations_after_export(
    args: argparse.Namespace,
    policy: NamingPolicy,
    named: Sequence[Assignment],
    found: list[dict[str, Any]] | None,
) -> int | None:
    """
    Finish the annotation work the conversion could not do itself.

    A book converted by this run already carries its annotations: they went in
    as the archive was written. Two things are left over.

    Books that were already on the shelf are untouched by a conversion that
    skipped them, so ``-ar`` says to go back over the whole shelf. Without it,
    ``-ae`` means what it says -- the books this run wrote carry their
    highlights -- and a shelf built over several runs is brought up to date by
    asking for it.

    :param args: Parsed command line arguments.
    :param policy: The naming policy in force.
    :param named: The names the export just used, so the refresh looks for the
        archives the export actually wrote.
    :param found: The annotations this run read, or None.

    :return: An exit code when something went wrong, None otherwise.
    """
    if args.dry_run:
        return None
    code = exits.SUCCESS
    if args.annotations_refresh and found is not None:
        code = _embed_in_shelf(args, policy, found, True, named)
    if code == exits.SUCCESS and args.annotations_detached and found is not None:
        code = _write_detached(found, args.annotations_detached)
    return None if code == exits.SUCCESS else code


def _annotations_only(args: argparse.Namespace, policy: NamingPolicy) -> int:
    """
    Write the detached file and stop.

    Reads Apple's container and nothing else: no library walk, no shelf, no
    output directory. Somebody who wants their highlights out should not have
    to convert a library to get them.

    :param args: Parsed command line arguments.
    :param policy: The naming policy, so each book names the file it will be
        found in rather than the one it came from.

    :return: A process exit code.
    """
    try:
        found = collect_annotations(policy=policy)
    except AnnotationsUnavailableError as exc:
        logger.critical("Could not read annotations: %s", exc)
        return exits.NO_SOURCE
    return _write_detached(found, args.annotations_only)


def _apply_annotations(
    args: argparse.Namespace,
    policy: NamingPolicy,
    *,
    converted: bool = False,
    named: Sequence[Assignment] | None = None,
) -> int:
    """
    Put the reader's annotations wherever this run asked for them.

    Applied to the shelf after a conversion rather than threaded through it, so
    that ``-ar`` -- which converts nothing -- and an ordinary run reach the
    books by exactly the same path. There is one place that decides what a book
    carries.

    Only archives already on the shelf are touched. Under ``-ar`` a book added
    to the library since the conversion is therefore not converted: that mode
    says what it does.

    :param args: Parsed command line arguments.
    :param policy: The naming policy, so a shelf name is worked out the way the
        conversion worked it out.
    :param converted: Whether books were put on the shelf by this run, which
        only changes what is said afterwards.
    :param named: The names the export already worked out, when there was one.
        Recomputing them re-parses every package document a second time under
        a metadata naming policy, which is the 2x read this project has
        already fixed once elsewhere.

    :return: A process exit code.
    """
    # Guarded here rather than at the call sites. It was checked on the route
    # through _annotations_after_export and not on the -ar route, so
    # "--dry-run -ae -ar" rewrote every archive on the shelf.
    if args.dry_run:
        logger.info("Dry run: annotations were read but nothing was written.")
        return exits.SUCCESS

    found = _gather_annotations(args, policy)
    if found is None:
        # The books are the point and they are already on the shelf. Reporting
        # NO_SOURCE here told a scheduled run the source directory was missing
        # when it had been found and used.
        return exits.SUCCESS if converted else exits.NO_SOURCE

    if args.annotations_embedded:
        code = _embed_in_shelf(args, policy, found, converted, named)
        if code != exits.SUCCESS:
            return code

    if args.annotations_detached:
        return _write_detached(found, args.annotations_detached)
    return exits.SUCCESS


def _embed_in_shelf(
    args: argparse.Namespace,
    policy: NamingPolicy,
    found: list[dict[str, Any]],
    converted: bool,
    named: Sequence[Assignment] | None,
) -> int:
    """
    Put each book's annotations inside the archive already on the shelf.

    :param args: Parsed command line arguments.
    :param policy: The naming policy in force.
    :param found: Every annotation read from Apple.
    :param converted: Whether this run also converted books.
    :param named: The names the export worked out, or None to work them out.

    :return: A process exit code.
    """
    # A glob over a missing directory yields nothing, which read as a clean
    # run over an empty shelf: -ar with a typo in -o said it had refreshed
    # every book it found, having looked at none.
    if not args.output_dir.is_dir():
        logger.critical("Output directory does not exist: %s", args.output_dir)
        return exits.NO_OUTPUT

    assignments = (
        list(named)
        if named is not None
        else assign_names(
            collect_package_dirs(args.source_dir), policy, args.on_collision
        )
    )
    index = index_annotations(found)
    ambiguous = _ambiguous_names(assignments)

    changed = 0
    progress = progress_for(len(assignments), 1)
    # The same lock the export takes. These writes go into the output
    # directory and leave partials there, and a concurrent run's sweep cannot
    # tell one of those from an abandoned one.
    with output_lock(args.output_dir):
        for item in assignments:
            marker = progress.tick()
            target = args.output_dir / item.filename if item.filename else None
            if target is None or not target.is_file():
                continue
            if item.package.name in ambiguous:
                logger.warning(
                    "Skipped annotations for %s: more than one package "
                    "directory has that name, so which book they belong to "
                    "cannot be told apart.",
                    printable(item.package.name),
                )
                continue
            mine = annotations_for_book(item.package.name, index)
            if not mine:
                continue
            try:
                if replace_annotations(target, mine):
                    changed += 1
                    logger.info(
                        "%s Refreshed %d annotation(s) in %s",
                        marker,
                        len(mine),
                        printable(target.name),
                    )
            except (OSError, BadZipFile, ArchiveInvalidError) as exc:
                # BadZipFile is not an OSError, so one damaged archive used to
                # abort the whole refresh and every book after it went
                # untouched. A damaged archive is an expected state: --verify
                # exists to find them.
                logger.error("Could not refresh %s: %s", printable(target.name), exc)
    if converted:
        logger.info("Refreshed annotations in %d book(s).", changed)
    else:
        logger.info("Refreshed annotations in %d book(s); converted nothing.", changed)
    return exits.SUCCESS


def _ambiguous_names(assignments: Sequence[Assignment]) -> set[str]:
    """
    Find package names that more than one directory answers to.

    An annotation records its book's package *name*, not its path, because the
    path runs through the reader's home directory. Two directories with the
    same name in different places are therefore indistinguishable to
    :func:`~epubconvert.annotations.for_book`, and embedding by name gave each
    of them the other's highlights. This is the only place that knows every
    path, so it is the place that settles it.

    :param assignments: Every book this run knows about.

    :return: The names that are not unique.
    """
    seen: dict[str, Path] = {}
    ambiguous: set[str] = set()
    for item in assignments:
        name = item.package.name
        if name in seen and seen[name] != item.package:
            ambiguous.add(name)
        seen.setdefault(name, item.package)
    return ambiguous


def _run_verify(args: argparse.Namespace) -> int:
    """
    Check the archives already in the output directory.

    :param args: Parsed command line arguments.

    :return: A process exit code; non-zero if anything is damaged.
    """
    # A glob over a missing directory yields nothing, which read as a clean
    # bill of health: the one command whose purpose is finding damage reported
    # success having checked not a single file.
    if not args.output_dir.is_dir():
        logger.critical("Output directory does not exist: %s", args.output_dir)
        return exits.NO_OUTPUT

    checked, damaged, broken = verify_output(args.output_dir, epubcheck=args.epubcheck)
    if not checked:
        print(f"No archives found in {args.output_dir}.")
        return 0
    print(f"Verified {checked} archive(s) in {args.output_dir}: {damaged} damaged.")
    if damaged:
        # Naming them matters: --force alone re-exports the whole library, and
        # the default cap then picks its subset at random, so following that
        # advice literally could leave every damaged book untouched and still
        # report success.
        print("Re-export each damaged book, for example:")
        for name in broken[:3]:
            print(f"  ibook2epub --match {shlex.quote(Path(name).stem)} --force")
        if len(broken) > 3:
            print(f"  ...and {len(broken) - 3} more")
    return exits.DAMAGED if damaged else exits.SUCCESS


def _plan_options(args: argparse.Namespace) -> PlanOptions:
    """
    Build planning options from parsed arguments.

    :param args: Parsed command line arguments.

    :return: The planner's settings.
    """
    return PlanOptions(
        force=args.force,
        refresh=args.refresh,
        check_incomplete=args.skip_incomplete,
        on_collision=args.on_collision,
    )


def _run_export(
    args: argparse.Namespace,
    policy: NamingPolicy,
    found: list[dict[str, Any]] | None,
) -> tuple[Report, int, list[Assignment]]:
    """
    Collect, select and export, under the output directory lock.

    :param args: Parsed command line arguments.
    :param policy: The naming policy in force.
    :param found: The annotations this run read, or None. Embedded as each
        archive is written rather than by rebuilding it afterwards: applying
        them after the fact serialised every annotated book twice. Measured
        over 200 books, 1.09s the two-pass way against 0.62s in one.

    :return: The run's report, the number of books still to convert, and the
        names it gave every book it converted. Annotations are applied against
        that same assignment afterwards: naming the library again disagreed
        with it under ``--match`` with a collision suffix, so the archive the
        refresh looked for did not exist.

    :raises OutputLockedError: If another run holds the output lock, or the
        lock file could not be opened.
    """
    discovered = collect_package_dirs(args.source_dir)
    packages = filter_packages(discovered, args.match)
    if not packages:
        logger.warning("No matching *.epub packages found under %s", args.source_dir)

    # Held here rather than inside the exporter so the partial counts survive
    # a Ctrl-C.
    report = Report()
    copyable = [] if args.no_copy_through else collect_copyable(args.source_dir)
    report.ignored = count_ignored(args.source_dir, discovered) - len(copyable)
    shared = _shared_names(packages, discovered, policy, args.on_collision)
    # The names this run actually uses, computed once. plan_exports would
    # otherwise work them out again from the same inputs.
    assigned = (
        shared
        if shared is not None
        else assign_names(packages, policy, args.on_collision)
    )
    report.orphaned = len(
        find_orphans(
            args.output_dir,
            policy,
            discovered,
            args.on_collision,
            claimed_extra=[copy_target_name(path, policy) for path in copyable],
            assigned=shared,
        )
    )

    if args.force and args.max_export_files and len(packages) > args.max_export_files:
        logger.warning(
            "--force selected %d book(s) but -m limits this run to %d; "
            "pass -m 0, or --match to name the books you mean.",
            len(packages),
            args.max_export_files,
        )

    options = ExportOptions(
        covers=args.covers,
        min_free_mb=args.min_free,
        validation=ValidationOptions(enabled=args.validate, epubcheck=args.epubcheck),
        plan=_plan_options(args),
        annotations=(
            index_annotations(found)
            if found is not None and args.annotations_embedded and not args.dry_run
            else None
        ),
    )

    # A dry run writes nothing, so it needs no lock and must not create one.
    lock = nullcontext(False) if args.dry_run else output_lock(args.output_dir)
    with lock as locked:
        # Only with real exclusivity. Unlocked, another run's in-flight
        # temporary looks exactly like an abandoned one, and deleting it makes
        # that run's closing replace fail.
        if locked and not args.dry_run:
            sweep_partials(args.output_dir)

        # Planned exactly once, and inside the lock. Both the work list and
        # the count of what is left come from this one plan, so they cannot
        # describe different libraries; planning outside the lock would let a
        # concurrent run move the output directory underneath the decisions.
        pending_before = 0
        try:
            if not args.dry_run:
                _copy_through_all(copyable, args.output_dir, policy, report)
            # Planning is inside the guard too: under --skip-incomplete it
            # walks every package in the library, which is minutes of work on
            # a cloud shelf, and a Ctrl-C there produced a raw traceback with
            # no summary and no 130.
            decisions = plan_exports(
                packages, args.output_dir, policy, options.plan, assigned=assigned
            )
            pending_before = count_pending_decisions(decisions)
            selected = cap_exports(
                decisions, args.max_export_files, randomise=not args.no_shuffle
            )
            asyncio.run(
                export_planned(
                    selected,
                    args.output_dir,
                    dry_run=args.dry_run,
                    max_workers=args.workers,
                    report=report,
                    options=options,
                )
            )
        except KeyboardInterrupt:
            # Stopping is a normal way to end a long run: every finished book
            # is already complete and atomically in place, so a rerun simply
            # continues.
            report.interrupted = True
            logger.warning(
                "Interrupted; %d book(s) exported before stopping.", report.exported
            )

    return report, max(0, pending_before - report.exported), assigned


def _run_read_only(args: argparse.Namespace, policy: NamingPolicy) -> int | None:
    """
    Run whichever reporting mode was asked for, if either was.

    ``--list`` and ``--verify`` only read. Creating the destination for them
    would turn a typo in ``-o`` into a stray directory instead of a report
    about the one that was meant.

    :param args: Parsed command line arguments.
    :param policy: The naming policy in force.

    :return: An exit code, or None when this is an ordinary export run.
    """
    # Before --list and --verify: it reads Apple's container rather than the
    # library or the shelf, so neither of their preconditions applies.
    if args.annotations_only:
        return _annotations_only(args, policy)
    if args.annotations_refresh:
        return _apply_annotations(args, policy)  # -ar converts nothing
    if args.list_only:
        return _run_listing(args, policy)
    if args.verify:
        return _run_verify(args)
    return None


def _copy_through_all(
    copyable: Sequence[Path], output_dir: Path, policy: NamingPolicy, report: Report
) -> None:
    """
    Put already-valid books on the shelf without converting them.

    Rerun-safe on the same terms as everything else: a file already there is
    left alone rather than rewritten, so a second run does nothing and says
    nothing.

    :param copyable: Files found beside the packages.
    :param output_dir: Directory to copy into.
    :param policy: Naming policy, so a copied file is named the same way a
        converted one is.
    :param report: Counted into as each file lands, rather than totalled and
        returned at the end. A Ctrl-C part-way through left the summary saying
        nothing was copied while the files were already on disk.
    """
    for source in copyable:
        target = output_dir / copy_target_name(source, policy)
        if target.exists():
            continue
        try:
            copy_through(source, target)
        except OSError as exc:
            logger.error("Could not copy %s: %s", printable(source.name), exc)
            continue
        report.copied += 1
        logger.info("Copied %s", printable(source.name))


def _check_environment(args: argparse.Namespace) -> int | None:
    """
    Check what the run needs from the machine, before it does anything.

    Kept out of argparse deliberately. ``parser.error`` always exits 2, so
    validating the environment there made a missing library, a missing extra
    and a typo'd flag indistinguishable to a script.

    :param args: Parsed command line arguments.

    :return: An exit code, or None when the environment is usable.
    """
    if not (args.verify or args.annotations_only) and not args.source_dir.is_dir():
        if args.source_auto:
            # Both known homes were probed and neither held books. Naming only
            # the fallback reads as "this one path is wrong" rather than "we
            # looked in these places, and here is what to do about it".
            probed = "\n".join(f"  {path}" for path in SOURCE_CANDIDATES)
            logger.critical(
                "No Apple Books library found. Looked in:\n%s\n"
                "If your books are somewhere else, pass -s DIR.",
                probed,
            )
        else:
            logger.critical("Source directory does not exist: %s", args.source_dir)
        return exits.NO_SOURCE

    if args.epubcheck and not epubcheck_available():
        logger.critical(
            "--epubcheck needs the 'epubcheck' tool on PATH "
            "(brew install epubcheck, or see w3c.github.io/epubcheck)"
        )
        return exits.MISSING_TOOL

    return None


def main(argv: Sequence[str] | None = None) -> int:
    """
    Run the conversion from the command line.

    :param argv: Argument list, defaulting to ``sys.argv[1:]``.

    :return: A process exit code. 0 success; 1 an export failed, the output
        directory could not be created, or --verify found damage; 2 portable
        naming is unavailable or --verify was given a directory that is not
        there; 3 another run holds the output lock; 130 interrupted.
    """
    args = parse_args(argv)

    verbosity = 0 if args.quiet else 1 + args.verbose
    app_logger.configure(verbosity=verbosity, log_file=args.log_file)

    unusable = _check_environment(args)
    if unusable is not None:
        return unusable

    try:
        policy = build_policy(args.portable_names, args.name_by)
    except PortableNamesUnavailableError as exc:
        logger.critical("%s", exc)
        return exits.MISSING_TOOL

    _log_preamble(args, policy)

    # --list and --verify only read. Creating the destination for them would
    # turn a typo in -o into a stray directory instead of a report about the
    # one that was meant.
    read_only = _run_read_only(args, policy)
    if read_only is not None:
        return read_only

    if not args.dry_run:
        try:
            args.output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.critical("Could not create output directory: %s", exc)
            return exits.NO_OUTPUT

    # Read once, before anything is written, and handed to both the export
    # that embeds them and the write that detaches them.
    found = _gather_annotations(args, policy)

    try:
        report, remaining, named = _run_export(args, policy, found)
    except OutputLockedError as exc:
        logger.critical("%s", exc)
        return exits.LOCKED if "already using" in str(exc) else exits.NO_OUTPUT

    # After the books are on the shelf, so annotations reach them by the same
    # path --annotations-refresh uses. A dry run writes nothing, here included.
    annotated = _annotations_after_export(args, policy, named, found)
    summary = format_summary(report, args.output_dir, args.dry_run, remaining)
    # Standard output belongs to the document when one is going there; a
    # summary in the middle of it would make the JSON unparsable, which is the
    # one thing a pipe cannot tolerate.
    print(summary, file=sys.stderr if args.annotations_detached == STDOUT else None)
    # Recorded in the log file only: the console already has it from the
    # print above, and logging it plainly printed every run's summary twice.
    app_logger.file_only(summary)
    logger.debug("Run finished: %d exported, %d failed", report.exported, report.failed)

    return annotated if annotated is not None else exit_code(report)
