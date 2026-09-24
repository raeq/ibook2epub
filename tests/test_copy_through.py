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

import errno
import threading
import time
from pathlib import Path
from zipfile import ZipFile

from epubconvert.collect import source as source_module
from epubconvert.export import archive, naming
from epubconvert.run import convert, copying, planning, run
from tests.conftest import corrupt_member, damaged_streams, make_package, recompress


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

        def stop_at_the_third(source, target):
            # Keyed on the name rather than a running count: the copies run
            # concurrently, and a shared counter would race.
            if source.name in {"Book 2.epub", "Book 3.epub"}:
                raise KeyboardInterrupt
            return real(source, target)

        monkeypatch.setattr(copying, "copy_through", stop_at_the_third)

        code = run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])

        assert code == 130
        assert len(list(output_dir.glob("*.epub"))) == 2
        assert "2 copied" in capsys.readouterr().out


class TestAFailedCopyIsCounted:
    """A book that did not reach the shelf is a failure, whichever way it went."""

    def test_a_copy_that_fails_fails_the_run(
        self, tmp_path, output_dir, monkeypatch, capsys
    ):
        # The error was logged and counted nowhere: the run exited 0 with a
        # clean summary and the PDF missing from the shelf, and a scheduled
        # run had no way to know.
        library = tmp_path / "lib"
        library.mkdir()
        (library / "Manual.pdf").write_bytes(b"%PDF-1.4\n")

        def full(_source, _target):
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr(copying, "copy_through", full)

        code = run.main(["-s", str(library), "-o", str(output_dir), "-m", "0"])

        assert code == 1
        assert "failed 1" in capsys.readouterr().out.strip().splitlines()[-1]

    def test_a_failed_copy_is_not_blamed_on_a_held_back_book(
        self, tmp_path, output_dir, monkeypatch, capsys
    ):
        # The advice after "remaining" is about books still to convert. A
        # failed copy is not one of them, so it must not turn a book the cap
        # held back into one that "failed: see the errors above".
        library = tmp_path / "lib"
        for index in range(3):
            make_package(library, f"Book {index}.epub")
        (library / "Manual.pdf").write_bytes(b"%PDF-1.4\n")

        def full(_source, _target):
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr(copying, "copy_through", full)

        run.main(["-s", str(library), "-o", str(output_dir), "-m", "1"])

        summary = capsys.readouterr().out.strip().splitlines()[-1]
        assert "2 remaining. 2 held back by --max-export-files" in summary
        assert "failed: see the errors above" not in summary


class TestCopiesRunConcurrently:
    """
    Copying a file iCloud has evicted is a download. A loop did them one at a
    time before the conversion pool existed, so every worker ``-w`` asked for
    sat idle through the slowest part of a first run (#10).
    """

    def test_copies_overlap(self, tmp_path, output_dir, monkeypatch):
        # Two copies that each wait for the other finish only if they run at
        # the same time. One after the other, the first times out waiting.
        library = tmp_path / "lib"
        library.mkdir()
        for name in ("One.pdf", "Two.pdf"):
            (library / name).write_bytes(b"%PDF-1.4\n")
        real = archive.copy_through
        both_started = threading.Barrier(2, timeout=10)

        def meet_then_copy(source, target):
            both_started.wait()
            return real(source, target)

        monkeypatch.setattr(copying, "copy_through", meet_then_copy)

        code = run.main(
            ["-s", str(library), "-o", str(output_dir), "-m", "0", "-w", "2", "-q"]
        )

        assert code == 0
        assert sorted(path.name for path in output_dir.glob("*.pdf")) == [
            "One.pdf",
            "Two.pdf",
        ]

    def test_workers_sets_how_many_copy_at_once(
        self, tmp_path, output_dir, monkeypatch
    ):
        # The overlap above would pass on the default pool too; this is what
        # shows -w reaches the copies at all.
        library = tmp_path / "lib"
        library.mkdir()
        for index in range(4):
            (library / f"Paper {index}.pdf").write_bytes(b"%PDF-1.4\n")
        real = archive.copy_through
        guard = threading.Lock()
        running = {"now": 0, "peak": 0}

        def counted(source, target):
            with guard:
                running["now"] += 1
                running["peak"] = max(running["peak"], running["now"])
            try:
                time.sleep(0.05)
                return real(source, target)
            finally:
                with guard:
                    running["now"] -= 1

        monkeypatch.setattr(copying, "copy_through", counted)

        code = run.main(
            ["-s", str(library), "-o", str(output_dir), "-m", "0", "-w", "1", "-q"]
        )

        assert code == 0
        assert running["peak"] == 1
        assert len(list(output_dir.glob("*.pdf"))) == 4


