"""
Constants fixed by the epub specification.

These live in one module because the writer and the validator must agree
about them by construction. Held separately, correcting one would leave the
other agreeing only with itself: the writer emitting a value the validator
rejects on every archive it produces, or a wrong value the validator blesses.
"""

from __future__ import annotations

import unicodedata

#: The archive member that must come first, uncompressed.
MIMETYPE_NAME = "mimetype"

#: That name as its local header holds it: the bytes a reader sniffs at 30.
MIMETYPE_BYTES = b"mimetype"

#: The exact bytes that member must contain.
MIMETYPE_CONTENT = b"application/epub+zip"

#: Extension of both an iBooks package directory and an exported archive.
PACKAGE_SUFFIX = ".epub"

#: Where the archive declares the location of its package document.
CONTAINER_PATH = "META-INF/container.xml"


def fold_name(name: str) -> str:
    """
    Fold a name the way a case-insensitive filesystem compares it.

    NFD, then full case folding, then NFC: Unicode's canonical caseless match
    (D145) with the result recomposed, as case-insensitive APFS compares
    names. Here because the writer
    (:func:`epubconvert.export.naming.filesystem_key`) and the validator's
    duplicate check must fold alike, and were two copies of one rule held
    together only by a test.

    Decomposed before folding and recomposed after, not folded once composed:
    folding composed text can leave it uncomposed. Upper-case H-circumflex
    with a macron below folded to ``ĥ`` and the macron, while its lower-case
    form, ``ẖ`` and a circumflex, stayed as it was, so two names differing
    only by case were given two keys. Recomposed rather than left decomposed
    so that an ordinary name -- ASCII, precomposed Latin, Hangul -- keeps the
    key it always had.

    :param name: A filename or an archive member name.

    :return: A key equal for any two names such a filesystem cannot tell apart.
    """
    return unicodedata.normalize("NFC", unicodedata.normalize("NFD", name).casefold())
