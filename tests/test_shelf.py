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
from pathlib import Path

from epubconvert.export import archive
from epubconvert.export.naming import MetadataNaming, PassthroughNaming, StripNaming
from epubconvert.run import planning, run
from epubconvert.run.orphans import find_orphans
from tests.conftest import make_metadata_package, make_package, remove_tree


class TestOrphansAreFound:
    """An archive no book in the library claims is worth naming."""

    def test_a_shelf_matching_the_library_has_no_orphans(self, tmp_path, output_dir):
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])
        packages = archive.collect_package_dirs(library)

        assert find_orphans(output_dir, PassthroughNaming(), packages) == []

    def test_a_book_removed_from_the_library_becomes_an_orphan(
        self, tmp_path, output_dir
    ):
        library = tmp_path / "lib"
        make_package(library, "Keep.epub")
        make_package(library, "Gone.epub")
        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])
        remove_tree(library / "Gone.epub")
        packages = archive.collect_package_dirs(library)

        orphans = find_orphans(output_dir, PassthroughNaming(), packages)

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

        orphans = find_orphans(output_dir, StripNaming(), packages)

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

        orphans = find_orphans(
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


class TestMatchNamesAgainstTheWholeLibrary:
    """A book's name does not depend on which other books --match selected."""

    ARGS = ["--name-by", "author-title", "--on-collision", "suffix", "-q"]

    def _library(self, tmp_path: Path) -> Path:
        library = tmp_path / "lib"
        for folder, identifier in (
            ("Dune (1965)", "urn:uuid:a"),
            ("Dune (Ace)", "urn:uuid:b"),
        ):
            make_metadata_package(
                library,
                f"{folder}.epub",
                title="Dune",
                creator="Frank Herbert",
                identifier=identifier,
            )
        return library

    def test_a_matched_book_keeps_the_name_a_full_run_gave_it(
        self, tmp_path, output_dir, capsys
    ):
        # Regression: names were assigned over the matched subset alone, where
        # one edition is not crowded and so gets no marker. A book the full
        # run had already exported as "Dune [digest]" was then pending under
        # the plain name, written a second time, and the duplicate reported
        # as an orphan by every later full run.
        library = self._library(tmp_path)
        base = ["-s", str(library), "-o", str(output_dir), "-m", "0", *self.ARGS]
        assert run.main(base) == 0
        exported = sorted(path.name for path in output_dir.glob("*.epub"))
        capsys.readouterr()

        assert run.main([*base, "--match", "1965"]) == 0

        assert sorted(path.name for path in output_dir.glob("*.epub")) == exported
        capsys.readouterr()
        run.main(
            ["-s", str(library), "-o", str(output_dir), "--list", "--json", *self.ARGS]
        )
        listed = json.loads(capsys.readouterr().out)
        assert sorted(entry["status"] for entry in listed) == ["exported", "exported"]


class TestTheListingNamesTheFileItWillWrite:
    """
    The listing printed the source package name, so `--list` under a policy
    that renames showed nothing about the renaming it was asked to preview.
    The decision already carried the answer in `target`; the renderer read the
    wrong field.

    Older than `--name-by`: `-p` has diverged since 1.2.0 and listed the wrong
    name the whole time, because the difference was one character.
    """

    def test_a_renaming_policy_shows_the_new_name(self, tmp_path, output_dir, capsys):
        library = tmp_path / "lib"
        make_metadata_package(
            library,
            "Earthsea.epub",
            title="A Wizard of Earthsea",
            file_as="Le Guin, Ursula K.",
        )

        run.main(
            [
                "-s",
                str(library),
                "-o",
                str(output_dir),
                "--list",
                "--name-by",
                "author-title",
                "-q",
            ]
        )

        assert (
            "Le Guin, Ursula K. - A Wizard of Earthsea.epub" in capsys.readouterr().out
        )

    def test_portable_names_shows_the_sanitised_name(
        self, tmp_path, output_dir, capsys
    ):
        library = tmp_path / "lib"
        make_package(library, "Sapiens: A Brief History.epub")

        run.main(["-s", str(library), "-o", str(output_dir), "--list", "-p", "-q"])

        listing = capsys.readouterr().out
        assert "Sapiens A Brief History.epub" in listing
        assert "Sapiens: A Brief History.epub" not in listing

    def test_a_collision_still_names_the_book_that_lost(
        self, tmp_path, output_dir, capsys
    ):
        # A losing book has no target at all, so the source name is both the
        # only thing available and the right thing to show.
        library = tmp_path / "lib"
        make_package(library / "a", "Same.epub")
        make_package(library / "b", "Same.epub")

        run.main(["-s", str(library), "-o", str(output_dir), "--list", "-q"])

        listing = capsys.readouterr().out
        assert "collision" in listing
        assert "Same.epub" in listing

    def test_an_orphan_still_shows_the_file_on_the_shelf(
        self, tmp_path, output_dir, capsys
    ):
        library = tmp_path / "lib"
        make_package(library, "Keep.epub")
        make_package(library, "Gone.epub")
        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])
        remove_tree(library / "Gone.epub")
        capsys.readouterr()

        run.main(["-s", str(library), "-o", str(output_dir), "--list", "-q"])

        assert "Gone.epub" in capsys.readouterr().out

    def test_the_json_form_is_unchanged(self, tmp_path, output_dir, capsys):
        # "name" means the source package there, and that is a contract.
        library = tmp_path / "lib"
        make_metadata_package(
            library,
            "Earthsea.epub",
            title="A Wizard of Earthsea",
            file_as="Le Guin, Ursula K.",
        )

        run.main(
            [
                "-s",
                str(library),
                "-o",
                str(output_dir),
                "--list",
                "--json",
                "--name-by",
                "author-title",
                "-q",
            ]
        )

        row = json.loads(capsys.readouterr().out)[0]
        assert row["name"] == "Earthsea.epub"
        assert row["target"].endswith("Le Guin, Ursula K. - A Wizard of Earthsea.epub")


