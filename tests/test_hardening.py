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
import io
import logging
import os
import threading
from collections.abc import Callable
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from epubconvert.collect import source, validate
from epubconvert.collect.library import read_package_once
from epubconvert.export import archive, inspect_output
from epubconvert.export.naming import StripNaming
from epubconvert.run import convert, run
from epubconvert.utils import contained
from epubconvert.utils.app_logger import logger
from epubconvert.utils.display import printable
from tests.conftest import make_metadata_package, make_package, needs_permissions


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


@pytest.fixture(name="debug_records")
def debug_records_fixture():
    """Capture everything the package logger says at -v and above, as rendered."""
    captured: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record.getMessage())

    handler = Capture(level=logging.DEBUG)
    level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield captured
    finally:
        logger.setLevel(level)
        logger.removeHandler(handler)


#: Encodings expat will not parse: multi-byte ones, which it refuses with
#: ValueError, and a name Python does not know, which raises LookupError.
UNPARSABLE_ENCODINGS = ["Shift_JIS", "EUC-JP", "UTF-32", "x-no-such-encoding"]


def _declaring(encoding: str, body: str) -> bytes:
    """An XML document whose declaration names *encoding*."""
    return f'<?xml version="1.0" encoding="{encoding}"?>{body}'.encode("ascii")


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

    @pytest.mark.parametrize("encoding", UNPARSABLE_ENCODINGS)
    def test_an_encoding_the_parser_refuses_is_protected(self, tmp_path, encoding):
        # expat raises ValueError for a multi-byte encoding and LookupError for
        # an unknown one, neither a ParseError, so the whole run died in
        # planning over one Japanese book's encryption.xml.
        package = make_package(tmp_path / "lib", "Declared.epub")
        (package / "META-INF" / "encryption.xml").write_bytes(
            _declaring(encoding, "<encryption/>")
        )

        protected, reason = source.has_drm(package)

        assert protected
        assert reason


class TestAnEncodingThePackageReaderRefusesCostsOneBook:
    @pytest.mark.parametrize("encoding", UNPARSABLE_ENCODINGS)
    def test_a_container_in_such_an_encoding_is_unreadable(self, tmp_path, encoding):
        # planning._metadata_of catches ValidationError and OSError, so under
        # --name-by author-title the ValueError ended the run.
        package = make_package(tmp_path / "lib", "Declared.epub")
        (package / "META-INF" / "container.xml").write_bytes(
            _declaring(encoding, "<container/>")
        )

        with pytest.raises(validate.ValidationError, match="not valid XML"):
            validate.read_package_dir(package)


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


def _within(seconds: float, fifo: Path, call: Callable[[], object]) -> object:
    """
    Run *call*, failing rather than hanging if it blocks on *fifo*.

    Opening a FIFO for reading blocks until a writer appears. On a timeout the
    FIFO is opened for writing and closed, which releases the stuck reader, so
    a regression fails the test instead of freezing the suite.
    """
    outcome: list[object] = []

    def run_it() -> None:
        try:
            outcome.append(call())
        except (validate.ValidationError, OSError) as exc:
            outcome.append(exc)

    worker = threading.Thread(target=run_it, daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
        worker.join(seconds)
        pytest.fail(f"blocked opening {fifo.name}")
    return outcome[0]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="this platform has no FIFOs")
class TestAMemberThatIsNotAFileIsNotOpened:
    """
    A FIFO passes every containment check: it is no link, it resolves inside
    the package, and it stats at size 0. Opening one for reading then waits for
    a writer that never comes, and the library export, the annotation export,
    metadata naming and cover extraction all froze on it.
    """

    def test_a_fifo_for_a_container_is_refused_not_waited_on(self, tmp_path):
        package = make_package(tmp_path / "lib", "Piped.epub")
        container = package / "META-INF" / "container.xml"
        container.unlink()
        os.mkfifo(container)

        raised = _within(5, container, lambda: validate.read_package_dir(package))

        assert isinstance(raised, validate.ValidationError)

    def test_the_rule_refuses_a_fifo_at_the_descriptor(self, tmp_path):
        # The check-time test can be raced: a file swapped for a FIFO between
        # the stat and the open. The open itself must not wait on one.
        fifo = tmp_path / "member.xhtml"
        os.mkfifo(fifo)

        raised = _within(5, fifo, lambda: contained.open_contained(fifo))

        assert isinstance(raised, OSError)

    def test_a_container_swapped_for_a_fifo_after_the_check_is_refused(
        self, tmp_path, monkeypatch
    ):
        package = make_package(tmp_path / "lib", "Swapped.epub")
        fifo = tmp_path / "swapped"
        os.mkfifo(fifo)
        monkeypatch.setattr(
            validate, "open_contained", lambda _path: contained.open_contained(fifo)
        )

        raised = _within(5, fifo, lambda: validate.read_package_dir(package))

        assert isinstance(raised, validate.ValidationError)
        assert "could not read" in str(raised)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="this platform has no FIFOs")
