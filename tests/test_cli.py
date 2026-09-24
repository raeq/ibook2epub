"""Tests for argument parsing, logging setup and the top-level entry point."""

# Test names describe the behaviour under test; separate docstrings would only
# restate them. Explicit empty-list comparisons read better than truthiness here.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=use-implicit-booleaness-not-comparison

import logging
import os
from pathlib import Path

import pytest

from epubconvert.run import cli, convert, run
from epubconvert.utils import app_logger, defaults, exits
from tests.conftest import make_package


class TestParseArgs:
    """Command line parsing and validation."""

    def test_defaults(self, library):
        args = cli.parse_args(["-s", str(library)])

        assert args.max_export_files == defaults.DEFAULT_MAX_EXPORT_FILES
        assert args.output_dir == defaults.DEFAULT_OUTPUT
        assert args.dry_run is False
        assert args.verbose == 0

    def test_short_flags(self, library, tmp_path):
        args = cli.parse_args(
            ["-s", str(library), "-o", str(tmp_path / "out"), "-m", "0", "-d"]
        )

        assert args.source_dir == library
        assert args.output_dir == tmp_path / "out"
        assert args.max_export_files == 0
        assert args.dry_run is True

    def test_output_dir_need_not_exist_yet(self, library, tmp_path):
        # Regression: the old click.Path(exists=True) rejected an output
        # directory the program was perfectly capable of creating.
        args = cli.parse_args(
            ["-s", str(library), "-o", str(tmp_path / "brand" / "new")]
        )

        assert not args.output_dir.exists()

    def test_output_inside_source_is_rejected(self, library):
        # Regression: .part files and finished exports landed inside the tree
        # being scanned, polluting the next run's picture of the library.
        with pytest.raises(SystemExit):
            cli.parse_args(["-s", str(library), "-o", str(library / "out")])

    def test_output_equal_to_source_is_rejected(self, library):
        with pytest.raises(SystemExit):
            cli.parse_args(["-s", str(library), "-o", str(library)])

    def test_output_beside_source_is_allowed(self, library, tmp_path):
        args = cli.parse_args(["-s", str(library), "-o", str(tmp_path / "out")])

        assert args.output_dir == tmp_path / "out"

    def test_a_missing_source_dir_is_not_argparse_business(self, tmp_path):
        # Environment checks moved to run.main so each can carry its own exit
        # code; parser.error always exits 2 and would collapse them again.
        args = cli.parse_args(["-s", str(tmp_path / "absent")])

        assert args.source_dir == tmp_path / "absent"

    def test_a_missing_source_dir_has_its_own_exit_code(self, tmp_path):
        code = run.main(
            ["-s", str(tmp_path / "absent"), "-o", str(tmp_path / "out"), "-q"]
        )

        assert code == exits.NO_SOURCE

    @pytest.mark.parametrize("where", ["-o", "-s"])
    @pytest.mark.parametrize("below", ["", "books"])
    def test_a_symlink_loop_is_the_run_s_business_not_a_traceback(
        self, library, tmp_path, where, below
    ):
        # Path.resolve() raises RuntimeError on a symlink loop on Python 3.10
        # to 3.12, out of argument parsing: a traceback and exit 1.
        loop = tmp_path / "loop"
        loop.symlink_to(loop)
        given = {"-s": str(library), "-o": str(tmp_path / "out")}
        given[where] = str(loop / below) if below else str(loop)

        args = cli.parse_args([part for pair in given.items() for part in pair])

        parsed = args.output_dir if where == "-o" else args.source_dir
        assert str(parsed) == given[where]

    def test_a_library_behind_a_symlink_loop_is_a_missing_library(self, tmp_path):
        loop = tmp_path / "loop"
        loop.symlink_to(loop)

        code = run.main(["-s", str(loop), "-o", str(tmp_path / "out"), "-q"])

        assert code == exits.NO_SOURCE

    @pytest.mark.parametrize("below", ["", "books"])
    @pytest.mark.parametrize("dry_run", [[], ["-d"]])
    def test_an_output_path_through_a_symlink_loop_cannot_be_created(
        self, library, tmp_path, below, dry_run
    ):
        # The real run fails at mkdir with 5. The dry run judged the nearest
        # part of the path that exists(), which a loop never does, so it looked
        # past the loop to its writable parent and exited 0.
        loop = tmp_path / "loop"
        loop.symlink_to(loop)
        output = loop / below if below else loop

        code = run.main(["-s", str(library), "-o", str(output), "-q", *dry_run])

        assert code == exits.NO_OUTPUT

    def test_negative_cap_is_rejected(self, library):
        with pytest.raises(SystemExit):
            cli.parse_args(["-s", str(library), "-m", "-1"])

    def test_a_negative_floor_is_rejected(self, library, capsys):
        # Accepted, it disabled the floor as 0 does, without saying so.
        with pytest.raises(SystemExit) as refused:
            cli.parse_args(["-s", str(library), "--min-free", "-1"])

        assert refused.value.code == exits.USAGE
        assert "--min-free must be 0 or greater" in capsys.readouterr().err

    def test_verbosity_accumulates(self, library):
        args = cli.parse_args(["-s", str(library), "-vv"])

        assert args.verbose == 2

    @pytest.mark.parametrize("width", [51, 69, 72, 75, 79, 120])
    def test_help_documents_the_no_limit_sentinel(self, monkeypatch, width):
        # The README quotes this help text; keep them honest about the
        # sentinel. "0=no limit" had a space argparse could wrap at, and did
        # at some widths, which split it across two lines.
        monkeypatch.setenv("COLUMNS", str(width))
        help_text = cli.build_parser().format_help()

        assert "0=unlimited" in help_text


