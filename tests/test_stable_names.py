"""
Tests for collision names that do not move when the library changes.

``--on-collision suffix`` numbered the members of a colliding group by their
position in it, so adding a book that sorted earlier renamed every later
member. ``_assign_names`` documented that against itself and pointed at
``dc:identifier`` as the real fix.

An identifier does not depend on what else is in the library, so it makes a
disambiguator that holds still. It is not, however, a usable *identity*: in a
surveyed 2,805-book library the string ``none`` is the identifier for 92 books,
``ISBN`` for three, and 52 books share a value with another book even after
those are filtered out. So the identifier decides what a colliding book is
called, and nothing else.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them. Explicit empty-list comparisons read better than truthiness here.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=use-implicit-booleaness-not-comparison,too-few-public-methods

from pathlib import Path

from epubconvert import archive, planning, run
from epubconvert.naming import MetadataNaming, disambiguator
from epubconvert.validate import Package, usable_identifier
from tests.conftest import make_metadata_package, remove_tree


def names_for(
    library: Path, mode: planning.CollisionMode = planning.SUFFIX
) -> list[str]:
    """Plan the library and return the assigned output names, sorted."""
    assigned = planning.assign_names(
        list(archive.collect_package_dirs(library)), MetadataNaming(), mode
    )
    return sorted(item.filename for item in assigned if item.filename)


class TestJunkIdentifiersAreTreatedAsAbsent:
    """Measured counts are from the 2,805-book survey in issue #2."""

    def test_a_real_uuid_is_usable(self):
        assert (
            usable_identifier(Package(opf_path="c.opf", identifier="urn:uuid:abc-123"))
            == "urn:uuid:abc-123"
        )

    def test_the_literal_string_none_is_not(self):
        # 92 books.
        assert usable_identifier(Package(opf_path="c.opf", identifier="none")) is None

    def test_the_literal_string_isbn_is_not(self):
        # 3 books: the placeholder was shipped instead of the value.
        assert usable_identifier(Package(opf_path="c.opf", identifier="ISBN")) is None

    def test_the_literal_string_unknown_is_not(self):
        assert (
            usable_identifier(Package(opf_path="c.opf", identifier="unknown")) is None
        )

    def test_matching_ignores_case(self):
        assert usable_identifier(Package(opf_path="c.opf", identifier="NONE")) is None

    def test_whitespace_only_is_not(self):
        assert usable_identifier(Package(opf_path="c.opf", identifier="   ")) is None

    def test_a_value_is_trimmed(self):
        assert (
            usable_identifier(Package(opf_path="c.opf", identifier="  urn:uuid:1  "))
            == "urn:uuid:1"
        )

    def test_no_metadata_at_all_is_not_usable(self):
        assert usable_identifier(None) is None


class TestTheDisambiguatorComesFromTheBook:
    def test_the_same_identifier_gives_the_same_marker(self):
        assert disambiguator("urn:uuid:abc") == disambiguator("urn:uuid:abc")

    def test_different_identifiers_give_different_markers(self):
        assert disambiguator("urn:uuid:abc") != disambiguator("urn:uuid:def")

    def test_the_marker_is_safe_in_a_filename(self):
        marker = disambiguator("urn:isbn:9780553383041")

        assert marker.isalnum()
        assert len(marker) == 8


class TestCollidingNamesUseTheIdentifier:
    def test_both_books_are_kept_and_neither_is_numbered(self, tmp_path):
        library = tmp_path / "lib"
        make_metadata_package(
            library / "a",
            "One.epub",
            title="Dune",
            file_as="Herbert, Frank",
            identifier="urn:uuid:aaa",
        )
        make_metadata_package(
            library / "b",
            "Two.epub",
            title="Dune",
            file_as="Herbert, Frank",
            identifier="urn:uuid:bbb",
        )

        names = names_for(library)

        assert len(names) == 2
        assert all(" (2)" not in name for name in names)
        assert all(name.startswith("Herbert, Frank - Dune [") for name in names)

    def test_a_book_that_does_not_collide_keeps_the_plain_name(self, tmp_path):
        library = tmp_path / "lib"
        make_metadata_package(
            library,
            "Solo.epub",
            title="Dune",
            file_as="Herbert, Frank",
            identifier="urn:uuid:aaa",
        )

        assert names_for(library) == ["Herbert, Frank - Dune.epub"]

    def test_skip_mode_is_unchanged(self, tmp_path):
        # 'skip' means export one and report the rest. Disambiguating there
        # would quietly turn it into 'suffix'.
        library = tmp_path / "lib"
        for folder, ident in (("a", "urn:uuid:aaa"), ("b", "urn:uuid:bbb")):
            make_metadata_package(
                library / folder,
                f"{folder}.epub",
                title="Dune",
                file_as="Herbert, Frank",
                identifier=ident,
            )

        assert names_for(library, planning.SKIP) == ["Herbert, Frank - Dune.epub"]


