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

from pathlib import Path
from zipfile import ZipFile

import pytest

from epubconvert import convert, exits, naming, run
from tests.conftest import make_package


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

        assert code == exits.MISSING_TOOL

    def test_a_missing_external_tool_has_its_own_code(self, tmp_path, monkeypatch):
        monkeypatch.setattr("epubconvert.run.epubcheck_available", lambda: False)
        library = tmp_path / "lib"
        make_package(library, "Book.epub")

        code = run.main(
            ["-s", str(library), "-o", str(tmp_path / "out"), "--epubcheck", "-q"]
        )

        assert code == exits.MISSING_TOOL

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


class TestTheDocumentedTableMatchesTheCode:
    """The README is the contract a script author reads."""

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
