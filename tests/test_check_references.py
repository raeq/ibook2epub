"""
Tests for ``--check-references``, the built-in reference check.

It took the place of the external epubcheck, which needed a Java runtime and
was mocked at the subprocess boundary because almost nobody had it installed.
This check is part of the package, so it runs for real here: a small book is
written with one broken link and the archive is refused.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them. Explicit empty-list comparisons read better than truthiness here.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=use-implicit-booleaness-not-comparison,too-few-public-methods

from __future__ import annotations

from pathlib import Path

import pytest

from epubconvert.collect import checks, validate
from epubconvert.collect.references import Finding, Report
from epubconvert.export.archive import zip_package
from epubconvert.export.inspect_output import verify_output

CONTAINER = (
    '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
    '<rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles>'
    "</container>"
)
OPF = (
    '<package xmlns="http://www.idpf.org/2007/opf" version="3.0"'
    ' unique-identifier="bid">'
    '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
    '<dc:identifier id="bid">urn:isbn:9780553383041</dc:identifier></metadata>'
    '<manifest><item id="ch1" href="text/chapter1.xhtml"'
    ' media-type="application/xhtml+xml"/></manifest>'
    '<spine><itemref idref="ch1"/></spine></package>'
)
BROKEN = '<a href="chapter2.xhtml">next</a>'
SOUND = '<p id="top"><a href="#top">back to the top</a></p>'
WITH_REFERENCES = checks.ValidationOptions(enabled=True, references=True)


def make_source(parent: Path, body: str) -> Path:
    """An unpacked package, as Apple keeps one, whose one chapter holds *body*."""
    package = parent / "Book.epub"
    layout = {
        "mimetype": "application/epub+zip",
        "META-INF/container.xml": CONTAINER,
        "OEBPS/content.opf": OPF,
        "OEBPS/text/chapter1.xhtml": (
            '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>t</title>'
            f"</head><body>{body}</body></html>"
        ),
    }
    for relative, text in layout.items():
        path = package / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return package


def export(tmp_path: Path, body: str, options: checks.ValidationOptions) -> Path:
    """Write the book into tmp_path/out and return where it went."""
    target = tmp_path / "out" / "Book.epub"
    target.parent.mkdir(exist_ok=True)
    zip_package(make_source(tmp_path / "src", body), target, options)
    return target


class TestTheCheckDuringExport:
    def test_a_book_with_a_broken_link_is_not_written(self, tmp_path):
        with pytest.raises(validate.ArchiveInvalidError) as refused:
            export(tmp_path, BROKEN, WITH_REFERENCES)

        assert refused.value.problems == [
            "ERROR(RSC-007): OEBPS/text/chapter1.xhtml(1): Referenced resource "
            '"OEBPS/text/chapter2.xhtml" could not be found in the EPUB'
        ]
        # As with the structural check: nothing lands, so the book is retried.
        assert not (tmp_path / "out" / "Book.epub").exists()

    def test_a_sound_book_is_written(self, tmp_path):
        assert export(tmp_path, SOUND, WITH_REFERENCES).exists()

    def test_validate_alone_still_checks_only_the_structure(self, tmp_path):
        target = export(tmp_path, BROKEN, checks.ValidationOptions(enabled=True))

        assert target.exists()


class TestTheCheckUnderVerify:
    def test_verify_names_a_book_with_a_broken_link(self, tmp_path):
        target = export(tmp_path, BROKEN, checks.ValidationOptions(enabled=True))

        assert verify_output(target.parent, references=True) == (1, 1, ["Book.epub"])

    def test_verify_without_it_passes_the_same_book(self, tmp_path):
        target = export(tmp_path, BROKEN, checks.ValidationOptions(enabled=True))

        assert verify_output(target.parent) == (1, 0, [])


def _report_with(*findings: Finding):
    return lambda _path: Report(list(findings))


class TestWhatCountsAsAProblem:
    def test_a_warning_does_not_fail_a_book(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            checks,
            "check_file",
            _report_with(
                Finding("HTM-025", "warning", "a.xhtml", 3, "unregistered scheme"),
                Finding("RSC-007", "error", "a.xhtml", 4, "missing file"),
            ),
        )

        assert checks.reference_problems(tmp_path / "Book.epub") == [
            "ERROR(RSC-007): a.xhtml(4): missing file"
        ]

    def test_a_fatal_error_fails_a_book(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            checks,
            "check_file",
            _report_with(Finding("RSC-016", "fatal", "a.xhtml", 9, "not well-formed")),
        )

        assert checks.reference_problems(tmp_path / "Book.epub") == [
            "FATAL(RSC-016): a.xhtml(9): not well-formed"
        ]

    def test_a_finding_with_no_line_names_the_file_alone(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            checks,
            "check_file",
            _report_with(Finding("PKG-008", "error", "big.xhtml", None, "too large")),
        )

        assert checks.reference_problems(tmp_path / "Book.epub") == [
            "ERROR(PKG-008): big.xhtml: too large"
        ]

    def test_at_most_ten_are_named_and_the_rest_counted(self, tmp_path, monkeypatch):
        many = [
            Finding("RSC-012", "error", "a.xhtml", line, "no such id")
            for line in range(1, 26)
        ]
        monkeypatch.setattr(checks, "check_file", _report_with(*many))

        problems = checks.reference_problems(tmp_path / "Book.epub")

        assert len(problems) == 11
        assert problems[-1] == "...and 15 more reference problem(s)"
