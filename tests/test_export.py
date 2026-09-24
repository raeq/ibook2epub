"""Tests for archive writing: reproducibility, interrupts, disk space, covers."""

# Test names describe the behaviour under test; separate docstrings would only
# restate them. Explicit empty-list comparisons read better than truthiness here.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=use-implicit-booleaness-not-comparison,too-few-public-methods

import errno
import hashlib
import json
import os
import shutil
import stat
import tracemalloc
import zipfile
from pathlib import Path
from zipfile import ZIP_STORED, ZipFile, ZipInfo

import pytest

from epubconvert.collect.annotations import EMBEDDED_PATH
from epubconvert.collect.validate import ValidationError, read_package_dir
from epubconvert.export import inspect_output
from epubconvert.export.archive import (
    ARCHIVE_TIMESTAMP,
    file_mode,
    replace_annotations,
    write_atomically,
    zip_package,
)
from epubconvert.run import convert, run
from tests.conftest import make_package, needs_permissions


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


class TestAMemberPastTheZip64Limit:
    """
    A member over 2 GiB needs ZIP64 headers, and zipfile decides whether to
    write them from the size it is told *before* the member is written. The
    limit is lowered here rather than a 2 GiB file written.
    """

    LIMIT = 4096

    @staticmethod
    def _package(library: Path) -> Path:
        package = make_package(library, "Big.epub")
        (package / "OEBPS" / "video.mp4").write_bytes(os.urandom(3 * 4096))
        return package

    def test_a_member_past_the_limit_is_exported(
        self, tmp_path, output_dir, monkeypatch
    ):
        # Regression: every member was opened from a ZipInfo whose file_size
        # was 0, so zipfile wrote 32-bit headers and raised RuntimeError at
        # the first member past 2 GiB. The book could never be exported.
        monkeypatch.setattr(zipfile, "ZIP64_LIMIT", self.LIMIT)
        package = self._package(tmp_path / "lib")
        target = output_dir / "Big.epub"

        zip_package(package, target)

        with ZipFile(target) as archive:
            assert (
                archive.read("OEBPS/video.mp4")
                == (package / "OEBPS" / "video.mp4").read_bytes()
            )
            assert archive.testzip() is None

    def test_sizing_members_ahead_changes_no_byte_of_an_ordinary_book(
        self, library, output_dir, monkeypatch
    ):
        # Re-exports must stay byte-identical across versions too, so the fix
        # above may only change books that need ZIP64. The comparison is with
        # every member opened unsized, which is how archives were written
        # before it.
        package = library / "Book One.epub"
        sized = output_dir / "sized.epub"
        zip_package(package, sized)

        real_open = ZipFile.open

        def unsized(self, name, mode="r", **kwargs):
            if mode == "w" and isinstance(name, ZipInfo):
                name.file_size = 0
            return real_open(self, name, mode, **kwargs)

        monkeypatch.setattr(ZipFile, "open", unsized)
        before = output_dir / "unsized.epub"
        zip_package(package, before)

        assert sized.read_bytes() == before.read_bytes()


class TestAPackageCarryingItsOwnAnnotations:
    """
    A sideloaded package can arrive with ``META-INF/annotations.json`` already
    in it. The zip format allows two members with one name; the OCF does not,
    and readers disagree about which of the two they see.
    """

    MINE: tuple[dict[str, object], ...] = ({"id": "MINE"},)

    @staticmethod
    def _package(library: Path) -> Path:
        package = make_package(library, "Book.epub")
        (package / EMBEDDED_PATH).write_text('{"annotations": [{"id": "THEIRS"}]}')
        return package

    def test_embedding_annotations_leaves_one_member_of_that_name(
        self, tmp_path, output_dir
    ):
        # Regression: the package's own copy was stored as a member and then
        # _embed_annotations wrote the name again, so the archive held two.
        target = output_dir / "Book.epub"

        count = zip_package(
            self._package(tmp_path / "lib"), target, annotations=list(self.MINE)
        )

        with ZipFile(target) as archive:
            names = archive.namelist()
            held = json.loads(archive.read(EMBEDDED_PATH))
        assert names.count(EMBEDDED_PATH) == 1
        assert held["annotations"] == list(self.MINE)
        assert count == len(names) - 1

    def test_without_annotations_the_package_keeps_its_own(self, tmp_path, output_dir):
        target = output_dir / "Book.epub"

        zip_package(self._package(tmp_path / "lib"), target)

        with ZipFile(target) as archive:
            assert b"THEIRS" in archive.read(EMBEDDED_PATH)


