"""
Putting a file on the shelf whole, or not at all.

Every write this tool makes into a directory a person keeps goes to a
temporary beside the target and is renamed over it, so an interrupted run
leaves the old file or the new one and never a prefix of either. Split from
:mod:`epubconvert.export.archive` when that module reached the line limit;
the archive writer and the refresh rebuild use these too.
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from pathlib import Path

from ..utils.contained import open_contained

#: Marks a half-written archive. The prefix matters as much as the suffix:
#: the sweep in :func:`epubconvert.run.convert.sweep_partials` deletes what it
#: matches, and a bare ``*.part`` glob also matches a browser's in-progress
#: download or a user's own file sitting in the output directory.
PARTIAL_PREFIX = ".ibook2epub-"
#: Appended to every temporary this tool writes.
PARTIAL_SUFFIX = ".part"

#: The process umask, read once at import. Reading it requires *setting* it --
#: there is no query-only call -- so doing that per book from up to 64 workers
#: let one thread observe another's zeroed window and write a world-writable
#: book, and could leave the process umask at 0 for everything afterwards.
#: Import happens before any thread exists, and this tool never changes it.
UMASK = os.umask(0)
os.umask(UMASK)


def file_mode() -> int:
    """
    Return the mode an exported file should carry, per the user's umask.

    :return: 0o666 with the umask applied.
    """
    return 0o666 & ~UMASK


def copy_through(source: Path, target: Path) -> None:
    """
    Put a file on the shelf without touching its bytes.

    Written to a temporary and moved into place, like every other write here,
    so an interrupted run never leaves a half-copied file that a later run
    mistakes for finished work.

    The copy keeps the source's modification time, taken from the descriptor
    it read: with its size, that is how a later run knows the file for this
    source's copy without opening either (copynames._same_file). By size
    alone, a book of the same size replacing a deleted one was taken as
    already copied.

    :param source: The file to copy.
    :param target: Where it should land.
    """
    handle, partial_name = tempfile.mkstemp(
        dir=target.parent, prefix=PARTIAL_PREFIX, suffix=PARTIAL_SUFFIX
    )
    os.close(handle)
    partial = Path(partial_name)
    try:
        partial.chmod(file_mode())
        with open_contained(source) as reading, partial.open("wb") as writing:
            shutil.copyfileobj(reading, writing)
            read = os.fstat(reading.fileno())
        os.utime(partial, ns=(read.st_atime_ns, read.st_mtime_ns))
        partial.replace(target)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise


def write_atomically(target: Path, text: str) -> None:
    """
    Replace a file's contents, or leave the old contents alone.

    ``write_text`` truncates before it writes, so a failure partway through --
    a full disk, a Ctrl-C -- left the export as a prefix of itself, which is
    neither the old file nor the new one. It is also not valid JSON, so every
    later run then refused to write to that path at all. The export is the
    artifact the merge machinery exists to protect; this is the same
    temporary-then-replace path
    :func:`~epubconvert.export.archive.zip_package` uses. A replace swaps in
    a new file, so three things the old one carried are carried across:

    - **Its mode.** Every rerun wrote the partial at the umask's mode, so an
      export the user had made 0600 became readable by everyone again.
    - **Its being a link.** The replace landed on the link itself, so a link
      into a synced folder became a regular file here and the synced copy went
      stale without a word. A link is now written through: the partial goes
      beside the file it resolves to, so the rename stays atomic there.
    - **Its contents, durably.** Without an fsync before the rename, a crash
      just after it can leave the name on a file whose data never reached the
      disk -- the old contents gone and the new ones empty.

    :param target: The file to replace.
    :param text: What it should hold.

    :raises OSError: If it could not be written. The old file survives.
    """
    target, mode = what_to_replace(target)
    partial: Path | None = None
    try:  # Made inside: a Ctrl-C as its descriptor closed left it behind.
        # Named before it is made: a Ctrl-C as mkstemp returned left its .part
        # in a vault. Made exclusively, so a name already taken is not removed.
        while partial is None:
            name = f"{PARTIAL_PREFIX}{os.urandom(8).hex()}{PARTIAL_SUFFIX}"
            partial = target.parent / name
            try:
                handle = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                partial = None
        os.close(handle)
        partial.write_text(text, encoding="utf-8")
        _sync(partial)
        # After the write, not before: a target the user made read-only would
        # otherwise make its own partial unwritable.
        partial.chmod(mode)
        partial.replace(target)
    except BaseException:
        if partial is not None:
            partial.unlink(missing_ok=True)
        raise


def what_to_replace(target: Path) -> tuple[Path, int]:
    """
    Find the file a replace should land on, and the mode it should keep.

    Resolved unconditionally: a plain path resolves to itself, give or take a
    linked parent, and a link resolves to the file the user meant, so the
    replace lands there and the link survives. Shared by every path that
    replaces a file the user may have set up: the refresh of a book on the
    shelf once reset its mode and replaced a linked entry, after the notes and
    exports had been fixed for exactly that.

    :param target: The path about to be replaced.

    :return: The file to replace, and the permission bits to give the new one.
    """
    resolved = Path(os.path.realpath(target))
    try:
        # Permission bits only: a setuid or sticky bit is not something to
        # reproduce.
        return resolved, stat.S_IMODE(resolved.stat().st_mode) & 0o777
    except FileNotFoundError:
        return resolved, file_mode()


def _sync(path: Path) -> None:
    """
    Push a written file's data to the disk.

    ``fsync`` flushes the file, not the descriptor it is called on, so a fresh
    descriptor on a file that has just been written and closed is enough.

    :param path: The file to flush.
    """
    descriptor = os.open(path, os.O_RDWR)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
