"""Tests for planning: collision handling, refresh, and the listing modes."""

# Test names describe the behaviour under test; separate docstrings would only
# restate them. Explicit empty-list comparisons read better than truthiness here.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=use-implicit-booleaness-not-comparison,too-few-public-methods

import json
import os
import unicodedata
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from zipfile import ZipFile

from epubconvert.collect import source
from epubconvert.collect.validate import read_package
from epubconvert.export.archive import collect_package_dirs
from epubconvert.export.naming import PassthroughNaming
from epubconvert.run import convert, planning, run
from epubconvert.utils.policy import NamingPolicy
from tests.conftest import make_metadata_package, make_package, remove_tree


def pending_count(
    packages: Sequence[Path],
    output_dir: Path,
    policy: NamingPolicy,
    options: planning.PlanOptions | None = None,
) -> int:
    """Count books still needing export, via the same path a run takes."""
    return convert.count_pending_decisions(
        planning.plan_exports(packages, output_dir, policy, options)
    )


class TestCollisionSuffix:
    def test_skip_mode_exports_only_the_first(self, tmp_path, output_dir):
        library = tmp_path / "lib"
        make_package(library / "a", "Same.epub")
        make_package(library / "b", "Same.epub")

        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])

        assert len(list(output_dir.glob("*.epub"))) == 1

    def test_suffix_mode_keeps_both(self, tmp_path, output_dir):
        library = tmp_path / "lib"
        make_package(library / "a", "Same.epub")
        make_package(library / "b", "Same.epub")

        run.main(
            [
                "-s",
                str(library),
                "-o",
                str(output_dir),
                "-m",
                "0",
                "--on-collision",
                "suffix",
                "-q",
            ]
        )

        names = sorted(p.name for p in output_dir.glob("*.epub"))
        assert names == ["Same (2).epub", "Same.epub"]

    def test_suffix_assignment_is_stable_across_runs(self, tmp_path, output_dir):
        # Assignment walks packages in sorted order, so a shuffled run still
        # gives the same book the same name.
        library = tmp_path / "lib"
        make_package(library / "a", "Same.epub")
        make_package(library / "b", "Same.epub")
        argv = [
            "-s",
            str(library),
            "-o",
            str(output_dir),
            "-m",
            "0",
            "--on-collision",
            "suffix",
            "-q",
        ]
        run.main(argv)
        first = {p.name: p.stat().st_mtime_ns for p in output_dir.glob("*.epub")}

        run.main(argv)

        second = {p.name: p.stat().st_mtime_ns for p in output_dir.glob("*.epub")}
        assert first == second  # nothing re-exported, nothing renamed

    def test_rerun_skips_both_suffixed_books(self, tmp_path, output_dir, capsys):
        library = tmp_path / "lib"
        make_package(library / "a", "Same.epub")
        make_package(library / "b", "Same.epub")
        argv = [
            "-s",
            str(library),
            "-o",
            str(output_dir),
            "-m",
            "0",
            "--on-collision",
            "suffix",
            "-q",
        ]
        run.main(argv)
        capsys.readouterr()

        run.main(argv)

        assert "Exported 0" in capsys.readouterr().out


class TestRefresh:
    def test_a_newer_source_is_re_exported(self, tmp_path, output_dir):
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        argv = ["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"]
        run.main(argv)
        exported = output_dir / "Book.epub"
        before = exported.stat().st_mtime_ns

        # Make the source look newer than the export.
        future = exported.stat().st_mtime + 100
        os.utime(library / "Book.epub", (future, future))
        run.main([*argv, "--refresh"])

        assert exported.stat().st_mtime_ns != before

    def test_an_unchanged_source_is_left_alone(self, tmp_path, output_dir):
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        argv = ["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"]
        run.main(argv)
        before = (output_dir / "Book.epub").stat().st_mtime_ns

        run.main([*argv, "--refresh"])

        assert (output_dir / "Book.epub").stat().st_mtime_ns == before


