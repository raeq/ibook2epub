"""
Tests for the guarantees that keep the output directory trustworthy.

The output directory is this tool's only record of completed work, so a book
that is written wrong is not merely wrong: it is recorded as finished and no
rerun retries it. Every test here covers a way that record was found to lie.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them. Explicit empty-list comparisons read better than truthiness here.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=use-implicit-booleaness-not-comparison,too-few-public-methods

from pathlib import Path
from zipfile import ZipFile

import pytest

from epubconvert import run
from epubconvert.archive import (
    PARTIAL_PREFIX,
    PARTIAL_SUFFIX,
    zip_package,
)
from epubconvert.naming import PortableNaming, StripNaming
from epubconvert.validate import ArchiveInvalidError
from tests.conftest import make_package

disarm = pytest.importorskip("disarm", reason="portable naming needs the disarm extra")


def members(archive: Path) -> list[str]:
    """Member names inside an exported archive."""
    with ZipFile(archive) as opened:
        return opened.namelist()


class TestPartialSweep:
    """The sweep must remove only temporaries this tool created."""

    def test_unrelated_part_files_survive(self, tmp_path, output_dir):
        # Regression: the sweep globbed *.part, which matches a browser's
        # in-progress download or a user's own file. The default output
        # directory is ~/Books, so this deleted real user data.
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        download = output_dir / "Some Download.epub.part"
        notes = output_dir / "notes.part"
        download.write_bytes(b"half a download")
        notes.write_bytes(b"my notes")

        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])

        assert download.exists()
        assert notes.exists()

    def test_our_own_temporaries_are_still_removed(self, tmp_path, output_dir):
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        stale = output_dir / f"{PARTIAL_PREFIX}abcd1234{PARTIAL_SUFFIX}"
        stale.write_bytes(b"half an archive")

        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])

        assert not stale.exists()


class TestSymlinksAreNotFollowed:
    """A package is a directory this tool owns, never a redirection."""

    def test_a_symlinked_package_is_refused(self, tmp_path, output_dir):
        # Regression: a symlink named *.epub was accepted as a package and the
        # whole target directory was zipped into the shelf, exfiltrating it.
        private = tmp_path / "private"
        private.mkdir()
        (private / "id_rsa").write_text("PRIVATE-KEY-MATERIAL", encoding="utf-8")
        library = tmp_path / "lib"
        library.mkdir()
        (library / "Harmless.epub").symlink_to(private)

        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])

        written = list(output_dir.glob("*.epub"))
        assert written == []

    def test_a_symlinked_member_is_not_dereferenced(self, tmp_path, output_dir):
        # Regression: symlinked files inside a package were followed and their
        # targets embedded in the archive under innocuous names.
        secret = tmp_path / "secret.txt"
        secret.write_text("TOP-SECRET", encoding="utf-8")
        package = make_package(tmp_path / "lib", "Book.epub")
        (package / "OEBPS" / "stolen.txt").symlink_to(secret)
        (package / "OEBPS" / "relative.txt").symlink_to("../../../secret.txt")

        target = output_dir / "Book.epub"
        zip_package(package, target)

        assert "OEBPS/stolen.txt" not in members(target)
        assert "OEBPS/relative.txt" not in members(target)


class TestAnArchiveIsNeverSilentlyIncomplete:
    """Nothing may be recorded as exported unless it is really a book."""

    def test_an_unreadable_directory_fails_the_export(self, tmp_path, output_dir):
        # Regression: rglob swallows PermissionError, so the archive was
        # written without that content, reported as a success, and recorded as
        # done -- so fixing the permissions and rerunning skipped it.
        package = make_package(tmp_path / "lib", "Locked.epub")
        locked = package / "OEBPS" / "locked"
        locked.mkdir()
        (locked / "chapter2.xhtml").write_text("<html/>", encoding="utf-8")
        locked.chmod(0o000)
        try:
            with pytest.raises(OSError):
                zip_package(package, output_dir / "Locked.epub")
        finally:
            locked.chmod(0o755)

        assert list(output_dir.glob("*.epub")) == []

    def test_an_empty_package_is_refused(self, tmp_path, output_dir):
        # Regression: an empty or vanished package produced a mimetype-only
        # archive, reported exported and recorded as finished work.
        empty = tmp_path / "lib" / "Empty.epub"
        empty.mkdir(parents=True)

        with pytest.raises(ArchiveInvalidError, match="no files"):
            zip_package(empty, output_dir / "Empty.epub")

        assert list(output_dir.glob("*.epub")) == []

    def test_a_package_without_a_container_is_refused(self, tmp_path, output_dir):
        package = tmp_path / "lib" / "NoContainer.epub"
        (package / "OEBPS").mkdir(parents=True)
        (package / "OEBPS" / "text.xhtml").write_text("<html/>", encoding="utf-8")

        with pytest.raises(ArchiveInvalidError, match="container.xml"):
            zip_package(package, output_dir / "NoContainer.epub")


class TestCaseOnlyNamesCollide:
    """On a case-insensitive volume two such names are one file."""

    def test_two_books_differing_only_in_case_do_not_both_claim_a_name(
        self, tmp_path, output_dir, capsys
    ):
        # Regression: identities differed so no collision was detected, but the
        # filesystem treated both as one file. The run claimed two exports and
        # wrote one, and reruns never converged.
        library = tmp_path / "lib"
        make_package(library / "a", "Book.epub")
        make_package(library / "b", "BOOK.epub")

        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])

        out = capsys.readouterr().out
        written = list(output_dir.glob("*.epub"))
        assert len(written) == 1
        assert "Exported 1" in out
        assert "collision" in out


class TestRomanizeKeepsTheExtension:
    """The strip-mode fix must hold for the romanizing policy too."""

    @pytest.mark.parametrize("title", ["?", "*", ":", '"', "<>"])
    def test_an_all_illegal_title_keeps_its_extension(self, title):
        # Regression: the strip policy was fixed to split the extension off
        # before cleaning; the romanize policy was left sanitizing the whole
        # name, so '?.epub' became 'epub' and rerun detection broke for ever.
        assert PortableNaming().filename(f"{title}.epub").endswith(".epub")

    @pytest.mark.parametrize("title", ["?", "*", ":", '"', "<>"])
    def test_both_policies_agree(self, title):
        assert StripNaming().filename(f"{title}.epub").endswith(".epub")

    def test_rerun_recognises_a_romanized_all_illegal_title(self, tmp_path, output_dir):
        library = tmp_path / "lib"
        make_package(library, "?.epub")
        argv = ["-s", str(library), "-o", str(output_dir), "-m", "0", "-p", "romanize"]
        run.main([*argv, "-q"])
        first = sorted(p.name for p in output_dir.glob("*.epub"))

        run.main([*argv, "-q"])

        assert len(first) == 1
        assert first == sorted(p.name for p in output_dir.glob("*.epub"))
