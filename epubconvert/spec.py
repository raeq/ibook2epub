"""
Constants fixed by the epub specification.

These live in one module because the writer and the validator must agree
about them by construction. Held separately, correcting one would leave the
other agreeing only with itself: the writer emitting a value the validator
rejects on every archive it produces, or a wrong value the validator blesses.
"""

from __future__ import annotations

#: The archive member that must come first, uncompressed.
MIMETYPE_NAME = "mimetype"

#: The exact bytes that member must contain.
MIMETYPE_CONTENT = b"application/epub+zip"

#: Extension of both an iBooks package directory and an exported archive.
PACKAGE_SUFFIX = ".epub"

#: Where the archive declares the location of its package document.
CONTAINER_PATH = "META-INF/container.xml"