class TestSameNamedCopiesStayDeterministic:
    """
    Two sources can map to one name: the same filename in two folders. The
    loop copied the first in sorted order and skipped the second, finding its
    target already there. Raced, the slower download lands last and replaces
    the first, and both are counted.
    """

    def test_the_first_in_sorted_order_still_wins(
        self, tmp_path, output_dir, monkeypatch, capsys
    ):
        library = tmp_path / "lib"
        (library / "a").mkdir(parents=True)
        (library / "b").mkdir()
        (library / "a" / "Paper.pdf").write_bytes(b"%PDF-1.4\nfirst\n")
        (library / "b" / "Paper.pdf").write_bytes(b"%PDF-1.4\nsecond\n")
        real = archive.copy_through

        def slow_first(source, target):
            # The first file is the slow download, so a race lets the second
            # land before it.
            if source.parent.name == "a":
                time.sleep(0.2)
            return real(source, target)

        monkeypatch.setattr(copying, "copy_through", slow_first)

        code = run.main(
            ["-s", str(library), "-o", str(output_dir), "-m", "0", "-w", "2", "-q"]
        )

        assert code == 0
        assert (output_dir / "Paper.pdf").read_bytes() == b"%PDF-1.4\nfirst\n"
        assert "1 copied" in capsys.readouterr().out


class _Evicted:
    """A stat result for a file whose contents live only in iCloud."""

    def __init__(self, real) -> None:
        self._real = real
        self.st_flags = source_module.SF_DATALESS

    def __getattr__(self, name):
        return getattr(self._real, name)


def _evict(monkeypatch, *paths: Path) -> None:
    """
    Make *paths* stat the way macOS reports a file iCloud has evicted.

    Patched on ``Path.stat`` rather than ``os.stat``: pathlib on 3.10 holds its
    own reference to ``os.stat``, so patching the module would miss it there.
    Every other path stats as itself.
    """
    evicted = {str(path) for path in paths}
    real_stat = Path.stat

    def stat(self, *args, **kwargs):
        result = real_stat(self, *args, **kwargs)
        return _Evicted(result) if str(self) in evicted else result

    monkeypatch.setattr(Path, "stat", stat)
    monkeypatch.setattr(source_module, "dataless_detection_available", lambda: True)


class TestTellingAnEvictedFile:
    def test_an_evicted_file_is_dataless(self, tmp_path, monkeypatch):
        path = tmp_path / "Paper.pdf"
        path.write_bytes(b"%PDF-1.4\n")
        _evict(monkeypatch, path)

        assert source_module.is_dataless(path) is True

    def test_a_local_file_is_not(self, tmp_path, monkeypatch):
        path = tmp_path / "Paper.pdf"
        path.write_bytes(b"%PDF-1.4\n")
        _evict(monkeypatch)

        assert source_module.is_dataless(path) is False

    def test_a_platform_without_st_flags_cannot_say(self, tmp_path, monkeypatch):
        path = tmp_path / "Paper.pdf"
        path.write_bytes(b"%PDF-1.4\n")
        _evict(monkeypatch, path)
        monkeypatch.setattr(
            source_module, "dataless_detection_available", lambda: False
        )

        assert source_module.is_dataless(path) is False

    def test_a_file_that_cannot_be_stated_is_left_to_the_copy(self, tmp_path):
        # The copy that follows reports the real reason; "not downloaded"
        # would be a guess.
        assert source_module.is_dataless(tmp_path / "Gone.pdf") is False