class TestARefreshStreamsItsMembers:
    """
    Refreshing the annotations of a book already on the shelf rebuilds the
    whole archive, so it must cost what the book's largest member costs to
    stream, not what the whole book costs to hold.
    """

    MINE: tuple[dict[str, object], ...] = ({"id": "MINE"},)

    @staticmethod
    def _shelved(tmp_path: Path, media: bytes) -> Path:
        package = make_package(tmp_path / "lib", "Book.epub")
        (package / "OEBPS" / "video.mp4").write_bytes(media)
        target = tmp_path / "out" / "Book.epub"
        target.parent.mkdir()
        zip_package(package, target)
        return target

    def test_a_refresh_does_not_hold_the_book_in_memory(self, tmp_path):
        # Regression: every member was read into a list before the rewrite, so
        # a 300 MB book peaked at 300 MB per refresh, and a MemoryError there
        # was not one of the errors a refresh reports and moves past.
        size = 8 * 1024 * 1024
        target = self._shelved(tmp_path, os.urandom(size))

        tracemalloc.start()
        try:
            replace_annotations(target, list(self.MINE))
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        assert peak < size // 4

    def test_a_refresh_writes_what_a_fresh_export_would(self, tmp_path):
        # Every member keeps its compression and normalized metadata, so a
        # book refreshed on the shelf and the same book exported with those
        # annotations in the first place are the same bytes.
        media = os.urandom(200_000)
        refreshed = self._shelved(tmp_path / "a", media)
        replace_annotations(refreshed, list(self.MINE))

        fresh = self._shelved(tmp_path / "b", media)
        package = tmp_path / "b" / "lib" / "Book.epub"
        zip_package(package, fresh, annotations=list(self.MINE))

        assert refreshed.read_bytes() == fresh.read_bytes()

    def test_a_member_past_the_zip64_limit_survives_a_refresh(
        self, tmp_path, monkeypatch
    ):
        media = os.urandom(3 * 4096)
        monkeypatch.setattr(zipfile, "ZIP64_LIMIT", 4096)
        target = self._shelved(tmp_path, media)

        replace_annotations(target, list(self.MINE))

        with ZipFile(target) as archive:
            assert archive.read("OEBPS/video.mp4") == media
            assert archive.testzip() is None


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

    def test_once_the_floor_is_crossed_no_further_book_starts(
        self, tmp_path, output_dir, monkeypatch, capsys
    ):
        # Only one book in `interval` measures. The floor used to stop only the
        # book whose sample found it crossed: every unsampled book after it
        # went on writing to a volume already below the floor. Twelve books on
        # four workers wrote ten.
        library = tmp_path / "lib"
        for index in range(12):
            make_package(library, f"Book {index:02}.epub")
        # Room for the pre-flight check and the first sample; below the floor
        # from the second sample on.
        readings = iter([10_000, 10_000])
        monkeypatch.setattr(convert, "free_megabytes", lambda _p: next(readings, 1))

        code = run.main(
            ["-s", str(library), "-o", str(output_dir), "-m", "0"]
            + ["--min-free", "100", "-w", "4"]
        )

        written = len(list(output_dir.glob("*.epub")))
        summary = capsys.readouterr().out.strip().splitlines()[-1]
        # The four books before the second sample, and at most the three that
        # had already passed their own check when it found the floor crossed.
        assert written <= 7
        # Nothing it declined to start failed: a rerun on a volume with room
        # converts them, which is what the summary has to say.
        assert code == 1
        assert summary.startswith("Aborted: not enough free space")
        assert "failed" not in summary
        assert f"{12 - written} not attempted: rerun to continue." in summary

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


#: Where extract_cover looks up its copy, for tests that make it fail.
COPY = "epubconvert.export.inspect_output.shutil.copyfileobj"


def _disk_fills(reading, writing) -> None:
    """Copy one buffer, then fail the way a full disk does."""
    writing.write(reading.read(65536))
    raise OSError(errno.ENOSPC, "No space left on device")


