"""
The one rule for turning a name from a book into a path on disk.

A ``*.epub/`` package is input. Its ``container.xml`` names the package
document, the package document names every manifest item, and the directory
itself names its own entries. All of those are chosen by whoever produced or
sideloaded the book, and every one of them is joined onto a real directory and
read.

This module exists because that rule was written three times. A traversal
through a manifest href was fixed once, at the call site that had it, and the
guard written for it was left where only one caller could find it. The archive
writer then grew its own weaker version, and the readers of ``container.xml``,
the package document and ``encryption.xml`` grew none at all -- so a book could
still point its own package document at any file the user could read.

Everything that resolves a name against a package goes through :func:`resolve`.
There is no second implementation, and a test asserts there is not.

**Links are refused outright** -- symlinks even when they point back inside
the package, and hardlinks, which are links the symlink test cannot see: a
hardlink reports ``is_symlink() == False`` and never resolves outside the
package, so it passed both halves of the rule and carried its target's bytes
into the archive under an innocuous name. A package Apple wrote contains
neither kind, so "a member is a file this package owns" is both true and far
easier to be sure of than "a link that happens to land somewhere acceptable".
"""

from __future__ import annotations

import os
import posixpath
import stat
from pathlib import Path
from typing import BinaryIO

#: Prefixes that name a resource outside the archive rather than a member of
#: it. The epub specification allows a remote manifest item, and resolving one
#: as an archive path would report a perfectly good book as missing a file.
REMOTE_PREFIXES = ("http://", "https://", "ftp://", "ftps://", "data:", "mailto:")


def is_remote(href: str) -> bool:
    """
    Report whether an href is a URL rather than an archive member.

    A remote resource is legitimate. It simply is not expected to be inside the
    archive, so it must not be looked for there.

    :param href: The manifest href to test.

    :return: True if the href is a remote or inline URL.
    """
    return href.lower().startswith(REMOTE_PREFIXES)


def escapes(path: str) -> bool:
    """
    Report whether an archive path resolves above the archive root.

    The textual half of the rule. An href carrying more ``..`` segments than
    the package document has parent directories resolves above the root, and an
    absolute path never was inside it. No archive member can match either.

    :param path: An archive path, normalized or not.

    :return: True if the path leaves the archive.
    """
    if path.startswith("/"):
        return True
    normalized = posixpath.normpath(path)
    return normalized == ".." or normalized.startswith("../")


def resolve(
    root: Path, relative: str | Path, *, resolved_root: Path | None = None
) -> Path | None:
    """
    Resolve a name against a package, or refuse it.

    Three checks, because no one of them covers the others:

    :func:`escapes` rejects the ``../`` and absolute paths that never referred
    to anything inside the package. A symlink test rejects a member that is a
    redirection rather than a file, which the textual check cannot see because
    the name is perfectly ordinary. Comparing the resolved path against the
    resolved root catches an intermediate directory that is itself a link out.

    :param root: The ``*.epub/`` package directory.
    :param relative: A path from the book, relative to *root*.
    :param resolved_root: *root* already resolved, when the caller holds it.
        Resolving walks every component, and it does not change across a
        package.

    :return: The path to read, or None if there is nothing safe to read.
    """
    text = relative.as_posix() if isinstance(relative, Path) else relative
    if not text or escapes(text):
        return None

    candidate = root / text
    if _is_linked(candidate):
        return None

    base = resolved_root if resolved_root is not None else root.resolve()
    try:
        if not candidate.resolve().is_relative_to(base):
            return None
    except (OSError, ValueError):
        # A NUL byte in the name, or a resolve on a broken mount. Callers are
        # promised None rather than a raise.
        return None

    return candidate


def open_contained(path: Path) -> BinaryIO:
    """
    Open a package member for reading, refusing to follow a link.

    :func:`resolve` and :func:`contains` answer at check time, and the caller
    opens the file afterwards -- two syscalls with a window between them. A
    member swapped for a symlink in that window was dereferenced, which is the
    exfiltration this module exists to stop, reopened as a race rather than a
    missing check. ``O_NOFOLLOW`` closes it by making the kernel refuse at
    open, with nothing in between to lose.

    The lexical and containment checks are still needed: this covers the final
    component only, and says nothing about where the path pointed.

    :param path: The member to open, already cleared by :func:`resolve` or
        :func:`contains`.

    :return: A binary file object.

    :raises OSError: If the final component is a symlink (``ELOOP``), or the
        open fails for any ordinary reason.
    """
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    return os.fdopen(descriptor, "rb")


def is_free(target: Path) -> bool:
    """
    Report whether a path can be created without writing through anything.

    The write-side half of path trust. ``exists()`` alone is not enough: a
    **dangling** symlink reports False, and a copy then follows it, so a link
    planted at the name a cover is about to take redirects those bytes
    anywhere the user can write.

    :param target: The path about to be created.

    :return: True if nothing already claims the name.
    """
    return not target.exists() and not target.is_symlink()


def _is_linked(candidate: Path) -> bool:
    """
    Report whether a path is a link of either kind.

    :param candidate: The path to test.

    :return: True for a symlink, or for a regular file with more than one name.
    """
    if candidate.is_symlink():
        return True
    try:
        info = candidate.lstat()
    except FileNotFoundError:
        # Absent is not linked. The caller asks about paths that may not
        # exist -- an optional META-INF/sinf.xml, for one -- and answering
        # "linked" there reported every unprotected book as DRM-protected.
        return False
    except (OSError, ValueError):
        # Anything else means we could not establish what this is, and the
        # rule fails closed.
        return True
    return stat.S_ISREG(info.st_mode) and info.st_nlink > 1


def contains(root: Path, candidate: Path, *, resolved_root: Path | None = None) -> bool:
    """
    Report whether an already-discovered path is safely inside a package.

    The same rule as :func:`resolve`, for callers that walked the directory
    themselves and hold absolute paths rather than names out of a document.

    :param root: The ``*.epub/`` package directory.
    :param candidate: A path found beneath *root*.
    :param resolved_root: *root* already resolved, when the caller holds it.

    :return: True if the path may be read as part of this package.
    """
    if _is_linked(candidate):
        return False
    base = resolved_root if resolved_root is not None else root.resolve()
    try:
        return candidate.resolve().is_relative_to(base)
    except (OSError, ValueError):
        return False