class TestSkipIncompleteCoversCopies:
    """
    ``--skip-incomplete`` checked packages only, so a run told to skip what is
    not local downloaded every evicted PDF anyway (#12).
    """

    def test_an_evicted_pdf_is_skipped_and_counted(
        self, tmp_path, output_dir, monkeypatch, capsys
    ):
        library = tmp_path / "lib"
        library.mkdir()
        (library / "Local.pdf").write_bytes(b"%PDF-1.4\nlocal\n")
        (library / "Evicted.pdf").write_bytes(b"%PDF-1.4\nevicted\n")
        _evict(monkeypatch, library / "Evicted.pdf")

        code = run.main(
            [
                "-s",
                str(library),
                "-o",
                str(output_dir),
                "-m",
                "0",
                "--skip-incomplete",
                "-q",
            ]
        )

        assert code == 0
        assert [path.name for path in output_dir.glob("*.pdf")] == ["Local.pdf"]
        assert "1 not downloaded" in capsys.readouterr().out

    def test_without_the_flag_an_evicted_pdf_is_still_copied(
        self, tmp_path, output_dir, monkeypatch, capsys
    ):
        # Reading it downloads it, and that is what a default run is for.
        library = tmp_path / "lib"
        library.mkdir()
        (library / "Evicted.pdf").write_bytes(b"%PDF-1.4\nevicted\n")
        _evict(monkeypatch, library / "Evicted.pdf")

        code = run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])

        assert code == 0
        assert (output_dir / "Evicted.pdf").read_bytes() == b"%PDF-1.4\nevicted\n"
        assert "not downloaded" not in capsys.readouterr().out

    def test_an_evicted_file_already_on_the_shelf_is_not_reported(
        self, tmp_path, output_dir, monkeypatch, capsys
    ):
        # Packages settle against the shelf before the source is inspected.
        # Copies have to as well, or every rerun after iCloud evicts the source
        # again reports the whole PDF shelf as not downloaded.
        library = tmp_path / "lib"
        library.mkdir()
        (library / "Evicted.pdf").write_bytes(b"%PDF-1.4\nevicted\n")
        (output_dir / "Evicted.pdf").write_bytes(b"%PDF-1.4\nevicted\n")
        _evict(monkeypatch, library / "Evicted.pdf")

        code = run.main(
            [
                "-s",
                str(library),
                "-o",
                str(output_dir),
                "-m",
                "0",
                "--skip-incomplete",
                "-q",
            ]
        )

        assert code == 0
        assert "not downloaded" not in capsys.readouterr().out

    def test_an_evicted_epub_is_not_opened_to_name_it(
        self, tmp_path, output_dir, monkeypatch
    ):
        # Under --name-by author-title a zipped epub is named from its own
        # metadata, and opening it is the download the flag exists to avoid.
        # The plan and the copy alone; TestCopiesAreNamedOnce drives a whole
        # run, orphan check included.
        library = tmp_path / "lib"
        library.mkdir()
        evicted = library / "Earthsea.epub"
        _book_with_metadata(evicted)
        _evict(monkeypatch, evicted)
        opened: list[str] = []
        real_zip = ZipFile

        def recording(file, *args, **kwargs):
            opened.append(str(file))
            return real_zip(file, *args, **kwargs)

        monkeypatch.setattr(planning, "ZipFile", recording)
        report = convert.Report()

        plan = copying.plan_copies(
            [evicted], naming.build_policy(None, "author-title"), skip_incomplete=True
        )
        copying.copy_through_all(plan, output_dir, report)

        assert opened == []
        assert report.incomplete == 1
        assert report.copied == 0


AUTHOR_TITLE = ["--name-by", "author-title"]


def _count_opens(monkeypatch) -> list[str]:
    """Record every zip the planner opens to name a copy, and still open it."""
    opened: list[str] = []

    def recording(file, *args, **kwargs):
        opened.append(str(file))
        return ZipFile(file, *args, **kwargs)

    monkeypatch.setattr(planning, "ZipFile", recording)
    return opened


