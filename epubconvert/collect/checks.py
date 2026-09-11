"""
How thoroughly an archive is checked after it is written.

``--validate`` runs the structural check in :mod:`epubconvert.collect.validate`
on every archive: zip integrity, the mimetype entry, and that every file the
package document lists is present. ``--check-references`` adds the reference
check in :mod:`epubconvert.collect.references` -- the links, fragments,
stylesheets and fonts inside the book, under the message IDs epubcheck gives the
same problems -- once the structural check has passed. That second step used to
run the external epubcheck, which needed a Java runtime and took a median 2.8 s
a book on a 2,798-book shelf, where the built-in check took a median 23 ms.

The choice lives here, above both checks, because the reference checker reads
the package document through :mod:`epubconvert.collect.validate`, which
therefore cannot call it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .references import check_file
from .validate import validate_archive

#: The most reference problems named for one archive. One book on that shelf
#: had more than a thousand, so the rest are counted instead.
MAX_REPORTED = 10


@dataclass(frozen=True)
class ValidationOptions:
    """How thoroughly to check an archive after writing it."""

    enabled: bool = False
    references: bool = False

    def check(self, path: Path) -> list[str]:
        """
        Run the configured checks over *path*.

        :param path: The archive to check.

        :return: A list of problems; empty means it passed.
        """
        if not self.enabled:
            return []
        problems = validate_archive(path)
        if problems or not self.references:
            return problems
        return reference_problems(path)


def reference_problems(path: Path) -> list[str]:
    """
    The reference check's errors for one archive, one line each.

    Warnings -- a link with an unregistered scheme, a metadata link to a
    missing file -- are left out, as epubcheck's were: they do not make a book
    invalid.

    :param path: The archive to check.

    :return: At most :data:`MAX_REPORTED` problems, then a count of the rest.
    """
    errors = [
        str(finding)
        for finding in check_file(path).findings
        if finding.severity != "warning"
    ]
    if len(errors) <= MAX_REPORTED:
        return errors
    rest = len(errors) - MAX_REPORTED
    return [*errors[:MAX_REPORTED], f"...and {rest} more reference problem(s)"]
