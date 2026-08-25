"""
Tests for what the tool can say about the output directory itself.

The library is seen richly: five statuses, reasons, tallies. The shelf was seen
not at all. After adopting ``-p`` a shelf can hold both ``Sapiens: A Brief
History.epub`` and ``Sapiens A Brief History.epub`` and no command would ever
surface that -- ``--verify`` blesses both because both are sound archives, and
``--list`` only ever looked at sources.

Nothing here deletes anything. The tool's never-deletes stance is deliberate;
the gap was that it would not tell you either.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them. Explicit empty-list comparisons read better than truthiness here.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=use-implicit-booleaness-not-comparison,too-few-public-methods

import json

from epubconvert import archive, planning, run
from epubconvert.naming import PassthroughNaming, StripNaming
from tests.conftest import make_package, remove_tree


class TestOrphansAreFound:
    """An archive no book in the library claims is worth naming."""

    def test_a_shelf_matching_the_library_has_no_orphans(self, tmp_path, output_dir):
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])
        packages = archive.collect_package_dirs(library)

        assert planning.find_orphans(output_dir, PassthroughNaming(), packages) == []

    def test_a_book_removed_from_the_library_becomes_an_orphan(
        self, tmp_path, output_dir
    ):
        library = tmp_path / "lib"
        make_package(library, "Keep.epub")
        make_package(library, "Gone.epub")
        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])
        remove_tree(library / "Gone.epub")
        packages = archive.collect_package_dirs(library)

        orphans = planning.find_orphans(output_dir, PassthroughNaming(), packages)

        assert [path.name for path in orphans] == ["Gone.epub"]

    def test_a_renaming_policy_leaves_the_old_file_as_an_orphan(
        self, tmp_path, output_dir
    ):
        # The case the README warns about and no command could show: adopting
        # -p renames every book, and the archive under the old name stays.
        library = tmp_path / "lib"
        make_package(library, "Sapiens: A Brief History.epub")
        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])
        run.main(
            ["-s", str(library), "-o", str(output_dir), "-m", "0", "-p", "strip", "-q"]
        )
        packages = archive.collect_package_dirs(library)

        orphans = planning.find_orphans(output_dir, StripNaming(), packages)

        assert [path.name for path in orphans] == ["Sapiens: A Brief History.epub"]
        assert len(list(output_dir.glob("*.epub"))) == 2

    def test_a_suffixed_archive_is_not_an_orphan(self, tmp_path, output_dir):
        # " (2)" names are assigned by the planner, so orphan detection has to
        # ask the planner rather than guess from the package name.
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
        packages = archive.collect_package_dirs(library)

        orphans = planning.find_orphans(
            output_dir, PassthroughNaming(), packages, on_collision=planning.SUFFIX
        )

        assert orphans == []


class TestOrphansAreReported:
    """Finding them is only useful if a command says so."""

    def test_the_listing_shows_them(self, tmp_path, output_dir, capsys):
        library = tmp_path / "lib"
        make_package(library, "Keep.epub")
        make_package(library, "Gone.epub")
        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])
        remove_tree(library / "Gone.epub")
        capsys.readouterr()

        run.main(["-s", str(library), "-o", str(output_dir), "--list", "-q"])

        listing = capsys.readouterr().out
        assert "orphan" in listing
        assert "Gone.epub" in listing

    def test_the_json_carries_them_with_no_source(self, tmp_path, output_dir, capsys):
        library = tmp_path / "lib"
        make_package(library, "Keep.epub")
        make_package(library, "Gone.epub")
        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])
        remove_tree(library / "Gone.epub")
        capsys.readouterr()

        run.main(["-s", str(library), "-o", str(output_dir), "--list", "--json", "-q"])

        rows = json.loads(capsys.readouterr().out)
        orphans = [row for row in rows if row["status"] == "orphan"]
        assert len(orphans) == 1
        assert orphans[0]["name"] == "Gone.epub"
        assert orphans[0]["source"] is None
        assert orphans[0]["target"].endswith("Gone.epub")

    def test_the_run_summary_counts_them(self, tmp_path, output_dir, capsys):
        library = tmp_path / "lib"
        make_package(library, "Keep.epub")
        make_package(library, "Gone.epub")
        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])
        remove_tree(library / "Gone.epub")
        capsys.readouterr()

        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])

        assert "1 orphaned" in capsys.readouterr().out

    def test_a_clean_shelf_says_nothing_about_orphans(
        self, tmp_path, output_dir, capsys
    ):
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])
        capsys.readouterr()

        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])

        assert "orphan" not in capsys.readouterr().out


class TestNothingIsDeleted:
    """Reporting only. The never-deletes stance is the point."""

    def test_an_orphan_survives_being_reported(self, tmp_path, output_dir):
        library = tmp_path / "lib"
        make_package(library, "Keep.epub")
        make_package(library, "Gone.epub")
        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])
        remove_tree(library / "Gone.epub")

        run.main(["-s", str(library), "-o", str(output_dir), "--list", "-q"])
        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])

        assert (output_dir / "Gone.epub").is_file()


class TestMatchDoesNotInventOrphans:
    """--match narrows the run, not the library."""

    def test_filtered_books_are_not_reported_as_orphans(
        self, tmp_path, output_dir, capsys
    ):
        # Orphan detection has to consider every package that exists, not the
        # subset this run happens to be looking at, or --match would report
        # the whole rest of the shelf as abandoned.
        library = tmp_path / "lib"
        for title in ["Dune.epub", "The Hobbit.epub"]:
            make_package(library, title)
        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])
        capsys.readouterr()

        run.main(
            [
                "-s",
                str(library),
                "-o",
                str(output_dir),
                "--match",
                "dune",
                "--list",
                "-q",
            ]
        )

        assert "orphan" not in capsys.readouterr().out
