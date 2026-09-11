"""
The grammars in ``epubconvert.grammar.syntaxes``, each tested on its own.

A grammar is validated here against the examples its specification gives,
apart from the code that reads its parse, so a grammar that stops matching its
specification fails here under the grammar's name rather than as some check
answering differently.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=too-few-public-methods

from __future__ import annotations

import pytest

from epubconvert.grammar import Grammar
from epubconvert.grammar.syntaxes import (
    CFI,
    FRAGMENTS,
    IDENTIFIERS,
    NOTES,
    PACKAGE,
    WHITESPACE,
)


class TestWhitespace:
    def test_the_class_holds_exactly_what_isspace_calls_whitespace(self):
        grammar = Grammar(f"space <- {WHITESPACE}")
        spaces = [chr(point) for point in range(0x110000) if chr(point).isspace()]
        # Nothing above U+3000 is whitespace, so every candidate can be tried.
        assert max(spaces) == "\u3000"

        matched = [
            chr(point)
            for point in range(0x3001)
            if grammar.match(chr(point)) is not None
        ]

        assert matched == spaces


class TestIdentifiers:
    @pytest.mark.parametrize(
        ("declared", "digits"),
        [
            ("9780553383041", "9780553383041"),
            ("978-0-553-38304-1", "9780553383041"),
            ("urn:isbn:9780553383041", "9780553383041"),
            ("URN:ISBN:978 0 553 38304 1", "9780553383041"),
            ("ISBN 978-0553383041", "9780553383041"),
            ("urn:ean:9780553383041", "9780553383041"),
            ("  isbn:\t9780553383041\u00a0", "9780553383041"),
        ],
    )
    def test_an_isbn_13_in_every_spelling(self, declared, digits):
        parse = IDENTIFIERS.match(declared)

        assert parse is not None
        assert parse.find("isbn13") is not None
        assert "".join(node.text for node in parse.find_all("digit")) == digits

    def test_an_isbn_10_and_its_x(self):
        parse = IDENTIFIERS.match("isbn: 0-439-42089-X")

        assert parse is not None
        assert "".join(node.text for node in parse.find_all("digit")) == "043942089"
        assert parse.child("check").find("ten") is not None

    @pytest.mark.parametrize(
        "declared",
        [
            "f81d4fae-7dec-11d0-a765-00a0c91e6bf6",
            "urn:uuid:F81D4FAE-7DEC-11D0-A765-00A0C91E6BF6",
            "UUID: f81d4fae-7dec-11d0-a765-00a0c91e6bf6 ",
        ],
    )
    def test_a_uuid(self, declared):
        parse = IDENTIFIERS.match(declared)

        assert parse is not None
        assert (
            parse.child("uuid").text.lower() == "f81d4fae-7dec-11d0-a765-00a0c91e6bf6"
        )

    @pytest.mark.parametrize(
        "declared",
        [
            "978055338304",  # twelve digits
            "isbn-9780553383041",  # a label needs a colon or a space after it
            "urn:issn:12345678",
            "f81d4fae-7dec-11d0-a765-00a0c91e6bf",
            "978055338304\uff11",  # a fullwidth digit is not an ISBN digit
            "\u00b2" * 13,  # nor is a superscript two
        ],
    )
    def test_what_is_neither(self, declared):
        assert IDENTIFIERS.match(declared) is None

    def test_the_canonical_isbn_form_is_lower_case_and_exact(self):
        assert IDENTIFIERS.match("urn:isbn:9780553383041", "canonical_isbn")
        assert not IDENTIFIERS.match("URN:ISBN:9780553383041", "canonical_isbn")
        assert not IDENTIFIERS.match("urn:isbn:978055338304", "canonical_isbn")

    def test_only_a_978_isbn_has_an_isbn_10_body(self):
        parse = IDENTIFIERS.match("9780553383041", "bookland")

        assert parse is not None
        assert parse.child("isbn10_body").text == "055338304"
        assert IDENTIFIERS.match("9791234567896", "bookland") is None


class TestCanonicalFragmentIdentifiers:
    # Every example the specification's sections on assertions, side bias,
    # ranges and escaping give.
    @pytest.mark.parametrize(
        "cfi",
        [
            "epubcfi(/6/4[chap01ref]!/4[body01]/10[para05]/3:10)",
            "epubcfi(/6/4[chap01ref]!/4[body01]/10[para05]/2/1:3[yyy])",
            "epubcfi(/6/4[chap01ref]!/4[body01]/10[para05]/1:3[xx,y])",
            "epubcfi(/6/4[chap01ref]!/4[body01]/10[para05]/2/1:3[,y])",
            "epubcfi(/6/4[chap01ref]!/4[body01]/10[para05]/2/1:3[;s=b])",
            "epubcfi(/6/4[chap01ref]!/4[body01]/10[para05]/2/1:3[yyy;s=b])",
            "epubcfi(/6/4[chap01ref]!/4[body01]/10[para05],/2/1:1,/3:4)",
            "epubcfi(/6/14[chap05ref]!/4[body01]/10/2/1:3[2^[1^]])",
            "epubcfi(/6/4!/2@1.25:0.5)",
            "epubcfi(/6/4!/2~23.5@50:50)",
        ],
    )
    def test_the_specifications_examples(self, cfi):
        assert CFI.match(cfi) is not None

    @pytest.mark.parametrize(
        "cfi",
        [
            "book.epub#epubcfi(/6/4[chap01ref]!/4[body01]/10[para05]/3:10)",
            "1234.ibooks#epubcfi(/6/44[n-1]!/4/4/2:0)",
            "epubcfi(/6/44[n-1]!,/4:0,/4/16[p7]:0)",
        ],
    )
    def test_the_forms_apple_books_stores(self, cfi):
        assert CFI.match(cfi) is not None

    @pytest.mark.parametrize(
        "cfi",
        [
            "/6/4[chap01ref]!/4",  # not wrapped
            "epubcfi(/06/4)",  # a leading zero
            "epubcfi(/6/4[a[b]!/4)",  # an unescaped bracket in an assertion
            "epubcfi(/6/4!/2@1.50:2)",  # a fraction ending in zero
            "epubcfi(/6/4!/2) trailing",
            "epubcfi(/6/4!)",  # an indirection to nothing, with no range after it
        ],
    )
    def test_what_the_syntax_refuses(self, cfi):
        assert CFI.match(cfi) is None

    def test_an_escaped_assertion_keeps_its_escapes_apart(self):
        parse = CFI.match("epubcfi(/6/14[chap05ref]!/4[body01]/10/2/1:3[2^[1^]])")

        assert parse is not None
        value = parse.find_all("assertion")[-1].child("value")
        assert [(part.name, part.text) for part in value.children] == [
            ("plain", "2"),
            ("escaped", "^["),
            ("plain", "1"),
            ("escaped", "^]"),
        ]

    def test_the_step_before_an_indirection_is_found_by_position(self):
        parse = CFI.match("epubcfi(/6/4[chap01ref]!/4[body01]/10[para05]/3:10)")

        assert parse is not None
        indirection = parse.child("redirected_path")
        before = [s for s in parse.find_all("step") if s.end == indirection.start]
        assert [step.text for step in before] == ["/4[chap01ref]"]


class TestFragments:
    @pytest.mark.parametrize(
        ("fragment", "name"),
        [
            ("section2", "section2"),
            ("chapter:~:text=word", "chapter"),
            (":~:text=word", None),
            ("epubcfi(/6/4!/2)", None),
            ("xpointer(id('a'))", None),
            ("f(x)y", "f(x)y"),
            ("a(b:~:c)", "a(b"),  # the directive is set aside before anything else
            ("t=10,20", None),
            ("xywh=160,120,320,240", None),
            ("track=audio&t=10", None),
            ("id=chapter-1", None),
            ("t=", "t="),  # a media fragment needs a value
            ("t=10&x", "t=10&x"),  # and every later part needs one too
            ("", None),
        ],
    )
    def test_an_xhtml_fragment(self, fragment, name):
        parse = FRAGMENTS.match(fragment, "html_fragment")

        assert parse is not None
        found = parse.find("html_name")
        assert (found.text if found else None) == name

    @pytest.mark.parametrize(
        ("fragment", "name"),
        [
            ("shape", "shape"),
            ("shape&t=1", "shape"),
            ("svgView(viewBox(0,0,200,200))", None),
            ("t=1", None),
            ("xywh=1,2,3,4&t=5", None),
            ("a=b", None),
            ("", None),
        ],
    )
    def test_an_svg_fragment(self, fragment, name):
        parse = FRAGMENTS.match(fragment, "svg_fragment")

        assert parse is not None
        found = parse.find("svg_name")
        assert (found.text or None if found else None) == name


class TestPackageAttributes:
    @pytest.mark.parametrize("version", ["3.0", "3", " 3.0\n", "3.1", "03.0"])
    def test_versions_that_are_epub_3(self, version):
        assert PACKAGE.match(version, "epub3_version") is not None

    @pytest.mark.parametrize("version", ["2.0", "30", "3.0b", "", "3."])
    def test_versions_that_are_not(self, version):
        assert PACKAGE.match(version, "epub3_version") is None

    @pytest.mark.parametrize(
        ("properties", "values"),
        [
            ("cover-image", [(None, "cover-image")]),
            (" nav  cover-image ", [(None, "nav"), (None, "cover-image")]),
            ("rendition:layout-pre-paginated", [("rendition", "layout-pre-paginated")]),
            ("not-cover-image", [(None, "not-cover-image")]),
            ("", []),
        ],
    )
    def test_properties(self, properties, values):
        parse = PACKAGE.match(properties, "properties")

        assert parse is not None
        assert [
            (
                prefix.text if (prefix := value.find("prefix")) else None,
                value.child("reference").text,
            )
            for value in parse.find_all("property")
        ] == values

    def test_a_prefix_with_no_reference_is_not_a_property(self):
        assert PACKAGE.match("rendition:", "properties") is None


class TestNoteMarkers:
    def test_the_start_marker_and_its_digest(self):
        line = "<!-- ibook2epub sha256=0123456789abcdef -->"

        parse = NOTES.match(line, "start_marker")

        assert parse is not None
        assert parse.child("digest").text == "0123456789abcdef"
        assert NOTES.match(line + "  ", "start_marker") is not None

    @pytest.mark.parametrize(
        "digest", ["0123456789abcde", "0" * 65, "0123456789ABCDEF"]
    )
    def test_a_digest_the_tool_would_not_write(self, digest):
        assert (
            NOTES.match(f"<!-- ibook2epub sha256={digest} -->", "start_marker") is None
        )

    def test_the_end_marker_is_matched_on_its_prefix(self):
        line = (
            "<!-- ibook2epub end \u2014 your notes below this line are never "
            "modified -->"
        )

        assert NOTES.match_prefix(line, "end_marker") is not None
        assert NOTES.match_prefix("<!-- ibook2epub ended", "end_marker") is not None
        assert NOTES.match_prefix("<!-- other end", "end_marker") is None

    @pytest.mark.parametrize(
        ("line", "is_fence"), [("---", True), ("--- ", True), ("----", False)]
    )
    def test_the_frontmatter_fence(self, line, is_fence):
        assert (NOTES.match(line, "fence") is not None) is is_fence

    @pytest.mark.parametrize(
        ("line", "at"),
        [
            ("# Heading", 0),
            ("   > quote", 3),
            ("- item", 0),
            ("12) item", 0),
            ("3. item", 0),
            ("\u00a0* item", 1),
            ("plain text", None),
            ("\u0661. not a list", None),  # an Arabic-Indic digit opens nothing
        ],
    )
    def test_block_openers(self, line, at):
        parse = NOTES.match_prefix(line, "block_opener")

        assert (parse.child("opener").start if parse else None) == at