class TestLoggerConfiguration:
    """Verbosity mapping and handler management."""

    @pytest.mark.parametrize(
        "verbosity,expected",
        [
            (0, logging.WARNING),
            (1, logging.INFO),
            (2, logging.DEBUG),
            (3, app_logger.TRACE),
            (9, app_logger.TRACE),
        ],
    )
    def test_verbosity_maps_to_level(self, verbosity, expected):
        assert app_logger.level_for_verbosity(verbosity) == expected

    def test_debug_records_are_emitted_at_verbosity_two(self, capsys):
        # Regression: the logger used to be pinned to INFO, so every
        # logger.debug() and logger.trace() call in the codebase was dead.
        app_logger.configure(verbosity=2)

        app_logger.logger.debug("a debug message")

        assert "a debug message" in capsys.readouterr().err

    def test_debug_records_are_suppressed_by_default(self, capsys):
        app_logger.configure(verbosity=1)

        app_logger.logger.debug("a debug message")

        assert "a debug message" not in capsys.readouterr().err

    def test_trace_level_is_available(self, capsys):
        app_logger.configure(verbosity=3)

        app_logger.logger.trace("a trace message")

        captured = capsys.readouterr().err
        assert "a trace message" in captured
        assert "TRACE" in captured

    def test_quiet_suppresses_info(self, capsys):
        app_logger.configure(verbosity=0)

        app_logger.logger.info("an info message")
        app_logger.logger.warning("a warning message")

        captured = capsys.readouterr().err
        assert "an info message" not in captured
        assert "a warning message" in captured

    def test_configure_is_idempotent(self):
        app_logger.configure(verbosity=1)
        first = len(app_logger.logger.handlers)
        app_logger.configure(verbosity=1)

        assert len(app_logger.logger.handlers) == first

    def test_no_log_file_is_written_unless_requested(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)

        app_logger.configure(verbosity=1)
        app_logger.logger.info("hello")

        assert not (tmp_path / "app.log").exists()

    def test_log_file_is_written_when_requested(self, tmp_path):
        log_path = tmp_path / "logs" / "app.log"

        app_logger.configure(verbosity=1, log_file=log_path)
        app_logger.logger.info("hello from the log file")
        app_logger.configure(verbosity=1)  # Close the file handler.

        assert "hello from the log file" in log_path.read_text(encoding="utf-8")

    def test_a_name_that_is_not_text_still_reaches_the_log_file(self, tmp_path):
        # os.walk hands back an undecodable filename as lone surrogates, which
        # a strict UTF-8 file handler cannot write: the line was dropped from
        # the transcript and a traceback printed in its place.
        log_path = tmp_path / "app.log"

        app_logger.configure(verbosity=1, log_file=log_path)
        app_logger.logger.warning("Could not read %s", "Caf\udce9.epub")
        app_logger.configure(verbosity=1)  # Close the file handler.

        assert "Could not read Caf" in log_path.read_text(encoding="utf-8")


