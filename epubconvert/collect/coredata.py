"""
Reading Apple Books' own databases.

Apple keeps a reader's library and their annotations in Core Data SQLite
databases inside the Books container. Neither schema is documented or promised,
and both are read the same way: the newest file matching a prefix, opened
read-only, with every column treated as untyped. This module holds what the
two readers share -- where the container is, how its clock is counted, and how
a database is opened without ever writing to it -- so that the annotation
export and the library export cannot drift apart on any of them.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

#: Where Apple keeps the databases, relative to the user's home.
CONTAINER = Path(
    "Library/Containers/com.apple.iBooksX/Data/Documents",
)

#: Core Data counts seconds from 2001-01-01, not from the Unix epoch.
APPLE_EPOCH_OFFSET = 978307200

#: The one way an instant is written by every export, matching the schemas.
INSTANT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

#: The remedy for a refusal, worded once so every path that meets one gives
#: the same advice.
FULL_DISK_ACCESS = (
    "macOS refused access, so the terminal needs Full Disk Access "
    "(System Settings > Privacy & Security > Full Disk Access)"
)


class ContainerUnavailableError(RuntimeError):
    """Raised when Apple's databases cannot be found or read."""


class ContainerPermissionError(ContainerUnavailableError):
    """
    Raised when the operating system refuses to let the container be read.

    A subclass, so every caller that reports an unavailable container reports
    this one too, and one that needs to tell the two apart can.
    """


def _listed(directory: Path) -> None:
    """
    Open a directory for listing, so the operating system says why it cannot.

    ``Path.is_dir()`` and ``Path.glob()`` both swallow the ``OSError`` that
    names the cause. That is how a missing container and a missing Full Disk
    Access grant came to share their messages, each chosen by a guess that was
    wrong for the other (#9). Listing raises it instead: ``PermissionError`` is
    the refusal -- a grant denied on macOS arrives as ``EPERM``, which Python
    raises as ``PermissionError`` too -- and ``FileNotFoundError`` the absence.

    :param directory: The directory to open.

    :raises ContainerPermissionError: If the operating system refuses.
    :raises OSError: For every other cause, ``FileNotFoundError`` among them,
        left for the caller to word.
    """
    try:
        os.scandir(directory).close()
    except PermissionError as exc:
        raise ContainerPermissionError(
            f"{directory} cannot be read: {FULL_DISK_ACCESS}"
        ) from exc


def container_directory(container: Path | None) -> Path:
    """
    Find the Books container, or say why it cannot be.

    :param container: The container directory, or None for Apple's.

    :return: The directory.

    :raises ContainerPermissionError: If macOS refuses access to it.
    :raises ContainerUnavailableError: If it is not there, is a file, or cannot
        be read.
    """
    directory = container if container is not None else Path.home() / CONTAINER
    try:
        _listed(directory)
    except FileNotFoundError as exc:
        raise ContainerUnavailableError(
            f"{directory} is not there; Apple Books may never have run here"
        ) from exc
    except NotADirectoryError as exc:
        # There, as a file. "Not there" sent the reader looking for something
        # they could already see (Copilot on #18).
        raise ContainerUnavailableError(
            f"{directory} is a file, not a folder, so it cannot be the Books container"
        ) from exc
    except OSError as exc:
        raise ContainerUnavailableError(
            f"{directory} could not be read: {exc}"
        ) from exc
    return directory


