"""
Tests for the one rule about turning a name from a book into a path on disk.

A ``*.epub/`` package is input. Its container document, its package document,
its manifest hrefs and its own directory entries are all names somebody else
chose, and every one of them ends up joined onto a real directory. This module
exists because that rule was implemented three times, in three places, with
three different amounts of care, and the two implementations that were not the
guard both let a book read outside itself.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them. Explicit empty-list comparisons read better than truthiness here.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=use-implicit-booleaness-not-comparison,too-few-public-methods

import ast
from pathlib import Path

import pytest

from epubconvert import contained, source, validate
from tests.conftest import make_package

OUTSIDE_OPF = """<package xmlns="http://www.idpf.org/2007/opf"
 xmlns:dc="http://purl.org/dc/elements/1.1/">
<metadata><dc:title>OUTSIDE</dc:title></metadata>
<manifest><item id="t" href="t.xhtml"/></manifest>
<spine><itemref idref="t"/></spine></package>
"""


class TestTheRuleItself:
    """One function decides, and it answers the same way for every caller."""

    def test_a_plain_relative_path_inside_the_package_is_allowed(self, tmp_path):
        root = tmp_path / "Book.epub"
        (root / "OEBPS").mkdir(parents=True)
        (root / "OEBPS" / "text.xhtml").write_text("<html/>", encoding="utf-8")

        assert contained.resolve(root, "OEBPS/text.xhtml") is not None

    @pytest.mark.parametrize(
        "href",
        ["../outside.txt", "../../outside.txt", "/etc/hosts", "..", "OEBPS/../../x"],
    )
    def test_a_path_leaving_the_package_is_refused(self, tmp_path, href):
        root = tmp_path / "Book.epub"
        root.mkdir(parents=True)

        assert contained.resolve(root, href) is None

    def test_a_symlink_pointing_out_is_refused(self, tmp_path):
        outside = tmp_path / "secret.txt"
        outside.write_text("SECRET", encoding="utf-8")
        root = tmp_path / "Book.epub"
        root.mkdir()
        (root / "link.txt").symlink_to(outside)

        assert contained.resolve(root, "link.txt") is None

    def test_a_symlink_pointing_within_is_still_refused(self, tmp_path):
        # A package Apple wrote contains no symlinks at all, so the safe rule
        # is the simple one: a member is a real file or it is not a member.
        root = tmp_path / "Book.epub"
        (root / "OEBPS").mkdir(parents=True)
        real = root / "OEBPS" / "real.xhtml"
        real.write_text("<html/>", encoding="utf-8")
        (root / "OEBPS" / "alias.xhtml").symlink_to(real)

        assert contained.resolve(root, "OEBPS/alias.xhtml") is None

    def test_a_directory_reached_through_a_symlink_is_refused(self, tmp_path):
        elsewhere = tmp_path / "elsewhere"
        (elsewhere / "deep").mkdir(parents=True)
        (elsewhere / "deep" / "file.txt").write_text("x", encoding="utf-8")
        root = tmp_path / "Book.epub"
        root.mkdir()
        (root / "OEBPS").symlink_to(elsewhere)

        assert contained.resolve(root, "OEBPS/deep/file.txt") is None


class TestEveryReaderUsesIt:
    """The rule is only worth having if nothing bypasses it."""

    def test_the_package_document_cannot_be_a_symlink(self, tmp_path):
        # A live hole after S1/S2 were fixed at their call sites: the OPF was
        # read straight off disk, so a book could point its own package
        # document at any file the user could read.
        package = make_package(tmp_path / "lib", "Book.epub")
        (tmp_path / "outside.opf").write_text(OUTSIDE_OPF, encoding="utf-8")
        opf = package / "OEBPS" / "content.opf"
        opf.unlink()
        opf.symlink_to(tmp_path / "outside.opf")
        (package / "META-INF" / "container.xml").write_text(
            '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles>'
            "</container>",
            encoding="utf-8",
        )

        with pytest.raises(validate.ValidationError):
            validate.read_package_dir(package)

    def test_the_container_cannot_be_a_symlink(self, tmp_path):
        package = make_package(tmp_path / "lib", "Book.epub")
        (tmp_path / "outside.xml").write_text("<container/>", encoding="utf-8")
        container = package / "META-INF" / "container.xml"
        container.unlink()
        container.symlink_to(tmp_path / "outside.xml")

        with pytest.raises(validate.ValidationError):
            validate.read_package_dir(package)

    def test_the_encryption_declaration_cannot_be_a_symlink(self, tmp_path):
        # has_drm decides whether a book is exportable at all, so a book that
        # can choose which file answers that question chooses its own fate.
        package = make_package(tmp_path / "lib", "Book.epub")
        (tmp_path / "outside.xml").write_text("<encryption/>", encoding="utf-8")
        (package / "META-INF" / "encryption.xml").symlink_to(tmp_path / "outside.xml")

        protected, reason = source.has_drm(package)

        assert protected
        assert reason


class TestNoSecondImplementation:
    """Structural: the rule must be stated once, not re-derived per caller."""

    def test_only_the_containment_module_tests_for_symlinks(self):
        # The concern this whole module answers: the guard existed and the
        # archive writer did not call it, so the same rule was written again
        # with less care. A second is_symlink() outside contained.py means
        # somebody has started a third implementation.
        offenders = []
        for path in sorted(Path("epubconvert").glob("*.py")):
            if path.name == "contained.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr == "is_symlink":
                    offenders.append(f"{path}:{node.lineno}")

        assert offenders == []

    def test_every_package_reader_imports_the_rule(self):
        readers = ["archive.py", "validate.py", "source.py", "inspect_output.py"]
        for name in readers:
            text = (Path("epubconvert") / name).read_text(encoding="utf-8")
            assert "from .contained import" in text, name
