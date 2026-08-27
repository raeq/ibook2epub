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


def _book_with_metadata(path: Path) -> bytes:
    """An already-valid epub that declares a title and a sort name."""
    opf = (
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0"'
        ' unique-identifier="bid">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"'
        ' xmlns:opf="http://www.idpf.org/2007/opf">'
        "<dc:title>A Wizard of Earthsea</dc:title>"
        '<dc:creator opf:file-as="Le Guin, Ursula K.">Ursula K. Le Guin</dc:creator>'
        '<dc:identifier id="bid">urn:uuid:1</dc:identifier></metadata>'
        '<manifest><item id="t" href="text.xhtml"'
        ' media-type="application/xhtml+xml"/></manifest>'
        '<spine><itemref idref="t"/></spine></package>'
    )
    container = (
        '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        '<rootfiles><rootfile full-path="content.opf"/></rootfiles></container>'
    )
    with ZipFile(path, "w") as opened:
        opened.writestr("mimetype", "application/epub+zip")
        opened.writestr("META-INF/container.xml", container)
        opened.writestr("content.opf", opf)
        opened.writestr("text.xhtml", "<html/>")
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


class TestCopiesGoThroughTheNamingPolicy:
    """
    Copying wrote `output_dir / source.name`, so a copied file never met the
    naming layer at all. Under `-p` that put a colon on a shelf bound for a
    Kindle -- the one thing `-p` exists to prevent -- and under
    `--name-by author-title` it left half the shelf named the old way.

    The name a copied file gets is now decided in one place, the same place
    that decides it for a converted book.
    """

    def test_an_illegal_character_is_sanitised_under_portable_names(
        self, tmp_path, output_dir
    ):
        library = tmp_path / "lib"
        library.mkdir()
        _zipped_book(library / "Sapiens: A Brief History.epub")

        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-p", "-q"])

        assert [path.name for path in output_dir.glob("*.epub")] == [
            "Sapiens A Brief History.epub"
        ]

    def test_a_copied_pdf_is_sanitised_too(self, tmp_path, output_dir):
        library = tmp_path / "lib"
        library.mkdir()
        (library / "Notes: Volume 1.pdf").write_bytes(b"%PDF-1.4\n")

        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-p", "-q"])

        assert [path.name for path in output_dir.glob("*.pdf")] == [
            "Notes Volume 1.pdf"
        ]

    def test_a_copied_epub_is_named_from_its_own_metadata(self, tmp_path, output_dir):
        # An already-zipped book carries the same dc:title and dc:creator a
        # package directory does. Ignoring them made the shelf inconsistent.
        library = tmp_path / "lib"
        library.mkdir()
        _book_with_metadata(library / "Earthsea.epub")

        run.main(
            [
                "-s",
                str(library),
                "-o",
                str(output_dir),
                "-m",
                "0",
                "--name-by",
                "author-title",
                "-q",
            ]
        )

        assert [path.name for path in output_dir.glob("*.epub")] == [
            "Le Guin, Ursula K. - A Wizard of Earthsea.epub"
        ]

    def test_a_pdf_keeps_its_name_when_it_has_no_metadata(self, tmp_path, output_dir):
        library = tmp_path / "lib"
        library.mkdir()
        (library / "Some Paper.pdf").write_bytes(b"%PDF-1.4\n")

        run.main(
            [
                "-s",
                str(library),
                "-o",
                str(output_dir),
                "-m",
                "0",
                "--name-by",
                "author-title",
                "-q",
            ]
        )

        assert (output_dir / "Some Paper.pdf").is_file()

    def test_an_unreadable_epub_keeps_its_name(self, tmp_path, output_dir):
        # A file that cannot be parsed still gets copied; it just cannot be
        # renamed from metadata it does not have.
        library = tmp_path / "lib"
        library.mkdir()
        _zipped_book(library / "Opaque.epub")

        run.main(
            [
                "-s",
                str(library),
                "-o",
                str(output_dir),
                "-m",
                "0",
                "--name-by",
                "author-title",
                "-q",
            ]
        )

        assert (output_dir / "Opaque.epub").is_file()

    def test_the_default_policy_still_copies_verbatim(self, tmp_path, output_dir):
        library = tmp_path / "lib"
        library.mkdir()
        original = _zipped_book(library / "Already Valid.epub")

        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])

        assert (output_dir / "Already Valid.epub").read_bytes() == original


class TestRenamedCopiesStayConsistentWithTheShelf:
    """The orphan report and the copy have to agree on the name."""

    def test_a_renamed_copy_is_not_reported_as_an_orphan(
        self, tmp_path, output_dir, capsys
    ):
        # Orphan detection is told which names the copies claim. Computing
        # that separately from the copy itself is what made them disagree.
        library = tmp_path / "lib"
        library.mkdir()
        _book_with_metadata(library / "Earthsea.epub")
        flags = [
            "-s",
            str(library),
            "-o",
            str(output_dir),
            "-m",
            "0",
            "--name-by",
            "author-title",
            "-q",
        ]
        run.main(flags)
        capsys.readouterr()

        run.main(flags + ["--list"])

        assert "orphan" not in capsys.readouterr().out

    def test_a_renamed_copy_reruns_without_copying_again(
        self, tmp_path, output_dir, capsys
    ):
        library = tmp_path / "lib"
        library.mkdir()
        _book_with_metadata(library / "Earthsea.epub")
        flags = [
            "-s",
            str(library),
            "-o",
            str(output_dir),
            "-m",
            "0",
            "--name-by",
            "author-title",
            "-q",
        ]
        run.main(flags)
        written = output_dir / "Le Guin, Ursula K. - A Wizard of Earthsea.epub"
        stamp = written.stat().st_mtime_ns
        capsys.readouterr()

        run.main(flags)

        assert written.stat().st_mtime_ns == stamp
        assert "copied" not in capsys.readouterr().out


class TestAnInterruptedCopyStillCounts:
    """
    ``convert``'s docstring promises a Ctrl-C cannot leave the summary
    disagreeing with the directory. Copy-through returned a total that was
    assigned only once the whole loop finished, so an interrupt part-way
    through reported nothing copied while the files were already on disk --
    written through the same atomic replace as everything else.
    """

    def test_files_copied_before_an_interrupt_are_reported(
        self, tmp_path, output_dir, monkeypatch, capsys
    ):
        library = tmp_path / "lib"
        library.mkdir()
        for index in range(4):
            _zipped_book(library / f"Book {index}.epub")

        real = archive.copy_through
        done = {"n": 0}

        def stop_after_two(source, target):
            if done["n"] >= 2:
                raise KeyboardInterrupt
            done["n"] += 1
            return real(source, target)

        monkeypatch.setattr(run, "copy_through", stop_after_two)

        code = run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])

        assert code == 130
        assert len(list(output_dir.glob("*.epub"))) == 2
        assert "2 copied" in capsys.readouterr().out
