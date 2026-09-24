"""
What a book's metadata identifies it by, and when that is worth anything.

A ``dc:identifier`` or ``dc:title`` from a package document is only a matching
key when the same book always yields the same string and a placeholder yields
nothing. This module holds those rules: which values are junk, how an
identifier is rendered canonically, and how an ISBN is verified and converted
between its ten- and thirteen-digit forms.
"""

from __future__ import annotations

import re

from ..utils.opf import Package

#: Values that appear where a ``dc:identifier`` should be but identify nothing.
#: Every one was found in a real 2,805-book library: ``none`` is the identifier
#: for 92 books, ``ISBN`` for three, ``unknown`` for one. Matched case-folded.
JUNK_IDENTIFIERS = frozenset(
    {"none", "null", "isbn", "unknown", "uuid", "calibre", "0"}
)


def usable_identifier(package: Package | None) -> str | None:
    """
    Return the package's identifier, if it identifies anything.

    An identifier is worth using only when it distinguishes this book from
    another. A placeholder the publisher never replaced does the opposite: 92
    books in a surveyed library all claim to be ``none``, so treating that as a
    real value would merge them.

    Uniqueness is *not* checked here, because it cannot be: two books can carry
    the same genuine identifier -- six unrelated technical books in that library
    share one converter's template UUID. Callers that need distinctness have to
    confirm it against the set they are naming.

    :param package: The parsed package document, or None if it was not read.

    :return: The trimmed identifier, or None if there is nothing usable.
    """
    if package is None or not package.identifier:
        return None
    trimmed = package.identifier.strip()
    if not trimmed or trimmed.casefold() in JUNK_IDENTIFIERS:
        return None
    return trimmed


#: A UUID, whatever case it is written in.
_UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")

#: The prefixes publishers put in front of the value itself. ``urn:`` scheme
#: names are case-insensitive by RFC 8141, and this library holds both
#: ``urn:isbn:`` and ``URN:ISBN:``.
_IDENTIFIER_PREFIX = re.compile(r"(?i)^(urn:)?(isbn|uuid|ean)[:\s]+")


def canonical_identifier(value: str) -> str:
    """
    Render an identifier the one way its book would always have written it.

    An identifier is a matching key, and it is only a key if the same book
    yields the same string. It does not: a surveyed 2,805-book library writes
    one ISBN six ways -- bare, ``urn:isbn:``, ``URN:ISBN:``, hyphenated,
    ``ISBN 978...``, and ``urn:ean:`` -- across 1,092 books, and one UUID two
    ways across 1,448. Two copies of the same book catalogued by different
    tools therefore did not match each other.

    Only what can be *verified* is normalised. An ISBN is recognised by its
    book prefix and check digit, never by counting digits: 68 identifiers in
    that library are 10 or 13 digits and fail the check, and a digit count
    would have relabelled every one of them. An ISBN-10 becomes the ISBN-13
    meaning the same book, which is exact arithmetic rather than a guess.
    Anything unrecognised is returned exactly as it came in, because the
    specification says this field is opaque and reshaping an opaque string is
    a claim about it.

    :param value: The identifier the package document declares.

    :return: The canonical form, or *value* unchanged when it is neither a
        valid ISBN nor a UUID.
    """
    body = _IDENTIFIER_PREFIX.sub("", value.strip())
    if _UUID.fullmatch(body):
        return f"urn:uuid:{body.lower()}"

    digits = re.sub(r"[-\s]", "", body)
    if _is_isbn13(digits):
        return f"urn:isbn:{digits}"
    if _is_isbn10(digits):
        return f"urn:isbn:{_as_isbn13(digits)}"
    return value.strip()


#: The prefix a canonical ISBN carries.
ISBN_URN = "urn:isbn:"


def isbn13_of(identifier: object) -> str | None:
    """
    Take the bare ISBN out of a canonical identifier, when it is one.

    Judged by :func:`_is_isbn13`, never by the ``urn:isbn:`` in front of it.
    :func:`canonical_identifier` leaves an identifier it cannot verify exactly
    as declared, so that prefix is not evidence that an ISBN follows: 68
    identifiers in a surveyed library are ten or thirteen digits and fail
    their check, and a tracker handed one of those matches the wrong book or
    none. Derived by stripping the prefix rather than looked up again, so it
    cannot disagree with the field it came from. About 41% of books have one:
    1,108 ``urn:isbn`` against 1,448 ``urn:uuid`` in that library.

    :param identifier: The canonical identifier, or None.

    :return: The thirteen digits, or None when the book is identified some
        other way or its ISBN does not verify.
    """
    if not isinstance(identifier, str) or not identifier.startswith(ISBN_URN):
        return None
    digits = identifier[len(ISBN_URN) :]
    return digits if _is_isbn13(digits) else None


