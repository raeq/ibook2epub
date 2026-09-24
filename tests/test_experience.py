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

import shlex
from pathlib import Path

import pytest

from epubconvert.run import cli, run
from tests.conftest import make_metadata_package, make_package


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
        assert "--match=Book1 --force" in advice

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


class TestVerifyAdviceRepairsTheBook:
    """
    Following ``--verify``'s advice as printed has to leave the shelf sound.

    It printed ``--match <stem> --force`` for every damaged file. ``--match``
    reads ``?``, ``[`` and ``*`` as a glob against the whole package name, so
    ``Who Moved My Cheese?`` and ``Foundation [Asimov]`` matched nothing and
    the run exited 0 having repaired nothing. A file copied through from the
    library, or named with a suffix, is no package's name at all: ``--match``
    finds nothing, and the copy is skipped because its name is taken.
    """

    @pytest.fixture(autouse=True)
    def _no_home_shelf(self, tmp_path, monkeypatch):
        # Advice that forgets -o writes to the default shelf: keep that one
        # in the test's own directory, never the real ~/Books.
        monkeypatch.setattr(cli, "DEFAULT_OUTPUT", tmp_path / "default shelf")

    @staticmethod
    def _damage_and_verify(
        library: Path,
        output_dir: Path,
        damaged: str,
        capsys: pytest.CaptureFixture[str],
        *flags: str,
    ) -> tuple[list[str], str]:
        base = ["-s", str(library), "-o", str(output_dir)]
        run.main([*base, *flags, "-m", "0", "-q"])
        (output_dir / damaged).write_bytes(b"CORRUPTED")
        capsys.readouterr()
        assert run.main([*base, "--verify", "-q"]) == 7
        return base, capsys.readouterr().out

    @staticmethod
    def _book(library: Path, name: str) -> Path:
        return make_metadata_package(library, name, title="Title")

    @pytest.mark.parametrize(
        "title",
        ["Who Moved My Cheese?.epub", "Foundation [Asimov].epub", "Plain.epub"],
    )
    def test_the_printed_command_re_exports_that_book_alone(
        self, tmp_path, output_dir, capsys, title
    ):
        library = tmp_path / "lib"
        # Complain.epub holds "Plain" and "Plain.epub": a substring pattern
        # would re-export it too.
        for name in (title, "Complain.epub", "Other [Asimov].epub"):
            self._book(library, name)
        base, advice = self._damage_and_verify(library, output_dir, title, capsys)

        [command] = [
            line.strip()
            for line in advice.splitlines()
            if line.strip().startswith("ibook2epub ")
        ]
        capsys.readouterr()
        # Exactly as printed: the run is told nothing the advice left out.
        run.main(shlex.split(command)[1:])

        assert "Exported 1 epub file(s)" in capsys.readouterr().out
        assert run.main([*base, "--verify", "-q"]) == 0

    def test_the_printed_command_names_the_shelf_and_the_library(
        self, tmp_path, capsys
    ):
        # It named neither: run as printed it looked for the library in its
        # default home and exited 4, and given -s it wrote a fresh copy to
        # ~/Books while the damaged file stayed where it was.
        library = tmp_path / "my lib"
        output_dir = tmp_path / "Kindle books"
        self._book(library, "Dune.epub")
        base, advice = self._damage_and_verify(library, output_dir, "Dune.epub", capsys)

        [command] = [
            line.strip()
            for line in advice.splitlines()
            if line.strip().startswith("ibook2epub ")
        ]
        assert f"-o {shlex.quote(str(output_dir))}" in command
        assert f"-s {shlex.quote(str(library))}" in command
        # --verify refuses the naming flags, so it cannot know them.
        assert "--name-by/-p/--on-collision" in advice
        run.main(shlex.split(command)[1:])
        assert run.main([*base, "--verify", "-q"]) == 0

    def test_a_discovered_library_is_left_to_discovery(
        self, tmp_path, capsys, monkeypatch
    ):
        library = tmp_path / "lib"
        self._book(library, "Dune.epub")
        monkeypatch.setattr("epubconvert.run.cli.discover_source", lambda: library)
        output_dir = tmp_path / "out"
        run.main(["-o", str(output_dir), "-q"])
        (output_dir / "Dune.epub").write_bytes(b"CORRUPTED")
        capsys.readouterr()
        run.main(["-o", str(output_dir), "--verify", "-q"])

        [command] = [
            line.strip()
            for line in capsys.readouterr().out.splitlines()
            if line.strip().startswith("ibook2epub ")
        ]
        assert " -s " not in command
        run.main(shlex.split(command)[1:])
        assert run.main(["-o", str(output_dir), "--verify", "-q"]) == 0

    @pytest.mark.parametrize(
        "title",
        [
            "Bad\x1bName.epub",
            "Tab\tTitle.epub",
            # What os.walk hands back for a name that is not UTF-8.
            "Bad\udcff name.epub",
            "-30-.epub",
            "--help.epub",
        ],
    )
    def test_a_name_display_escapes_still_gets_a_working_command(
        self, tmp_path, output_dir, capsys, title
    ):
        # The pattern was the name escaped for display, which --match reads
        # literally: 'Bad\x1bName' matched nothing. And "--match -30-" made
        # argparse read the pattern as a flag and exit 2.
        library = tmp_path / "lib"
        for name in (title, "Other.epub"):
            self._book(library, name)
        base, advice = self._damage_and_verify(library, output_dir, title, capsys)

        [command] = [
            line.strip()
            for line in advice.splitlines()
            if line.strip().startswith("ibook2epub ")
        ]
        capsys.readouterr()
        run.main(shlex.split(command)[1:])

        assert "Exported 1 epub file(s)" in capsys.readouterr().out
        assert run.main([*base, "--verify", "-q"]) == 0

    def test_a_masked_name_that_selects_two_books_is_moved_aside(
        self, tmp_path, output_dir, capsys
    ):
        # Each control character becomes "?", which matches the other too.
        library = tmp_path / "lib"
        for name in ("Bad\x1bName.epub", "Bad\x1cName.epub"):
            self._book(library, name)
        _, advice = self._damage_and_verify(
            library, output_dir, "Bad\x1bName.epub", capsys
        )

        assert "--match" not in advice
        assert "\n  Bad\\x1bName.epub\n" in advice

    def test_a_shelf_named_like_a_flag_is_still_the_shelf(
        self, tmp_path, capsys, monkeypatch
    ):
        # Typed as "-o=-shelf"; printed as "-o -shelf", -shelf reads as a flag.
        monkeypatch.chdir(tmp_path)
        library = tmp_path / "lib"
        self._book(library, "Dune.epub")
        base = ["-s", str(library), "-o=-shelf"]
        run.main([*base, "-q"])
        (tmp_path / "-shelf" / "Dune.epub").write_bytes(b"CORRUPTED")
        capsys.readouterr()
        assert run.main([*base, "--verify", "-q"]) == 7
        advice = capsys.readouterr().out

        [command] = [
            line.strip()
            for line in advice.splitlines()
            if line.strip().startswith("ibook2epub ")
        ]
        run.main(shlex.split(command)[1:])
        assert run.main([*base, "--verify", "-q"]) == 0

    def test_a_book_copied_through_is_moved_aside_not_forced(
        self, tmp_path, output_dir, capsys
    ):
        library = tmp_path / "lib"
        self._book(library, "Book.epub")
        # A sound zipped book, made the way the tool makes one.
        staging = tmp_path / "staging"
        self._book(staging / "lib", "Zipped.epub")
        run.main(["-s", str(staging / "lib"), "-o", str(staging / "out"), "-q"])
        (staging / "out" / "Zipped.epub").rename(library / "Zipped.epub")
        base, advice = self._damage_and_verify(
            library, output_dir, "Zipped.epub", capsys
        )

        assert "--match" not in advice
        assert "\n  Zipped.epub\n" in advice
        (output_dir / "Zipped.epub").unlink()
        run.main([*base, "-q"])
        assert run.main([*base, "--verify", "-q"]) == 0

    def test_a_suffixed_name_is_moved_aside_not_forced(
        self, tmp_path, output_dir, capsys
    ):
        library = tmp_path / "lib"
        self._book(library / "one", "Dune.epub")
        self._book(library / "two", "Dune.epub")
        flags = ("--on-collision", "suffix")
        base, advice = self._damage_and_verify(
            library, output_dir, "Dune (2).epub", capsys, *flags
        )

        assert "--match" not in advice
        assert "\n  Dune (2).epub\n" in advice
        (output_dir / "Dune (2).epub").unlink()
        run.main([*base, *flags, "-q"])
        assert run.main([*base, "--verify", "-q"]) == 0


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