class TestListing:
    def test_table_reports_each_status(self, tmp_path, output_dir, capsys):
        library = tmp_path / "lib"
        make_package(library, "Pending.epub")
        locked = make_package(library, "Locked.epub")
        (locked / "META-INF").mkdir(parents=True, exist_ok=True)
        (locked / "META-INF" / "sinf.xml").write_text("<sinf/>", encoding="utf-8")

        code = run.main(["-s", str(library), "-o", str(output_dir), "--list", "-q"])

        out = capsys.readouterr().out
        assert code == 0
        assert "pending" in out
        assert "drm" in out
        assert "Locked.epub" in out

    def test_json_is_machine_readable(self, tmp_path, output_dir, capsys):
        library = tmp_path / "lib"
        make_package(library, "Book.epub")

        run.main(["-s", str(library), "-o", str(output_dir), "--list", "--json", "-q"])

        payload = json.loads(capsys.readouterr().out)
        assert payload[0]["name"] == "Book.epub"
        assert payload[0]["status"] == "pending"
        assert payload[0]["target"].endswith("Book.epub")

    def test_already_exported_books_are_marked(self, tmp_path, output_dir, capsys):
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])
        capsys.readouterr()

        run.main(["-s", str(library), "-o", str(output_dir), "--list", "--json", "-q"])

        payload = json.loads(capsys.readouterr().out)
        assert payload[0]["status"] == "exported"

    def test_listing_converts_nothing(self, tmp_path, output_dir):
        library = tmp_path / "lib"
        make_package(library, "Book.epub")

        run.main(["-s", str(library), "-o", str(output_dir), "--list", "-q"])

        assert list(output_dir.glob("*.epub")) == []


class TestCountPending:
    def test_unexported_books_are_counted(self, tmp_path, output_dir):
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        packages = collect_package_dirs(library)

        assert pending_count(packages, output_dir, PassthroughNaming()) == 1

    def test_books_that_can_never_be_exported_are_excluded(self, tmp_path, output_dir):
        # A remaining count that can never reach zero is not progress. A
        # DRM-protected book is reported on its own line instead.
        library = tmp_path / "lib"
        locked = make_package(library, "Locked.epub")
        (locked / "META-INF").mkdir(parents=True, exist_ok=True)
        (locked / "META-INF" / "sinf.xml").write_text("<sinf/>", encoding="utf-8")
        packages = collect_package_dirs(library)

        assert pending_count(packages, output_dir, PassthroughNaming()) == 0

    def test_suffixed_collisions_are_counted_separately(self, tmp_path, output_dir):
        # Both books need writing, under two different names. Comparing bare
        # filenames would see one.
        library = tmp_path / "lib"
        make_package(library / "a", "Same.epub")
        make_package(library / "b", "Same.epub")
        packages = collect_package_dirs(library)

        count = pending_count(
            packages,
            output_dir,
            PassthroughNaming(),
            planning.PlanOptions(on_collision="suffix"),
        )

        assert count == 2

    def test_exported_books_are_not_counted(self, tmp_path, output_dir):
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])
        packages = collect_package_dirs(library)

        assert pending_count(packages, output_dir, PassthroughNaming()) == 0

    def test_undownloaded_books_are_not_counted(
        self, tmp_path, output_dir, monkeypatch
    ):
        # Regression: the count forced check_incomplete off to save the stub
        # walk, so an undownloaded book planned as pending here and as
        # incomplete in the run that followed. --skip-incomplete then reported
        # books remaining that it would never export, on that run and on every
        # rerun after it.
        library = tmp_path / "lib"
        make_package(library, "Stub.epub")
        monkeypatch.setattr(source, "has_dataless_files", lambda _package: True)
        packages = collect_package_dirs(library)
        options = planning.PlanOptions(check_incomplete=True)

        decisions = planning.plan_exports(
            packages, output_dir, PassthroughNaming(), options
        )

        assert [d.status for d in decisions] == [planning.INCOMPLETE]
        assert pending_count(packages, output_dir, PassthroughNaming(), options) == 0