class TestMain:
    """End-to-end runs through the entry point."""

    def test_creates_a_missing_output_directory(self, library, tmp_path, capsys):
        output = tmp_path / "brand" / "new"

        code = run.main(
            ["-s", str(library), "-o", str(output), "-m", "0", "--no-shuffle"]
        )

        assert code == 0
        assert output.is_dir()
        assert len(list(output.glob("*.epub"))) == 2
        assert "Exported 2" in capsys.readouterr().out

    def test_dry_run_touches_nothing(self, library, output_dir, capsys):
        code = run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-d"])

        out = capsys.readouterr().out
        assert code == 0
        assert list(output_dir.iterdir()) == []
        # Regression: dry runs used to print "Exported N epub files" having
        # written nothing at all.
        assert "would export 2" in out
        assert "Exported" not in out

    def test_dry_run_does_not_create_the_output_directory(self, library, tmp_path):
        output = tmp_path / "never"

        run.main(["-s", str(library), "-o", str(output), "-d"])

        assert not output.exists()

    def test_cap_limits_the_export(self, library, output_dir):
        run.main(["-s", str(library), "-o", str(output_dir), "-m", "1"])

        assert len(list(output_dir.glob("*.epub"))) == 1

    def test_rerunning_is_safe(self, library, output_dir, capsys):
        argv = ["-s", str(library), "-o", str(output_dir), "-m", "0", "--no-shuffle"]
        run.main(argv)
        capsys.readouterr()

        code = run.main(argv)

        assert code == 0
        assert "Exported 0" in capsys.readouterr().out
        assert len(list(output_dir.glob("*.epub"))) == 2

    def test_failure_produces_a_non_zero_exit_code(
        self, library, output_dir, monkeypatch
    ):
        def boom(_name):
            raise OSError("disk fell over")

        monkeypatch.setattr("epubconvert.export.archive.is_excluded", boom)

        code = run.main(["-s", str(library), "-o", str(output_dir), "-m", "0"])

        assert code == 1

    def test_empty_source_directory_succeeds_quietly(self, tmp_path, output_dir):
        empty = tmp_path / "empty"
        empty.mkdir()

        code = run.main(["-s", str(empty), "-o", str(output_dir)])

        assert code == 0
        # The run lock is expected; no books should have been written.
        assert list(output_dir.glob("*.epub")) == []


class TestSourceDiscovery:
    def test_prefers_a_candidate_holding_books(self, tmp_path, monkeypatch):
        empty = tmp_path / "empty"
        stocked = tmp_path / "stocked"
        empty.mkdir()
        make_package(stocked, "Dune.epub")
        monkeypatch.setattr(defaults, "SOURCE_CANDIDATES", (empty, stocked))

        assert defaults.discover_source() == stocked

    def test_falls_back_to_one_that_exists(self, tmp_path, monkeypatch):
        empty = tmp_path / "empty"
        empty.mkdir()
        missing = tmp_path / "missing"
        monkeypatch.setattr(defaults, "SOURCE_CANDIDATES", (empty, missing))

        assert defaults.discover_source() == empty

    def test_a_candidate_macos_refuses_to_examine_is_passed_over(
        self, tmp_path, monkeypatch
    ):
        # The second home is inside another app's container, which macOS
        # answers with EPERM without Full Disk Access. is_dir() swallows only
        # "absent" errors, so discovery raised a traceback out of parse_args
        # even when the library was in the first home.
        stocked = tmp_path / "stocked"
        make_package(stocked, "Dune.epub")
        refused = tmp_path / "refused"
        monkeypatch.setattr(defaults, "SOURCE_CANDIDATES", (stocked, refused))
        original = Path.is_dir

        def is_dir(self):
            if self == refused:
                raise PermissionError(1, "Operation not permitted", str(self))
            return original(self)

        monkeypatch.setattr(Path, "is_dir", is_dir)

        assert defaults.discover_source() == stocked

    def test_falls_back_to_the_default_when_nothing_exists(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            defaults, "SOURCE_CANDIDATES", (tmp_path / "a", tmp_path / "b")
        )

        assert defaults.discover_source() == defaults.DEFAULT_SOURCE

    def test_explicit_source_skips_discovery(self, library):
        args = cli.parse_args(["-s", str(library)])

        assert args.source_dir == library
        assert args.source_auto is False