def _ascii_digits(text: str) -> bool:
    """
    Whether *text* is ASCII digits, and only those.

    ``str.isdigit`` is not that test. It accepts a superscript two, which
    ``int`` then refuses, so an identifier of superscript digits stopped the
    run with a ValueError; and it accepts Arabic-Indic digits, which ``int``
    reads, so such an identifier was written back as an ISBN in those digits.
    """
    return text.isascii() and text.isdigit()


def _isbn13_sum(digits: str) -> int:
    """ISO 2108's ISBN-13 weighting: the digits weigh 1 and 3 alternately."""
    return sum((1 if i % 2 == 0 else 3) * int(c) for i, c in enumerate(digits))


def _isbn10_sum(body: str) -> int:
    """ISO 2108's ISBN-10 weighting of the nine body digits: 10 down to 2."""
    return sum((10 - i) * int(c) for i, c in enumerate(body))


#: The EAN-13 prefixes ISO 2108 assigns to books. Every ISBN-13 is an EAN-13,
#: but an EAN outside these is a product barcode, and the check digit alone
#: relabelled one ``urn:isbn`` and exported it to a tracker as an ISBN13.
ISBN13_PREFIXES = ("978", "979")


#: Nine-digit ISBN bodies a converter writes as filler: ``0123456789`` and
#: ``123456789X`` carry valid check characters. So does every repeated digit,
#: which weighs 55 times itself, a multiple of 11: ``0000000000`` passed as an
#: ISBN and reached a tracker's import and the notes' frontmatter as one.
_PLACEHOLDER_BODIES = frozenset({"012345678", "123456789"})


def _placeholder(body: str) -> bool:
    """Whether an ISBN's nine-digit body is filler rather than a book's."""
    return len(set(body)) == 1 or body in _PLACEHOLDER_BODIES


def _is_isbn13(digits: str) -> bool:
    """
    Whether *digits* is a book's EAN-13: 978 or 979, and a valid check digit.

    The body between the prefix and the check digit is judged as an ISBN-10's
    is, so the thirteen-digit form of a placeholder is refused as well.
    """
    if len(digits) != 13 or not digits.startswith(ISBN13_PREFIXES):
        return False
    if _placeholder(digits[3:12]):
        return False
    return _ascii_digits(digits) and _isbn13_sum(digits) % 10 == 0


def _is_isbn10(digits: str) -> bool:
    """Whether *digits* is ten characters carrying a valid check digit."""
    if len(digits) != 10 or not _ascii_digits(digits[:9]):
        return False
    if not (_ascii_digits(digits[9]) or digits[9] in "Xx"):
        return False
    if _placeholder(digits[:9]):
        return False
    check = 10 if digits[9] in "Xx" else int(digits[9])
    return (_isbn10_sum(digits[:9]) + check) % 11 == 0


def isbn10_of(isbn13: str | None) -> str | None:
    """
    Derive the ISBN-10 that means the same book, when one exists.

    The inverse of :func:`_as_isbn13`, kept beside it so the two weightings
    cannot drift apart. Only a ``978`` ISBN-13 has one; ``979`` books were
    never given an ISBN-10. Written because Goodreads exports both columns and
    an importer may match on either.

    :param isbn13: Thirteen digits, already verified, or None.

    :return: The ten characters, or None, including for anything that is not
        thirteen ASCII digits: a prefix test alone made ``"978"`` into ``"0"``
        and raised ValueError on ``"978abc..."``.
    """
    if isbn13 is None or len(isbn13) != 13 or not isbn13.startswith("978"):
        return None
    if not _ascii_digits(isbn13):
        return None
    body = isbn13[3:12]
    check = (11 - _isbn10_sum(body) % 11) % 11
    return body + ("X" if check == 10 else str(check))


def _as_isbn13(digits: str) -> str:
    """
    Convert a valid ISBN-10 to the ISBN-13 naming the same book.

    :param digits: Ten characters, already checked.

    :return: Thirteen digits.
    """
    body = f"978{digits[:9]}"
    return f"{body}{(10 - _isbn13_sum(body) % 10) % 10}"


#: Values a converter writes where a title should be. Narrower than
#: :data:`JUNK_IDENTIFIERS` and applied only to titles: 300 books in a surveyed
#: library declare a *creator* of "Unknown", and filing those together under U
#: is a real cataloguing answer, where a title of "none" says less than the
#: folder name the book already has. Matched case-folded, and whole: "None of
#: This Is True" is a real book.
JUNK_TITLES = frozenset({"none", "null", "n/a", "unknown"})


def usable_title(package: Package | None) -> str | None:
    """
    Return the package's title, if it names anything.

    31 books in a surveyed library carry the literal string "none" in every
    metadata field they have, including the one Apple derives its folder name
    from. Treating that as a title made all 31 want ``none.epub``, so they
    collided with each other and were suffixed into a pile.

    :param package: The parsed package document, or None if it was not read.

    :return: The trimmed title, or None if there is nothing usable.
    """
    if package is None or not package.title:
        return None
    trimmed = package.title.strip()
    if not trimmed or trimmed.casefold() in JUNK_TITLES:
        return None
    return trimmed