class TestANameOnTheShelfIsNotProofOfTheBook:
    """
    A file with a book's name is taken for that book only if it holds it.

    Three editions of one title, named by author and title so all three want
    one name. formal/RerunPlanner.tla found each of these; every book here has
    its own identifier, so the archive can be told apart from the source.
    """

    EDITIONS = ("Dune (1965)", "Dune (Ace)", "Dune (Chilton)")

    def _library(
        self, tmp_path: Path, identifiers: Sequence[str] | None = None
    ) -> Path:
        library = tmp_path / "lib"
        for number, folder in enumerate(self.EDITIONS, 1):
            identifier = (
                identifiers[number - 1]
                if identifiers
                else f"urn:uuid:00000000-0000-0000-0000-00000000000{number}"
            )
            make_metadata_package(
                library,
                f"{folder}.epub",
                title="Dune",
                creator="Frank Herbert",
                identifier=identifier,
            )
        return library

    @staticmethod
    def _run(library: Path, output_dir: Path, *extra: str) -> int:
        return run.main(
            [
                "-s", str(library), "-o", str(output_dir), "-m", "0", "-q",
                "--name-by", "author-title", *extra,
            ]
        )  # fmt: skip

    @staticmethod
    def _holder(output_dir: Path) -> str | None:
        with ZipFile(output_dir / "Frank Herbert - Dune.epub") as archive:
            return read_package(archive).identifier

    @staticmethod
    def _listed(
        library: Path, output_dir: Path, capsys: Any, *extra: str
    ) -> dict[str, dict[str, Any]]:
        capsys.readouterr()
        run.main(
            [
                "-s", str(library), "-o", str(output_dir), "--list", "--json",
                "--name-by", "author-title", *extra,
            ]
        )  # fmt: skip
        return {
            item["name"]: item
            for item in json.loads(capsys.readouterr().out)
            if item["status"] != planning.ORPHAN
        }

    def test_a_narrowed_run_does_not_call_another_editions_file_its_own(
        self, tmp_path, output_dir, capsys
    ):
        library = self._library(tmp_path)
        self._run(library, output_dir, "--match", "1965")

        listed = self._listed(library, output_dir, capsys, "--match", "Ace")

        assert listed["Dune (Ace).epub"]["status"] == planning.COLLISION
        assert "holds another book" in listed["Dune (Ace).epub"]["reason"]

    def test_the_next_edition_is_not_exported_by_a_deleted_ones_archive(
        self, tmp_path, output_dir, capsys
    ):
        library = self._library(tmp_path)
        self._run(library, output_dir)
        remove_tree(library / "Dune (1965).epub")

        listed = self._listed(library, output_dir, capsys)

        assert listed["Dune (Ace).epub"]["status"] == planning.COLLISION

    def test_refresh_does_not_write_over_another_books_archive(
        self, tmp_path, output_dir
    ):
        library = self._library(tmp_path)
        self._run(library, output_dir)
        first = self._holder(output_dir)
        remove_tree(library / "Dune (1965).epub")
        later = (output_dir / "Frank Herbert - Dune.epub").stat().st_mtime + 60
        os.utime(library / "Dune (Ace).epub", (later, later))

        self._run(library, output_dir, "--refresh")

        assert self._holder(output_dir) == first

    def test_force_does_not_write_over_another_books_archive(
        self, tmp_path, output_dir
    ):
        library = self._library(tmp_path)
        self._run(library, output_dir)
        first = self._holder(output_dir)
        remove_tree(library / "Dune (1965).epub")

        self._run(library, output_dir, "--force")

        assert self._holder(output_dir) == first

    def test_the_book_that_holds_its_name_is_still_exported(
        self, tmp_path, output_dir, capsys
    ):
        library = self._library(tmp_path)
        self._run(library, output_dir)

        listed = self._listed(library, output_dir, capsys)

        assert listed["Dune (1965).epub"]["status"] == planning.EXPORTED

    def test_a_placeholder_identifier_leaves_the_name_trusted(
        self, tmp_path, output_dir, capsys
    ):
        # "none" says nothing about which book an archive is, so there is
        # nothing to compare: the name decides, as it did before.
        library = self._library(tmp_path, identifiers=("none", "none", "none"))
        self._run(library, output_dir)
        remove_tree(library / "Dune (1965).epub")

        listed = self._listed(library, output_dir, capsys)

        assert listed["Dune (Ace).epub"]["status"] == planning.EXPORTED

    def test_an_unreadable_archive_leaves_the_name_trusted(
        self, tmp_path, output_dir, capsys
    ):
        library = self._library(tmp_path)
        (output_dir / "Frank Herbert - Dune.epub").write_bytes(b"not a zip")

        listed = self._listed(library, output_dir, capsys)

        assert listed["Dune (1965).epub"]["status"] == planning.EXPORTED


