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

from ..collect.coredata import FULL_DISK_ACCESS
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
    try:
        return None if nearest is None or nearest.is_dir() else nearest
    except OSError:
        # A link into a directory the run may not search: whether it is one
        # cannot be told, and the probe that reads the shelf says why.
        return None


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
    unusable = _check_source(args)
    if unusable is None:
        unusable = _check_shelf(args)
    if unusable is not None:
        return unusable

    if args.epubcheck and not epubcheck_available():
        logger.critical(
            "--epubcheck needs the 'epubcheck' tool on PATH "
            "(brew install epubcheck, or see w3c.github.io/epubcheck)"
        )
        return exits.MISSING_TOOL

    return None


def _check_source(args: argparse.Namespace) -> int | None:
    """
    Check the library is there, when the run reads it.

    :param args: Parsed command line arguments.

    :return: An exit code, or None when the library can be read.
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
    if not (converts or writes_a_vault):
        return None
    try:
        # Asked of stat rather than Path.is_dir, which raises everything
        # but "absent": a library in a directory the run may not search --
        # behind Full Disk Access, on macOS -- was a traceback and exit 1.
        found = stat.S_ISDIR(args.source_dir.stat().st_mode)
    except (FileNotFoundError, NotADirectoryError):
        found = False
    except OSError as exc:
        refused = isinstance(exc, PermissionError)
        logger.critical(
            "Cannot read source directory %s: %s%s",
            printable(str(args.source_dir)),
            printable(exc.strerror or str(exc)),
            f". {FULL_DISK_ACCESS}." if refused else "",
        )
        return exits.NO_PERMISSION if refused else exits.NO_SOURCE
    if found:
        return None
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


def _check_shelf(args: argparse.Namespace) -> int | None:
    """
    Check the shelf can be read, and written when the run writes it.

    :param args: Parsed command line arguments.

    :return: An exit code, or None when the shelf is usable.
    """
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
    unreadable = _unreadable(args.output_dir) if uses_shelf else None
    if unreadable is not None:
        logger.critical(
            "Cannot read output directory %s: %s",
            printable(str(args.output_dir)),
            printable(unreadable.strerror or str(unreadable)),
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
    return None


def _unreadable(output_dir: Path) -> OSError | None:
    """
    Find why the shelf cannot be read, if it cannot.

    A shelf that cannot be listed read as an empty one: --verify found "No
    archives", --list showed every book pending, and a run on a directory it
    could write but not read (mode 300) exported them all again. Listing is
    not enough either: one that can be listed but not searched (mode 600)
    passed, and --list and --verify then died on the first stat. And a shelf
    in a directory the run may not search made ``Path.is_dir`` raise, in a
    traceback, where this probe gives the reason.

    :param output_dir: The output directory as given.

    :return: Why not, or None when it can be listed and searched, or is not
        there yet: a run that writes creates it, and one that reads says so.
    """
    try:
        os.scandir(output_dir).close()
        # A name inside it, which only searching reaches. Spelt as a string:
        # a Path drops the ".", and stats the directory from its parent.
        os.stat(os.path.join(output_dir, os.curdir))  # noqa: PTH116, PTH118
    except FileNotFoundError:
        return None
    except OSError as exc:
        return exc
    return None