class TestVerifyChecksOnlyFiles:
    """
    --verify reads back whatever in the output directory is named ``*.epub``.
    A FIFO of that name froze it for ever, and a directory of that name was
    reported damaged, failing the run with advice to --force.
    """

    def test_a_fifo_is_refused_not_waited_on(self, tmp_path):
        fifo = tmp_path / "Piped.epub"
        os.mkfifo(fifo)

        problems = _within(5, fifo, lambda: validate.validate_archive(fifo))

        assert problems == ["not a regular file"]

    def test_a_directory_is_refused(self, tmp_path):
        (tmp_path / "Folder.epub").mkdir()

        assert validate.validate_archive(tmp_path / "Folder.epub") == [
            "not a regular file"
        ]

    def test_only_the_files_on_the_shelf_are_verified(self, tmp_path):
        shelf = tmp_path / "out"
        shelf.mkdir()
        _sound_epub(shelf / "Good.epub")
        os.mkfifo(shelf / "Piped.epub")
        (shelf / "Folder.epub").mkdir()

        verified = _within(
            5, shelf / "Piped.epub", lambda: inspect_output.verify_output(shelf)
        )

        assert verified == (1, 0, [])

    def test_a_directory_named_like_an_archive_does_not_fail_the_run(
        self, tmp_path, output_dir, capsys
    ):
        _sound_epub(output_dir / "Good.epub")
        (output_dir / "Folder.epub").mkdir()
        source_dir = tmp_path / "lib"
        source_dir.mkdir()

        code = run.main(
            ["-s", str(source_dir), "-o", str(output_dir), "--verify", "-q"]
        )

        assert code == 0
        assert "0 damaged" in capsys.readouterr().out


def _sound_epub(path: Path) -> Path:
    package = make_metadata_package(path.parent / "src", path.name, title="Good")
    archive.zip_package(package, path)
    return path


class TestAMemberThatGrowsWhileReadIsStillBounded:
    def test_the_read_stops_at_the_bound(self, tmp_path, monkeypatch):
        # The size is measured before the open, so a file that grows in
        # between used to be read whole, however large it had become.
        package = make_package(tmp_path / "lib", "Growing.epub")
        monkeypatch.setattr(validate, "MAX_XML_BYTES", 64)
        monkeypatch.setattr(
            validate, "open_contained", lambda _path: io.BytesIO(b" " * 1000)
        )

        with pytest.raises(validate.ValidationError, match="grew while read"):
            validate.read_package_dir(package)


def _symlink_loop(parent: Path, name: str) -> Path:
    """A package path that is a symlink to itself, or a skip where none can be."""
    loop = parent / name
    try:
        loop.symlink_to(loop.name)
    except (OSError, NotImplementedError):
        pytest.skip("this filesystem cannot hold a symlink")
    return loop


class TestASymlinkLoopCostsOneBook:
    """
    Python 3.10 to 3.12 raise RuntimeError, not OSError, resolving a symlink
    loop. Every reader of a package caught ValidationError and OSError, so one
    looped package took down the library export, the annotation export and
    metadata naming, where each should have lost one book.
    """

    def test_a_looped_package_is_an_unreadable_package(self, tmp_path):
        loop = _symlink_loop(tmp_path, "Loop.epub")

        with pytest.raises(validate.ValidationError):
            validate.read_package_dir(loop)

    def test_the_library_reads_a_looped_package_as_nothing(self, tmp_path):
        loop = _symlink_loop(tmp_path, "Loop.epub")

        assert read_package_once(loop, {}) is None


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

    def test_the_stub_walk_escapes_a_symlinked_directory_name(
        self, tmp_path, debug_records
    ):
        # has_dataless_files logged the entry's own name raw at -v, so a
        # directory called ESC[2K could erase the line that reported it.
        package = make_package(tmp_path / "lib", "Book.epub")
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (package / "OEBPS" / "\x1b[2KFAKE").symlink_to(elsewhere)

        assert source.has_dataless_files(package)
        assert debug_records
        assert not any("\x1b" in message for message in debug_records)

    def test_the_stub_walk_escapes_a_path_it_could_not_read(
        self, tmp_path, debug_records
    ):
        assert source.has_dataless_files(tmp_path / "\x1b[2KGone.epub")
        assert debug_records
        assert not any("\x1b" in message for message in debug_records)


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

        monkeypatch.setattr("epubconvert.run.convert.fcntl.flock", unsupported)
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

    @pytest.mark.skipif(not Path("/dev/full").exists(), reason="needs /dev/full")
    def test_a_full_volume_does_not_stop_the_run_at_the_lock(
        self, output_dir, monkeypatch
    ):
        # The holder's pid is diagnostic, and writing it was unguarded: on a
        # full volume ENOSPC escaped, again when the buffered handle closed,
        # and the run died with two tracebacks and exit 1 before --min-free
        # could stop it cleanly. /dev/full answers every write with ENOSPC; it
        # also refuses truncation, which a real full volume allows, so that is
        # let through.
        (output_dir / convert.LOCK_NAME).symlink_to("/dev/full")
        monkeypatch.setattr(os, "ftruncate", lambda _fd, _length: None)

        # Locked for real: only the note of who holds it was lost.
        with convert.output_lock(output_dir) as locked:
            assert locked


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

    @needs_permissions
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
            "epubconvert.export.inspect_output.shutil.disk_usage", unmeasurable
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