class TestADecomposedNameIsTheSameName:
    """
    An archive read back decomposed is still the book that wrote it.

    HFS+ stores names in NFD, so a shelf that has lived there hands back
    ``Cafe\\u0301.epub`` for the ``Caf\\u00e9.epub`` the planner computes.
    The lookup is keyed through NFC and found the file; the identity comparison
    behind it was not, and called the book's own archive another book's --
    a collision it could never refresh or force its way out of.
    """

    @staticmethod
    def _decomposed_shelf(tmp_path: Path, output_dir: Path) -> tuple[Path, Path]:
        library = tmp_path / "lib"
        package = make_package(library, unicodedata.normalize("NFC", "Café.epub"))
        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])
        [written] = output_dir.glob("*.epub")
        decomposed = output_dir / unicodedata.normalize("NFD", written.name)
        written.rename(decomposed)
        return package, decomposed

    def test_the_books_own_archive_is_exported(self, tmp_path, output_dir):
        package, decomposed = self._decomposed_shelf(tmp_path, output_dir)

        [decision] = planning.plan_exports([package], output_dir, PassthroughNaming())

        assert decision.status == planning.EXPORTED
        assert decision.target == decomposed

    def test_force_rewrites_the_books_own_archive(self, tmp_path, output_dir):
        package, decomposed = self._decomposed_shelf(tmp_path, output_dir)

        [decision] = planning.plan_exports(
            [package],
            output_dir,
            PassthroughNaming(),
            planning.PlanOptions(force=True),
        )

        assert decision.status == planning.PENDING
        assert decision.target == decomposed


class TestAFolderNameIsNotProofOfTheBook:
    """
    A folder-named shelf is verified before anything is written over it.

    The policies that name a book after its folder read no package document,
    so there was no identifier to compare and the name was trusted. Folder
    names are not unique: the library is walked recursively, so two
    subfolders can each hold a ``Dune.epub``, and ``romanize`` folds
    ``Café`` and ``Cafe`` to one name. After the book holding the name left
    the library, ``--refresh`` or ``--force`` wrote the other over its
    archive -- likely the last copy of a book deleted from Apple Books.
    """

    @staticmethod
    def _identifier(path: Path) -> str | None:
        with ZipFile(path) as archive:
            return read_package(archive).identifier

    @staticmethod
    def _nested(tmp_path: Path, output_dir: Path) -> tuple[Path, list[str]]:
        library = tmp_path / "lib"
        make_metadata_package(
            library / "a", "Dune.epub", title="Dune", identifier="urn:uuid:A"
        )
        make_metadata_package(
            library / "b", "Dune.epub", title="Dune Messiah", identifier="urn:uuid:B"
        )
        argv = ["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"]
        run.main(argv)
        remove_tree(library / "a" / "Dune.epub")
        return library, argv

    def test_refresh_does_not_write_over_a_nested_namesakes_archive(
        self, tmp_path, output_dir
    ):
        library, argv = self._nested(tmp_path, output_dir)
        later = (output_dir / "Dune.epub").stat().st_mtime + 60
        os.utime(library / "b" / "Dune.epub", (later, later))

        run.main([*argv, "--refresh"])

        assert self._identifier(output_dir / "Dune.epub") == "urn:uuid:A"

    def test_force_does_not_write_over_a_nested_namesakes_archive(
        self, tmp_path, output_dir
    ):
        _, argv = self._nested(tmp_path, output_dir)

        run.main([*argv, "--force"])

        assert self._identifier(output_dir / "Dune.epub") == "urn:uuid:A"

    def test_the_book_that_would_have_written_is_a_collision(
        self, tmp_path, output_dir
    ):
        library, _ = self._nested(tmp_path, output_dir)

        [decision] = planning.plan_exports(
            collect_package_dirs(library),
            output_dir,
            PassthroughNaming(),
            planning.PlanOptions(force=True),
        )

        assert decision.status == planning.COLLISION
        assert "holds another book, urn:uuid:A" in (decision.reason or "")

    def test_refresh_does_not_write_over_a_romanized_namesakes_archive(
        self, tmp_path, output_dir
    ):
        library = tmp_path / "lib"
        make_metadata_package(
            library, "Café.epub", title="Café", identifier="urn:uuid:A"
        )
        argv = ["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"]
        run.main([*argv, "-p", "romanize"])
        [written] = output_dir.glob("*.epub")
        remove_tree(library / "Café.epub")
        make_metadata_package(
            library, "Cafe.epub", title="Cafe", identifier="urn:uuid:B"
        )
        later = written.stat().st_mtime + 60
        os.utime(library / "Cafe.epub", (later, later))

        run.main([*argv, "-p", "romanize", "--refresh"])

        assert self._identifier(written) == "urn:uuid:A"

    def test_refresh_still_rewrites_the_books_own_archive(self, tmp_path, output_dir):
        library = tmp_path / "lib"
        make_metadata_package(
            library, "Dune.epub", title="Dune", identifier="urn:uuid:A"
        )
        argv = ["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"]
        run.main(argv)
        exported = output_dir / "Dune.epub"
        before = exported.stat().st_mtime_ns
        later = exported.stat().st_mtime + 60
        os.utime(library / "Dune.epub", (later, later))

        run.main([*argv, "--refresh"])

        assert exported.stat().st_mtime_ns != before


