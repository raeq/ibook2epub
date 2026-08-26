"""
Tests for the optional external ``epubcheck`` validator.

It is a much stricter check than the structural one, it needs a JVM, and almost
nobody has it installed -- so every branch past "is it on PATH" ran
unexercised. The subprocess is mocked rather than the tool installed, which
keeps the suite dependency-free while still pinning what the wrapper does with
what the tool says.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them. Explicit empty-list comparisons read better than truthiness here.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=use-implicit-booleaness-not-comparison,too-few-public-methods

import subprocess

from epubconvert import cli, exits, run, validate


class TestEpubcheck:
    def test_availability_probe_does_not_raise(self):
        assert isinstance(validate.epubcheck_available(), bool)

    def test_missing_tool_is_reported(self, tmp_path, monkeypatch):
        monkeypatch.setattr("epubconvert.validate.shutil.which", lambda _name: None)

        assert validate.run_epubcheck(tmp_path / "x.epub") == [
            "epubcheck is not on PATH"
        ]

    def test_a_missing_tool_has_its_own_exit_code(self, library, tmp_path, monkeypatch):
        # Was a usage error, indistinguishable from a typo'd flag. Whether the
        # tool is installed is a fact about the machine, not about the command
        # line, so it is checked in run.main and carries its own code.
        monkeypatch.setattr("epubconvert.run.epubcheck_available", lambda: False)

        code = run.main(
            ["-s", str(library), "-o", str(tmp_path / "out"), "--epubcheck", "-q"]
        )

        assert code == exits.MISSING_TOOL

    def test_flag_implies_validate(self, library):
        args = cli.parse_args(["-s", str(library), "--epubcheck"])

        assert args.validate is True


class _Completed:
    """A stand-in for ``subprocess.CompletedProcess``."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestRunningEpubcheck:
    """
    ``epubcheck`` is an optional external tool and is rarely installed, so
    every branch past "is it on PATH" ran unexercised. Mocking the subprocess
    keeps the suite free of a JVM while still pinning what the wrapper does
    with what the tool says.
    """

    @staticmethod
    def _installed(
        monkeypatch, result=None, raises=None
    ) -> list[tuple[list[str], dict[str, object]]]:
        """Pretend the tool is installed, and record how it was invoked."""
        calls: list[tuple[list[str], dict[str, object]]] = []
        monkeypatch.setattr(
            "epubconvert.validate.shutil.which", lambda _name: "/usr/bin/epubcheck"
        )

        def fake_run(command, **options):
            calls.append((command, options))
            if raises is not None:
                raise raises
            return result

        monkeypatch.setattr("epubconvert.validate.subprocess.run", fake_run)
        return calls

    def test_a_clean_archive_reports_nothing(self, tmp_path, monkeypatch):
        self._installed(monkeypatch, _Completed(returncode=0))

        assert validate.run_epubcheck(tmp_path / "Book.epub") == []

    def test_error_lines_are_returned_stripped(self, tmp_path, monkeypatch):
        stderr = (
            "  ERROR(RSC-005): bad markup\nINFO: checking\n  ERROR(OPF-030): oops\n"
        )
        self._installed(monkeypatch, _Completed(returncode=1, stderr=stderr))

        assert validate.run_epubcheck(tmp_path / "Book.epub") == [
            "ERROR(RSC-005): bad markup",
            "ERROR(OPF-030): oops",
        ]

    def test_stdout_is_read_when_stderr_is_empty(self, tmp_path, monkeypatch):
        # The tool writes to either stream depending on how it was built.
        self._installed(
            monkeypatch, _Completed(returncode=1, stdout="ERROR(PKG-001): broken")
        )

        assert validate.run_epubcheck(tmp_path / "Book.epub") == [
            "ERROR(PKG-001): broken"
        ]

    def test_a_failure_with_no_error_lines_names_the_exit_code(
        self, tmp_path, monkeypatch
    ):
        # Silence plus a non-zero status still has to say something, or the
        # archive is reported as failing validation for no stated reason.
        self._installed(monkeypatch, _Completed(returncode=3, stderr="WARNING: hmm"))

        assert validate.run_epubcheck(tmp_path / "Book.epub") == [
            "epubcheck failed with exit code 3"
        ]

    def test_at_most_ten_errors_are_reported(self, tmp_path, monkeypatch):
        stderr = "\n".join(f"ERROR({index}): bad" for index in range(25))
        self._installed(monkeypatch, _Completed(returncode=1, stderr=stderr))

        assert len(validate.run_epubcheck(tmp_path / "Book.epub")) == 10

    def test_a_tool_that_cannot_be_run_is_reported(self, tmp_path, monkeypatch):
        self._installed(monkeypatch, raises=OSError("Exec format error"))

        problems = validate.run_epubcheck(tmp_path / "Book.epub")

        assert problems == ["epubcheck could not be run: Exec format error"]

    def test_a_timeout_is_reported_rather_than_raised(self, tmp_path, monkeypatch):
        # A book big enough to exceed the timeout must fail the check, not the
        # run: everything already converted stays converted.
        self._installed(monkeypatch, raises=subprocess.TimeoutExpired("epubcheck", 120))

        problems = validate.run_epubcheck(tmp_path / "Book.epub")

        assert len(problems) == 1
        assert problems[0].startswith("epubcheck could not be run:")

    def test_the_tool_is_invoked_on_the_archive_with_no_shell(
        self, tmp_path, monkeypatch
    ):
        # A fixed executable and a list argument, so a filename containing
        # shell metacharacters is an argument rather than a command.
        archive_path = tmp_path / "Book; rm -rf x.epub"
        calls = self._installed(monkeypatch, _Completed(returncode=0))

        validate.run_epubcheck(archive_path)

        command, options = calls[0]
        assert command == ["/usr/bin/epubcheck", str(archive_path)]
        assert "shell" not in options

    def test_the_timeout_is_passed_through_and_failure_is_not_raised(
        self, tmp_path, monkeypatch
    ):
        # check=False because a non-zero status is the answer, not an error:
        # raising here would abort a run over thousands of books on one bad
        # archive.
        calls = self._installed(monkeypatch, _Completed(returncode=0))

        validate.run_epubcheck(tmp_path / "Book.epub", timeout=7)

        _command, options = calls[0]
        assert options["timeout"] == 7
        assert options["check"] is False
        assert options["capture_output"] is True