class TestTheRemainingLineSaysWhyBooksRemain:
    """
    ``N remaining`` counts every pending book the run did not export, and they
    remain for different reasons. The line advised a rerun and ``-m 0`` for all
    of them, which was wrong twice over on a real run whose one remaining book
    had failed under ``-m 0``: the flag was already given, and a rerun fails the
    same book again (#15). An empty package fails the way that one did.
    """

    def test_books_the_cap_held_back_are_told_how_to_continue(
        self, tmp_path, output_dir, capsys
    ):
        library = tmp_path / "lib"
        for index in range(3):
            make_package(library, f"Book{index}.epub")

        code = run.main(["-s", str(library), "-o", str(output_dir), "-m", "1", "-q"])

        summary = capsys.readouterr().out
        assert code == 0
        assert "2 remaining" in summary
        assert "2 held back" in summary
        assert "-m 0" in summary
        assert "failed" not in summary

    def test_a_failure_under_no_cap_is_not_sent_to_rerun(
        self, tmp_path, output_dir, capsys
    ):
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        (library / "Empty.epub").mkdir()

        code = run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])

        summary = capsys.readouterr().out
        assert code == 1
        assert "1 remaining" in summary
        assert "1 failed: see the errors above" in summary
        assert "-m 0" not in summary
        assert "rerun" not in summary

    def test_held_back_and_failed_are_told_apart(self, tmp_path, output_dir, capsys):
        # --no-shuffle and a name that sorts first put the empty package
        # inside the cap, so one book fails and one is held back.
        library = tmp_path / "lib"
        (library / "A Empty.epub").mkdir(parents=True)
        for index in range(2):
            make_package(library, f"Book{index}.epub")

        code = run.main(
            [
                "-s",
                str(library),
                "-o",
                str(output_dir),
                "-m",
                "2",
                "--no-shuffle",
                "-q",
            ]
        )

        summary = capsys.readouterr().out
        assert code == 1
        assert "2 remaining" in summary
        assert "1 held back" in summary
        assert "-m 0" in summary
        assert "1 failed: see the errors above" in summary


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

    def test_non_book_items_are_counted(self, tmp_path, output_dir, capsys):
        # Anything in the source appeared in no output of any command: not
        # skipped, not counted, not listed, so the summary read complete while
        # some of what the user put there was never considered. Books are now
        # copied through; everything else is still counted.
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        (library / "notes.txt").write_text("mine", encoding="utf-8")
        (library / "cover.png").write_bytes(b"\x89PNG")

        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])

        assert "2 ignored" in capsys.readouterr().out

    def test_the_listing_mentions_them_too(self, tmp_path, output_dir, capsys):
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        (library / "notes.txt").write_text("mine", encoding="utf-8")

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
            "epubconvert.run.cli.discover_source", lambda: tmp_path / "nowhere"
        )

        run.main(["-o", str(tmp_path / "out")])

        message = capsys.readouterr().err
        assert "Looked in" in message
        assert "-s DIR" in message


class TestTheLibraryNotFoundListingLinesUp:
    """
    The message lists both known homes so it reads as "we looked in these
    places" rather than "this one path is wrong". A join separator that carried
    its own indent gave the first path two spaces and the rest four.
    """

    def test_every_candidate_is_indented_the_same(self, tmp_path, monkeypatch, capsys):
        candidates = (tmp_path / "one", tmp_path / "two")
        monkeypatch.setattr("epubconvert.run.run.SOURCE_CANDIDATES", candidates)
        monkeypatch.setattr(
            "epubconvert.run.cli.discover_source", lambda: candidates[0]
        )

        run.main(["-o", str(tmp_path / "out"), "-q"])

        listed = [
            line
            for line in capsys.readouterr().err.splitlines()
            if str(tmp_path) in line
        ]
        assert len(listed) == 2
        assert {len(line) - len(line.lstrip()) for line in listed} == {2}