class TestAReasonCannotSteerTheTerminal:
    """
    A reason names a file on the shelf or in the library, so it is input too.

    Names were escaped wherever they were shown, but the reason beside them
    was printed and logged raw: ``--list`` put a package's ``ESC[2K`` on the
    terminal, and a name ``os.walk`` could not decode left a lone surrogate
    that made ``--list`` and ``--list --json`` die with UnicodeEncodeError on
    a UTF-8 stdout.
    """

    @staticmethod
    def _namesakes(tmp_path: Path, name: str) -> Path:
        library = tmp_path / "lib"
        make_package(library / "a", name)
        make_package(library / "b", name)
        return library

    def test_the_listing_escapes_a_reason(self, tmp_path, output_dir, capsys):
        library = self._namesakes(tmp_path, "Innocent\x1b[2KDONE.epub")

        run.main(["-s", str(library), "-o", str(output_dir), "--list", "-q"])

        out = capsys.readouterr().out
        assert planning.COLLISION in out
        assert "\x1b" not in out

    def test_the_log_escapes_a_reason(self, tmp_path, output_dir, capsys):
        library = self._namesakes(tmp_path, "Innocent\x1b[2KDONE.epub")

        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0"])

        err = capsys.readouterr().err
        assert "Name collision" in err
        assert "\x1b" not in err

    def test_the_listing_survives_an_undecodable_name(
        self, tmp_path, output_dir, capsys
    ):
        library = self._namesakes(tmp_path, "Bo\udcffk.epub")

        code = run.main(["-s", str(library), "-o", str(output_dir), "--list", "-q"])

        assert code == 0
        assert planning.COLLISION in capsys.readouterr().out

    def test_the_json_carries_an_undecodable_name_intact(
        self, tmp_path, output_dir, capsys
    ):
        # Escaped as JSON escapes it, so a reader gets the very name back and
        # can still open the file; everything else stays readable as written.
        name = "Café \x9b2K Bo\udcffk.epub"
        library = self._namesakes(tmp_path, name)

        code = run.main(
            ["-s", str(library), "-o", str(output_dir), "--list", "--json", "-q"]
        )

        out = capsys.readouterr().out
        assert code == 0
        assert "Café" in out
        assert "\x9b" not in out
        assert [item["name"] for item in json.loads(out)] == [name, name]
