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
# pylint: disable=use-implicit-booleaness-not-comparison,too-few-public-methods

import posixpath
from pathlib import Path
from zipfile import ZipFile

import pytest

from epubconvert.collect import package as package_reader
from epubconvert.collect import validate
from epubconvert.export.archive import zip_package
from epubconvert.run import run
from epubconvert.utils.display import printable
from tests.conftest import make_metadata_package, remove_tree
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


#: Hrefs urlsplit reads as a bracketed IPv6 host once it has stripped the
#: leading space or removed the tab or carriage return, and then refuses. The
#: control characters are character references: XML turns a literal one in an
#: attribute into a space.
UNSPLITTABLE = [
    pytest.param(" //[x#f", " //[x", id="space"),
    pytest.param("&#9;//[x#f", "\t//[x", id="tab"),
    pytest.param("&#13;//[x#f", "\r//[x", id="carriage-return"),
    pytest.param("/&#9;/[x#f", "/\t/[x", id="tab-inside"),
]


def _resolved(name: str) -> str:
    return posixpath.normpath(posixpath.join("OEBPS", name))


def _with_item(opf: str, href: str) -> str:
    return opf.replace(
        "</manifest>",
        f'<item id="odd" href="{href}" media-type="application/xhtml+xml"/></manifest>',
    )


class TestAnHrefUrlsplitRefusesIsStillAPath:
    @pytest.mark.parametrize(("href", "name"), UNSPLITTABLE)
    def test_it_is_read_as_a_member_name(self, tmp_path, href, name):
        # Regression: the fragment was split off with urldefrag, which raised
        # ValueError("Invalid IPv6 URL") out of the package reader, and no
        # caller catches that.
        members = dict(MEMBERS)
        members["OEBPS/content.opf"] = _with_item(members["OEBPS/content.opf"], href)
        path = write_epub(tmp_path / "Book.epub", members)

        with ZipFile(path) as opened:
            package = package_reader.read_package(opened)

        assert package.manifest["odd"] == _resolved(name)

    @pytest.mark.parametrize(("href", "name"), UNSPLITTABLE)
    def test_validation_reports_the_missing_member(self, tmp_path, href, name):
        members = dict(MEMBERS)
        members["OEBPS/content.opf"] = _with_item(members["OEBPS/content.opf"], href)
        path = write_epub(tmp_path / "Book.epub", members)

        problems = validate.validate_archive(path)

        entry = printable(f"odd -> {_resolved(name)}")
        assert problems == [f"manifest item is not in the archive: {entry}"]

    def test_any_other_value_error_is_a_validation_error(self, tmp_path, monkeypatch):
        # Defence in depth: every reader of a package catches ValidationError,
        # so the next ValueError out of the standard library must become one.
        def refuse(base, href):
            raise ValueError("Invalid IPv6 URL")

        monkeypatch.setattr(package_reader, "_resolve", refuse)
        path = write_epub(tmp_path / "Book.epub")

        with ZipFile(path) as opened, pytest.raises(package_reader.ValidationError):
            package_reader.read_package(opened)

    def test_its_message_reaches_the_terminal_escaped(self, tmp_path, monkeypatch):
        # A ValueError's text can quote the href it choked on.
        def refuse(base, href):
            raise ValueError(f"bad href {href}\x1b[2K\r")

        monkeypatch.setattr(package_reader, "_resolve", refuse)
        path = write_epub(tmp_path / "Book.epub")

        with (
            ZipFile(path) as opened,
            pytest.raises(package_reader.ValidationError) as err,
        ):
            package_reader.read_package(opened)

        assert "\x1b" not in str(err.value) and "\r" not in str(err.value)


def _hostile_library(tmp_path: Path, zipped: bool) -> Path:
    library = tmp_path / "library"
    library.mkdir()
    make_metadata_package(
        library, "Innocent.epub", title="Innocent", creator="Someone Else"
    )
    hostile = make_metadata_package(
        library, "Hostile.epub", title="Hostile", creator="Someone"
    )
    opf = hostile / "OEBPS" / "content.opf"
    opf.write_text(_with_item(opf.read_text(encoding="utf-8"), " //[x#f"), "utf-8")
    if zipped:
        zip_package(hostile, library / "Zipped.epub")
        remove_tree(hostile)
    return library


class TestOneBookWithSuchAnHrefDoesNotStopTheRun:
    @pytest.mark.parametrize("zipped", [False, True], ids=["package", "zipped"])
    @pytest.mark.parametrize("dry_run", [False, True], ids=["export", "dry-run"])
    def test_the_innocent_book_is_still_exported(
        self, tmp_path, output_dir, zipped, dry_run
    ):
        # Regression: a package directory, or a zipped book copied through,
        # named by author and title raised ValueError out of the planner and
        # the copier, which catch ValidationError, and the run died with a
        # traceback, even under --dry-run.
        library = _hostile_library(tmp_path, zipped)
        flags = ["--dry-run"] if dry_run else []

        code = run.main(
            ["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"]
            + ["--name-by", "author-title", *flags]
        )

        assert code == 0
        exported = ["Someone - Hostile.epub", "Someone Else - Innocent.epub"]
        shelf = sorted(path.name for path in output_dir.glob("*.epub"))
        assert shelf == ([] if dry_run else exported)