def database_in(directory: Path, folder: str, what: str) -> Path:
    """
    Find the live copy of one of Apple's databases, or say why it cannot be.

    Only a refusal is blamed on the permission. A folder that is absent, or
    there and empty, has been read, so the permission is not the reason, and
    saying it was sent the reader to grant what they may already have granted.

    :param directory: The container directory.
    :param folder: The subdirectory, which is also the filename prefix:
        ``AEAnnotation`` or ``BKLibrary``.
    :param what: What to call the database in a message.

    :return: The database.

    :raises ContainerPermissionError: If macOS refuses access to the folder.
    :raises ContainerUnavailableError: If the folder or the database is not
        there, because Apple Books has not created it yet, or if the folder
        is a file.
    """
    location = directory / folder
    try:
        _listed(location)
    except FileNotFoundError as exc:
        raise ContainerUnavailableError(
            f"no {folder} folder under {directory}; Apple Books has not created "
            f"the {what} database here yet"
        ) from exc
    except NotADirectoryError as exc:
        raise ContainerUnavailableError(
            f"{location} is a file, not a folder, so it cannot hold the {what} database"
        ) from exc
    except OSError as exc:
        raise ContainerUnavailableError(f"{location} could not be read: {exc}") from exc
    database = newest(location, folder)
    if database is None:
        raise ContainerUnavailableError(
            f"no {what} database under {location}; Apple Books has not created one yet"
        )
    return database


def newest(directory: Path, prefix: str) -> Path | None:
    """
    Find the database Apple is currently using.

    The filenames carry a version and a build stamp, and old ones are left in
    place across upgrades, so the name cannot be hard-coded and the newest is
    the live one.

    :param directory: The container subdirectory to look in.
    :param prefix: The filename prefix to match.

    :return: The most recently modified match, or None if there is none.
    """
    # Stat inside the sort key raised on a dangling symlink among the
    # candidates, out of a function whose whole contract is "or None". Books
    # leaves old files here across upgrades, which is why the glob exists.
    dated: list[tuple[float, Path]] = []
    for path in directory.glob(f"{prefix}*.sqlite"):
        try:
            dated.append((path.stat().st_mtime, path))
        except OSError:
            continue
    if not dated:
        return None
    return max(dated, key=lambda pair: pair[0])[1]


def rows(database: Path, query: str) -> list[sqlite3.Row]:
    """
    Run one query against a database, without writing to it.

    Opened read-only through a URI so that reading somebody's library cannot
    modify it, and so that a live Books process holding the write lock does not
    stop the export.

    :param database: The database file.
    :param query: The query to run.

    :return: The rows, as mappings.

    :raises ContainerUnavailableError: If the database cannot be read.
    """
    # as_uri() percent-encodes "?", "#" and "%" itself. Building the URI by
    # concatenation let a filename carrying "?" append its own parameters
    # ahead of mode=ro -- a read path that could open the database writable.
    try:
        connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    except (sqlite3.Error, ValueError, OSError) as exc:
        raise ContainerUnavailableError(f"could not open {database.name}") from exc
    try:
        connection.row_factory = sqlite3.Row
        return list(connection.execute(query))
    except sqlite3.Error as exc:
        # A schema change in a Books update lands here rather than as a
        # traceback: the columns the readers name are Apple's, and Apple never
        # promised them.
        raise ContainerUnavailableError(
            f"{database.name} is not shaped as expected: {exc}"
        ) from exc
    finally:
        connection.close()


def moment(seconds: object) -> str | None:
    """
    Render a Core Data timestamp as UTC.

    :param seconds: Seconds since 2001-01-01, or None.

    :return: An RFC 3339 instant ending in ``Z``, or None.
    """
    # Apple's column is untyped and undocumented. A string, a NaN or a value
    # past the year 9999 all reach here, and every one of them used to take
    # the whole export down with it.
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool):
        return None
    if seconds == 0:
        # Apple writes 0 where it has no date. Rendered, that claimed every
        # such book was acquired, and every such highlight modified, at
        # midnight on New Year's Day 2001. Decided here, once, so that the
        # two readers cannot read the same storage convention two ways.
        return None
    try:
        when = datetime.fromtimestamp(seconds + APPLE_EPOCH_OFFSET, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return when.strftime(INSTANT_FORMAT)


def now() -> str:
    """Return this instant as UTC, to the second, ending in ``Z``."""
    return datetime.now(tz=timezone.utc).strftime(INSTANT_FORMAT)
