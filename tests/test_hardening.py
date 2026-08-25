"""
Tests for the guards that keep untrusted input and hostile filesystems honest.

A ``*.epub/`` package comes from a publisher or a sideload, so its XML and its
filenames are input, not fact. These cases cover the places where believing
them cost something.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them. Explicit empty-list comparisons read better than truthiness here.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=use-implicit-booleaness-not-comparison,too-few-public-methods

import errno
import logging
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from epubconvert import archive, convert, inspect_output, run, source, validate
from epubconvert.app_logger import logger
from epubconvert.display import printable
from epubconvert.naming import StripNaming
from tests.conftest import make_package


@pytest.fixture(name="records")
def records_fixture():
    """Capture messages from the package logger, which does not propagate."""
    captured: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record.getMessage())

    handler = Capture(level=logging.WARNING)
    logger.addHandler(handler)
    try:
        yield captured
    finally:
        logger.removeHandler(handler)


class TestDrmDetectionFailsClosed:
    """An encryption declaration we cannot interpret means protected."""

    def test_encrypted_data_without_an_algorithm_is_protected(self, tmp_path):
        # Regression (S4): only EncryptionMethod carried an Algorithm, so an
        # EncryptedData block without one yielded an empty set and the book
        # read as unprotected. It then exported as an unopenable archive and
        # was recorded as finished work no rerun retries.
        package = make_package(tmp_path / "lib", "Locked.epub")
        (package / "META-INF" / "encryption.xml").write_text(
            '<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container"'
            ' xmlns:enc="http://www.w3.org/2001/04/xmlenc#">'
            "<enc:EncryptedData><enc:CipherData>"
            '<enc:CipherReference URI="OEBPS/text/chapter1.xhtml"/>'
            "</enc:CipherData></enc:EncryptedData></encryption>",
            encoding="utf-8",
        )

        protected, reason = source.has_drm(package)

        assert protected
        assert reason


class TestUntrustedXmlIsBounded:
    """The source side must be capped like the archive side."""

    def test_an_implausibly_large_container_is_refused(self, tmp_path):
        # Regression (S5): read_package_dir had no size cap while the archive
        # reader capped at MAX_XML_BYTES -- and the uncapped side is the
        # untrusted one.
        package = make_package(tmp_path / "lib", "Huge.epub")
        (package / "META-INF" / "container.xml").write_text(
            "<container>" + "<pad/>" * 10, encoding="utf-8"
        )
        oversized = validate.MAX_XML_BYTES + 1
        with (package / "META-INF" / "container.xml").open("wb") as handle:
            handle.write(b"<container>" + b" " * oversized + b"</container>")

        with pytest.raises(validate.ValidationError, match="implausibly large"):
            validate.read_package_dir(package)


class TestMimetypeIsNotReadWhole:
    """A member declaring a huge size must be rejected on the declaration."""

    def test_an_oversized_mimetype_member_is_refused_without_reading_it(
        self, output_dir
    ):
        # Regression (S6): the whole member was read to compare 20 bytes, so a
        # member declaring 512 MiB was materialised first. --verify runs over
        # files this tool may not have written.
        archive_path = output_dir / "Big.epub"
        with ZipFile(archive_path, "w", ZIP_DEFLATED) as opened:
            opened.writestr("mimetype", b"x" * (1024 * 1024))

        problems = validate.validate_archive(archive_path)

        assert any("mimetype" in problem for problem in problems)


class TestControlCharactersDoNotReachTheTerminal:
    """A book must not be able to rewrite the line that reports it."""

    def test_a_package_name_carrying_an_escape_is_rendered_safely(self):
        # Regression (S7): names reached stdout verbatim, so a sideloaded book
        # could erase its own status line with ESC[2K.
        assert "\x1b" not in printable("Innocent\x1b[2KALL DONE.epub")
        assert "\r" not in printable("a\rb")

    def test_ordinary_names_are_untouched(self):
        assert printable("Ursula K. Le Guin — Earthsea.epub") == (
            "Ursula K. Le Guin — Earthsea.epub"
        )


class TestExportedFilesHonourTheUmask:
    """0644 must not be forced over a restrictive umask."""

    def test_the_umask_in_force_is_respected(self, tmp_path, output_dir):
        # Regression (S8): chmod(0o644) was unconditional, so `umask 077`
        # still produced world-readable books. The umask is now read once at
        # import -- reading it requires setting it, which is not safe to do
        # per book from 64 workers -- so this asserts against the value that
        # was in force then rather than changing it mid-process.
        library = tmp_path / "lib"
        make_package(library, "Book.epub")

        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])

        mode = (output_dir / "Book.epub").stat().st_mode & 0o777
        assert mode == archive.file_mode()


class TestLockFailuresAreDistinguished:
    """Contention is not the same as a filesystem without locking."""

    def test_an_unlockable_filesystem_does_not_claim_contention(
        self, tmp_path, output_dir, monkeypatch
    ):
        # Regression (R4): every OSError read as contention, so a share
        # without advisory locking reported "another run is already using..."
        # -- quoting a pid from a long-dead run.
        def unsupported(*_args, **_kwargs):
            raise OSError(errno.ENOTSUP, "not supported")

        monkeypatch.setattr("epubconvert.convert.fcntl.flock", unsupported)
        library = tmp_path / "lib"
        make_package(library, "Book.epub")

        code = run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])

        assert code == 0
        assert len(list(output_dir.glob("*.epub"))) == 1

    def test_real_contention_is_still_refused(self, output_dir):
        with (
            convert.output_lock(output_dir),
            pytest.raises(convert.OutputLockedError),
            convert.output_lock(output_dir),
        ):
            pass


class TestUnwritablePathsAreReported:
    """A raw traceback is not an error message."""

    def test_an_unwritable_log_file_does_not_kill_the_run(
        self, tmp_path, output_dir, capsys
    ):
        # Regression (R3/2.6): configure() did unguarded filesystem work and
        # was the first thing main did, so a bad --log-file exited with a
        # traceback before any logging existed to report it.
        blocked = tmp_path / "blocked"
        blocked.write_text("not a directory", encoding="utf-8")
        library = tmp_path / "lib"
        make_package(library, "Book.epub")

        code = run.main(
            [
                "-s",
                str(library),
                "-o",
                str(output_dir),
                "-m",
                "0",
                "--log-file",
                str(blocked / "run.log"),
            ]
        )

        assert code == 0
        assert len(list(output_dir.glob("*.epub"))) == 1
        del capsys

    def test_an_unwritable_output_directory_is_reported(self, tmp_path, capsys):
        # Regression (R2): output_lock opened the lock file unguarded, so a
        # read-only output directory died with a raw PermissionError.
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        out = tmp_path / "ro"
        out.mkdir()
        out.chmod(0o555)
        try:
            code = run.main(["-s", str(library), "-o", str(out), "-m", "0", "-q"])
        finally:
            out.chmod(0o755)

        assert code != 0
        del capsys


class TestUnmeasurableFreeSpaceIsAnnounced:
    """--min-free silently not applying is worse than not having it."""

    def test_a_failure_to_measure_is_logged(self, tmp_path, monkeypatch, records):
        # Regression (R5): free_megabytes returned 1 PiB on OSError and said
        # nothing, so --min-free became a no-op on exactly the removable and
        # network volumes it exists for.
        def unmeasurable(_path):
            raise OSError("no statvfs here")

        monkeypatch.setattr(
            "epubconvert.inspect_output.shutil.disk_usage", unmeasurable
        )

        free = inspect_output.free_megabytes(tmp_path)

        assert free > 0
        assert any("free space" in text for text in records)


class TestOneBookCannotKillTheRun:
    """A surprise in a worker costs that book, not the summary."""

    def test_an_unexpected_cover_failure_is_counted_not_raised(
        self, tmp_path, output_dir, monkeypatch
    ):
        # Regression (1.5/R1/R12): extract_cover was called outside the guard
        # and caught only three exception types, so anything else escaped the
        # worker after the archive was in place -- the book on disk, uncounted,
        # and the whole run dead with no summary.
        def boom(*_args, **_kwargs):
            raise MemoryError("cover too large")

        monkeypatch.setattr(convert, "extract_cover", boom)
        library = tmp_path / "lib"
        make_package(library, "Book.epub")

        code = run.main(
            ["-s", str(library), "-o", str(output_dir), "-m", "0", "--covers", "-q"]
        )

        assert code == 0
        assert len(list(output_dir.glob("*.epub"))) == 1


class TestSurrogateNamesDoNotAbortTheRun:
    """One undecodable filename must not cost the whole library."""

    def test_a_surrogate_escape_survives_naming(self):
        # Regression (1.6): every byte-budget encode("utf-8") raised
        # UnicodeEncodeError during planning, outside any handler.
        assert StripNaming().filename("Bo\udcffk.epub").endswith(".epub")


class TestPlatformsWithoutStatFlags:
    """--skip-incomplete must not silently do nothing."""

    def test_a_platform_without_st_flags_is_announced(
        self, tmp_path, monkeypatch, records
    ):
        # Regression (2.9): getattr(stat, "st_flags", 0) degrades to 0 on
        # Linux, so the check was a silent no-op while the help promised it
        # and the user paid a full walk of every package for it.
        package = make_package(tmp_path / "lib", "Book.epub")
        monkeypatch.setattr(source, "dataless_detection_available", lambda: False)

        result = source.has_dataless_files(package)

        assert result is False
        assert any("--skip-incomplete" in text for text in records)

    def test_the_walk_still_runs_where_the_flag_exists(self, tmp_path):
        package = make_package(tmp_path / "lib", "Book.epub")
        source.dataless_detection_available.cache_clear()

        assert source.has_dataless_files(package) is False
