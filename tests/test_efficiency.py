"""
Tests for the work the tool does per book, and the work it declines to do.

These pin behaviour a measurement established, so a later change cannot quietly
undo it: which compression level is actually applied, how many threads the pool
takes by default, and which expensive checks run only for books that will
really be written.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them. Explicit empty-list comparisons read better than truthiness here.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=use-implicit-booleaness-not-comparison,too-few-public-methods

import os
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

from epubconvert import archive, convert, inspect_output, planning, run, source
from epubconvert.naming import PassthroughNaming
from tests.conftest import make_package


class TestCompressionLevelIsActuallyApplied:
    """The constant must reach zlib, and must be the level worth paying for."""

    def test_the_entry_carries_the_configured_level(self):
        # Regression (7.1): ZipFile(compresslevel=) is consulted only when
        # open() builds its own ZipInfo. _entry supplies a prebuilt one, so the
        # constant had never been applied and every member deflated at zlib's
        # default. Measured at level 9: 3.2x the CPU for 0.6% smaller.
        entry = archive.entry("OEBPS/text.xhtml", ZIP_DEFLATED)

        assert archive.level_of(entry) == archive.COMPRESS_LEVEL

    def test_the_level_is_the_one_measurement_supports(self):
        assert archive.COMPRESS_LEVEL == 6


class TestAlreadyCompressedMediaIsStored:
    """Deflating a JPEG spends CPU to save nothing."""

    def test_an_image_member_is_stored_not_deflated(self, tmp_path, output_dir):
        # Measured on an image-heavy book: storing media is 3.8x faster for
        # +0.02% size.
        package = make_package(tmp_path / "lib", "Book.epub")
        images = package / "OEBPS" / "images"
        images.mkdir(parents=True)
        (images / "cover.jpg").write_bytes(b"\xff\xd8\xff" + os.urandom(4096))

        target = output_dir / "Book.epub"
        archive.zip_package(package, target)

        with ZipFile(target) as opened:
            assert opened.getinfo("OEBPS/images/cover.jpg").compress_type == ZIP_STORED
            assert opened.getinfo("OEBPS/content.opf").compress_type == ZIP_DEFLATED

    def test_storing_media_keeps_exports_byte_identical(self, tmp_path, output_dir):
        package = make_package(tmp_path / "lib", "Book.epub")
        (package / "OEBPS" / "art.png").write_bytes(os.urandom(2048))
        first = output_dir / "a.epub"
        second = output_dir / "b.epub"

        archive.zip_package(package, first)
        archive.zip_package(package, second)

        assert first.read_bytes() == second.read_bytes()


class TestWorkerDefaultSuitsBlockingWork:
    """The pool is waiting on iCloud, not on the CPU."""

    def test_the_default_exceeds_the_cpu_count(self):
        # Measured under an iCloud stall model: 14 workers 19.2 s, 48 workers
        # 7.12 s, 64 workers 4.76 s. The CPU-bound cost of raising it is 6%.
        assert convert.default_workers() > (os.cpu_count() or 1)

    def test_an_explicit_count_still_wins(self):
        assert convert.default_workers(7) == 7


class TestTheStubWalkStaysBeforeTheCap:
    """Deferring it would be faster and would break the remaining count.

    Finding 5.1 proposed walking for undownloaded files only after
    ``--max-export-files`` has chosen its subset, which measured 801 scandirs
    and 3,696 stats to export 5 books out of 200. It was **not** taken.

    The planner can only know a book is undownloaded by walking it. Deferred
    past the cap, a book outside the subset is never walked, so it plans as
    pending on every run and ``N remaining`` never reaches zero -- which is
    precisely the defect an earlier review fixed, where ``count_pending``
    forced the check off and ``--skip-incomplete`` reported books remaining
    that it would never export.

    This test pins the correctness requirement so the optimisation cannot be
    reintroduced without noticing the conflict.
    """

    def test_an_undownloaded_book_is_excluded_from_the_remaining_count(
        self, tmp_path, output_dir, monkeypatch, capsys
    ):
        library = tmp_path / "lib"
        make_package(library, "Stub.epub")
        make_package(library, "Real.epub")
        monkeypatch.setattr(
            source, "has_dataless_files", lambda package: package.name == "Stub.epub"
        )

        run.main(
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

        out = capsys.readouterr().out
        assert "not downloaded" in out
        assert "remaining" not in out


class TestCollisionSearchDoesNotRescan:
    """A full group must not re-try every position for each later member."""

    def test_identity_is_not_recomputed_for_exhausted_names(self, tmp_path):
        # Regression (5.3): once a group used all MAX_SUFFIX positions, every
        # later package retried all 99 candidates before losing.
        packages = [make_package(tmp_path / str(i), "Same.epub") for i in range(4)]
        calls = {"n": 0}

        class Counting(PassthroughNaming):
            def identity(self, filename: str) -> str:
                calls["n"] += 1
                return super().identity(filename)

        planning.assign_names(packages, Counting(), planning.SUFFIX)

        assert calls["n"] < len(packages) * planning.MAX_SUFFIX


class TestVerifyChecksEveryArchive:
    """The read-back path pools its work but must miss nothing."""

    def test_every_archive_is_still_checked(self, output_dir):
        for index in range(5):
            with ZipFile(output_dir / f"Book{index}.epub", "w") as opened:
                opened.writestr("mimetype", "application/epub+zip")

        checked, damaged = inspect_output.verify_output(output_dir)

        assert checked == 5
        assert damaged == 5


class TestFreeSpaceIsSampled:
    """One statvfs per book is a syscall the floor does not need."""

    def test_the_volume_is_not_measured_once_per_book(
        self, tmp_path, output_dir, monkeypatch
    ):
        # Regression (7.8): --min-free defaults on, and its own help names the
        # volumes where statvfs is slowest.
        library = tmp_path / "lib"
        for index in range(8):
            make_package(library, f"Book{index}.epub")
        calls = {"n": 0}
        real = inspect_output.free_megabytes

        def counted(path):
            calls["n"] += 1
            return real(path)

        monkeypatch.setattr(convert, "free_megabytes", counted)

        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])

        assert calls["n"] < 8