class TestACoverIsWrittenWholeOrNotAtAll:
    """
    A cover is written only when its name is free, so a truncated one is never
    rewritten: it has to be complete the moment it has its name.
    """

    @staticmethod
    def _book(tmp_path: Path) -> tuple[Path, Path]:
        package = _cover_package(tmp_path / "lib" / "Book.epub")
        (package / "OEBPS" / "images" / "cover.jpg").write_bytes(b"J" * 200_000)
        target = tmp_path / "out" / "Book.epub"
        target.parent.mkdir()
        target.write_bytes(b"the book")
        return package, target

    def test_a_copy_that_fails_partway_leaves_no_cover_behind(
        self, tmp_path, monkeypatch
    ):
        # Regression: the cover was opened under its final name and written in
        # place. A full disk partway through left a prefix of the image, and
        # every later run saw the name taken and never wrote it again.
        package, target = self._book(tmp_path)
        monkeypatch.setattr(COPY, _disk_fills)

        assert inspect_output.extract_cover(package, target) is None
        assert sorted(p.name for p in target.parent.iterdir()) == ["Book.epub"]

    def test_the_next_run_writes_the_whole_cover(self, tmp_path, monkeypatch):
        package, target = self._book(tmp_path)
        with monkeypatch.context() as patched:
            patched.setattr(COPY, _disk_fills)
            inspect_output.extract_cover(package, target)

        cover = inspect_output.extract_cover(package, target)

        assert cover is not None
        assert cover.read_bytes() == b"J" * 200_000

    def test_a_cover_honours_the_umask(self, tmp_path):
        package, target = self._book(tmp_path)

        cover = inspect_output.extract_cover(package, target)

        assert cover is not None
        assert stat.S_IMODE(cover.stat().st_mode) == file_mode()

    def test_a_volume_without_hard_links_still_gets_its_cover(
        self, tmp_path, monkeypatch
    ):
        # exFAT and FAT, the volumes a shelf is most often copied to, have no
        # hard links, so publishing by link alone would silently stop writing
        # covers there.
        package, target = self._book(tmp_path)

        def no_links(*_args):
            raise PermissionError(errno.EPERM, "Operation not permitted")

        monkeypatch.setattr("epubconvert.export.inspect_output.os.link", no_links)

        cover = inspect_output.extract_cover(package, target)

        assert cover is not None
        assert cover.read_bytes() == b"J" * 200_000
        assert sorted(p.name for p in target.parent.iterdir()) == [
            "Book.epub",
            "Book.jpg",
        ]

    def test_without_hard_links_a_name_taken_meanwhile_is_still_kept(
        self, tmp_path, monkeypatch
    ):
        package, target = self._book(tmp_path)
        cover = target.with_name("Book.jpg")

        def taken_and_no_links(*_args):
            cover.write_bytes(b"THEIRS")
            raise PermissionError(errno.EPERM, "Operation not permitted")

        monkeypatch.setattr(
            "epubconvert.export.inspect_output.os.link", taken_and_no_links
        )

        assert inspect_output.extract_cover(package, target) is None
        assert cover.read_bytes() == b"THEIRS"

    def test_a_name_taken_while_the_cover_was_copied_is_not_overwritten(
        self, tmp_path, monkeypatch
    ):
        package, target = self._book(tmp_path)
        cover = target.with_name("Book.jpg")
        real = shutil.copyfileobj

        def someone_else_writes_first(reading, writing):
            real(reading, writing)
            cover.write_bytes(b"THEIRS")

        monkeypatch.setattr(COPY, someone_else_writes_first)

        assert inspect_output.extract_cover(package, target) is None
        assert cover.read_bytes() == b"THEIRS"
        assert sorted(p.name for p in target.parent.iterdir()) == [
            "Book.epub",
            "Book.jpg",
        ]


