"""
Tests for ``--library-export`` as a command: what it writes, what it refuses,
and how it composes with ``--annotations-only``.

``test_library.py`` covers reading the database and rendering the two shapes.
This covers the run around them: the file on disk, standard output, the dry
run, the flags that contradict it, and the flags a run that converts nothing
has no use for.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them. Explicit empty-list comparisons read better than truthiness here.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=use-implicit-booleaness-not-comparison,too-few-public-methods

import json
from pathlib import Path

import pytest

from epubconvert.collect import annotations, library
from epubconvert.export.catalogue import schema_problems
from epubconvert.run import cli
from epubconvert.run.run import main
from tests.conftest import make_metadata_package
from tests.test_annotations import highlight, library_row, make_databases
from tests.test_library import BOUGHT, _csv_rows


def _container(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[str]:
    package = make_metadata_package(
        tmp_path / "lib",
        "Leviathan Wakes.epub",
        title="Leviathan Wakes",
        identifier="urn:isbn:9781449340360",
    )
    make_databases(
        tmp_path / "container",
        rows=[highlight()],
        books=[
            library_row(path=str(package), added=BOUGHT),
            library_row(asset="B", title="Bare", path="/x/Bare.epub"),
        ],
    )
    monkeypatch.setattr(
        "epubconvert.export.detached.collect_library",
        lambda policy=None, identifiers=True: library.collect(
            tmp_path / "container", policy, identifiers=identifiers
        ),
    )
    monkeypatch.setattr(
        "epubconvert.run.run.collect_annotations",
        lambda policy=None: annotations.collect(tmp_path / "container", policy),
    )
    return ["-s", str(tmp_path / "no-such-library"), "-o", str(tmp_path / "out")]


class TestTheCommandLineMode:
    """
    Its own mode, like ``-ao``: somebody who wants a catalogue of what they
    own may not want three thousand epub files. It needs no library on disk,
    no output directory, and writes nothing but the file it was given.
    """

    def test_it_writes_a_csv_and_converts_nothing(self, tmp_path, monkeypatch):
        flags = _container(monkeypatch, tmp_path)
        target = tmp_path / "library.csv"

        code = main([*flags, "--library-export", str(target), "-q"])

        assert code == 0
        rows = _csv_rows(target.read_text(encoding="utf-8"))
        assert [row["Title"] for row in rows] == ["Bare", "Leviathan Wakes"]
        assert rows[1]["ISBN13"] == '="9781449340360"'
        assert not (tmp_path / "out").exists()

    def test_json_is_a_format_away(self, tmp_path, monkeypatch):
        flags = _container(monkeypatch, tmp_path)
        target = tmp_path / "library.json"

        code = main(
            [*flags, "--library-export", str(target), "--library-format", "json", "-q"]
        )

        assert code == 0
        document = json.loads(target.read_text(encoding="utf-8"))
        assert schema_problems(document) == []
        assert len(document["books"]) == 2

    def test_with_no_file_it_goes_to_standard_output(
        self, tmp_path, monkeypatch, capsys
    ):
        flags = _container(monkeypatch, tmp_path)

        code = main([*flags, "--library-export"])

        assert code == 0
        out = capsys.readouterr().out
        assert out.startswith("Book Id,Title,")
        assert len(_csv_rows(out)) == 2

    def test_an_existing_file_is_left_alone(self, tmp_path, monkeypatch):
        # A snapshot, not a file that is merged into: there is nothing in a
        # CSV to merge on, and a Goodreads export at that path would carry
        # the very same header, so recognising "ours" is not possible.
        flags = _container(monkeypatch, tmp_path)
        target = tmp_path / "library.csv"
        target.write_text("precious", encoding="utf-8")

        code = main([*flags, "--library-export", str(target), "-q"])

        assert code == 5
        assert target.read_text(encoding="utf-8") == "precious"

    def test_a_dry_run_says_what_a_real_run_would_refuse(
        self, tmp_path, monkeypatch, capsys
    ):
        flags = _container(monkeypatch, tmp_path)
        target = tmp_path / "library.csv"
        target.write_text("precious", encoding="utf-8")

        code = main([*flags, "--library-export", str(target), "--dry-run"])

        assert code == 0
        assert "would refuse" in capsys.readouterr().err
        assert target.read_text(encoding="utf-8") == "precious"


class TestWhenItCannotWrite:
    """
    Every refusal, and what it leaves behind. A run that cannot write must
    leave the reader exactly where they were, and say which of the two files
    it could not write.
    """

    def test_a_refused_highlights_file_leaves_no_catalogue_behind(
        self, tmp_path, monkeypatch
    ):
        # The catalogue used to be written first, so a refused -ao target
        # left a CSV that the retry then refused to replace.
        flags = _container(monkeypatch, tmp_path)
        catalogue = tmp_path / "library.csv"
        highlights = tmp_path / "highlights.json"
        highlights.mkdir()

        code = main(
            [*flags, "--library-export", str(catalogue), "-ao", str(highlights), "-q"]
        )

        assert code == 5
        assert not catalogue.exists()

    def test_a_skipped_catalogue_says_why_it_was_skipped(
        self, tmp_path, monkeypatch, capsys
    ):
        # A vault failure can be per-note and stable, so a catalogue skipped
        # in silence was written once and never again.
        flags = _container(monkeypatch, tmp_path)
        catalogue = tmp_path / "library.csv"
        highlights = tmp_path / "highlights.json"
        highlights.mkdir()

        main([*flags, "--library-export", str(catalogue), "-ao", str(highlights)])

        assert "library was not exported" in capsys.readouterr().err

    def test_a_refused_catalogue_says_the_highlights_were_skipped(
        self, tmp_path, monkeypatch, capsys
    ):
        # The composed command in the README worked once: the second run
        # stopped at the catalogue and said nothing about the highlights,
        # which merge and would have been safe to rerun.
        flags = _container(monkeypatch, tmp_path)
        catalogue = tmp_path / "library.csv"
        catalogue.write_text("from an earlier run", encoding="utf-8")

        main(
            [
                *flags,
                "--library-export",
                str(catalogue),
                "-ao",
                str(tmp_path / "highlights.json"),
            ]
        )

        assert "highlights were not written either" in capsys.readouterr().err

    def test_a_refused_catalogue_leaves_no_highlights_behind(
        self, tmp_path, monkeypatch
    ):
        # Both destinations are judged before either is written: a composed
        # run that wrote one and was refused the other left half an answer.
        flags = _container(monkeypatch, tmp_path)
        catalogue = tmp_path / "library.csv"
        catalogue.write_text("precious", encoding="utf-8")
        highlights = tmp_path / "highlights.json"

        code = main(
            [*flags, "--library-export", str(catalogue), "-ao", str(highlights), "-q"]
        )

        assert code == 5
        assert not highlights.exists()
        assert catalogue.read_text(encoding="utf-8") == "precious"

    def test_a_directory_is_named_as_one_rather_than_answered_with_force(
        self, tmp_path, monkeypatch, capsys
    ):
        # --force does not make a directory writable, so sending the reader
        # there wasted their next run.
        flags = _container(monkeypatch, tmp_path)
        target = tmp_path / "catalogue"
        target.mkdir()

        code = main([*flags, "--library-export", str(target)])

        err = capsys.readouterr().err
        assert code == 5
        assert "is a directory" in err
        assert "--force" not in err

    def test_a_catalogue_inside_the_vault_this_run_makes_is_not_homeless(
        self, tmp_path, monkeypatch
    ):
        # Judged before the vault is created, its parent looked missing and
        # the whole run aborted saying so.
        _container(monkeypatch, tmp_path)
        vault = tmp_path / "vault"

        code = main(
            [
                "-s",
                str(tmp_path / "lib"),
                "-o",
                str(tmp_path / "out"),
                "-ao",
                str(vault),
                "--annotations-format",
                "markdown",
                "--library-export",
                str(vault / "library.csv"),
                "-q",
            ]
        )

        assert code == 0
        assert (vault / "library.csv").is_file()

    def test_a_dry_run_does_not_predict_a_refusal_of_a_vault_it_would_make(
        self, tmp_path, monkeypatch, capsys
    ):
        # The prediction is what a dry run is for, and this one was made
        # against a vault the real run creates moments earlier.
        _container(monkeypatch, tmp_path)
        vault = tmp_path / "vault"

        code = main(
            [
                "-s",
                str(tmp_path / "lib"),
                "-ao",
                str(vault),
                "--annotations-format",
                "markdown",
                "--library-export",
                str(vault / "library.csv"),
                "--dry-run",
            ]
        )

        assert code == 0
        assert "would refuse" not in capsys.readouterr().err


class TestWhenTheDestinationIsWrong:
    """
    Where the catalogue may not be written, judged before the library is read
    so a refusal costs no work and leaves nothing behind.
    """

    @pytest.mark.parametrize("name", ["Leviathan Wakes.md", "Leviathan Wakes.md.new"])
    def test_the_catalogue_will_not_take_a_note_s_name(
        self, tmp_path, monkeypatch, name
    ):
        # The vault is written first, so the name was free when the catalogue
        # was judged; --force then wrote CSV over the reader's highlights.
        _container(monkeypatch, tmp_path)
        vault = tmp_path / "vault"

        code = main(
            [
                "-s",
                str(tmp_path / "lib"),
                "-ao",
                str(vault),
                "--annotations-format",
                "markdown",
                "--library-export",
                str(vault / name),
                "--force",
                "-q",
            ]
        )

        assert code == 5
        assert not (vault / name).exists()

    def test_the_vault_is_recognised_however_its_path_is_spelled(
        self, tmp_path, monkeypatch
    ):
        # "-ao vault --library-export $PWD/vault/l.csv" names one directory
        # twice, and the run was refused for the spelling.
        _container(monkeypatch, tmp_path)
        vault = tmp_path / "vault"
        spelled = tmp_path / "." / "vault" / "library.csv"

        code = main(
            [
                "-s",
                str(tmp_path / "lib"),
                "-ao",
                str(vault),
                "--annotations-format",
                "markdown",
                "--library-export",
                str(spelled),
                "-q",
            ]
        )

        assert code == 0
        assert (vault / "library.csv").is_file()

    def test_a_missing_parent_directory_is_refused_before_the_read(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            "epubconvert.export.detached.collect_library",
            lambda **_: pytest.fail("read the library before refusing"),
        )

        code = main(
            [
                "-s",
                str(tmp_path),
                "--library-export",
                str(tmp_path / "nope" / "library.csv"),
                "-q",
            ]
        )

        assert code == 5

    def test_an_occupied_target_is_refused_before_the_library_is_read(
        self, tmp_path, monkeypatch
    ):
        # The read opens every book's package document; a run that was never
        # going to write should not pay for 2,805 of them first.
        target = tmp_path / "library.csv"
        target.write_text("precious", encoding="utf-8")
        monkeypatch.setattr(
            "epubconvert.export.detached.collect_library",
            lambda **_: pytest.fail("read the library before refusing"),
        )

        assert main(["-s", str(tmp_path), "--library-export", str(target), "-q"]) == 5

    def test_force_replaces_it(self, tmp_path, monkeypatch):
        flags = _container(monkeypatch, tmp_path)
        target = tmp_path / "library.csv"
        target.write_text("stale", encoding="utf-8")

        code = main([*flags, "--library-export", str(target), "--force", "-q"])

        assert code == 0
        assert target.read_text(encoding="utf-8").startswith("Book Id,")

    def test_a_dry_run_writes_nothing_and_still_says_how_much_will_match(
        self, tmp_path, monkeypatch, capsys
    ):
        # The estimate belongs before a 3,620-row import into a service where
        # undoing one is manual, which is what a dry run is for.
        flags = _container(monkeypatch, tmp_path)
        target = tmp_path / "library.csv"

        code = main([*flags, "--library-export", str(target), "--dry-run"])

        assert code == 0
        assert not target.exists()
        assert "1 of 2 book(s) carry an ISBN" in capsys.readouterr().err

    def test_the_json_export_gets_no_tracker_advice(
        self, tmp_path, monkeypatch, capsys
    ):
        # The advice is about the CSV's ISBN columns. The JSON carries every
        # identifier, UUIDs included, and nobody imports it into a tracker.
        flags = _container(monkeypatch, tmp_path)

        main(
            [
                *flags,
                "--library-export",
                str(tmp_path / "library.json"),
                "--library-format",
                "json",
            ]
        )

        err = capsys.readouterr().err
        assert "Read 2 book(s)" in err
        assert "import unmatched" not in err

    def test_a_convert_nothing_run_announces_neither_path(
        self, tmp_path, monkeypatch, capsys
    ):
        # It opens no source and creates no output directory, and naming them
        # described a run that never happened.
        flags = _container(monkeypatch, tmp_path)

        main([*flags, "--library-export", str(tmp_path / "library.csv")])

        err = capsys.readouterr().err
        assert "Examining source" not in err
        assert "Writing output to" not in err

    def test_without_isbns_the_estimate_says_none_will_match(
        self, tmp_path, monkeypatch, capsys
    ):
        flags = _container(monkeypatch, tmp_path)

        main([*flags, "--library-export", str(tmp_path / "l.csv"), "--no-isbn"])

        assert "--no-isbn" in capsys.readouterr().err

    def test_it_composes_with_annotations_only(self, tmp_path, monkeypatch):
        flags = _container(monkeypatch, tmp_path)
        catalogue = tmp_path / "library.csv"
        highlights = tmp_path / "highlights.json"

        code = main(
            [*flags, "--library-export", str(catalogue), "-ao", str(highlights), "-q"]
        )

        assert code == 0
        assert catalogue.is_file()
        assert json.loads(highlights.read_text(encoding="utf-8"))["annotations"]

    def test_an_unavailable_container_has_its_own_exit_code(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            "epubconvert.export.detached.collect_library",
            lambda **_: library.collect(tmp_path / "absent"),
        )

        assert main(["-s", str(tmp_path), "--library-export", "-", "-q"]) == 4

    def test_a_target_that_cannot_be_written_has_its_own_exit_code(
        self, tmp_path, monkeypatch
    ):
        flags = _container(monkeypatch, tmp_path)

        code = main(
            [*flags, "--library-export", str(tmp_path / "nope" / "library.csv"), "-q"]
        )

        assert code == 5

    def test_an_empty_library_is_reported_without_an_estimate(
        self, tmp_path, monkeypatch, capsys
    ):
        make_databases(tmp_path / "container", books=[])
        monkeypatch.setattr(
            "epubconvert.export.detached.collect_library",
            lambda **_: library.collect(tmp_path / "container"),
        )

        code = main(["-s", str(tmp_path), "--library-export", "-"])

        assert code == 0
        err = capsys.readouterr().err
        assert "Read 0 book(s)" in err
        assert "ISBN" not in err

    def test_a_library_that_matches_throughout_is_told_so(
        self, tmp_path, monkeypatch, capsys
    ):
        package = make_metadata_package(
            tmp_path / "lib",
            "Book.epub",
            title="Book",
            identifier="urn:isbn:9781449340360",
        )
        make_databases(tmp_path / "container", books=[library_row(path=str(package))])
        monkeypatch.setattr(
            "epubconvert.export.detached.collect_library",
            lambda **_: library.collect(tmp_path / "container"),
        )

        main(["-s", str(tmp_path), "--library-export", "-"])

        assert "Every book carries an ISBN" in capsys.readouterr().err

    def test_a_composed_dry_run_writes_neither_file(self, tmp_path, monkeypatch):
        # --library-export honoured --dry-run and returned; the run then fell
        # through to -ao, which had no dry-run check and wrote its file.
        flags = _container(monkeypatch, tmp_path)
        catalogue = tmp_path / "library.csv"
        highlights = tmp_path / "highlights.json"

        code = main(
            [*flags, "--library-export", str(catalogue), "-ao", str(highlights), "-d"]
        )

        assert code == 0
        assert not catalogue.exists()
        assert not highlights.exists()

    def test_the_unknown_shelf_reaches_the_csv(self, tmp_path, monkeypatch):
        flags = _container(monkeypatch, tmp_path)
        target = tmp_path / "library.csv"

        main([*flags, "--library-export", str(target), "--unknown-shelf", "to-read"])

        rows = _csv_rows(target.read_text(encoding="utf-8"))
        assert {row["Exclusive Shelf"] for row in rows} == {"to-read"}


class TestTheFlagsRefuseWhatTheyCannotDo:
    def test_an_empty_filename_is_refused(self):
        with pytest.raises(SystemExit):
            cli.parse_args(["--library-export="])

    @pytest.mark.parametrize(
        "flags",
        [["--no-isbn"], ["--unknown-shelf", "to-read"], ["--library-format", "json"]],
    )
    def test_a_modifier_without_the_export_is_refused(self, flags):
        # A flag that changes nothing is a run doing something other than
        # what was asked, silently.
        with pytest.raises(SystemExit):
            cli.parse_args(flags)

    def test_the_unknown_shelf_has_no_json_column_to_fill(self):
        with pytest.raises(SystemExit):
            cli.parse_args(
                ["--library-export", "l.json", "--library-format", "json"]
                + ["--unknown-shelf", "to-read"]
            )

    def test_only_a_goodreads_shelf_is_accepted(self):
        with pytest.raises(SystemExit):
            cli.parse_args(["--library-export", "l.csv", "--unknown-shelf", "maybe"])

    @pytest.mark.parametrize("other", [["-ae"], ["-ad", "h.json"]])
    def test_it_contradicts_converting(self, other):
        with pytest.raises(SystemExit):
            cli.parse_args(["--library-export", "l.csv", *other])

    @pytest.mark.parametrize("mode", [["--library-export", "l.csv"], ["-ao", "h.json"]])
    @pytest.mark.parametrize(
        "other",
        [
            ["--list"],
            ["--verify"],
            ["--covers"],
            ["--validate"],
            ["--check-references"],
            ["--epubcheck"],
            ["--refresh"],
            ["--skip-incomplete"],
            ["--match", "hobbit"],
            ["-m", "0"],
            ["--workers", "3"],
            ["--min-free", "0"],
            ["--no-copy-through"],
            ["--no-shuffle"],
        ],
    )
    def test_a_convert_nothing_mode_refuses_every_conversion_flag(self, mode, other):
        # The same flag was refused in one convert-nothing mode and silently
        # swallowed in the other. One list, checked for both.
        with pytest.raises(SystemExit):
            cli.parse_args([*mode, *other])

    @pytest.mark.parametrize("mode", [["--library-export", "l.csv"], ["-ao", "h.json"]])
    @pytest.mark.parametrize(
        "naming",
        [["--on-collision", "suffix"], ["--name-by", "author-title"], ["-p"]],
    )
    def test_the_naming_flags_still_shape_a_convert_nothing_run(self, mode, naming):
        # A vault names its notes the way the shelf names its books, and a
        # colliding pair under --on-collision skip loses a note; refusing the
        # flag in -ao would have made that loss unavoidable.
        assert cli.parse_args([*mode, *naming])

    def test_the_annotation_default_has_nothing_to_state(self):
        # -an states the default of a conversion, and this run converts none.
        with pytest.raises(SystemExit):
            cli.parse_args(["--library-export", "l.csv", "-an"])

    def test_force_is_refused_where_it_would_do_nothing(self):
        # It means "replace the file" to --library-export and nothing at all
        # to -ao, which merges; accepting it there promised an overwrite.
        with pytest.raises(SystemExit):
            cli.parse_args(["-ao", "h.json", "--force"])

    def test_force_says_nothing_to_standard_output(self):
        # There is nothing at "-" to replace.
        with pytest.raises(SystemExit):
            cli.parse_args(["--library-export", "-", "--force"])

    def test_force_is_accepted_where_it_means_something(self, tmp_path):
        args = cli.parse_args(
            ["--library-export", str(tmp_path / "l.csv"), "-ao", "h.json", "--force"]
        )

        assert args.force is True

    def test_two_documents_cannot_share_standard_output(self):
        with pytest.raises(SystemExit):
            cli.parse_args(["--library-export", "-ao"])

    def test_a_symlink_loop_in_a_destination_is_a_usage_error(self, tmp_path):
        # Path.resolve() raised RuntimeError out of argument parsing on
        # Python 3.10 to 3.12; a bad path is a usage error, exit 2.
        loop = tmp_path / "loop"
        loop.symlink_to(loop)
        with pytest.raises(SystemExit) as refused:
            cli.parse_args(
                ["--library-export", str(loop / "l.csv"), "-ao", str(loop / "l.csv")]
            )

        assert refused.value.code == 2

    def test_two_documents_cannot_share_a_file(self, tmp_path):
        # The library export ran first and, with --force, replaced the
        # highlights file with the catalogue before -ao refused to write it.
        target = tmp_path / "notes.json"
        with pytest.raises(SystemExit):
            cli.parse_args(
                [
                    "--library-export",
                    str(target),
                    "-ao",
                    str(tmp_path / "." / "notes.json"),
                ]
            )

    def test_one_of_them_may(self, tmp_path):
        args = cli.parse_args(["--library-export", "-ao", str(tmp_path / "h.json")])

        assert args.library_export == "-"

    def test_csv_remains_the_default(self):
        assert cli.parse_args([]).library_format == "csv"
