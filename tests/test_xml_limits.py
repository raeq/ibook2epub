"""
Tests for what parsing a book's own XML may cost.

Every document read from a book -- ``container.xml``, the package document,
``encryption.xml`` -- goes through one parse, and the size cap on the stored
bytes bounds none of what a document can make the parser build from them.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them.
# pylint: disable=missing-function-docstring,missing-class-docstring

import tracemalloc
from pathlib import Path
from xml.etree import ElementTree
from zipfile import ZipFile

import pytest

from epubconvert.collect import package as package_reader
from epubconvert.collect import source
from epubconvert.run import run

CONTAINER = (
    '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
    '<rootfiles><rootfile full-path="c.opf"/></rootfiles></container>'
)

#: What refusing a hostile document may allocate at most.
PEAK = 4 * 1024 * 1024


def _opf(doctype: str, items: str = '<item id="t" href="t.xhtml"/>') -> str:
    return (
        f'<?xml version="1.0"?>\n{doctype}\n'
        '<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="i">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        '<dc:identifier id="i">x</dc:identifier><dc:title>Real Book</dc:title>'
        f'</metadata><manifest>{items}</manifest><spine><itemref idref="t"/></spine>'
        "</package>"
    )


def _package(parent: Path, opf: str, *, container: str = CONTAINER) -> Path:
    package = parent / "Book.epub"
    (package / "META-INF").mkdir(parents=True)
    (package / "META-INF" / "container.xml").write_text(container, encoding="utf-8")
    (package / "c.opf").write_text(opf, encoding="utf-8")
    (package / "t.xhtml").write_text("<html/>", encoding="utf-8")
    return package


def _archive(path: Path, opf: str) -> Path:
    with ZipFile(path, "w") as opened:
        opened.writestr("mimetype", "application/epub+zip")
        opened.writestr("META-INF/container.xml", CONTAINER)
        opened.writestr("c.opf", opf)
        opened.writestr("t.xhtml", "<html/>")
    return path


#: Every ``<item/>`` is given a copy of an attribute default: a 1.6 KB book
#: declaring a 1 MB one reached 2.9 GB, past the entity check.
ATTLIST = '<!DOCTYPE package [<!ATTLIST item x CDATA "AAAA">]>'


class TestAnInternalSubsetIsRefused:
    """
    The entity check stopped at an entity declaration, and an internal subset
    can declare more than entities. ``<!ATTLIST item x CDATA "...">`` gives
    every ``<item/>`` its own copy of the default, so a few kilobytes stored
    became gigabytes parsed, and --verify and --list died of a MemoryError.
    A package document, a container and an encryption declaration have no use
    for an internal subset, so any at all is refused.
    """

    def test_an_attribute_default_is_refused(self, tmp_path):
        opf = _opf(ATTLIST, '<item id="t" href="t.xhtml"/>' + "<item/>" * 10)

        with pytest.raises(package_reader.ValidationError, match="internal subset"):
            package_reader.read_package_dir(_package(tmp_path, opf))

    def test_the_default_is_never_copied(self):
        default = "A" * (256 * 1024)
        document = (
            f'<!DOCTYPE e [<!ATTLIST a x CDATA "{default}">]><e>' + "<a/>" * 64 + "</e>"
        ).encode("ascii")

        tracemalloc.start()
        try:
            with pytest.raises(ElementTree.ParseError, match="internal subset"):
                package_reader.parse_xml(document)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

        assert peak < PEAK

    @pytest.mark.parametrize(
        "declaration",
        [
            "<!ELEMENT package ANY>",
            '<!NOTATION n SYSTEM "n">',
            "<!-- nothing but a comment -->",
            "",
        ],
    )
    def test_any_internal_subset_is_refused(self, declaration):
        document = f"<!DOCTYPE package [{declaration}]><package/>".encode()

        with pytest.raises(ElementTree.ParseError, match="internal subset"):
            package_reader.parse_xml(document)

    def test_an_archive_is_refused_the_same_way(self, tmp_path):
        path = _archive(tmp_path / "Book.epub", _opf(ATTLIST))

        with pytest.raises(package_reader.ValidationError, match="internal subset"):
            package_reader.read_archive_package(path)

    def test_the_container_is_refused_the_same_way(self, tmp_path):
        container = '<!DOCTYPE container [<!ATTLIST rootfile x CDATA "y">]>' + CONTAINER
        package = _package(tmp_path, _opf(""), container=container)

        with pytest.raises(package_reader.ValidationError, match="internal subset"):
            package_reader.read_package_dir(package)

    def test_an_encryption_declaration_fails_closed(self, tmp_path):
        package = _package(tmp_path, _opf(""))
        (package / "META-INF" / "encryption.xml").write_text(
            '<!DOCTYPE e [<!ATTLIST a x CDATA "A">]><e>' + "<a/>" * 10 + "</e>",
            encoding="utf-8",
        )

        protected, reason = source.has_drm(package)

        assert protected is True
        assert "internal subset" in (reason or "")

    def test_verify_reports_the_book_rather_than_passing_it(
        self, tmp_path, output_dir, capsys
    ):
        _archive(output_dir / "Bomb.epub", _opf(ATTLIST))
        library = tmp_path / "lib"
        library.mkdir()

        code = run.main(["-s", str(library), "-o", str(output_dir), "--verify", "-q"])

        assert code != 0
        assert "internal subset" in capsys.readouterr().err


class TestADoctypeWithoutASubsetStillParses:
    """
    One real book in a 2,804-package library carries a bare DOCTYPE, and EPUB
    2 tooling wrote the OEB package's public identifier. Neither declares
    anything, and expat reads no external subset, so both are left alone.
    """

    @pytest.mark.parametrize(
        "doctype",
        [
            "<!DOCTYPE package>",
            '<!DOCTYPE package PUBLIC "+//ISBN 0-9673008-1-9//DTD OEB 1.2 Package//EN"'
            ' "http://openebook.org/dtds/oeb-1.2/oebpkg12.dtd">',
            '<!DOCTYPE package SYSTEM "package.dtd">',
        ],
    )
    def test_a_package_document(self, tmp_path, doctype):
        package = package_reader.read_package_dir(_package(tmp_path, _opf(doctype)))

        assert package.title == "Real Book"
        assert package.manifest == {"t": "t.xhtml"}

    def test_an_html_doctype(self):
        document = b'<?xml version="1.0"?><!DOCTYPE html><html/>'

        assert package_reader.parse_xml(document).tag == "html"