class TestACoverSuffixComesFromAShortList:
    """
    The cover's extension is taken from an href the book chose, and it names a
    file in the output directory.
    """

    @staticmethod
    def _book(tmp_path: Path, href: str) -> tuple[Path, Path]:
        package = _cover_package(tmp_path / "lib" / "Book.epub")
        images = package / "OEBPS" / "images"
        (images / "cover.jpg").rename(images / href)
        opf = package / "OEBPS" / "content.opf"
        opf.write_text(
            opf.read_text(encoding="utf-8").replace("cover.jpg", href),
            encoding="utf-8",
        )
        target = tmp_path / "out" / "Book.epub"
        target.parent.mkdir()
        target.write_bytes(b"the book")
        return package, target

    @pytest.mark.parametrize("href", ["cover.EPUB", "cover.Epub"])
    def test_a_cover_cannot_take_the_book_suffix_in_another_case(self, tmp_path, href):
        # Regression: only cover == target_archive was refused, and that
        # comparison is case-sensitive. Book.EPUB beside Book.epub is one file
        # on a case-insensitive volume, so copying the shelf to one replaced
        # the book with the image, or the image with the book.
        package, target = self._book(tmp_path, href)

        assert inspect_output.extract_cover(package, target) is None
        assert sorted(p.name for p in target.parent.iterdir()) == ["Book.epub"]

    @pytest.mark.parametrize("href", ["cover.exe", "cover.html", "cover.plist"])
    def test_a_suffix_that_is_not_an_image_is_refused(self, tmp_path, href):
        package, target = self._book(tmp_path, href)

        assert inspect_output.extract_cover(package, target) is None
        assert sorted(p.name for p in target.parent.iterdir()) == ["Book.epub"]

    @pytest.mark.parametrize(
        ("href", "written"),
        [
            ("cover.JPG", "Book.jpg"),
            ("cover.jpeg", "Book.jpeg"),
            ("cover.PNG", "Book.png"),
            ("cover.gif", "Book.gif"),
            ("cover.webp", "Book.webp"),
            ("cover.svg", "Book.svg"),
        ],
    )
    def test_an_image_suffix_is_kept_in_lower_case(self, tmp_path, href, written):
        package, target = self._book(tmp_path, href)

        cover = inspect_output.extract_cover(package, target)

        assert cover == target.with_name(written)


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


class TestRewritingASidecarKeepsWhatTheUserSet:
    """
    ``write_atomically`` replaces the detached export and the notes on every
    rerun. A replace swaps in a new file, so whatever the user set on the old
    one -- its mode, the fact that it is a link -- has to be carried across.
    """

    def test_a_private_file_stays_private(self, tmp_path):
        # Regression: every rerun wrote a fresh partial at the umask's mode,
        # so an export the user had made 0600 became 0644 again.
        target = tmp_path / "highlights.json"
        write_atomically(target, "first")
        target.chmod(0o600)

        write_atomically(target, "second")

        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert target.read_text(encoding="utf-8") == "second"

    def test_a_new_file_honours_the_umask(self, tmp_path):
        target = tmp_path / "highlights.json"

        write_atomically(target, "first")

        assert stat.S_IMODE(target.stat().st_mode) == file_mode()

    def test_a_symlinked_target_is_written_through(self, tmp_path):
        # Regression: the replace landed on the link itself, so a link into a
        # synced folder became a regular file here and the synced copy went
        # stale without a word.
        real = tmp_path / "synced" / "highlights.json"
        real.parent.mkdir()
        real.write_text("old", encoding="utf-8")
        link = tmp_path / "highlights.json"
        link.symlink_to(real)

        write_atomically(link, "new")

        assert link.is_symlink()
        assert real.read_text(encoding="utf-8") == "new"
        assert [p.name for p in real.parent.iterdir()] == ["highlights.json"]
        assert sorted(p.name for p in tmp_path.iterdir()) == [
            "highlights.json",
            "synced",
        ]

    def test_the_new_contents_reach_the_disk_before_the_rename(
        self, tmp_path, monkeypatch
    ):
        # Without an fsync, a crash just after the rename can leave the name
        # pointing at a file whose data was never written: the old contents
        # gone and the new ones empty.
        target = tmp_path / "highlights.json"
        target.write_text("old", encoding="utf-8")
        synced: list[str] = []
        real_fsync = os.fsync

        def recording_fsync(descriptor):
            synced.append(target.read_text(encoding="utf-8"))
            real_fsync(descriptor)

        monkeypatch.setattr("epubconvert.export.archive.os.fsync", recording_fsync)

        write_atomically(target, "new")

        assert synced == ["old"]
        assert target.read_text(encoding="utf-8") == "new"

    @needs_permissions
    def test_a_read_only_file_can_still_be_rewritten(self, tmp_path):
        target = tmp_path / "highlights.json"
        write_atomically(target, "first")
        target.chmod(0o444)

        write_atomically(target, "second")

        assert stat.S_IMODE(target.stat().st_mode) == 0o444
        assert target.read_text(encoding="utf-8") == "second"
