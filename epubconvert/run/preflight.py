"""
Checking what a run needs from the machine, before it does anything.

The library, the shelf and the tools a run was asked to use: each is judged
here, before the first book is read, so a run that cannot finish says so and
exits with its own code rather than failing halfway. Split from
:mod:`epubconvert.run.run` when that module reached the line limit; ``main``
still decides when to check.
"""

from __future__ import annotations

import argparse
import os
import stat
from pathlib import Path

from ..collect.validate import epubcheck_available
from ..export.detached import vault_of
from ..utils import exits
from ..utils.app_logger import logger
from ..utils.defaults import SOURCE_CANDIDATES
from ..utils.display import printable
from .convert import LOCK_NAME, lock_file_refusal


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


def _unwritable_shelf(args: argparse.Namespace) -> tuple[Path, str] | None:
    """
    Find what stops this run creating or locking the shelf, when it writes one.

    A dry run on a read-only volume exited 0, and the real run could neither
    create the shelf nor open its lock file and exited 5: the rehearsal said
    all was well for a run that could not start. Judged on the part of the
    path that exists, as :func:`_file_in_the_way` judges it.

    The lock file is what the run opens, so when there is one it is judged
    instead: the directory alone let a lock file the run could not open (mode
    444) pass the rehearsal, and refused a read-only shelf whose lock file
    would have opened. On a read-only volume opening it fails too, with EROFS.

    :param args: Parsed command line arguments.

    :return: The lock file or directory that cannot be written, and why,
        otherwise None. Always None for ``--list`` and ``--verify``, which
        only read the shelf, and for the runs that never touch it.
    """
    if args.list_only or args.verify or args.annotations_only or args.library_export:
        return None
    lock = args.output_dir / LOCK_NAME
    if os.path.lexists(lock):
        # A lock file that is a link is refused for what it is, as the real
        # run's output_lock refuses it, not called unwritable.
        refusal = lock_file_refusal(lock)
        return None if refusal is None else (lock, refusal)
    nearest = _nearest_existing(args.output_dir)
    if nearest is None or os.access(nearest, os.W_OK | os.X_OK):
        return None
    return nearest, "is not writable"


def check_environment(args: argparse.Namespace) -> int | None:
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
            logger.critical(
                "Source directory does not exist: %s", printable(str(args.source_dir))
            )
        return exits.NO_SOURCE

    # A file where the shelf should be. The real run failed at mkdir with 5,
    # but a dry run and --list only read, found an empty "shelf" and exited
    # 0: the rehearsal said all was well for a run that could not start. The
    # runs that read only Apple's container never touch the shelf.
    uses_shelf = not (args.annotations_only or args.library_export)
    blocker = _file_in_the_way(args.output_dir) if uses_shelf else None
    if blocker is not None:
        # "is a file" was said of a dangling symlink and of a symlink loop too.
        try:
            mode = blocker.lstat().st_mode
        except OSError:  # pragma: no cover - removed since it was found
            mode = 0
        kind = "is not a directory"
        if stat.S_ISLNK(mode):
            kind = "is a symlink to no directory"
        elif stat.S_ISREG(mode):
            kind = "is a file"
        logger.critical(
            "Output path is not a directory: %s (%s %s)",
            printable(str(args.output_dir)),
            printable(str(blocker)),
            kind,
        )
        return exits.NO_OUTPUT
    # A shelf that cannot be listed reads as an empty one: --verify found "No
    # archives", --list showed every book pending, and a run on a directory
    # it could write but not read (mode 300) exported them all again.
    if uses_shelf and args.output_dir.is_dir():
        try:
            os.scandir(args.output_dir).close()
        except OSError as exc:
            logger.critical(
                "Cannot read output directory %s: %s",
                printable(str(args.output_dir)),
                printable(exc.strerror or str(exc)),
            )
            return exits.NO_OUTPUT
    unwritable = _unwritable_shelf(args)
    if unwritable is not None:
        logger.critical(
            "Cannot create or lock output directory %s: %s %s",
            printable(str(args.output_dir)),
            printable(str(unwritable[0])),
            unwritable[1],
        )
        return exits.NO_OUTPUT

    if args.epubcheck and not epubcheck_available():
        logger.critical(
            "--epubcheck needs the 'epubcheck' tool on PATH "
            "(brew install epubcheck, or see w3c.github.io/epubcheck)"
        )
        return exits.MISSING_TOOL

    return None
