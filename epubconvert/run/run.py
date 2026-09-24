"""
Driving one run from the command line.

Everything between parsing arguments and returning an exit code: what a run
announces before it starts, the read-only ``--list`` and ``--verify`` branches,
and the export itself under the output directory lock. Where the reader's
annotations go is :mod:`epubconvert.run.annotating`'s concern.

Held apart from :mod:`epubconvert.run.convert` so the exporter can be used as a
library without argparse in the call chain.
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import os
import shlex
import sys
from collections.abc import Sequence
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..collect.annotations import STDOUT
from ..collect.validate import (
    ValidationOptions,
    epubcheck_available,
)
from ..export.archive import (
    collect_copyable,
    collect_package_dirs,
    count_ignored,
    index_by_package,
)
from ..export.detached import vault_of
from ..export.inspect_output import verify_output
from ..export.naming import (
    PortableNamesUnavailableError,
    PortableNaming,
    StripNaming,
    build_policy,
)
from ..utils import app_logger, exits
from ..utils.app_logger import logger
from ..utils.defaults import SOURCE_CANDIDATES
from ..utils.display import emit, printable
from ..utils.policy import Assignment, NamingPolicy
from .annotating import (
    annotations_after_export,
    apply_annotations,
    gather_annotations,
    run_container_only,
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
    matches_pattern,
    output_lock,
    sweep_partials,
)
from .copying import (
    CopyPlan,
    copy_through_all,
    placed_copies,
    plan_copies,
    select_copies,
)
from .copynames import Names, claim_copies
from .placing import settled
from .planning import (
    COLLISION,
    Decision,
    PlanOptions,
    assign_names,
    find_orphans,
    orphan_decisions,
    plan_exports,
    render_listing,
)


def _log_preamble(args: argparse.Namespace, policy: NamingPolicy) -> None:
    """
    Say what this run is about to do, before it does it.

    :param args: Parsed command line arguments.
    :param policy: The naming policy in force.
    """
    logger.debug("Naming policy: %s", policy.label)
    # A run that converts nothing touches neither, and announcing them named
    # a source it never opened and an output directory it never created.
    # A vault is the exception: it names its notes from the library.
    converts_nothing = bool(args.library_export or args.annotations_only)
    if not converts_nothing or vault_of(args) is not None:
        if args.source_auto:
            logger.info("Using discovered iBooks library: %s", args.source_dir)
        else:
            logger.info("Examining source: %s", args.source_dir)
    if args.list_only or args.verify:
        # Both only read the shelf, and "Writing" said otherwise.
        logger.info("Reading output directory: %s", args.output_dir)
    elif not converts_nothing:
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
    args: argparse.Namespace,
    discovered: Sequence[Path],
    policy: NamingPolicy,
    copies: CopyPlan,
) -> tuple[Names, CopyPlan]:
    """
    Name every package and every file copied through once, for every caller.

    Always the whole library, never the subset ``--match`` selected. Naming
    the subset gave a matched book a different name from the one a full run
    gives it: alone in its selection, one edition of a crowded title got no
    marker, so a book already exported as ``Dune [digest]`` was pending under
    the plain name, written again, and the duplicate became an orphan. The
    run plans only the books it selected, looked up in this one assignment.

    It also saves work. Naming reads a package document per book under a
    metadata policy, and planning and orphan detection each named their own
    set: on a real 2,805-book library, 5,610 reads for one listing.

    The copies take their names in the same pass, after the packages
    (:func:`~epubconvert.run.copynames.claim_copies`), and are placed on the
    shelf as the plan places a book, so the copy writes where the plan and
    the orphan check expect it.

    :param args: Parsed command line arguments.
    :param discovered: Every package in the library.
    :param policy: The naming policy in force.
    :param copies: The files to take along, with the names they want.

    :return: Every name, and the copy plan under the names it is written to.
    """
    names = claim_copies(
        assign_names(discovered, policy, args.on_collision),
        copies.named,
        policy,
        args.on_collision,
        output_dir=args.output_dir,
        unopened=copies.evicted,
    )
    if not names.copies:
        return names, copies
    placed = settled([*names.packages, *names.copies], args.output_dir, policy)[
        len(names.packages) :
    ]
    return Names(names.packages, placed), placed_copies(copies, placed)


def _plan_copies(args: argparse.Namespace, policy: NamingPolicy) -> CopyPlan:
    """
    Find the files to take along and name them, once, for every caller.

    The orphan check, the copy and the ignored count all read this one plan.

    :param args: Parsed command line arguments.
    :param policy: The naming policy in force.

    :return: The plan, empty under ``--no-copy-through``. When some files went
        unnamed, the run is told what that costs rather than left with an
        orphan count it cannot explain.
    """
    plan = plan_copies(
        [] if args.no_copy_through else collect_copyable(args.source_dir),
        policy,
        max_workers=args.workers,
        skip_incomplete=args.skip_incomplete,
    )
    if plan.unnamed:
        logger.warning(
            "%d book(s) not downloaded from iCloud could not be named without "
            "downloading them; a copy of one already on the shelf is counted "
            "as an orphan.",
            plan.unnamed,
        )
    return plan


def _run_listing(args: argparse.Namespace, policy: NamingPolicy) -> int:
    """
    Render the plan without converting anything.

    :param args: Parsed command line arguments.
    :param policy: The naming policy in force.

    :return: A process exit code.
    """
    discovered = collect_package_dirs(args.source_dir)
    packages = filter_packages(discovered, args.match)
    shared, copies = _shared_names(args, discovered, policy, _plan_copies(args, policy))
    everything = [*shared.packages, *shared.copies]
    decisions = plan_exports(
        packages, args.output_dir, policy, _plan_options(args), assigned=everything
    )
    # The copies that lost their name, as the run reports them.
    decisions += [
        Decision(source, COLLISION, reason=reason)
        for source, reason in select_copies(copies, args.match).lost
    ]
    # Orphans come from the whole library, not this run's filtered subset:
    # --match narrows a run, not the shelf. Files copied through claim their
    # names too, or the shelf would report what this run just put there.
    orphans = orphan_decisions(
        find_orphans(
            args.output_dir,
            policy,
            discovered,
            args.on_collision,
            assigned=everything,
        )
    )
    emit(render_listing(decisions + orphans, args.as_json))
    ignored = count_ignored(args.source_dir, discovered) - len(copies.sources)
    if ignored and not args.as_json:
        emit(f"{ignored} ignored (not books)")
    return 0


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
        emit(f"No archives found in {printable(str(args.output_dir))}.")
        return 0
    shelf = printable(str(args.output_dir))
    emit(f"Verified {checked} archive(s) in {shelf}: {damaged} damaged.")
    if damaged:
        _advise_repair(args, broken)
    return exits.DAMAGED if damaged else exits.SUCCESS


def _advise_repair(args: argparse.Namespace, broken: Sequence[str]) -> None:
    """
    Tell the reader how to replace each damaged archive, in words that work.

    Naming them matters: ``--force`` alone re-exports the whole library, and
    the default cap then picks its subset at random, so following that advice
    literally could leave every damaged book untouched and still report
    success.

    Only an archive named exactly as a package in the library is sent to
    ``--match``, with a pattern checked against the rule that will read it.
    The stem used to be printed for every file, and ``--match`` reads ``?``,
    ``[`` and ``*`` as a glob against the whole package name, so ``Who Moved
    My Cheese?`` and ``Foundation [Asimov]`` matched nothing and the run it
    advised exited 0 having repaired nothing. A file copied through from the
    library, or named by a suffix or a naming option, is no package's name,
    and ``--force`` does not copy a file again: it is moved aside instead,
    and any run puts back a book missing from the shelf.

    :param args: Parsed command line arguments.
    :param broken: The damaged archives' names.
    """
    # Read only now, and only if it is there: --verify checks a shelf on a
    # machine that may never have had a library.
    packages = collect_package_dirs(args.source_dir) if args.source_dir.is_dir() else []
    patterns = {name: _repair_pattern(name, packages) for name in broken}
    forced = [name for name in broken if patterns[name] is not None]
    aside = [name for name in broken if patterns[name] is None]
    # Quoting makes a name one shell word, and does nothing about the ESC and
    # CR that rewrite the line: a path or a name is escaped with printable,
    # and a pattern holds "?" for each such character instead, since --match
    # would read the escape literally (see _repair_pattern).
    if forced:
        emit("Re-export each damaged book, for example:")
        shelf = _shelf_flags(args)
        for name in forced[:3]:
            # Joined to the flag, so a name that starts with a dash is not
            # read as one.
            quoted = shlex.quote(patterns[name] or "")
            emit(f"  ibook2epub --match={quoted} --force {shelf}")
        if len(forced) > 3:
            emit(f"  ...and {len(forced) - 3} more")
        # --verify refuses them, so it cannot know what the shelf was named by.
        emit("  (add the --name-by/-p/--on-collision flags you export with)")
    if aside:
        emit(
            f"Move each of these out of {printable(str(args.output_dir))} and "
            "rerun as before: --force cannot single it out, and a run puts "
            "back a book missing from the shelf."
        )
        for name in aside[:3]:
            emit(f"  {printable(name)}")
        if len(aside) > 3:
            emit(f"  ...and {len(aside) - 3} more")


def _shelf_flags(args: argparse.Namespace) -> str:
    """
    Spell out the shelf and the library a repair command has to name.

    The advice named neither. Run as printed, it looked for the library in
    its default home and exited 4; given ``-s`` it wrote a fresh copy to
    ``~/Books`` and left the damaged file where it was.

    :param args: Parsed command line arguments.

    :return: ``-s`` when the library was given rather than discovered, and
        ``-o`` always, each quoted as one shell word and escaped for display.
    """
    flags = [] if args.source_auto else ["-s", _as_word(args.source_dir)]
    return printable(shlex.join([*flags, "-o", _as_word(args.output_dir)]))


def _as_word(path: Path) -> str:
    """
    Spell a path so that argparse cannot take it for a flag.

    :param path: A path as the user gave it.

    :return: The path, led by ``./`` when it would otherwise start with a dash.
    """
    text = str(path)
    return f"./{text}" if text.startswith("-") else text


def _repair_pattern(name: str, packages: Sequence[Path]) -> str | None:
    """
    Find a ``--match`` pattern that selects exactly the packages named *name*.

    :param name: A damaged archive's name on the shelf.
    :param packages: Every package in the library.

    :return: The plainest pattern that selects them and nothing else, or None
        when no package has that name, or none can be printed that does.
    """
    wanted = {package for package in packages if package.name.lower() == name.lower()}
    if not wanted:
        return None
    stem, suffix = Path(name).stem, Path(name).suffix
    # The stem reads best but matches anywhere, so "Plain" finds Complain too.
    # The escaped name is anchored only if escaping gave it a bracket. The
    # bracketed dot makes the last a glob, which matches the whole name.
    for pattern in (
        stem,
        glob.escape(name),
        f"{glob.escape(stem)}[.]{suffix[1:]}",
    ):
        # What the advice prints is escaped for display, and --match reads
        # "\x1b" as four characters: a character printable would escape
        # becomes "?", which makes the pattern a glob, and is then checked
        # like any other, since "?" matches more than that one character.
        masked = "".join(char if printable(char) == char else "?" for char in pattern)
        chosen = {p for p in packages if matches_pattern(p.name, masked)}
        if chosen == wanted:
            return masked
    return None


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


def _survey(
    args: argparse.Namespace, policy: NamingPolicy, report: Report
) -> tuple[list[Path], CopyPlan, list[Assignment]]:
    """
    Work out what the library holds and what the shelf already has.

    Everything the export needs before it takes the lock: the packages, the
    files to copy, the names, and the orphans on the shelf.

    :param args: Parsed command line arguments.
    :param policy: The naming policy in force.
    :param report: Counted into for the ignored and orphaned files.

    :return: The packages this run converts, the files it copies, and the
        names it gives the packages.
    """
    discovered = collect_package_dirs(args.source_dir)
    packages = filter_packages(discovered, args.match)
    if not packages:
        logger.warning("No matching *.epub packages found under %s", args.source_dir)

    copies = _plan_copies(args, policy)
    report.ignored = count_ignored(args.source_dir, discovered) - len(copies.sources)
    shared, copies = _shared_names(args, discovered, policy, copies)
    everything = [*shared.packages, *shared.copies]
    report.orphaned = len(
        find_orphans(
            args.output_dir,
            policy,
            discovered,
            args.on_collision,
            assigned=everything,
        )
    )
    return packages, copies, everything


def _run_export(
    args: argparse.Namespace,
    policy: NamingPolicy,
    found: list[dict[str, Any]] | None,
) -> tuple[Report, int, list[Assignment], list[Path]]:
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
        refresh looked for did not exist. And the library's copyable files,
        which the annotation step needs for the same reason the embed does.

    :raises OutputLockedError: If another run holds the output lock, or the
        lock file could not be opened.
    """
    # Held here rather than inside the exporter so the partial counts survive
    # a Ctrl-C.
    report = Report()
    try:
        packages, copies, assigned = _survey(args, policy, report)
    except KeyboardInterrupt:
        # Guarded as the export is. Under a metadata policy this reads every
        # package document, minutes on a cloud library, and a Ctrl-C here was
        # a traceback with no summary and no 130.
        report.interrupted = True
        logger.warning("Interrupted before anything was written.")
        return report, 0, [], []
    if args.force and args.max_export_files and len(packages) > args.max_export_files:
        logger.warning(
            "--force selected %d book(s) but -m limits this run to %d; "
            "pass -m 0, or --match to name the books you mean.",
            len(packages),
            args.max_export_files,
        )

    copyable = _copyable(args, copies) if found is not None else []
    options = ExportOptions(
        covers=args.covers,
        min_free_mb=args.min_free,
        validation=ValidationOptions(enabled=args.validate, epubcheck=args.epubcheck),
        plan=_plan_options(args),
        annotations=(
            index_by_package(found, packages, copyable=copyable)
            if found is not None and args.annotations_embedded and not args.dry_run
            else None
        ),
    )

    # Planned exactly once, and inside the lock. Both the work list and the
    # count of what is left come from this one plan, so they cannot describe
    # different libraries; planning outside the lock would let a concurrent
    # run move the output directory underneath the decisions.
    pending_before = 0
    decisions: list[Decision] = []
    # The guard covers taking the lock and the sweep too. They were outside
    # it, and a Ctrl-C there escaped to main's last resort: 130, but no
    # summary, and nothing on stdout at all under -q.
    try:
        # A dry run writes nothing, so it needs no lock and must not create
        # one.
        lock = nullcontext(False) if args.dry_run else output_lock(args.output_dir)
        with lock as locked:
            # Only with real exclusivity. Unlocked, another run's in-flight
            # temporary looks exactly like an abandoned one, and deleting it
            # makes that run's closing replace fail.
            if locked and not args.dry_run:
                sweep_partials(args.output_dir)
            copy_through_all(
                select_copies(copies, args.match),
                args.output_dir,
                report,
                max_workers=args.workers,
                min_free_mb=args.min_free,
                dry_run=args.dry_run,
            )
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
            # Counted where the cap is applied, so the summary can tell books
            # it held back from books that failed rather than infer it.
            report.held_back = pending_before - count_pending_decisions(selected)
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
        # Stopping is a normal way to end a long run: every finished book is
        # already complete and atomically in place, so a rerun simply
        # continues.
        report.interrupted = True
        logger.warning(
            "Interrupted; %d book(s) exported before stopping.", report.exported
        )

    # A dry run exports nothing, so what it would export is what it takes
    # off: counting only exports said "-m 0 -d" would leave every book it
    # had just listed.
    done = report.planned if args.dry_run else report.exported
    return (
        report,
        max(0, pending_before - done),
        # The files it copies too: a vault writes a note for each of them.
        _selected(
            assigned,
            [*packages, *select_copies(copies, args.match).sources],
            decisions,
            policy,
        ),
        copyable,
    )


def _copyable(args: argparse.Namespace, copies: CopyPlan) -> list[Path]:
    """
    Every file in the library that is a book without being a package.

    Wanted for the annotations, which name a book only by its name: a zipped
    ``b/Foo.epub`` and a package ``a/Foo.epub/`` are one key, and counting
    only the packages gave the zipped book's highlights to the package.

    :param args: Parsed command line arguments.
    :param copies: The plan this run already made, which walked for them.

    :return: The files, from the plan when it has them. Under
        ``--no-copy-through`` it has none, but the zipped book is still in the
        library and still owns its highlights, so the library is walked.
    """
    if args.no_copy_through:
        return collect_copyable(args.source_dir)
    return copies.sources


def _selected(
    assigned: Sequence[Assignment],
    packages: Sequence[Path],
    decisions: Sequence[Decision],
    policy: NamingPolicy,
) -> list[Assignment]:
    """
    Keep the assignments of this run's own books, as the plan left them.

    The assignment names the whole library, and ``--match`` narrows what the
    run touches: only the books it selected go on to the annotation step.

    A book the plan found to be a collision has no name there either. Under a
    policy that names from the folder the plan reads a book's identifier only
    before it writes, so ``--force`` and ``--refresh`` could call a book a
    collision whose name the annotation step then trusted: its highlights
    reached no file, and the warning, finding a file of that name, said
    nothing. A book the plan placed at another file, such as its marked name,
    is renamed to it; see :func:`_as_decided`.

    :param assigned: The assignment of every package in the library.
    :param packages: The packages this run selected.
    :param decisions: What the plan decided about them, where it got that far.
    :param policy: The naming policy the names came from.

    :return: Their assignments, in the library's order.
    """
    chosen = set(packages)
    decided = {decision.package: decision for decision in decisions}
    return [
        _as_decided(entry, decided.get(entry.package), policy)
        for entry in assigned
        if entry.package in chosen
    ]


def _as_decided(
    entry: Assignment, decision: Decision | None, policy: NamingPolicy
) -> Assignment:
    """
    Rename one book's assignment to the file the plan placed it at.

    :param entry: Its assignment.
    :param decision: What the plan decided about it, if it got that far.
    :param policy: The naming policy the names came from.

    :return: The assignment with no name for a collision, or the name of the
        file the plan exports it to or found it at. A vault note shares that
        file's stem: named from the assignment, an edition that moved on to
        its marked name wrote its highlights into the other edition's note.
    """
    if decision is None:
        return entry
    if decision.status == COLLISION:
        return replace(entry, filename="", reason=decision.reason)
    if decision.target is not None and decision.target.name != entry.filename:
        name = decision.target.name
        return replace(entry, filename=name, identity=policy.identity(name))
    return entry


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
    # Before --list and --verify: these read Apple's container rather than the
    # library or the shelf, so neither of their preconditions applies.
    if args.annotations_only or args.library_export:
        return run_container_only(args, policy)
    if args.annotations_refresh:
        return apply_annotations(args, policy)  # -ar converts nothing
    if args.list_only:
        return _run_listing(args, policy)
    if args.verify:
        return _run_verify(args)
    return None


def _file_in_the_way(output_dir: Path) -> Path | None:
    """
    Find a file that stands where the shelf, or a directory above it, must go.

    Only the path itself used to be checked, and only if it existed, so
    ``-o afile/books`` passed: a dry run and ``--list`` exited 0 and the real
    run failed at ``mkdir`` with 5. The nearest part of the path that exists
    is what ``mkdir(parents=True)`` will build on, so that is what is judged.

    :param output_dir: The output directory as given.

    :return: The nearest existing part of the path when it is not a directory,
        otherwise None.
    """
    nearest = _nearest_existing(output_dir)
    return None if nearest is None or nearest.is_dir() else nearest


def _nearest_existing(output_dir: Path) -> Path | None:
    """
    Find the part of the output path that ``mkdir(parents=True)`` builds on.

    :param output_dir: The output directory as given.

    :return: The path itself if it exists, else its nearest existing parent.
    """
    for candidate in (output_dir, *output_dir.parents):
        # lexists: a symlink loop or a dangling link never exists(), so the
        # search looked past it to a writable parent, while mkdir fails on it.
        if os.path.lexists(candidate):
            return candidate
    return None


def _unwritable_shelf(args: argparse.Namespace) -> Path | None:
    """
    Find what stops this run creating or locking the shelf, when it writes one.

    A dry run on a read-only volume exited 0, and the real run could neither
    create the shelf nor open its lock file and exited 5: the rehearsal said
    all was well for a run that could not start. Judged on the part of the
    path that exists, as :func:`_file_in_the_way` judges it.

    :param args: Parsed command line arguments.

    :return: That directory when it cannot be written, otherwise None. Always
        None for ``--list`` and ``--verify``, which only read the shelf, and
        for the runs that never touch it.
    """
    if args.list_only or args.verify or args.annotations_only or args.library_export:
        return None
    nearest = _nearest_existing(args.output_dir)
    if nearest is None or os.access(nearest, os.W_OK | os.X_OK):
        return None
    return nearest


def _check_environment(args: argparse.Namespace) -> int | None:
    """
    Check what the run needs from the machine, before it does anything.

    Kept out of argparse deliberately. ``parser.error`` always exits 2, so
    validating the environment there made a missing library, a missing extra
    and a typo'd flag indistinguishable to a script.

    :param args: Parsed command line arguments.

    :return: An exit code, or None when the environment is usable.
    """
    # A vault names its notes the way the shelf names its books, so writing
    # one needs the library even though -ao otherwise does not. Without this
    # the run reported "Wrote 0 note(s)" and exited 0, having written none.
    #
    # An independent reason rather than an exception to the convert-nothing
    # modes: written as one, adding --library-export to the same command
    # cancelled it and the empty vault came back.
    writes_a_vault = vault_of(args) is not None
    converts = not (args.verify or args.annotations_only or args.library_export)
    if (converts or writes_a_vault) and not args.source_dir.is_dir():
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

    # A file where the shelf should be. The real run failed at mkdir with 5,
    # but a dry run and --list only read, found an empty "shelf" and exited
    # 0: the rehearsal said all was well for a run that could not start. The
    # runs that read only Apple's container never touch the shelf.
    uses_shelf = not (args.annotations_only or args.library_export)
    blocker = _file_in_the_way(args.output_dir) if uses_shelf else None
    if blocker is not None:
        logger.critical(
            "Output path is not a directory: %s (%s is a file)",
            args.output_dir,
            blocker,
        )
        return exits.NO_OUTPUT
    unwritable = _unwritable_shelf(args)
    if unwritable is not None:
        logger.critical(
            "Cannot create or lock output directory %s: %s is not writable",
            args.output_dir,
            unwritable,
        )
        return exits.NO_OUTPUT

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

    :return: A process exit code, one defined with its meaning in
        :mod:`epubconvert.utils.exits` and listed in the README's exit-code
        table. A bad command line never returns here: argparse exits with
        :data:`~epubconvert.utils.exits.USAGE` itself.
    """
    args = parse_args(argv)

    verbosity = 0 if args.quiet else 1 + args.verbose
    app_logger.configure(verbosity=verbosity, log_file=args.log_file)

    try:
        return _run(args)
    except KeyboardInterrupt:
        # The export stops cleanly and says what it finished. Everything else
        # -- reading the highlights, --list, --verify, the -ar refresh -- has
        # no report to finish, and a Ctrl-C there was a traceback and exit 1.
        # Each writes by atomic replace, so nothing is left half-written.
        logger.warning("Interrupted; rerun to continue.")
        return exits.INTERRUPTED


def _after_export(
    args: argparse.Namespace,
    policy: NamingPolicy,
    report: Report,
    *,
    named: Sequence[Assignment],
    found: list[dict[str, Any]] | None,
    copyable: Sequence[Path],
) -> int | None:
    """
    Do the annotation work the export leaves over, unless the run was stopped.

    Ctrl-C asks the run to stop, and this went on regardless: it wrote a vault
    of notes after the reader had asked for nothing more to be written, and
    warned that highlights "reached no file" for books that were never
    attempted, which says a book cannot be converted when it was only not
    reached.

    :param args: Parsed command line arguments.
    :param policy: The naming policy the names came from.
    :param report: The export's report, which says whether it was stopped.
    :param named: The names the export used.
    :param found: The annotations this run read, or None.
    :param copyable: The library's already-zipped books and PDFs.

    :return: What :func:`~epubconvert.run.annotating.annotations_after_export`
        returns, or None when the run was stopped, before this or during it.
        Stopped during it, *report* is marked interrupted, so the summary
        still says what the run finished and the run still exits 130.
    """
    if not report.interrupted:
        try:
            return annotations_after_export(
                args, policy, named, found, copyable=copyable
            )
        except KeyboardInterrupt:
            # A Ctrl-C while the detached file or the vault was written
            # escaped to main's last resort, which prints no summary: the
            # books this run had finished went unreported, on stdout and in
            # the log file. Each write is atomic, so nothing is half-written.
            report.interrupted = True
    # Said only when there was somewhere else for them to go. Under -ae alone
    # every book converted before the Ctrl-C already carries its own.
    elsewhere = args.annotations_detached or args.annotations_refresh
    if found is not None and elsewhere and not args.dry_run:
        logger.warning(
            "Your highlights were not written: the run was interrupted. "
            "Rerun to write them."
        )
    return None


def _run(args: argparse.Namespace) -> int:
    """
    Do what the command line asked, once logging is set up.

    :param args: Parsed command line arguments.

    :return: A process exit code, as for :func:`main`.
    """
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
    found = gather_annotations(args, policy)

    try:
        report, remaining, named, copyable = _run_export(args, policy, found)
    except OutputLockedError as exc:
        logger.critical("%s", exc)
        return exc.exit_code

    # After the books are on the shelf, so annotations reach them by the same
    # path --annotations-refresh uses. A dry run writes nothing, here included.
    annotated = _after_export(
        args, policy, report, named=named, found=found, copyable=copyable
    )
    summary = format_summary(report, args.output_dir, args.dry_run, remaining)
    # Standard output belongs to the document when one is going there; a
    # summary in the middle of it would make the JSON unparsable, which is the
    # one thing a pipe cannot tolerate.
    if args.annotations_detached == STDOUT:
        print(summary, file=sys.stderr)
    else:
        emit(summary)  # `ibook2epub | head` closes the pipe before it.
    # Recorded in the log file only: the console already has it from the
    # print above, and logging it plainly printed every run's summary twice.
    app_logger.file_only(summary)
    logger.debug(
        "Run finished: %d exported, %d failed",
        report.exported,
        report.failed + report.copies_failed,
    )

    return _outcome(report, annotated)


def _outcome(report: Report, annotated: int | None) -> int:
    """
    Choose the one exit code for a run that converted and then annotated.

    The first of these that applies: 130 if the run was stopped with Ctrl-C;
    1 if a book failed or the run could not proceed; the annotation step's
    own code, such as 5 for a destination it could not write; otherwise 0.
    The README's exit-code section states the same order.

    The annotation code used to win outright, so a run stopped with Ctrl-C,
    or one whose book had failed, exited 5 under a summary that said
    "Interrupted" or "failed 1". The summary describes the books, and the
    books are what the run is for, so their outcome comes first; the
    annotation step has already logged its own reason on stderr.

    :param report: The export's report.
    :param annotated: The annotation step's exit code, or None when it had
        nothing to report.

    :return: A process exit code.
    """
    converted = exit_code(report)
    if converted != exits.SUCCESS or annotated is None:
        return converted
    return annotated
