"""Tests for argument parsing, logging setup and the top-level entry point."""

# Test names describe the behaviour under test; separate docstrings would only
# restate them. Explicit empty-list comparisons read better than truthiness here.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=use-implicit-booleaness-not-comparison

import logging
import os

import pytest

from epubconvert import app_logger, cli, convert, defaults, exits, run
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

    def test_negative_cap_is_rejected(self, library):
        with pytest.raises(SystemExit):
            cli.parse_args(["-s", str(library), "-m", "-1"])

    def test_verbosity_accumulates(self, library):
        args = cli.parse_args(["-s", str(library), "-vv"])

        assert args.verbose == 2

    def test_help_documents_the_no_limit_sentinel(self):
        # The README quotes this help text; keep them honest about 0=no limit.
        help_text = cli.build_parser().format_help()

        assert "0=no limit" in help_text


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

        monkeypatch.setattr("epubconvert.archive.is_excluded", boom)

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

    def test_a_stale_lock_file_does_not_block(self, output_dir):
        # flock is released by the kernel when the holder dies, so a lock file
        # left behind by a killed run is inert. No PID liveness check needed.
        pytest.importorskip("fcntl", reason="advisory locking needs fcntl")
        (output_dir / convert.LOCK_NAME).write_text("pid=999999 host=ghost\n")

        with convert.output_lock(output_dir):
            pass  # acquired without complaint
