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

from epubconvert import contained, inspect_output, source, validate
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
        # with less care. An is_symlink() outside contained.py means somebody
        # has started another implementation -- on the read side (is a member
        # safe to open) or the write side (is this name free to create).
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


class TestOpeningRefusesToFollow:
    """The kernel enforces the rule at open time, not just at check time."""

    def test_opening_a_symlink_is_refused(self, tmp_path):
        # resolve() clears a path and the caller opens it later. A member
        # swapped for a symlink in between was dereferenced -- the check and
        # the open are separate syscalls. O_NOFOLLOW closes that window
        # because the kernel refuses at open, with nothing in between.
        outside = tmp_path / "secret.txt"
        outside.write_text("SECRET", encoding="utf-8")
        package = tmp_path / "Book.epub"
        package.mkdir()
        (package / "member.xhtml").symlink_to(outside)

        with (
            pytest.raises(OSError),
            contained.open_contained(package / "member.xhtml"),
        ):
            pass

    def test_opening_an_ordinary_file_works(self, tmp_path):
        package = tmp_path / "Book.epub"
        package.mkdir()
        (package / "member.xhtml").write_text("<html/>", encoding="utf-8")

        with contained.open_contained(package / "member.xhtml") as handle:
            assert handle.read() == b"<html/>"

    def test_read_capped_refuses_a_swapped_symlink(self, tmp_path):
        # The same window on the document-reading path.
        outside = tmp_path / "outside.xml"
        outside.write_text("<container/>", encoding="utf-8")
        package = make_package(tmp_path / "lib", "Book.epub")
        container = package / "META-INF" / "container.xml"
        container.unlink()
        container.symlink_to(outside)

        with pytest.raises(validate.ValidationError):
            validate.read_package_dir(package)


class TestTheRuleFailsClosed:
    """
    Every branch here refuses a path it could not establish anything about.
    That is the correct direction, and none of it was pinned by a test: a
    refactor that flipped any of them to fail *open* would have passed the
    suite. This module has already shipped one such defect, when ``_is_linked``
    treated ``FileNotFoundError`` as "linked" and reported every unprotected
    book as FairPlay-protected.
    """

    def test_a_name_that_cannot_be_examined_is_treated_as_linked(self, tmp_path):
        # A NUL byte makes lstat raise rather than answer. Fail closed: the
        # rule cannot tell what this is, so it refuses.
        package = tmp_path / "Book.epub"
        package.mkdir()

        assert contained.resolve(package, "chapter\x00.xhtml") is None

    def test_a_path_that_cannot_be_resolved_is_refused(self, tmp_path, monkeypatch):
        package = tmp_path / "Book.epub"
        package.mkdir()
        (package / "content.opf").write_text("<package/>", encoding="utf-8")
        resolved = package.resolve()

        def unresolvable(self, strict=False):
            raise OSError("broken mount")

        monkeypatch.setattr(Path, "resolve", unresolvable)

        assert contained.resolve(package, "content.opf", resolved_root=resolved) is None

    def test_contains_refuses_a_path_that_cannot_be_resolved(
        self, tmp_path, monkeypatch
    ):
        package = tmp_path / "Book.epub"
        package.mkdir()
        member = package / "content.opf"
        member.write_text("<package/>", encoding="utf-8")
        resolved = package.resolve()

        def unresolvable(self, strict=False):
            raise OSError("broken mount")

        monkeypatch.setattr(Path, "resolve", unresolvable)

        assert contained.contains(package, member, resolved_root=resolved) is False


class TestTheCoverIsReadThroughTheRule:
    """
    Cover extraction resolved its source through the rule and then reopened it
    with ``shutil.copyfile``, which follows symlinks. That is the check-then-
    open window ``open_contained``'s O_NOFOLLOW exists to close, reopened by
    the one reader that did not use it.
    """

    def test_no_module_copies_a_file_outside_the_rule(self):
        # Derived rather than remembered: shutil.copyfile opens its source with
        # a plain open(), so a package member must never be read that way. The
        # census is taken from the source, not from a list in a docstring.
        offenders = []
        for path in sorted(Path("epubconvert").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr in {
                    "copyfile",
                    "copy",
                    "copy2",
                }:
                    offenders.append(f"{path}:{node.lineno}")

        assert offenders == [], f"read through open_contained instead: {offenders}"

    def test_a_symlinked_cover_is_not_extracted(self, tmp_path):
        secret = tmp_path / "secret.jpg"
        secret.write_bytes(b"not the cover")
        package = tmp_path / "Book.epub"
        (package / "OEBPS").mkdir(parents=True)
        (package / "OEBPS" / "cover.jpg").symlink_to(secret)
        (package / "META-INF").mkdir()
        (package / "META-INF" / "container.xml").write_text(
            '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/c.opf"/></rootfiles></container>',
            encoding="utf-8",
        )
        (package / "OEBPS" / "c.opf").write_text(
            '<package xmlns="http://www.idpf.org/2007/opf"><metadata/>'
            '<manifest><item id="cover" href="cover.jpg" media-type="image/jpeg"'
            ' properties="cover-image"/></manifest>'
            '<spine><itemref idref="cover"/></spine></package>',
            encoding="utf-8",
        )
        archive_path = tmp_path / "out" / "Book.epub"
        archive_path.parent.mkdir()
        archive_path.write_bytes(b"stand-in")

        assert inspect_output.extract_cover(package, archive_path) is None
        assert not (tmp_path / "out" / "Book.jpg").exists()
