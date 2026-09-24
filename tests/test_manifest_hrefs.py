"""
Which manifest hrefs name a member of the book, and which member.

An href is a URL, resolved against the package document. Most name a file
in the archive; some name something outside it, which --validate must not
look for. Getting that wrong rejects a sound book, and a rejected book is
never written to the shelf and is retried on every run.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them.
# pylint: disable=missing-function-docstring,missing-class-docstring
# An empty problem list is the assertion: "no problems", not a falsy value.
# pylint: disable=use-implicit-booleaness-not-comparison

import pytest

from epubconvert.collect import validate
from tests.test_validate import MEMBERS, write_epub


class TestManifestHrefs:
    @pytest.mark.parametrize(
        "href",
        [
            "kindle:embed:0002?mime=image/jpg",
            "tel:+1234",
            "urn:isbn:9780553383041",
            "HTTPS://example.com/font.otf",
            "//cdn.example.com/font.otf",
        ],
    )
    def test_any_url_with_a_scheme_or_host_is_not_a_member(self, tmp_path, href):
        # Only six schemes used to count as remote. Every other one was joined
        # onto OEBPS/ as a member name, so --validate reported it missing,
        # rejected a sound book, and the book was never written.
        members = dict(MEMBERS)
        members["OEBPS/content.opf"] = members["OEBPS/content.opf"].replace(
            "</manifest>",
            f'<item id="elsewhere" href="{href}" media-type="image/jpeg"/></manifest>',
        )
        path = write_epub(tmp_path / "Book.epub", members)

        assert validate.validate_archive(path) == []

    def test_a_query_is_not_part_of_the_member_name(self, tmp_path):
        members = dict(MEMBERS)
        members["OEBPS/content.opf"] = members["OEBPS/content.opf"].replace(
            'href="text/chapter1.xhtml"', 'href="text/chapter1.xhtml?x=1"'
        )
        path = write_epub(tmp_path / "Book.epub", members)

        assert validate.validate_archive(path) == []

    def test_a_percent_encoded_question_mark_is_part_of_the_name(self, tmp_path):
        members = dict(MEMBERS)
        members["OEBPS/text/a?b.xhtml"] = members.pop("OEBPS/text/chapter1.xhtml")
        members["OEBPS/content.opf"] = members["OEBPS/content.opf"].replace(
            'href="text/chapter1.xhtml"', 'href="text/a%3Fb.xhtml"'
        )
        path = write_epub(tmp_path / "Book.epub", members)

        assert validate.validate_archive(path) == []

    @pytest.mark.parametrize(
        "href", ["text/chapter1.xhtml?x=1#f", "text/chapter1.xhtml#f?x=1"]
    )
    def test_a_query_and_a_fragment_in_either_order_leave_the_name(
        self, tmp_path, href
    ):
        members = dict(MEMBERS)
        members["OEBPS/content.opf"] = members["OEBPS/content.opf"].replace(
            'href="text/chapter1.xhtml"', f'href="{href}"'
        )
        path = write_epub(tmp_path / "Book.epub", members)

        assert validate.validate_archive(path) == []

    def test_a_colon_in_the_first_segment_is_reached_through_dot_slash(self, tmp_path):
        # "c:1.xhtml" is an absolute URL with scheme "c" (RFC 3986, 4.2), and
        # OCF forbids ":" in a file name anyway. The docstring's way to name
        # such a member is "./c:1.xhtml"; this pins that it still works.
        members = dict(MEMBERS)
        members["OEBPS/c:1.xhtml"] = members.pop("OEBPS/text/chapter1.xhtml")
        members["OEBPS/content.opf"] = members["OEBPS/content.opf"].replace(
            'href="text/chapter1.xhtml"', 'href="./c:1.xhtml"'
        )
        path = write_epub(tmp_path / "Book.epub", members)

        assert validate.validate_archive(path) == []