class TestLockDiagnostics:
    def test_lock_file_records_the_pid(self, output_dir):
        pytest.importorskip("fcntl", reason="advisory locking needs fcntl")
        with convert.output_lock(output_dir):
            contents = (output_dir / convert.LOCK_NAME).read_text(encoding="utf-8")

        assert f"pid={os.getpid()}" in contents

    def test_contended_error_names_the_holder(self, output_dir):
        pytest.importorskip("fcntl", reason="advisory locking needs fcntl")
        with (
            convert.output_lock(output_dir),
            pytest.raises(convert.OutputLockedError) as excinfo,
            convert.output_lock(output_dir),
        ):
            pass

        assert f"pid={os.getpid()}" in str(excinfo.value)

    def test_the_holder_is_quoted_escaped(self, output_dir):
        # Read back from the output directory, so anyone who can write there
        # decides what a refused run prints: it was printed raw.
        pytest.importorskip("fcntl", reason="advisory locking needs fcntl")
        with convert.output_lock(output_dir):
            (output_dir / convert.LOCK_NAME).write_text(
                "pid=\x1b[2Kforged host=\x9b31mX\n", encoding="utf-8"
            )
            with (
                pytest.raises(convert.OutputLockedError) as excinfo,
                convert.output_lock(output_dir),
            ):
                pass

        assert "forged" in str(excinfo.value)
        assert not {"\x1b", "\x9b"} & set(str(excinfo.value))

    def test_a_holder_that_is_not_text_is_still_a_held_lock(self, output_dir):
        # A host name is bytes the system chose, and the lock file anyone's:
        # reading it as UTF-8 raised UnicodeDecodeError, a traceback and exit
        # 1 where a held lock exits 3.
        pytest.importorskip("fcntl", reason="advisory locking needs fcntl")
        with convert.output_lock(output_dir):
            (output_dir / convert.LOCK_NAME).write_bytes(b"pid=1 host=\xff\xfe\n")
            with (
                pytest.raises(convert.OutputLockedError) as excinfo,
                convert.output_lock(output_dir),
            ):
                pass

        assert "pid=1 host=\\udcff\\udcfe" in str(excinfo.value)

    def test_a_stale_lock_file_does_not_block(self, output_dir):
        # flock is released by the kernel when the holder dies, so a lock file
        # left behind by a killed run is inert. No PID liveness check needed.
        pytest.importorskip("fcntl", reason="advisory locking needs fcntl")
        (output_dir / convert.LOCK_NAME).write_text("pid=999999 host=ghost\n")

        with convert.output_lock(output_dir):
            pass  # acquired without complaint


class TestAReportIgnoresNoTypedFlag:
    """
    ``--list`` and ``--verify`` refused only annotation flags beside them, so
    a conversion flag either report never consults was dropped without a word:
    ``--verify --match X`` verified, and exited 7 for, books outside X.
    Judged against the parser's defaults, as the convert-nothing modes are.
    """

    @pytest.mark.parametrize(
        "other",
        [
            ["--match", "hobbit"],
            ["-f"],
            ["--covers"],
            ["--refresh"],
            ["--skip-incomplete"],
            ["-m", "1"],
            ["--workers", "3"],
            ["--min-free", "1"],
            ["--no-copy-through"],
            ["--no-shuffle"],
            # It names nothing: it opens whatever *.epub the shelf holds.
            ["-p"],
            ["--portable-names", "romanize"],
            ["--name-by", "author-title"],
            ["--on-collision", "suffix"],
            # It writes nothing either way, and it reads no annotations.
            ["-d"],
            ["-an"],
        ],
    )
    def test_verify_refuses_what_it_never_consults(self, other):
        # It checks every archive in the output directory. --match names
        # books in the library to convert, not archives on the shelf, whose
        # names differ under any metadata naming policy.
        with pytest.raises(SystemExit) as refused:
            cli.parse_args(["--verify", *other])

        assert refused.value.code == 2

    @pytest.mark.parametrize("check", [["--epubcheck"], ["--validate"]])
    def test_verify_keeps_the_checks_it_runs(self, check):
        assert cli.parse_args(["--verify", *check]).verify

    @pytest.mark.parametrize(
        "other",
        [
            ["--covers"],
            ["--validate"],
            ["--epubcheck"],
            ["-m", "1"],
            ["--min-free", "1"],
            ["--no-shuffle"],
            ["-d"],
            ["-an"],
        ],
    )
    def test_list_refuses_what_it_never_consults(self, other):
        # "--list --covers --validate -m 1 --min-free 1 --no-shuffle" listed
        # every book exactly as a bare --list did, and exited 0.
        with pytest.raises(SystemExit) as refused:
            cli.parse_args(["--list", *other])

        assert refused.value.code == 2

    @pytest.mark.parametrize(
        "shaping",
        [
            ["--match", "hobbit"],
            ["-f"],
            ["--refresh"],
            ["--skip-incomplete"],
            ["--no-copy-through"],
            ["--workers", "3"],
        ],
    )
    def test_list_keeps_what_changes_the_listing(self, shaping):
        # --match narrows it; -f, --refresh and --skip-incomplete change the
        # status the planner gives a book; --no-copy-through changes what is
        # claimed and so what is an orphan; --workers sizes the pool that
        # names the files copied through.
        assert cli.parse_args(["--list", *shaping]).list_only

    def test_a_flag_typed_as_its_default_changes_nothing_and_passes(self):
        default = str(defaults.DEFAULT_MAX_EXPORT_FILES)

        assert cli.parse_args(["--verify", "-m", default]).verify