class TestNamesHoldStillWhenTheLibraryChanges:
    """The defect this whole part exists to fix."""

    def test_adding_a_book_that_sorts_earlier_renames_nothing(self, tmp_path):
        library = tmp_path / "lib"
        make_metadata_package(
            library / "m",
            "M.epub",
            title="Dune",
            file_as="Herbert, Frank",
            identifier="urn:uuid:mmm",
        )
        make_metadata_package(
            library / "z",
            "Z.epub",
            title="Dune",
            file_as="Herbert, Frank",
            identifier="urn:uuid:zzz",
        )
        before = names_for(library)

        make_metadata_package(
            library / "a",
            "A.epub",
            title="Dune",
            file_as="Herbert, Frank",
            identifier="urn:uuid:aaa",
        )
        after = names_for(library)

        assert set(before).issubset(set(after))
        assert len(after) == 3

    def test_removing_a_member_renames_nothing(self, tmp_path):
        library = tmp_path / "lib"
        for folder, ident in (
            ("a", "urn:uuid:aaa"),
            ("m", "urn:uuid:mmm"),
            ("z", "urn:uuid:zzz"),
        ):
            make_metadata_package(
                library / folder,
                f"{folder}.epub",
                title="Dune",
                file_as="Herbert, Frank",
                identifier=ident,
            )
        before = names_for(library)

        remove_tree(library / "a" / "a.epub")
        after = names_for(library)

        assert set(after).issubset(set(before))
        assert len(after) == 2

    def test_a_disambiguated_book_is_recognised_on_rerun(self, tmp_path, output_dir):
        library = tmp_path / "lib"
        for folder, ident in (("a", "urn:uuid:aaa"), ("b", "urn:uuid:bbb")):
            make_metadata_package(
                library / folder,
                f"{folder}.epub",
                title="Dune",
                file_as="Herbert, Frank",
                identifier=ident,
            )
        flags = [
            "-s",
            str(library),
            "-o",
            str(output_dir),
            "-m",
            "0",
            "--name-by",
            "author-title",
            "--on-collision",
            "suffix",
            "-q",
        ]
        run.main(flags)
        stamps = {p.name: p.stat().st_mtime_ns for p in output_dir.glob("*.epub")}

        run.main(flags)

        assert {
            p.name: p.stat().st_mtime_ns for p in output_dir.glob("*.epub")
        } == stamps
        assert len(stamps) == 2


class TestDegradingHonestly:
    """A disambiguator that pretends to be stable is worse than a number."""

    def test_a_junk_identifier_falls_back_to_the_number(self, tmp_path):
        library = tmp_path / "lib"
        for folder in ("a", "b"):
            make_metadata_package(
                library / folder,
                f"{folder}.epub",
                title="Dune",
                file_as="Herbert, Frank",
                identifier="none",
            )

        names = names_for(library)

        assert names == ["Herbert, Frank - Dune (2).epub", "Herbert, Frank - Dune.epub"]

    def test_two_books_sharing_one_identifier_fall_back_to_the_number(self, tmp_path):
        # 52 books in the surveyed library share a value with another book even
        # after the junk placeholders are filtered out.
        library = tmp_path / "lib"
        for folder in ("a", "b"):
            make_metadata_package(
                library / folder,
                f"{folder}.epub",
                title="Dune",
                file_as="Herbert, Frank",
                identifier="urn:uuid:shared",
            )

        names = names_for(library)

        assert len(names) == 2
        assert sum(" (2)" in name for name in names) == 1


class TestTheRunSaysWhatHappened:
    def test_a_collision_names_the_book_that_won(self, tmp_path, output_dir, capsys):
        library = tmp_path / "lib"
        for folder in ("a", "b"):
            make_metadata_package(
                library / folder,
                f"{folder}.epub",
                title="Dune",
                file_as="Herbert, Frank",
                identifier="none",
            )

        run.main(
            [
                "-s",
                str(library),
                "-o",
                str(output_dir),
                "-m",
                "0",
                "--name-by",
                "author-title",
                "-q",
            ]
        )

        assert "Herbert, Frank - Dune.epub" in capsys.readouterr().err

    def test_books_named_without_an_author_are_counted(
        self, tmp_path, output_dir, capsys
    ):
        library = tmp_path / "lib"
        make_metadata_package(library, "Known.epub", title="Dune", file_as="Herbert")
        make_metadata_package(library / "x", "Anon.epub", title="Beowulf")

        run.main(
            [
                "-s",
                str(library),
                "-o",
                str(output_dir),
                "-m",
                "0",
                "--name-by",
                "author-title",
                "-q",
            ]
        )

        assert "1 book(s) named without an author" in capsys.readouterr().err