class TestAFileHoldingAnotherBookIsNotClaimed:
    """
    A file is claimed by the book the planner would call it, not by its name.

    The planner reports a book whose name is held by another book's archive
    as a collision. The orphan check still counted that file as the book's,
    so the archive of a book deleted from the library -- likely its last
    copy -- was reported as nothing at all.
    """

    def test_a_deleted_editions_archive_is_an_orphan(self, tmp_path, output_dir):
        library = tmp_path / "lib"
        for number, folder in enumerate(("Dune (1965)", "Dune (Ace)"), 1):
            make_metadata_package(
                library,
                f"{folder}.epub",
                title="Dune",
                creator="Frank Herbert",
                identifier=f"urn:uuid:{number}",
            )
        run.main(
            ["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"]
            + ["--name-by", "author-title"]
        )
        remove_tree(library / "Dune (1965).epub")

        orphans = find_orphans(
            output_dir, MetadataNaming(), archive.collect_package_dirs(library)
        )

        assert [path.name for path in orphans] == ["Frank Herbert - Dune.epub"]

    def test_a_file_of_another_identity_is_an_orphan(self, tmp_path, output_dir):
        # One file to a case-insensitive filesystem, two books to passthrough:
        # the planner calls BOOK.epub a collision with Book.epub, whose
        # archive declares another identifier.
        library = tmp_path / "lib"
        make_metadata_package(library, "Book.epub", title="B", identifier="urn:1")
        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])
        remove_tree(library / "Book.epub")
        make_metadata_package(library, "BOOK.epub", title="B", identifier="urn:2")
        packages = archive.collect_package_dirs(library)

        orphans = find_orphans(output_dir, PassthroughNaming(), packages)

        [decision] = planning.plan_exports(packages, output_dir, PassthroughNaming())
        assert decision.status == planning.COLLISION
        assert [path.name for path in orphans] == ["Book.epub"]


class TestAnArchiveOfABookInTheLibraryIsNotAnOrphan:
    """
    A file holding a book the library still has is that book's, whatever it is named.

    Skip mode: the Ace edition was exported alone under the plain name, then an
    earlier edition sorting first was added and took that name. The plan calls
    both a collision, which is right; the orphan check called the Ace
    edition's archive -- its only copy -- something no book claims, which is
    the list a person reviews before deleting.
    """

    PLAIN = "Frank Herbert - Dune.epub"

    def test_an_edition_that_lost_its_name_still_claims_its_archive(
        self, tmp_path, output_dir
    ):
        library = tmp_path / "lib"
        make_metadata_package(
            library,
            "Dune (Ace).epub",
            title="Dune",
            creator="Frank Herbert",
            identifier="urn:uuid:2",
        )
        run.main(
            ["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"]
            + ["--name-by", "author-title"]
        )
        make_metadata_package(
            library,
            "Dune (1965).epub",
            title="Dune",
            creator="Frank Herbert",
            identifier="urn:uuid:1",
        )
        packages = archive.collect_package_dirs(library)

        orphans = find_orphans(output_dir, MetadataNaming(), packages)

        assert (output_dir / self.PLAIN).is_file()
        assert orphans == []

    def test_a_book_that_lost_the_naming_contest_keeps_its_identifier(self, tmp_path):
        library = tmp_path / "lib"
        for number, folder in enumerate(("Dune (1965)", "Dune (Ace)"), 1):
            make_metadata_package(
                library,
                f"{folder}.epub",
                title="Dune",
                creator="Frank Herbert",
                identifier=f"urn:uuid:{number}",
            )

        _, lost = planning.assign_names(
            archive.collect_package_dirs(library), MetadataNaming(), planning.SKIP
        )

        assert (lost.filename, lost.identifier) == ("", "urn:uuid:2")

    def test_a_folder_named_book_that_lost_its_name_still_claims_its_archive(
        self, tmp_path, output_dir, capsys
    ):
        # b/dune.epub was exported alone; a/Dune.epub sorts first and takes
        # the one file a case-insensitive filesystem has for both. Folder
        # names read no identifier, so only the name said whose it was, and
        # the loser carries the name it wanted.
        library = tmp_path / "lib"
        make_metadata_package(
            library / "b", "dune.epub", title="Dune", identifier="urn:uuid:LOWER"
        )
        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])
        make_metadata_package(
            library / "a", "Dune.epub", title="Dune", identifier="urn:uuid:UPPER"
        )
        capsys.readouterr()

        orphans = find_orphans(
            output_dir, PassthroughNaming(), archive.collect_package_dirs(library)
        )
        run.main(["-s", str(library), "-o", str(output_dir), "--list", "--json"])
        listed = json.loads(capsys.readouterr().out)

        assert orphans == []
        assert "orphan" not in {row["status"] for row in listed}
