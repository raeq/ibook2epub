"""
Tests for files that need no conversion and are taken along anyway.

A real library holds both forms: Apple's ``*.epub/`` package directories, and
books that arrived already zipped or as PDFs. Converting only the first and
saying nothing about the rest meant "export my library" produced a partial
shelf whose summary read complete.

These are copied, not converted. The bytes are not touched, so an already-valid
epub stays byte-identical and a PDF stays a PDF.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them. Explicit empty-list comparisons read better than truthiness here.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=use-implicit-booleaness-not-comparison,too-few-public-methods

from pathlib import Path
from zipfile import ZipFile

from epubconvert import archive, run
from tests.conftest import make_package


def _zipped_book(path: Path) -> bytes:
    """Write a real, already-valid epub file and return its bytes."""
    with ZipFile(path, "w") as opened:
        opened.writestr("mimetype", "application/epub+zip")
        opened.writestr("META-INF/container.xml", "<container/>")
        opened.writestr("OEBPS/text.xhtml", "<html/>")
    written: bytes = path.read_bytes()
    return written


class TestAlreadyValidFilesAreTakenAlong:
    """The point of the run is "get my library out", not "run a converter"."""

    def test_a_zipped_epub_is_copied(self, tmp_path, output_dir):
        library = tmp_path / "lib"
        library.mkdir()
        original = _zipped_book(library / "Already Valid.epub")

        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])

        assert (output_dir / "Already Valid.epub").read_bytes() == original

    def test_a_pdf_is_copied(self, tmp_path, output_dir):
        library = tmp_path / "lib"
        library.mkdir()
        (library / "Some Paper.pdf").write_bytes(b"%PDF-1.4\nnot really\n")

        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])

        assert (output_dir / "Some Paper.pdf").read_bytes() == b"%PDF-1.4\nnot really\n"

    def test_copies_are_counted_in_the_summary(self, tmp_path, output_dir, capsys):
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        _zipped_book(library / "Already Valid.epub")
        (library / "Paper.pdf").write_bytes(b"%PDF-1.4\n")

        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])

        assert "2 copied" in capsys.readouterr().out

    def test_unrelated_files_are_still_only_counted(self, tmp_path, output_dir, capsys):
        # Copy-through is for books, not for everything in the directory.
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        (library / "notes.txt").write_text("mine", encoding="utf-8")

        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])

        assert not (output_dir / "notes.txt").exists()
        assert "1 ignored" in capsys.readouterr().out


class TestCopiesRerunSafely:
    """The same invariant the converter holds."""

    def test_a_rerun_does_not_copy_again(self, tmp_path, output_dir, capsys):
        library = tmp_path / "lib"
        library.mkdir()
        _zipped_book(library / "Already Valid.epub")
        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])
        stamp = (output_dir / "Already Valid.epub").stat().st_mtime_ns
        capsys.readouterr()

        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])

        assert (output_dir / "Already Valid.epub").stat().st_mtime_ns == stamp
        assert "copied" not in capsys.readouterr().out

    def test_a_copied_file_is_not_an_orphan(self, tmp_path, output_dir, capsys):
        # It has no source *package*, so naive orphan detection would report
        # the file it just wrote as abandoned.
        library = tmp_path / "lib"
        library.mkdir()
        _zipped_book(library / "Already Valid.epub")
        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])
        capsys.readouterr()

        run.main(["-s", str(library), "-o", str(output_dir), "--list", "-q"])

        assert "orphan" not in capsys.readouterr().out

    def test_a_dry_run_copies_nothing(self, tmp_path, output_dir):
        library = tmp_path / "lib"
        library.mkdir()
        _zipped_book(library / "Already Valid.epub")

        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-d", "-q"])

        assert list(output_dir.iterdir()) == []


class TestCopyThroughCanBeTurnedOff:
    """Some people want a converter and nothing else."""

    def test_the_flag_disables_it(self, tmp_path, output_dir, capsys):
        library = tmp_path / "lib"
        library.mkdir()
        _zipped_book(library / "Already Valid.epub")
        (library / "Paper.pdf").write_bytes(b"%PDF-1.4\n")

        run.main(
            [
                "-s",
                str(library),
                "-o",
                str(output_dir),
                "-m",
                "0",
                "--no-copy-through",
                "-q",
            ]
        )

        assert list(output_dir.glob("*.epub")) == []
        assert list(output_dir.glob("*.pdf")) == []
        assert "2 ignored" in capsys.readouterr().out


class TestOnlyRealFilesAreCopied:
    """The same trust rules as everything else that reads a path."""

    def test_a_symlinked_book_is_not_copied(self, tmp_path, output_dir):
        outside = tmp_path / "secret.epub"
        _zipped_book(outside)
        library = tmp_path / "lib"
        library.mkdir()
        (library / "Linked.epub").symlink_to(outside)

        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])

        assert not (output_dir / "Linked.epub").exists()

    def test_the_copyable_scan_finds_both_kinds(self, tmp_path):
        library = tmp_path / "lib"
        make_package(library, "Package.epub")
        _zipped_book(library / "Zipped.epub")
        (library / "Paper.pdf").write_bytes(b"%PDF-1.4\n")
        (library / "notes.txt").write_text("no", encoding="utf-8")

        found = archive.collect_copyable(library)

        assert sorted(path.name for path in found) == ["Paper.pdf", "Zipped.epub"]
