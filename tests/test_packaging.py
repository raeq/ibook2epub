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

from importlib import metadata
from pathlib import Path

import pytest

import epubconvert
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