class TestAnUnscannableNameIsLoggedSafely:
    """A directory the walk cannot read is named in a warning, escaped."""

    @pytest.mark.parametrize(
        "scan", [archive.collect_package_dirs, archive.collect_copyable]
    )
    def test_the_name_in_the_warning_is_escaped(self, tmp_path, records, scan):
        # Regression: exc.filename went into the warning raw, so a directory
        # named with ESC[2K erased the line reporting it, and one holding an
        # undecodable byte made the log file's handler raise on the surrogate.
        hostile = tmp_path / "Shelf\x1b[2K\udcff"
        hostile.write_bytes(b"not a directory, so scandir fails on it")

        scan(hostile)

        assert records
        for text in records:
            assert text == printable(text), text


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


class TestEntityDeclarationsAreRefused:
    """
    A package document is attacker-controlled data. XML entity declarations
    let a small file expand into a large one -- the billion-laughs attack --
    and the size cap cannot see it, because the cap measures the file and the
    expansion happens after.

    expat has capped the amplification factor since 2.4, so this is already
    refused on a current Python. That protection is implicit, version-
    dependent and silent, and this tool supports Python 3.10+. The guard makes
    the rule the tool's own, and testable.

    Safe to enforce: of 2,804 package documents in a real library, one carries
    a DOCTYPE and none declares an entity.
    """

    @staticmethod
    def _package(tmp_path: Path, opf: str) -> Path:
        package = tmp_path / "Book.epub"
        (package / "META-INF").mkdir(parents=True)
        (package / "META-INF" / "container.xml").write_text(
            '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="c.opf"/></rootfiles></container>',
            encoding="utf-8",
        )
        (package / "c.opf").write_text(opf, encoding="utf-8")
        return package

    def test_an_expanding_entity_is_refused(self, tmp_path):
        opf = (
            '<?xml version="1.0"?>\n<!DOCTYPE package [\n'
            '<!ENTITY a "aaaaaaaaaa">\n'
            '<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">\n]>\n'
            '<package xmlns="http://www.idpf.org/2007/opf"><metadata '
            'xmlns:dc="http://purl.org/dc/elements/1.1/">'
            "<dc:title>&b;</dc:title></metadata><manifest/><spine/></package>"
        )

        with pytest.raises(validate.ValidationError, match="entities"):
            validate.read_package_dir(self._package(tmp_path, opf))

    def test_a_parameter_entity_is_refused(self, tmp_path):
        opf = (
            '<?xml version="1.0"?>\n<!DOCTYPE package [<!ENTITY % a "x">]>\n'
            '<package xmlns="http://www.idpf.org/2007/opf"><metadata/>'
            "<manifest/><spine/></package>"
        )

        with pytest.raises(validate.ValidationError, match="entities"):
            validate.read_package_dir(self._package(tmp_path, opf))

    def test_a_quoted_angle_bracket_does_not_end_the_scan(self, tmp_path):
        # A SYSTEM identifier may contain ">". Walking to the first unbracketed
        # ">" stopped inside the quotes and missed the declaration that
        # followed, while expat parsed the document perfectly happily.
        opf = (
            '<?xml version="1.0"?>\n'
            '<!DOCTYPE package SYSTEM "a>b" [<!ENTITY x "yyy">]>\n'
            '<package xmlns="http://www.idpf.org/2007/opf"><metadata '
            'xmlns:dc="http://purl.org/dc/elements/1.1/">'
            "<dc:title>&x;</dc:title></metadata><manifest/><spine/></package>"
        )

        with pytest.raises(validate.ValidationError, match="entities"):
            validate.read_package_dir(self._package(tmp_path, opf))

    def test_a_single_quoted_angle_bracket_is_handled_too(self, tmp_path):
        opf = (
            '<?xml version="1.0"?>\n'
            "<!DOCTYPE package SYSTEM 'a>b' [<!ENTITY x \"yyy\">]>\n"
            '<package xmlns="http://www.idpf.org/2007/opf"><metadata/>'
            "<manifest/><spine/></package>"
        )

        with pytest.raises(validate.ValidationError, match="entities"):
            validate.read_package_dir(self._package(tmp_path, opf))

    def test_a_doctype_without_entities_is_allowed(self, tmp_path):
        # One real book in a 2,804-package library has a bare DOCTYPE.
        opf = (
            '<?xml version="1.0"?>\n<!DOCTYPE package>\n'
            '<package xmlns="http://www.idpf.org/2007/opf"><metadata '
            'xmlns:dc="http://purl.org/dc/elements/1.1/">'
            "<dc:title>Real Book</dc:title></metadata>"
            '<manifest><item id="t" href="t.xhtml" media-type="text/html"/></manifest>'
            '<spine><itemref idref="t"/></spine></package>'
        )

        assert validate.read_package_dir(self._package(tmp_path, opf)).title == (
            "Real Book"
        )

    def test_entity_like_text_in_the_body_is_not_a_declaration(self, tmp_path):
        # The guard reads the DOCTYPE declaration, not the whole document, so
        # a book that merely writes about entities still parses.
        opf = (
            '<?xml version="1.0"?>\n'
            '<package xmlns="http://www.idpf.org/2007/opf"><metadata '
            'xmlns:dc="http://purl.org/dc/elements/1.1/">'
            "<dc:title>On &lt;!ENTITY a&gt; and other XML</dc:title></metadata>"
            '<manifest><item id="t" href="t.xhtml" media-type="text/html"/></manifest>'
            '<spine><itemref idref="t"/></spine></package>'
        )

        assert validate.read_package_dir(self._package(tmp_path, opf)).title == (
            "On <!ENTITY a> and other XML"
        )

    def test_the_container_is_guarded_too(self, tmp_path):
        package = tmp_path / "Book.epub"
        (package / "META-INF").mkdir(parents=True)
        (package / "META-INF" / "container.xml").write_text(
            '<?xml version="1.0"?>\n<!DOCTYPE container [<!ENTITY a "x">]>\n'
            '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="c.opf"/></rootfiles></container>',
            encoding="utf-8",
        )

        with pytest.raises(validate.ValidationError, match="entities"):
            validate.read_package_dir(package)

    def test_an_archive_is_guarded_the_same_way(self, tmp_path):
        # Both readers go through the same member-reading path, so the guard
        # must not need writing twice.
        opf = (
            '<?xml version="1.0"?>\n<!DOCTYPE package [<!ENTITY a "x">]>\n'
            '<package xmlns="http://www.idpf.org/2007/opf"><metadata/>'
            "<manifest/><spine/></package>"
        )
        path = tmp_path / "Book.epub"
        with ZipFile(path, "w") as opened:
            opened.writestr("mimetype", "application/epub+zip")
            opened.writestr(
                "META-INF/container.xml",
                '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                '<rootfiles><rootfile full-path="c.opf"/></rootfiles></container>',
            )
            opened.writestr("c.opf", opf)

        with (
            ZipFile(path) as archive_file,
            pytest.raises(validate.ValidationError, match="entities"),
        ):
            validate.read_package(archive_file)

    def test_a_comment_holding_a_decoy_doctype_does_not_hide_a_declaration(
        self, tmp_path
    ):
        # The second bypass of a hand-written scan: the scan found the decoy
        # inside the comment, stopped at the comment's own ">", and never
        # reached the real declaration. expat is not fooled, so it is asked.
        opf = (
            "<!--decoy <!DOCTYPE z -->\n"
            '<!DOCTYPE package [<!ENTITY x "PAYLOAD">]>\n'
            '<package xmlns="http://www.idpf.org/2007/opf"><metadata '
            'xmlns:dc="http://purl.org/dc/elements/1.1/">'
            "<dc:title>&x;</dc:title></metadata><manifest/><spine/></package>"
        )

        with pytest.raises(validate.ValidationError, match="entities"):
            validate.read_package_dir(self._package(tmp_path, opf))

    def test_an_external_entity_is_refused_as_a_declaration(self, tmp_path):
        opf = (
            '<!DOCTYPE package [<!ENTITY x SYSTEM "file:///etc/passwd">]>\n'
            '<package xmlns="http://www.idpf.org/2007/opf"><metadata/>'
            "<manifest/><spine/></package>"
        )

        with pytest.raises(validate.ValidationError, match="entities"):
            validate.read_package_dir(self._package(tmp_path, opf))