class TestCopiesAreNamedOnce:
    """
    Under ``--name-by author-title`` a zipped book is named from its own
    metadata, which means opening it. The orphan check named every copy in a
    loop before the run started, and the copy then named them all again, so on
    an evicted library each zipped book was downloaded one at a time before
    any work began -- and under ``--skip-incomplete``, downloaded at all.
    """

    def test_a_zipped_book_is_opened_once_per_run(
        self, tmp_path, output_dir, monkeypatch
    ):
        library = tmp_path / "lib"
        library.mkdir()
        book = library / "Earthsea.epub"
        _book_with_metadata(book)
        opened = _count_opens(monkeypatch)

        code = run.main(
            ["-s", str(library), "-o", str(output_dir), "-m", "0", *AUTHOR_TITLE, "-q"]
        )

        assert code == 0
        assert opened.count(str(book)) == 1
        assert [path.name for path in output_dir.glob("*.epub")] == [
            "Le Guin, Ursula K. - A Wizard of Earthsea.epub"
        ]

    def test_naming_runs_in_the_pool(self, tmp_path, output_dir, monkeypatch):
        # Two opens that each wait for the other finish only if they run at
        # the same time. In the orphan check's loop the first times out.
        library = tmp_path / "lib"
        library.mkdir()
        for name in ("One.epub", "Two.epub"):
            _book_with_metadata(library / name)
        both_opened = threading.Barrier(2, timeout=5)

        def meet_then_open(file, *args, **kwargs):
            both_opened.wait()
            return ZipFile(file, *args, **kwargs)

        monkeypatch.setattr(planning, "ZipFile", meet_then_open)

        code = run.main(
            [
                "-s",
                str(library),
                "-o",
                str(output_dir),
                "-m",
                "0",
                "-w",
                "2",
                *AUTHOR_TITLE,
                "-q",
            ]
        )

        assert code == 0

    def test_skip_incomplete_leaves_an_evicted_book_unopened_all_run(
        self, tmp_path, output_dir, monkeypatch, capsys
    ):
        library = tmp_path / "lib"
        library.mkdir()
        evicted = library / "Earthsea.epub"
        _book_with_metadata(evicted)
        _evict(monkeypatch, evicted)
        opened = _count_opens(monkeypatch)

        code = run.main(
            [
                "-s",
                str(library),
                "-o",
                str(output_dir),
                "-m",
                "0",
                *AUTHOR_TITLE,
                "--skip-incomplete",
                "-q",
            ]
        )

        assert code == 0
        assert opened == []
        assert "1 not downloaded" in capsys.readouterr().out

    def test_the_listing_leaves_it_unopened_too(
        self, tmp_path, output_dir, monkeypatch
    ):
        library = tmp_path / "lib"
        library.mkdir()
        evicted = library / "Earthsea.epub"
        _book_with_metadata(evicted)
        _evict(monkeypatch, evicted)
        opened = _count_opens(monkeypatch)

        code = run.main(
            [
                "-s",
                str(library),
                "-o",
                str(output_dir),
                "--list",
                *AUTHOR_TITLE,
                "--skip-incomplete",
            ]
        )

        assert code == 0
        assert opened == []

    def test_a_book_that_could_not_be_named_is_owned_up_to(
        self, tmp_path, output_dir, monkeypatch, capsys
    ):
        # Its copy may well be on the shelf, and nothing short of downloading
        # the source can say which file that is. So the run says so, rather
        # than letting the orphan count stand unexplained.
        library = tmp_path / "lib"
        library.mkdir()
        evicted = library / "Earthsea.epub"
        _book_with_metadata(evicted)
        (output_dir / "Le Guin, Ursula K. - A Wizard of Earthsea.epub").write_bytes(
            evicted.read_bytes()
        )
        _evict(monkeypatch, evicted)

        run.main(
            [
                "-s",
                str(library),
                "-o",
                str(output_dir),
                "-m",
                "0",
                *AUTHOR_TITLE,
                "--skip-incomplete",
                "-q",
            ]
        )

        captured = capsys.readouterr()
        assert "could not be named" in captured.out + captured.err


class TestADamagedZippedBookIsStillTakenAlong:
    """
    A zipped book whose package document cannot be inflated cannot be named
    from its metadata. It keeps its own filename, and the run goes on around it.
    """

    @damaged_streams
    def test_a_corrupt_package_document_does_not_stop_the_run(
        self, tmp_path, output_dir, method, raising
    ):
        # Regression (#21): naming read content.opf through a reader that
        # caught BadZipFile and OSError only, so one damaged book raised
        # zlib.error or lzma.LZMAError and the run wrote nothing at all.
        library = tmp_path / "lib"
        library.mkdir()
        _book_with_metadata(library / "Earthsea.epub")
        damaged = library / "Damaged.epub"
        _book_with_metadata(damaged)
        corrupt_member(recompress(damaged, method), "content.opf", raising)

        code = run.main(
            ["-s", str(library), "-o", str(output_dir), "-m", "0", *AUTHOR_TITLE, "-q"]
        )

        assert code == 0
        assert sorted(path.name for path in output_dir.glob("*.epub")) == [
            "Damaged.epub",
            "Le Guin, Ursula K. - A Wizard of Earthsea.epub",
        ]
