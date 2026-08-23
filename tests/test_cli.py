"""Tests for argument parsing, logging setup and the top-level entry point."""

# Test names describe the behaviour under test; separate docstrings would only
# restate them. Explicit empty-list comparisons read better than truthiness here.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=use-implicit-booleaness-not-comparison

import logging

import pytest

from epubconvert import app_logger, convert


class TestParseArgs:
    """Command line parsing and validation."""

    def test_defaults(self, library):
        args = convert.parse_args(["-s", str(library)])

        assert args.max_export_files == convert.DEFAULT_MAX_EXPORT_FILES
        assert args.output_dir == convert.DEFAULT_OUTPUT
        assert args.dry_run is False
        assert args.verbose == 0

    def test_short_flags(self, library, tmp_path):
        args = convert.parse_args(
            ["-s", str(library), "-o", str(tmp_path / "out"), "-m", "0", "-d"]
        )

        assert args.source_dir == library
        assert args.output_dir == tmp_path / "out"
        assert args.max_export_files == 0
        assert args.dry_run is True

    def test_output_dir_need_not_exist_yet(self, library, tmp_path):
        # Regression: the old click.Path(exists=True) rejected an output
        # directory the program was perfectly capable of creating.
        args = convert.parse_args(
            ["-s", str(library), "-o", str(tmp_path / "brand" / "new")]
        )

        assert not args.output_dir.exists()

    def test_missing_source_dir_is_rejected(self, tmp_path):
        with pytest.raises(SystemExit):
            convert.parse_args(["-s", str(tmp_path / "absent")])

    def test_negative_cap_is_rejected(self, library):
        with pytest.raises(SystemExit):
            convert.parse_args(["-s", str(library), "-m", "-1"])

    def test_verbosity_accumulates(self, library):
        args = convert.parse_args(["-s", str(library), "-vv"])

        assert args.verbose == 2

    def test_help_documents_the_no_limit_sentinel(self):
        # The README quotes this help text; keep them honest about 0=no limit.
        help_text = convert.build_parser().format_help()

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

        code = convert.main(
            ["-s", str(library), "-o", str(output), "-m", "0", "--no-shuffle"]
        )

        assert code == 0
        assert output.is_dir()
        assert len(list(output.glob("*.epub"))) == 2
        assert "Exported 2" in capsys.readouterr().out

    def test_dry_run_touches_nothing(self, library, output_dir, capsys):
        code = convert.main(
            ["-s", str(library), "-o", str(output_dir), "-m", "0", "-d"]
        )

        out = capsys.readouterr().out
        assert code == 0
        assert list(output_dir.iterdir()) == []
        # Regression: dry runs used to print "Exported N epub files" having
        # written nothing at all.
        assert "would export 2" in out
        assert "Exported" not in out

    def test_dry_run_does_not_create_the_output_directory(self, library, tmp_path):
        output = tmp_path / "never"

        convert.main(["-s", str(library), "-o", str(output), "-d"])

        assert not output.exists()

    def test_cap_limits_the_export(self, library, output_dir):
        convert.main(["-s", str(library), "-o", str(output_dir), "-m", "1"])

        assert len(list(output_dir.glob("*.epub"))) == 1

    def test_rerunning_is_safe(self, library, output_dir, capsys):
        argv = ["-s", str(library), "-o", str(output_dir), "-m", "0", "--no-shuffle"]
        convert.main(argv)
        capsys.readouterr()

        code = convert.main(argv)

        assert code == 0
        assert "Exported 0" in capsys.readouterr().out
        assert len(list(output_dir.glob("*.epub"))) == 2

    def test_failure_produces_a_non_zero_exit_code(
        self, library, output_dir, monkeypatch
    ):
        def boom(_name):
            raise OSError("disk fell over")

        monkeypatch.setattr(convert, "is_excluded", boom)

        code = convert.main(["-s", str(library), "-o", str(output_dir), "-m", "0"])

        assert code == 1

    def test_empty_source_directory_succeeds_quietly(self, tmp_path, output_dir):
        empty = tmp_path / "empty"
        empty.mkdir()

        code = convert.main(["-s", str(empty), "-o", str(output_dir)])

        assert code == 0
        assert list(output_dir.iterdir()) == []
