"""
Tests for what ships, and for the version the shipped thing reports.

A release is the one artefact nobody can patch after the fact: PyPI does not
allow re-uploading a file. These check the two claims a release makes about
itself before it is cut.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=too-few-public-methods

import os
import subprocess
import sys
from importlib import metadata
from pathlib import Path

import pytest

import epubconvert
from epubconvert import annotations
from tests.conftest import needs_permissions

ROOT = Path(__file__).resolve().parent.parent


class TestTheReportedVersionIsTheInstalledOne:
    """--version must not be able to lie."""

    def test_the_package_version_matches_the_distribution(self):
        # The publish workflow gates the release tag against pyproject.toml.
        # Nothing tied epubconvert.__version__ to either, so --version could
        # report one number while the installed distribution was another.
        assert epubconvert.__version__ == metadata.version("ibook2epub")


class TestTheSourceDistributionIsComplete:
    """A test suite that ships must be a test suite that runs."""

    def test_every_test_support_file_is_declared(self):
        # setuptools auto-includes test*.py by a legacy rule, which picks up
        # tests/test_*.py and silently leaves out conftest.py and __init__.py
        # -- and every fixture lives in conftest. A packager running the suite
        # from the sdist gets collection errors, not tests.
        manifest = ROOT / "MANIFEST.in"

        assert manifest.is_file(), "MANIFEST.in is what completes the sdist"
        text = manifest.read_text(encoding="utf-8")
        assert "recursive-include tests" in text

    def test_the_support_files_that_must_ship_exist(self):
        assert (ROOT / "tests" / "conftest.py").is_file()
        assert (ROOT / "tests" / "__init__.py").is_file()


@needs_permissions
class TestPermissionsAreMeaningfulForThisUser:
    """A sanity check that the permission tests elsewhere can mean something."""

    def test_an_unwritable_directory_is_really_unwritable(self, tmp_path):
        blocked = tmp_path / "blocked"
        blocked.mkdir()
        blocked.chmod(0o555)
        try:
            with pytest.raises(OSError):
                (blocked / "file.txt").write_text("x", encoding="utf-8")
        finally:
            blocked.chmod(0o755)


class TestTheSchemaIsShipped:
    """
    `annotations.schema_problems` reads the schema at runtime, and the schema
    is the contract a consumer builds against. Both need it inside the
    installed package, not merely beside the source.
    """

    def test_the_schema_file_is_installed_beside_the_module(self):
        assert annotations.SCHEMA_PATH.is_file()

    def test_the_schema_is_declared_as_package_data(self):
        # A test that reads the file from the source tree passes whether or not
        # the wheel carries it, so the declaration is what gets asserted.
        # Read as text rather than parsed: tomllib is 3.11+ and this package
        # supports 3.10, so importing it would fail the oldest job in CI.
        config = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(
            encoding="utf-8"
        )

        assert "[tool.setuptools.package-data]" in config
        assert 'epubconvert = ["annotations.schema.json"]' in config

    def test_validating_a_document_does_not_need_the_source_tree(self):
        document = annotations.build_document([])

        assert not annotations.schema_problems(document)


class TestTheCoverageGateActuallyFailsTheBuild:
    """
    The gate reported a failure and passed the build.

    ``coverage.results.should_fail_under`` is ``round(total, precision) <
    fail_under``, and the precision defaults to 0. So 96.92% rounded to 97,
    compared equal to a ``fail_under`` of 97, and exited 0 -- while the report
    printed "FAIL ... not reached" from its own unrounded comparison. Anything
    from 96.5 upwards slipped through, which is a real regression landing on
    main behind a green tick.

    RELEASING.md says to judge every gate by its exit code. This is the gate
    where reading the output was the only way to know.
    """

    def _config(self) -> str:
        return (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(
            encoding="utf-8"
        )

    def test_the_comparison_precision_is_pinned(self):
        # Asked of the config coverage itself builds, not grepped out of the
        # file: "precision = 2" under [tool.coverage.html] satisfies a
        # substring check and leaves the gate exactly as broken as it was.
        config = pytest.importorskip("coverage.config")
        held = config.CoverageConfig()
        held.from_file(str(ROOT / "pyproject.toml"), warn=lambda _: None, our_file=True)

        assert held.precision == 2
        assert held.fail_under == 97

    def test_the_threshold_is_still_declared(self):
        assert "fail_under = 97" in self._config()

    def test_rounding_no_longer_hides_a_regression(self):
        # Asked for at call time, not imported at the top: the default install
        # is pure standard library and the test-minimal job has no coverage
        # package at all, so a module-level import made the whole file
        # unimportable there. importorskip states that dependency where it is
        # actually needed, and skips rather than fails without it.
        results = pytest.importorskip("coverage.results")

        assert results.should_fail_under(96.92, 97, 0) is False  # what shipped
        assert results.should_fail_under(96.92, 97, 2) is True  # configured


class TestTheReadmeQuotesTheRealHelp:
    """
    The README pastes ``--help`` into a fenced block, and a paste goes stale
    the moment a flag is added. It has twice: once when the annotation flags
    landed, and again when ``--annotations-format`` did, each time leaving the
    README describing a command line that no longer existed.

    Derived rather than eyeballed, so adding a flag without regenerating the
    block fails here.
    """

    def _quoted(self) -> str:
        lines = (ROOT / "README.md").read_text(encoding="utf-8").split("\n")
        start = next(
            index
            for index, line in enumerate(lines)
            if line.strip() == "```text" and lines[index + 1].startswith("usage:")
        )
        end = next(
            index
            for index, line in enumerate(lines)
            if line.strip() == "```" and index > start
        )
        return "\n".join(lines[start + 1 : end])

    def _real(self) -> str:
        # The width matters: argparse wraps to COLUMNS, so a block generated at
        # a different width differs everywhere and says nothing useful.
        return subprocess.run(
            [sys.executable, "-m", "epubconvert", "--help"],
            capture_output=True,
            text=True,
            check=True,
            cwd=ROOT,
            env={**os.environ, "COLUMNS": "79"},
        ).stdout.rstrip()

    def test_the_block_is_what_the_program_prints(self):
        assert self._quoted() == self._real()

    def test_every_flag_the_parser_takes_is_in_the_block(self):
        # A second question the exact match already answers, kept because it
        # names what went wrong when it fails.
        quoted = self._quoted()
        missing = [
            action
            for action in ("--annotations-format", "-ao", "-ad", "-ae", "-ar", "-an")
            if action not in quoted
        ]

        assert missing == []
