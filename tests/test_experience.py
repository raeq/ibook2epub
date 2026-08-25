"""
Tests for what the tool tells the person running it.

The conversion machinery is covered elsewhere. These are about the seams where
it talks: whether advice it gives actually works, whether a message teaches the
way out of the state it describes, and whether anything the user put in the
source directory can go unmentioned.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them. Explicit empty-list comparisons read better than truthiness here.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=use-implicit-booleaness-not-comparison,too-few-public-methods

from zipfile import ZipFile

import pytest

from epubconvert import cli, run
from tests.conftest import make_package


class TestAdviceThatWorks:
    """A remedy the tool prescribes has to cure the thing it diagnosed."""

    def test_verify_names_the_damaged_book_in_its_advice(
        self, tmp_path, output_dir, capsys
    ):
        # --force re-exports everything, and the default cap then picks five
        # at random, so following "re-export with --force" literally left the
        # damaged book untouched and reported success.
        library = tmp_path / "lib"
        for index in range(3):
            make_package(library, f"Book{index}.epub")
        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])
        (output_dir / "Book1.epub").write_bytes(b"CORRUPTED")
        capsys.readouterr()

        run.main(["-s", str(library), "-o", str(output_dir), "--verify", "-q"])

        advice = capsys.readouterr().out
        # The stem, not the filename: that is what --match takes, and the
        # advice has to be runnable as printed.
        assert "--match Book1 --force" in advice

    def test_force_warns_when_the_cap_will_cut_it_short(
        self, tmp_path, output_dir, capsys
    ):
        library = tmp_path / "lib"
        for index in range(4):
            make_package(library, f"Book{index}.epub")
        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])
        capsys.readouterr()

        run.main(["-s", str(library), "-o", str(output_dir), "-m", "2", "-f"])

        assert "--force" in capsys.readouterr().err


class TestTheCapTeachesTheWayOut:
    """A message describing a limit should name the flag that lifts it."""

    def test_the_remaining_line_names_the_no_limit_flag(
        self, tmp_path, output_dir, capsys
    ):
        # Against the README's 2,805-book reference library the default cap
        # means 561 reruns, and the message only ever taught rerunning.
        library = tmp_path / "lib"
        for index in range(4):
            make_package(library, f"Book{index}.epub")

        run.main(["-s", str(library), "-o", str(output_dir), "-m", "2", "-q"])

        summary = capsys.readouterr().out
        assert "remaining" in summary
        assert "-m 0" in summary


class TestTheSummaryIsPrintedOnce:
    """One run, one summary on screen."""

    def test_the_summary_appears_once_on_a_terminal(self, tmp_path, output_dir, capsys):
        # The log copy exists so a --log-file transcript of an interrupted run
        # is not indistinguishable from a complete one. It belongs on the file
        # handler, not on the console the print already reached.
        library = tmp_path / "lib"
        make_package(library, "Book.epub")

        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0"])

        captured = capsys.readouterr()
        together = captured.out + captured.err
        assert together.count("epub file(s) (") == 1

    def test_the_log_file_still_records_it(self, tmp_path, output_dir):
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        log = tmp_path / "run.log"

        run.main(
            [
                "-s",
                str(library),
                "-o",
                str(output_dir),
                "-m",
                "0",
                "--log-file",
                str(log),
            ]
        )

        assert "epub file(s) (" in log.read_text(encoding="utf-8")


class TestInteractiveOutputIsPlain:
    """Timestamps and level names are for transcripts, not for a terminal."""

    def test_default_verbosity_prints_bare_lines(self, tmp_path, output_dir, capsys):
        library = tmp_path / "lib"
        make_package(library, "Book.epub")

        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0"])

        assert " - INFO - " not in capsys.readouterr().err

    def test_verbose_keeps_the_full_dress(self, tmp_path, output_dir, capsys):
        library = tmp_path / "lib"
        make_package(library, "Book.epub")

        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-v"])

        assert " - DEBUG - " in capsys.readouterr().err


class TestContradictoryVerbosityIsRefused:
    """The house rule: never silently ignore a flag the user typed."""

    def test_quiet_and_verbose_together_are_refused(self, tmp_path):
        source = tmp_path / "lib"
        source.mkdir()

        with pytest.raises(SystemExit):
            cli.parse_args(["-s", str(source), "-q", "-v"])


class TestNothingInTheSourceGoesUnmentioned:
    """A partial export must not read as a complete one."""

    def test_non_package_items_are_counted(self, tmp_path, output_dir, capsys):
        # A sideloaded already-zipped book and an unrelated file appeared in
        # no output of any command: not skipped, not counted, not listed. The
        # summary read complete while two of five items were never considered.
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        with ZipFile(library / "Already Valid.epub", "w") as opened:
            opened.writestr("mimetype", "application/epub+zip")
        (library / "Some Paper.pdf").write_text("pdf", encoding="utf-8")

        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])

        assert "2 ignored" in capsys.readouterr().out

    def test_the_listing_mentions_them_too(self, tmp_path, output_dir, capsys):
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        (library / "Some Paper.pdf").write_text("pdf", encoding="utf-8")

        run.main(["-s", str(library), "-o", str(output_dir), "--list", "-q"])

        assert "1 ignored" in capsys.readouterr().out

    def test_a_clean_library_says_nothing_about_ignored_items(
        self, tmp_path, output_dir, capsys
    ):
        library = tmp_path / "lib"
        make_package(library, "Book.epub")

        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])

        assert "ignored" not in capsys.readouterr().out


class TestFirstContactOrients:
    """An error on a machine with no library should say what to do."""

    def test_the_missing_source_message_names_the_path(self, tmp_path, capsys):
        missing = tmp_path / "nowhere"

        run.main(["-s", str(missing), "-o", str(tmp_path / "out")])

        assert str(missing) in capsys.readouterr().err

    def test_the_missing_library_message_lists_where_it_looked(
        self, tmp_path, capsys, monkeypatch
    ):
        # Auto-discovery: both known homes probed, neither holding books.
        monkeypatch.setattr(
            "epubconvert.cli.discover_source", lambda: tmp_path / "nowhere"
        )

        run.main(["-o", str(tmp_path / "out")])

        message = capsys.readouterr().err
        assert "Looked in" in message
        assert "-s DIR" in message
