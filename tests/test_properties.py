"""
Property-based tests: what a pure function promises, for any input.

The example tests beside these pin the cases someone thought of. These state
the claim a function's docstring makes and let Hypothesis look for an input
that breaks it -- including lone surrogates, which os.walk hands back for an
undecodable filename and which most hand-picked examples leave out.

Hypothesis is a development dependency; tests/conftest.py leaves this
module uncollected without it.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them.
# pylint: disable=missing-function-docstring
# Several properties are of private helpers, which is where the rule lives.
# pylint: disable=protected-access

import re

from hypothesis import given
from hypothesis import strategies as st

from epubconvert.collect import annotations, validate
from epubconvert.export import notes
from epubconvert.export.naming import encode_name, split_extension, truncate_bytes
from epubconvert.run.planning import marked, suffixed
from epubconvert.utils.display import printable

#: Any code point Python can hold in a str, lone surrogates included.
ANY_TEXT = st.text(alphabet=st.characters(codec=None, exclude_categories=()))

#: Nine ASCII digits: the body every ISBN-10 and 978 ISBN-13 shares.
BODY = st.text(alphabet="0123456789", min_size=9, max_size=9)


# ------------------------------------------------------------- truncate_bytes


@given(ANY_TEXT, st.integers(min_value=0, max_value=300))
def test_truncation_fits_and_keeps_the_longest_whole_prefix(text, limit):
    cut = truncate_bytes(text, limit)

    assert text.startswith(cut)
    assert len(encode_name(cut)) <= limit
    # Nothing longer would have fitted: the next character, if any, is the
    # one that would cross the limit.
    if cut != text:
        assert len(encode_name(text[: len(cut) + 1])) > limit


# --------------------------------------------------------- suffixed and marked


#: A name as the planner suffixes one: a package's *.epub or a copied *.pdf.
#: An extensionless name ending in "." is not in the domain: suffixing "0."
#: gives "0. (2)", which split_extension reads as having the extension ". (2)".
FILENAMES = st.builds(
    lambda stem, ext: stem + ext,
    st.text(min_size=1).filter(lambda s: s.strip(" .")),
    st.sampled_from([".epub", ".pdf"]),
)


@given(FILENAMES, st.integers(min_value=2, max_value=99))
def test_a_suffixed_name_keeps_its_extension_and_stays_distinct(name, position):
    candidate = suffixed(name, position, 0)

    assert split_extension(candidate)[1] == split_extension(name)[1]
    assert f" ({position})" in candidate
    assert candidate != name


# A 20-character tag is at most 83 bytes as a marker, and ".epub" is 5, so
# every budget drawn leaves the stem at least one byte.
@given(FILENAMES, st.text(min_size=1, max_size=20), st.integers(100, 255))
def test_a_marked_name_fits_the_budget_and_keeps_marker_and_extension(
    name, tag, budget
):
    marker = f" [{tag}]"
    extension = split_extension(name)[1]

    candidate = marked(name, marker, budget)

    assert len(encode_name(candidate)) <= budget
    assert candidate.endswith(marker + extension)


@given(FILENAMES, st.text(min_size=1, max_size=20))
def test_a_marked_name_that_fits_is_the_name_with_the_marker(name, tag):
    marker = f" [{tag}]"
    stem, extension = split_extension(name)

    assert marked(name, marker, 0) == f"{stem}{marker}{extension}"


# ------------------------------------------------------------- notes._escape


@given(ANY_TEXT.map(lambda s: s.replace("\n", "").replace("\r", "")))
def test_an_escaped_line_opens_no_block_and_forges_no_marker(line):
    escaped = notes._escape(line)

    assert not notes.BLOCK_OPENERS.match(escaped)
    assert not notes.START_PATTERN.match(escaped)
    assert not notes.END_PATTERN.match(escaped)


@given(ANY_TEXT.map(lambda s: s.replace("\n", "").replace("\r", "")))
def test_escaping_changes_only_a_line_that_needed_it(line):
    needed = bool(
        notes.BLOCK_OPENERS.match(line)
        or notes.START_PATTERN.match(line)
        or notes.END_PATTERN.match(line)
    )

    assert (notes._escape(line) != line) == needed


@given(ANY_TEXT.map(lambda s: s.replace("\n", "").replace("\r", "")))
def test_escaping_an_escaped_line_changes_nothing(line):
    once = notes._escape(line)

    assert notes._escape(once) == once


@given(
    st.sampled_from(
        ["# ", "> ", "- ", "+ ", "* ", "1. ", "12) ", "  3. "]
        + ["```", "~~~", "<!--", "<div", "| ", "[x]: "]
    ),
    st.text(),
)
def test_the_backslash_goes_on_the_openers_punctuation(opener, rest):
    # CommonMark escapes only ASCII punctuation, so a backslash in front of a
    # list number's digits escapes nothing and shows.
    escaped = notes._escape(opener + rest)

    backslash = escaped.index("\\")
    assert escaped[backslash + 1] in "#>+-*.)`~<|["


@given(
    st.text(alphabet=" \t", min_size=1),
    st.text(alphabet=st.characters(exclude_characters="\n\r"), min_size=1).filter(
        lambda s: s[0] not in " \t"
    ),
)
def test_indentation_that_opens_code_is_kept_as_columns_of_text(lead, rest):
    # Four columns open an indented code block; a no-break space is text, so
    # the line stays where the reader put it and opens nothing.
    columns = len(lead.expandtabs(notes.TAB_WIDTH))
    escaped = notes._escape(lead + rest)

    if columns >= 4:
        assert escaped == notes.INDENT * columns + rest
    else:
        assert escaped.startswith(lead)


# ------------------------------------------------------------------------ ISBN


def _isbn10(body: str) -> str:
    """Nine digits and the check character that makes them an ISBN-10."""
    check = (11 - sum((10 - i) * int(c) for i, c in enumerate(body)) % 11) % 11
    return body + ("X" if check == 10 else str(check))


@given(BODY)
def test_an_isbn10_becomes_a_valid_isbn13_that_leads_back_to_it(body):
    isbn10 = _isbn10(body)

    isbn13 = validate._as_isbn13(isbn10)

    assert validate._is_isbn13(isbn13)
    assert validate.isbn10_of(isbn13) == isbn10


@given(BODY, st.sampled_from("0123456789X"))
def test_exactly_one_check_character_makes_an_isbn10(body, check):
    assert validate._is_isbn10(body + check) == (body + check == _isbn10(body))


@given(ANY_TEXT)
def test_canonicalising_an_identifier_twice_changes_nothing(value):
    once = validate.canonical_identifier(value)

    assert validate.canonical_identifier(once) == once


#: Ten or thirteen characters that str.isdigit() accepts, in any script:
#: Unicode decimal digits (Nd) and other digits such as superscripts (No).
ANY_SCRIPT_DIGITS = st.one_of(
    [
        st.text(alphabet=st.characters(categories=["Nd", "No"]), min_size=n, max_size=n)
        for n in (10, 13)
    ]
)


@given(ANY_SCRIPT_DIGITS)
def test_only_ascii_digits_make_an_isbn(value):
    # str.isdigit() accepts a superscript two, which int() refuses, and
    # Arabic-Indic digits, which int() reads: the one raised out of the run,
    # the other was written back as an ISBN in those digits.
    canonical = validate.canonical_identifier(value)

    if canonical.startswith("urn:isbn:"):
        assert canonical.removeprefix("urn:isbn:").isascii()
        assert value.isascii()


@given(BODY, st.lists(st.sampled_from(["-", " "]), min_size=9, max_size=9))
def test_a_hyphenated_isbn_is_the_same_book(body, separators):
    isbn10 = _isbn10(body)
    written = "".join(d + s for d, s in zip(isbn10, [*separators, ""], strict=True))

    assert validate.canonical_identifier(f"ISBN {written}") == (
        validate.canonical_identifier(isbn10)
    )


# -------------------------------------------------------------- CFI assertions

#: The characters EPUB CFI 1.1 makes special, which an ID must escape.
CFI_SPECIAL = re.compile(r"([\^\[\](),;=])")


@given(
    st.text(
        alphabet=st.characters(codec="utf-8", exclude_characters="\x00"), min_size=1
    )
)
def test_an_id_escaped_into_a_cfi_is_looked_up_as_written(identifier):
    escaped = CFI_SPECIAL.sub(r"^\1", identifier)
    cfi = f"epubcfi(/6/4[{escaped}]!/4/2/1:0)"

    assert annotations._assertion_of(cfi) == identifier


@given(ANY_TEXT)
def test_no_input_makes_the_cfi_reader_raise(location):
    annotations._assertion_of(location)


# ------------------------------------------------------------------ printable


@given(ANY_TEXT)
def test_a_printable_name_carries_no_control_character_or_surrogate(name):
    shown = printable(name)

    assert not any(
        ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F or 0xD800 <= ord(c) <= 0xDFFF
        for c in shown
    )
    # A surrogate that survives would make the log handler raise.
    shown.encode("utf-8")
