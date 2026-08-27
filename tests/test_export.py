"""Tests for archive writing: reproducibility, interrupts, disk space, covers."""

# Test names describe the behaviour under test; separate docstrings would only
# restate them. Explicit empty-list comparisons read better than truthiness here.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=use-implicit-booleaness-not-comparison,too-few-public-methods

import hashlib
import os
from pathlib import Path
from zipfile import ZIP_STORED, ZipFile

import pytest

from epubconvert import convert, inspect_output, run
from epubconvert.archive import ARCHIVE_TIMESTAMP, zip_package
from epubconvert.validate import ValidationError, read_package_dir
from tests.conftest import make_package


def digest(path: Path) -> str:
    """Hash a file's bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TestDeterministicArchives:
    def test_re_export_is_byte_identical(self, library, output_dir):
        package = library / "Book One.epub"
        first = output_dir / "first.epub"
        second = output_dir / "second.epub"

        zip_package(package, first)
        zip_package(package, second)

        assert digest(first) == digest(second)

    def test_timestamps_are_normalized(self, library, output_dir):
        target = output_dir / "Book One.epub"

        zip_package(library / "Book One.epub", target)

        with ZipFile(target) as archive:
            for info in archive.infolist():
                assert info.date_time == ARCHIVE_TIMESTAMP

    def test_touching_the_source_does_not_change_the_bytes(self, library, output_dir):
        package = library / "Book One.epub"
        first = output_dir / "first.epub"
        zip_package(package, first)
        before = digest(first)

        for path in package.rglob("*"):
            if path.is_file():
                os_stat = path.stat()
                os_utime = (os_stat.st_atime + 10_000, os_stat.st_mtime + 10_000)
                os.utime(path, os_utime)
        second = output_dir / "second.epub"
        zip_package(package, second)

        assert digest(second) == before

    def test_archive_is_still_spec_valid(self, library, output_dir):
        target = output_dir / "Book One.epub"

        zip_package(library / "Book One.epub", target)

        with ZipFile(target) as archive:
            names = archive.namelist()
            assert names[0] == "mimetype"
            assert archive.getinfo("mimetype").compress_type == ZIP_STORED
            assert archive.read("mimetype") == b"application/epub+zip"
            assert archive.testzip() is None

    def test_content_still_round_trips(self, library, output_dir):
        target = output_dir / "Book One.epub"

        zip_package(library / "Book One.epub", target)

        with ZipFile(target) as archive:
            assert b"Chapter one" in archive.read("OEBPS/text/chapter1.xhtml")

    def test_members_are_readable_not_owner_only(self, library, output_dir):
        target = output_dir / "Book One.epub"

        zip_package(library / "Book One.epub", target)

        with ZipFile(target) as archive:
            for info in archive.infolist():
                assert (info.external_attr >> 16) & 0o044


class TestInterrupt:
    def test_partial_counts_survive(self, library, output_dir, monkeypatch, capsys):
        real = zip_package
        calls = {"n": 0}

        def stop_after_one(source: Path, target: Path, *args) -> int:
            calls["n"] += 1
            if calls["n"] > 1:
                raise KeyboardInterrupt
            return real(source, target, *args)

        monkeypatch.setattr(convert, "zip_package", stop_after_one)

        code = run.main(
            [
                "-s",
                str(library),
                "-o",
                str(output_dir),
                "-m",
                "0",
                "--no-shuffle",
                "-w",
                "1",
                "-q",
            ]
        )

        out = capsys.readouterr().out
        assert code == 130
        assert "Interrupted." in out
        # The book that finished is reported, not silently discarded.
        assert "Exported 1" in out

    def test_finished_books_are_intact(self, library, output_dir, monkeypatch):
        real = zip_package
        calls = {"n": 0}

        def stop_after_one(source: Path, target: Path, *args) -> int:
            calls["n"] += 1
            if calls["n"] > 1:
                raise KeyboardInterrupt
            return real(source, target, *args)

        monkeypatch.setattr(convert, "zip_package", stop_after_one)
        run.main(
            [
                "-s",
                str(library),
                "-o",
                str(output_dir),
                "-m",
                "0",
                "--no-shuffle",
                "-w",
                "1",
                "-q",
            ]
        )

        written = list(output_dir.glob("*.epub"))
        assert len(written) == 1
        with ZipFile(written[0]) as archive:
            assert archive.testzip() is None
        assert list(output_dir.glob("*.part")) == []

    def test_rerun_after_interrupt_continues(self, library, output_dir, monkeypatch):
        real = zip_package
        calls = {"n": 0}

        def stop_after_one(source: Path, target: Path, *args) -> int:
            calls["n"] += 1
            if calls["n"] > 1:
                raise KeyboardInterrupt
            return real(source, target, *args)

        monkeypatch.setattr(convert, "zip_package", stop_after_one)
        argv = [
            "-s",
            str(library),
            "-o",
            str(output_dir),
            "-m",
            "0",
            "--no-shuffle",
            "-w",
            "1",
            "-q",
        ]
        run.main(argv)
        monkeypatch.setattr(convert, "zip_package", real)

        assert run.main(argv) == 0
        assert len(list(output_dir.glob("*.epub"))) == 2


class TestDiskFloor:
    def test_a_later_book_is_stopped_when_space_runs_out_mid_run(
        self, tmp_path, output_dir, monkeypatch
    ):
        # The pre-export check catches a volume that is already full. This is
        # the other half of the rule: the volume fills up *during* the run, and
        # the sampled mid-export check has to stop the rest. Only the first
        # half had a test, so convert.py's mid-export branch never ran.
        library = tmp_path / "lib"
        for index in range(4):
            make_package(library, f"Book {index}.epub")

        # One worker means the sampling interval is already one book, so
        # every book re-measures: the first sees room, the rest do not.
        readings = iter([10_000] + [1] * 20)
        monkeypatch.setattr(convert, "free_megabytes", lambda _p: next(readings, 1))

        code = run.main(
            [
                "-s",
                str(library),
                "-o",
                str(output_dir),
                "-m",
                "0",
                "--min-free",
                "100",
                "-w",
                "1",
                "-q",
            ]
        )

        assert code == 1
        assert len(list(output_dir.glob("*.epub"))) < 4

    def test_export_stops_when_space_is_short(self, tmp_path, output_dir, monkeypatch):
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        monkeypatch.setattr(convert, "free_megabytes", lambda _p: 5)

        code = run.main(
            [
                "-s",
                str(library),
                "-o",
                str(output_dir),
                "-m",
                "0",
                "--min-free",
                "100",
                "-q",
            ]
        )

        # Non-zero because the run could not do its job -- but nothing is
        # counted as failed, since nothing was attempted.
        assert code == 1
        assert list(output_dir.glob("*.epub")) == []

    def test_zero_disables_the_check(self, tmp_path, output_dir, monkeypatch):
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        monkeypatch.setattr(convert, "free_megabytes", lambda _p: 0)

        code = run.main(
            [
                "-s",
                str(library),
                "-o",
                str(output_dir),
                "-m",
                "0",
                "--min-free",
                "0",
                "-q",
            ]
        )

        assert code == 0
        assert len(list(output_dir.glob("*.epub"))) == 1

    def test_free_space_is_reported_as_an_int(self, tmp_path):
        assert isinstance(inspect_output.free_megabytes(tmp_path), int)


class TestCovers:
    def test_cover_is_written_beside_the_book(self, tmp_path, output_dir):
        library = tmp_path / "lib"
        _cover_package(library / "Book.epub")

        run.main(
            ["-s", str(library), "-o", str(output_dir), "-m", "0", "--covers", "-q"]
        )

        assert (output_dir / "Book.jpg").exists()
        assert (output_dir / "Book.jpg").read_bytes() == b"JPEGDATA"

    def test_no_cover_flag_writes_no_image(self, tmp_path, output_dir):
        library = tmp_path / "lib"
        _cover_package(library / "Book.epub")

        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])

        assert list(output_dir.glob("*.jpg")) == []

    def test_covers_do_not_confuse_the_export_record(
        self, tmp_path, output_dir, capsys
    ):
        library = tmp_path / "lib"
        _cover_package(library / "Book.epub")
        argv = ["-s", str(library), "-o", str(output_dir), "-m", "0", "--covers", "-q"]
        run.main(argv)
        capsys.readouterr()

        run.main(argv)

        # Identity globs *.epub, so the .jpg beside it must not affect reruns.
        assert "skipped 1" in capsys.readouterr().out

    def test_a_cover_named_like_a_book_cannot_overwrite_it(self, tmp_path, output_dir):
        # Regression: the cover path was built with Path.with_suffix, which
        # *replaces* the extension. A cover href ending in ".epub" therefore
        # resolved to the exported archive itself and overwrote the book with
        # image bytes -- while the run still reported it as exported, so the
        # output directory recorded a destroyed book as finished work.
        library = tmp_path / "lib"
        package = _cover_package(library / "Book.epub")
        (package / "OEBPS" / "images" / "cover.jpg").unlink()
        (package / "OEBPS" / "images" / "cover.epub").write_bytes(b"NOT-A-BOOK")
        opf = package / "OEBPS" / "content.opf"
        opf.write_text(
            opf.read_text(encoding="utf-8").replace("cover.jpg", "cover.epub"),
            encoding="utf-8",
        )

        run.main(
            ["-s", str(library), "-o", str(output_dir), "-m", "0", "--covers", "-q"]
        )

        exported = output_dir / "Book.epub"
        assert exported.read_bytes() != b"NOT-A-BOOK"
        with ZipFile(exported) as archive:
            assert archive.namelist()[0] == "mimetype"
            assert archive.testzip() is None

    def test_a_cover_never_clobbers_an_existing_file(self, tmp_path, output_dir):
        library = tmp_path / "lib"
        _cover_package(library / "Book.epub")
        guard = output_dir / "Book.jpg"
        guard.write_bytes(b"PRE-EXISTING")

        run.main(
            ["-s", str(library), "-o", str(output_dir), "-m", "0", "--covers", "-q"]
        )

        assert guard.read_bytes() == b"PRE-EXISTING"

    def test_a_book_without_a_cover_is_fine(self, tmp_path, output_dir):
        library = tmp_path / "lib"
        make_package(library, "Plain.epub")

        code = run.main(
            ["-s", str(library), "-o", str(output_dir), "-m", "0", "--covers", "-q"]
        )

        assert code == 0
        assert list(output_dir.glob("*.jpg")) == []

    def test_a_cover_href_cannot_reach_outside_the_package(self, tmp_path, output_dir):
        # Regression: the href was joined onto the package directory without a
        # containment check, and _resolve keeps leading "../" segments. A book
        # could name any file the user could read and have its bytes copied
        # into the output directory, to travel on to whatever device the shelf
        # was copied to.
        library = tmp_path / "lib"
        package = _cover_package(library / "Book.epub")
        secret = tmp_path / "secret.txt"
        secret.write_bytes(b"PRIVATE-KEY")
        opf = package / "OEBPS" / "content.opf"
        opf.write_text(
            opf.read_text(encoding="utf-8").replace(
                "images/cover.jpg", "../../../secret.txt"
            ),
            encoding="utf-8",
        )

        code = run.main(
            ["-s", str(library), "-o", str(output_dir), "-m", "0", "--covers", "-q"]
        )

        assert code == 0
        written = [p.read_bytes() for p in output_dir.iterdir() if p.is_file()]
        assert b"PRIVATE-KEY" not in written

    def test_a_rootfile_cannot_reach_outside_the_package(self, tmp_path):
        # The same escape one level up: container.xml's full-path is joined
        # onto the package directory too, and an absolute path replaces it
        # outright.
        library = tmp_path / "lib"
        package = _cover_package(library / "Book.epub")
        (package / "META-INF" / "container.xml").write_text(
            '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="../../../elsewhere.opf"/></rootfiles>'
            "</container>",
            encoding="utf-8",
        )

        with pytest.raises(ValidationError, match="outside the package"):
            read_package_dir(package)


def _cover_package(package: Path) -> Path:
    """Build a package whose OPF declares a cover image."""
    opf = """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>Covered</dc:title>
    <dc:identifier id="bid">urn:uuid:1</dc:identifier>
  </metadata>
  <manifest>
    <item id="ch1" href="text/ch1.xhtml" media-type="application/xhtml+xml"/>
    <item id="cover" href="images/cover.jpg" media-type="image/jpeg"
          properties="cover-image"/>
  </manifest>
  <spine><itemref idref="ch1"/></spine>
</package>
"""
    container = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf"
              media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""
    layout = {
        "mimetype": "application/epub+zip",
        "META-INF/container.xml": container,
        "OEBPS/content.opf": opf,
        "OEBPS/text/ch1.xhtml": "<html><body>hi</body></html>",
    }
    for relative, body in layout.items():
        path = package / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    cover = package / "OEBPS" / "images" / "cover.jpg"
    cover.parent.mkdir(parents=True, exist_ok=True)
    cover.write_bytes(b"JPEGDATA")
    return package
