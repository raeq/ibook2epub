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

import io
from pathlib import Path
from zipfile import ZipFile

import pytest

from epubconvert import cli, inspect_output, planning, run
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


class TestForceRewritesTheArchiveItFound:
    """--force must overwrite the stale file, not sit beside it."""

    def test_force_does_not_leave_a_second_copy(self, tmp_path, output_dir):
        # Regression (1.3): under a policy whose identity is looser than its
        # filename, --force targeted a freshly computed name, so the old
        # archive stayed put and kept satisfying the identity check for ever.
        # The --refresh branch already documented this hazard and wrote to
        # `found`; the force path never got the fix.
        library = tmp_path / "lib"
        make_package(library, "Café.epub")
        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])

        run.main(
            [
                "-s",
                str(library),
                "-o",
                str(output_dir),
                "-m",
                "0",
                "-p",
                "romanize",
                "--force",
                "-q",
            ]
        )

        assert len(list(output_dir.glob("*.epub"))) == 1


class TestOutputDirectoriesAreNotMistakenForBooks:
    """Only files count as completed work."""

    def test_a_directory_named_like_a_book_is_not_completed_work(
        self, tmp_path, output_dir, capsys
    ):
        # Regression (1.7): `existing` was built with no is_file() filter, so
        # an unpacked epub sitting in the output directory registered as done.
        # The book was reported skipped-as-exported and never written. It
        # cannot be written while a directory holds the name, so the honest
        # outcome is a reported failure with the book still counted as
        # remaining -- not a silent success.
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        (output_dir / "Book.epub").mkdir()

        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])

        out = capsys.readouterr().out
        assert "skipped" not in out
        assert "failed 1" in out
        assert "1 remaining" in out


class TestSuffixesRespectTheExtension:
    """_suffixed must use the module's own splitter."""

    def test_a_bare_extension_gets_the_suffix_before_it(self):
        # Regression (1.9): Path().suffix treats ".epub" as extension-less, so
        # the marker landed after it -- ".epub (2)", which no *.epub glob
        # matches. _split_extension exists for exactly this.
        assert planning.suffixed(".epub", 2, 0).endswith(".epub")


class TestVerifySurvivesAnUnreadableArchive:
    """One bad file must not end the sweep."""

    def test_an_unsupported_compression_method_is_reported_not_raised(self, output_dir):
        # Regression (2.3): validate_archive caught only BadZipFile and
        # OSError. zipfile also raises NotImplementedError for an unsupported
        # method, so --verify -- the one command whose job is finding damage --
        # died and checked nothing further.
        good = _minimal_archive(output_dir / "Good.epub")
        assert good.exists()
        bad = output_dir / "Bad.epub"
        bad.write_bytes(_archive_with_bad_method())

        checked, damaged = inspect_output.verify_output(output_dir)

        assert checked == 2
        assert damaged == 1


class TestVerifyRefusesAMissingDirectory:
    """A typo in -o must not read as a clean bill of health."""

    def test_a_nonexistent_output_directory_is_an_error(self, tmp_path, capsys):
        # Regression (R7): the glob found nothing and --verify reported
        # "0 damaged", exit 0, having checked nothing.
        source = tmp_path / "lib"
        source.mkdir()

        code = run.main(
            ["-s", str(source), "-o", str(tmp_path / "typo"), "--verify", "-q"]
        )

        assert code != 0
        assert "0 damaged" not in capsys.readouterr().out


class TestContradictoryFlagsAreRejected:
    """Silently ignoring a flag the user passed is not acceptable."""

    def test_json_without_list_is_refused(self, tmp_path):
        source = tmp_path / "lib"
        source.mkdir()

        with pytest.raises(SystemExit):
            cli.parse_args(["-s", str(source), "--json"])


def _minimal_archive(path: Path) -> Path:
    """Write the smallest archive validate_archive calls sound."""
    with ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr(
            "META-INF/container.xml",
            '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="content.opf"/></rootfiles></container>',
        )
        archive.writestr(
            "content.opf",
            '<package xmlns="http://www.idpf.org/2007/opf">'
            '<manifest><item id="t" href="t.xhtml"/></manifest>'
            '<spine><itemref idref="t"/></spine></package>',
        )
        archive.writestr("t.xhtml", "<html/>")
    return path


def _archive_with_bad_method() -> bytes:
    """An archive whose mimetype member declares an unsupported method."""
    buffer = io.BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
    raw = bytearray(buffer.getvalue())
    # Compression method lives at offset 8 of the local header and 10 of the
    # central directory entry; 99 is not a method zipfile implements.
    for marker in (b"PK\x03\x04", b"PK\x01\x02"):
        at = raw.find(marker)
        offset = at + 8 if marker == b"PK\x03\x04" else at + 10
        raw[offset : offset + 2] = (99).to_bytes(2, "little")
    return bytes(raw)
