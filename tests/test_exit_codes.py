"""
Tests for the exit codes, which are the tool's contract with a script.

The lock file and rerun safety invite scheduling this from cron or launchd, and
a scheduled run is read by its status, not its prose. Every distinct reason a
run cannot proceed gets its own code, because "retry in an hour", "install
something", "fix the path" and "a book is broken" are four different responses.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=too-few-public-methods

import re
from pathlib import Path
from zipfile import ZipFile

import pytest

from epubconvert.collect import annotations
from epubconvert.collect import library as library_module
from epubconvert.export import naming
from epubconvert.run import convert, run
from epubconvert.utils import exits
from tests.conftest import make_package, needs_permissions


class TestTheCodesAreDistinct:
    """A code that means two things tells a script nothing."""

    def test_every_code_has_exactly_one_meaning(self):
        named = {
            name: value
            for name, value in vars(exits).items()
            if name.isupper() and isinstance(value, int)
        }
        assert len(named) == len(set(named.values()))

    def test_every_code_is_documented(self):
        named = {
            value
            for name, value in vars(exits).items()
            if name.isupper() and isinstance(value, int)
        }
        assert named == set(exits.MEANINGS)


class TestEachFailureHasItsOwnCode:
    """Reproduced one by one; these all used to be 2."""

    def test_success(self, tmp_path, output_dir):
        library = tmp_path / "lib"
        make_package(library, "Book.epub")

        code = run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])

        assert code == exits.SUCCESS

    def test_a_usage_error_keeps_the_conventional_code(self):
        with pytest.raises(SystemExit) as raised:
            run.main(["--no-such-flag"])

        assert raised.value.code == exits.USAGE

    def test_contradictory_flags_are_a_usage_error(self, tmp_path):
        source = tmp_path / "lib"
        source.mkdir()

        with pytest.raises(SystemExit) as raised:
            run.main(["-s", str(source), "-q", "-v"])

        assert raised.value.code == exits.USAGE

    def test_a_missing_source_has_its_own_code(self, tmp_path):
        code = run.main(
            ["-s", str(tmp_path / "absent"), "-o", str(tmp_path / "out"), "-q"]
        )

        assert code == exits.NO_SOURCE

    def test_a_missing_extra_has_its_own_code(self, tmp_path, monkeypatch):
        monkeypatch.setattr(naming, "disarm", None)
        library = tmp_path / "lib"
        make_package(library, "Book.epub")

        code = run.main(
            ["-s", str(library), "-o", str(tmp_path / "out"), "-p", "romanize", "-q"]
        )

        assert code == exits.MISSING_EXTRA

    def test_a_verify_target_that_is_not_there_has_its_own_code(self, tmp_path):
        source = tmp_path / "lib"
        source.mkdir()

        code = run.main(
            ["-s", str(source), "-o", str(tmp_path / "absent"), "--verify", "-q"]
        )

        assert code == exits.NO_OUTPUT

    def test_damaged_archives_have_their_own_code(self, tmp_path, output_dir):
        source = tmp_path / "lib"
        source.mkdir()
        (output_dir / "Broken.epub").write_bytes(b"not a zip")

        code = run.main(["-s", str(source), "-o", str(output_dir), "--verify", "-q"])

        assert code == exits.DAMAGED

    def test_a_failed_conversion_has_its_own_code(self, tmp_path, output_dir):
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        (output_dir / "Book.epub").mkdir()

        code = run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])

        assert code == exits.FAILED

    def test_a_held_lock_has_its_own_code(self, tmp_path, output_dir):
        library = tmp_path / "lib"
        make_package(library, "Book.epub")

        with convert.output_lock(output_dir):
            code = run.main(
                ["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"]
            )

        assert code == exits.LOCKED

    def test_an_unusable_output_directory_has_its_own_code(self, tmp_path):
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        blocked = tmp_path / "blocked"
        blocked.write_text("not a directory", encoding="utf-8")

        code = run.main(
            ["-s", str(library), "-o", str(blocked / "out"), "-m", "0", "-q"]
        )

        assert code == exits.NO_OUTPUT


@pytest.fixture(name="refused")
def _refused(tmp_path):
    """A Books container this run may not read; chmod stands in for TCC."""
    parent = tmp_path / "Containers"
    container = parent / "Documents"
    container.mkdir(parents=True)
    parent.chmod(0)
    yield container
    parent.chmod(0o700)


class TestARefusalHasItsOwnCode:
    """
    #19: a refused container and a missing library both exited 4, so a
    scheduled run could not tell "grant Full Disk Access" from "there is no
    library here". Every route that reads the container is checked, since each
    turns the failure into a code on its own.
    """

    @needs_permissions
    def test_the_annotation_export(self, refused, monkeypatch):
        monkeypatch.setattr(
            "epubconvert.run.run.collect_annotations",
            lambda policy=None: annotations.collect(refused, policy),
        )

        assert run.main(["-ao", "-", "-q"]) == exits.NO_PERMISSION

    @needs_permissions
    def test_the_library_export(self, refused, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "epubconvert.export.detached.collect_library",
            lambda **_: library_module.collect(refused),
        )

        code = run.main(["-s", str(tmp_path), "--library-export", "-", "-q"])

        assert code == exits.NO_PERMISSION

    @needs_permissions
    def test_a_refresh_that_converts_nothing(self, refused, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "epubconvert.run.run.collect_annotations",
            lambda policy=None: annotations.collect(refused, policy),
        )
        source = tmp_path / "lib"
        source.mkdir()

        code = run.main(
            ["-s", str(source), "-o", str(tmp_path / "out"), "-ar", "-ae", "-q"]
        )

        assert code == exits.NO_PERMISSION

    def test_an_absent_container_keeps_its_code(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "epubconvert.run.run.collect_annotations",
            lambda policy=None: annotations.collect(tmp_path / "absent", policy),
        )

        assert run.main(["-ao", "-", "-q"]) == exits.NO_SOURCE

    def test_the_code_is_eight(self):
        # A number a script writes down, so it is pinned where it can be read.
        assert exits.NO_PERMISSION == 8


class TestTheDocumentedTableMatchesTheCode:
    """The README is the contract a script author reads."""

    def test_run_main_names_no_code_by_number(self):
        # Its docstring listed codes by number, and four of them had moved on:
        # 5 was given as 1 and as 2, 7 as 1, and 6 as 2 (#23). exits defines
        # every code once, so the docstring points there instead.
        _, marker, returns = (run.main.__doc__ or "").partition(":return:")

        assert marker
        assert "exits" in returns
        assert re.findall(r"\b\d+\b", returns) == []

    def test_every_code_appears_in_the_readme(self):
        readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(
            encoding="utf-8"
        )
        for code in exits.MEANINGS:
            assert f"| `{code}`" in readme, f"exit {code} is undocumented"

    def test_a_valid_archive_still_verifies_clean(self, output_dir, tmp_path):
        source = tmp_path / "lib"
        source.mkdir()
        with ZipFile(output_dir / "Fine.epub", "w") as opened:
            opened.writestr("mimetype", "application/epub+zip")
            opened.writestr(
                "META-INF/container.xml",
                '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                '<rootfiles><rootfile full-path="c.opf"/></rootfiles>'
                "</container>",
            )
            opened.writestr(
                "c.opf",
                '<package xmlns="http://www.idpf.org/2007/opf"><manifest>'
                '<item id="t" href="t.xhtml"/></manifest>'
                '<spine><itemref idref="t"/></spine></package>',
            )
            opened.writestr("t.xhtml", "<html/>")

        code = run.main(["-s", str(source), "-o", str(output_dir), "--verify", "-q"])

        assert code == exits.SUCCESS
