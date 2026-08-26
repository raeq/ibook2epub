"""
Tests for naming books after what the book says about itself.

Every other policy derives the output name from the package directory name.
This one derives it from ``dc:title`` and ``dc:creator``, because a filename is
the whole interface between this tool and the reader the books end up on: a
Kobo sorts by what the filename says, and Apple's package names are usually
just the title.

The rules encoded here were measured against a real 2,805-book library rather
than guessed. Where a number appears in a comment, that is where it came from.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them. Explicit empty-list comparisons read better than truthiness here.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=use-implicit-booleaness-not-comparison,too-few-public-methods

from pathlib import Path

from epubconvert import archive, planning, run, validate
from epubconvert.naming import (
    MAX_FILENAME_BYTES,
    MetadataNaming,
    PassthroughNaming,
    encode_name,
)
from epubconvert.validate import Package
from tests.conftest import make_metadata_package, make_package


def book(**fields: str | None) -> Package:
    """A parsed package carrying only the metadata a name is built from."""
    return Package(opf_path="OEBPS/content.opf", **fields)  # type: ignore[arg-type]


class TestTheNameComesFromTheBook:
    def test_author_and_title_are_joined(self):
        policy = MetadataNaming()

        name = policy.filename(
            "A_Wizard_of_Earthsea.epub",
            book(title="A Wizard of Earthsea", creator_sort="Le Guin, Ursula K."),
        )

        assert name == "Le Guin, Ursula K. - A Wizard of Earthsea.epub"

    def test_the_publishers_sort_name_wins(self):
        # 74% of the surveyed library supplies a sort name. It beats guessing.
        policy = MetadataNaming()

        name = policy.filename(
            "Earthsea.epub",
            book(
                title="A Wizard of Earthsea",
                creator="Ursula K. Le Guin",
                creator_sort="Le Guin, Ursula K.",
            ),
        )

        assert name.startswith("Le Guin, Ursula K. - ")

    def test_a_missing_sort_name_uses_the_creator_verbatim(self):
        # The other 26%. Inverting is not safe: 'Patterson, James' appears as
        # dc:creator text with no attribute to say it is already inverted, so a
        # split-on-last-space rule would produce 'James, Patterson'.
        policy = MetadataNaming()

        name = policy.filename(
            "Ninja.epub", book(title="Ninja", creator="Patterson, James")
        )

        assert name == "Patterson, James - Ninja.epub"

    def test_a_display_order_creator_is_not_rearranged(self):
        policy = MetadataNaming()

        name = policy.filename(
            "Notes.epub", book(title="Notes", creator="Ursula K. Le Guin")
        )

        assert name == "Ursula K. Le Guin - Notes.epub"

    def test_a_book_with_no_creator_is_named_by_title_alone(self):
        # 46 books of 2,804. The directory name is usually the title anyway, so
        # the title is the more honest answer than falling back to the folder.
        policy = MetadataNaming()

        name = policy.filename("Dracula .epub", book(title="Dracula"))

        assert name == "Dracula.epub"

    def test_a_book_with_no_title_keeps_its_directory_name(self):
        policy = MetadataNaming()

        name = policy.filename("Whatever This Is.epub", book(creator="Nobody"))

        assert name == "Whatever This Is.epub"

    def test_unreadable_metadata_keeps_the_directory_name(self):
        # read_package_dir raises for one package in the surveyed library.
        policy = MetadataNaming()

        assert policy.filename("Broken.epub", None) == "Broken.epub"


class TestGeneratedNamesAreSafeToWrite:
    """A directory name was already a valid filename. A title is not."""

    def test_characters_other_filesystems_reject_are_replaced(self):
        policy = MetadataNaming()

        name = policy.filename(
            "Sapiens.epub",
            book(title="Sapiens: A Brief History", creator_sort="Harari, Yuval Noah"),
        )

        assert ":" not in name
        assert name.endswith(".epub")

    def test_a_path_separator_in_a_title_cannot_escape_the_directory(self):
        policy = MetadataNaming()

        name = policy.filename("Ratio.epub", book(title="../../etc/passwd"))

        assert "/" not in name

    def test_an_overlong_name_is_clamped(self):
        # Four names in the surveyed library exceed 255 bytes; the longest is
        # 422, an anthology with sixteen '&'-joined authors.
        policy = MetadataNaming()

        name = policy.filename(
            "Anthology.epub", book(title="Story " * 80, creator="Author " * 20)
        )

        assert len(encode_name(name)) <= MAX_FILENAME_BYTES
        assert name.endswith(".epub")

    def test_a_title_of_only_illegal_characters_still_ends_in_epub(self):
        # The glob that decides what is already exported is '*.epub'. A name
        # that loses its extension is re-converted on every run for ever.
        policy = MetadataNaming()

        name = policy.filename("Odd.epub", book(title="???"))

        assert name.endswith(".epub")


class TestTheAuthorIsSanitisedToo:
    """
    The author is attacker-controlled in exactly the way the title is: both
    come out of a package document the tool did not write. Every test above
    aims at the title, which is the shape of defect this codebase has shipped
    twice -- a rule fixed and tested at one call site of two.

    Composition happens before sanitising, so one pass covers both halves.
    These pin that, so a refactor that sanitised the title alone would fail
    rather than pass quietly.
    """

    @staticmethod
    def _named(creator: str, title: str = "Dune") -> str:
        return MetadataNaming().filename(
            "Book.epub", book(title=title, creator=creator)
        )

    def test_a_path_separator_in_the_author_cannot_escape_the_directory(self):
        name = self._named("../../etc/passwd")

        assert "/" not in name
        assert name.endswith(".epub")

    def test_a_windows_separator_in_the_author_is_replaced(self):
        assert "\\" not in self._named("a\\b")

    def test_control_characters_are_removed_from_the_author(self):
        name = self._named("Name\x00hidden\nsecond")

        assert not [char for char in name if ord(char) < 32]

    def test_characters_other_filesystems_reject_are_replaced_in_the_author(self):
        name = self._named('Quote"Colon:Star*Pipe|')

        assert not [char for char in name if char in '<>:"/\\|?*']

    def test_an_overlong_author_is_clamped(self):
        name = self._named("Surname, " * 60)

        assert len(encode_name(name)) <= MAX_FILENAME_BYTES
        assert name.endswith(".epub")

    def test_an_author_that_cleans_away_to_nothing_is_treated_as_absent(self):
        # '..' and '?' survive the truthiness check but not the cleaning, and
        # the join then left a separator with nothing in front of it.
        for creator in ["..", ".", "?", "***"]:
            assert self._named(creator) == "Dune.epub", creator


class TestTheTitleSurvivesALongAuthorList:
    """
    The title is how a person finds the book; the author list is often a dozen
    contributors to an anthology. Clamping trimmed the end of the composed
    name, which is the title, so a real book came out as

        Peralta, Samuel & ... & Wecks, Erik - The Time Travel.epub

    with every one of fourteen authors intact and "Chronicles" gone. The budget
    is spent on the author now, and the title is kept whole.
    """

    @staticmethod
    def _named(creator: str, title: str) -> str:
        return MetadataNaming().filename(
            "Book.epub", book(title=title, creator=creator)
        )

    def test_a_joined_author_list_collapses_to_the_first_name(self):
        authors = " & ".join(f"Surname{index}, Given" for index in range(14))

        name = self._named(authors, "The Time Travel Chronicles")

        assert name == "Surname0, Given et al. - The Time Travel Chronicles.epub"

    def test_the_real_anthology_keeps_its_whole_title(self):
        authors = (
            "Peralta, Samuel & Sawyer, Robert J. & Walker, Rysa & Bale, Lucas"
            " & Vicino, Anthony & Lindsey, Ernie & Davis, Carol & Bolz, Stefan"
            " & Christy, Ann & Banghart, Tracy & Holden, Michael"
            " & Smith, Daniel Arthur & Luis, Ernie & Wecks, Erik"
        )

        name = self._named(authors, "The Time Travel Chronicles")

        assert name.endswith(" - The Time Travel Chronicles.epub")
        assert name.startswith("Peralta, Samuel et al.")
        assert len(encode_name(name)) <= MAX_FILENAME_BYTES

    def test_two_contributors_are_both_named(self):
        name = self._named("Pratchett, Terry & Gaiman, Neil", "Good Omens")

        assert name == "Pratchett, Terry & Gaiman, Neil - Good Omens.epub"

    def test_three_contributors_collapse(self):
        name = self._named("Adams, A & Brown, B & Clark, C", "Short Title")

        assert name == "Adams, A et al. - Short Title.epub"

    def test_a_long_list_collapses_even_when_it_would_fit(self):
        # The defect: collapsing was conditional on the byte budget, so a
        # twelve-author list that came to 244 bytes was written out in full.
        # A 244-byte filename is not a usable filename.
        authors = (
            "Peralta, Samuel & Webb, Nick & Weil, Raymond L. & Scott, Jasper T."
            " & Wells, Jennifer Foehner & Adams, David & Jennsen, G. S."
            " & DaCosta, Pippa & Thyer, Matthew Alan & Reher, Chris"
            " & Savage, Felix R. & Wilson, Nicolas"
        )

        name = self._named(authors, "The Galaxy Chronicles")

        assert name == "Peralta, Samuel et al. - The Galaxy Chronicles.epub"

    def test_the_author_prefix_survives_a_runaway_title(self):
        # One book's dc:title is its entire jacket blurb. Dropping the author
        # there defeats the only thing this policy exists to do -- the shelf
        # stops sorting by author for exactly the books that need it most.
        blurb = "There are five children in this book: " * 8

        name = self._named("Aung Myo Min", blurb)

        assert name.startswith("Aung Myo Min - ")
        assert len(encode_name(name)) <= MAX_FILENAME_BYTES
        assert name.endswith(".epub")

    def test_a_trimmed_title_does_not_end_mid_word(self):
        # "...watch television a" is a damaged name; "...watch television" is a
        # deliberate one. The bytes are the same either way.
        blurb = "There are five children in this book: " * 8

        name = self._named("Aung Myo Min", blurb)

        stem = name[: -len(".epub")]
        assert not stem.endswith(" a")
        assert stem == stem.rstrip(" .,;:-")

    def test_a_title_with_no_spaces_is_still_trimmed(self):
        # Nothing to back off to, so a hard cut is the only option.
        name = self._named("Author, An", "A" * 400)

        assert len(encode_name(name)) <= MAX_FILENAME_BYTES
        assert name.startswith("Author, An - AAA")

    def test_backing_off_never_costs_most_of_the_title(self):
        # A single space early in a long title must not throw the rest away.
        name = self._named("Author, An", "A " + "B" * 400)

        assert len(encode_name(name)) > MAX_FILENAME_BYTES // 2

    def test_the_author_never_takes_more_than_its_share(self):
        # A single enormous author must not squeeze the title out either.
        name = self._named("Surname" * 60, "A Reasonable Title")

        author_part = name.split(" - ")[0]
        assert len(encode_name(author_part)) <= MAX_FILENAME_BYTES // 3
        assert name.endswith(" - A Reasonable Title.epub")

    def test_a_single_overlong_author_is_truncated_not_the_title(self):
        name = self._named("Surname" * 60, "A Short Title")

        assert name.endswith(" - A Short Title.epub")
        assert len(encode_name(name)) <= MAX_FILENAME_BYTES

    def test_a_title_that_alone_exceeds_the_budget_is_still_named(self):
        # One book's dc:title is its whole jacket blurb. Nothing can be
        # protected there, but the name still has to be writable and end .epub.
        name = self._named("Aung Myo Min", "There are five children in this book: " * 8)

        assert len(encode_name(name)) <= MAX_FILENAME_BYTES
        assert name.endswith(".epub")

    def test_every_shortening_stays_within_the_budget(self):
        for count in (2, 5, 14, 40):
            authors = " & ".join(f"Surname{index}, Given" for index in range(count))
            name = self._named(authors, "A Reasonably Long Anthology Title Here")

            assert len(encode_name(name)) <= MAX_FILENAME_BYTES, count
            assert name.endswith(".epub"), count


class TestIdentityRoundTrips:
    """The output directory stays the sole record of completed work."""

    def test_a_generated_name_identifies_itself(self):
        policy = MetadataNaming()
        package = book(title="A Wizard of Earthsea", creator_sort="Le Guin, Ursula K.")

        name = policy.filename("Earthsea.epub", package)

        assert policy.identity(name) == policy.identity(
            policy.filename("Earthsea.epub", package)
        )

    def test_the_policy_declares_that_it_needs_metadata(self):
        # The planner skips the per-package read entirely for policies that do
        # not need it, so a default rerun still opens no source files.
        assert MetadataNaming().needs_metadata is True


class TestThePlannerSuppliesTheMetadata:
    """
    Composition is useless unless the planner reads the book. It must also
    *not* read it for the policies that do not ask, or a no-op rerun over
    thousands of books stops being free.
    """

    def test_a_planned_name_comes_from_the_package_document(self, tmp_path):
        library = tmp_path / "lib"
        make_metadata_package(
            library,
            "Earthsea.epub",
            title="A Wizard of Earthsea",
            file_as="Le Guin, Ursula K.",
        )

        assigned = planning.assign_names(
            list(archive.collect_package_dirs(library)), MetadataNaming(), planning.SKIP
        )

        assert [item.filename for item in assigned] == [
            "Le Guin, Ursula K. - A Wizard of Earthsea.epub"
        ]

    def test_a_package_that_cannot_be_parsed_keeps_its_directory_name(self, tmp_path):
        library = tmp_path / "lib"
        make_package(library, "Unparsable.epub")

        assigned = planning.assign_names(
            list(archive.collect_package_dirs(library)), MetadataNaming(), planning.SKIP
        )

        assert [item.filename for item in assigned] == ["Unparsable.epub"]

    def test_the_default_policy_reads_no_package_documents(self, tmp_path, monkeypatch):
        # The invariant this feature is most likely to cost. Measured on a real
        # library: a no-op rerun over 2,805 books opens zero source files.
        library = tmp_path / "lib"
        for index in range(3):
            make_metadata_package(library, f"Book {index}.epub", title=f"Book {index}")
        reads: list[Path] = []
        monkeypatch.setattr(planning, "read_package_dir", reads.append)

        planning.assign_names(
            list(archive.collect_package_dirs(library)),
            PassthroughNaming(),
            planning.SKIP,
        )

        assert reads == []

    def test_the_metadata_policy_reads_each_package_once(self, tmp_path, monkeypatch):
        library = tmp_path / "lib"
        for index in range(3):
            make_metadata_package(library, f"Book {index}.epub", title=f"Book {index}")
        reads: list[Path] = []
        original = validate.read_package_dir

        def counting(package: Path) -> Package:
            reads.append(package)
            return original(package)

        monkeypatch.setattr(planning, "read_package_dir", counting)

        planning.assign_names(
            list(archive.collect_package_dirs(library)), MetadataNaming(), planning.SKIP
        )

        assert len(reads) == 3


class TestRenamedBooksRerunSafely:
    """The whole point of the output directory being the sole record."""

    def test_a_second_run_does_not_export_again(self, tmp_path, output_dir, capsys):
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
                "-m",
                "0",
                "--name-by",
                "author-title",
                "-q",
            ]
        )
        written = output_dir / "Le Guin, Ursula K. - A Wizard of Earthsea.epub"
        stamp = written.stat().st_mtime_ns
        capsys.readouterr()

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

        assert written.stat().st_mtime_ns == stamp
        assert len(list(output_dir.glob("*.epub"))) == 1

    def test_the_flag_changes_the_written_name(self, tmp_path, output_dir):
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
                "-m",
                "0",
                "--name-by",
                "author-title",
                "-q",
            ]
        )

        assert [p.name for p in output_dir.glob("*.epub")] == [
            "Le Guin, Ursula K. - A Wizard of Earthsea.epub"
        ]

    def test_the_default_is_unchanged(self, tmp_path, output_dir):
        library = tmp_path / "lib"
        make_metadata_package(
            library,
            "Earthsea.epub",
            title="A Wizard of Earthsea",
            file_as="Le Guin, Ursula K.",
        )

        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])

        assert [p.name for p in output_dir.glob("*.epub")] == ["Earthsea.epub"]

    def test_adopting_the_policy_leaves_the_old_file_as_an_orphan(
        self, tmp_path, output_dir, capsys
    ):
        # Renaming a whole shelf is exactly when shelf-side blindness costs
        # something. Orphan reporting landed first for this.
        library = tmp_path / "lib"
        make_metadata_package(
            library,
            "Earthsea.epub",
            title="A Wizard of Earthsea",
            file_as="Le Guin, Ursula K.",
        )
        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])
        capsys.readouterr()

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

        assert "1 orphaned" in capsys.readouterr().out


class TestTheRunSaysWhenItFellBack:
    """
    A shelf that is half 'Author - Title.epub' and half folder names is a
    surprise worth one line of output. Saying nothing meant the only way to
    notice was to look at the directory afterwards and wonder.
    """

    def test_an_unreadable_package_is_counted(self, tmp_path, output_dir, capsys):
        library = tmp_path / "lib"
        make_metadata_package(library, "Known.epub", title="Dune", file_as="Herbert")
        make_package(library / "x", "Unparsable.epub")

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

        assert "1 book(s) kept their folder name" in capsys.readouterr().err

    def test_a_book_with_no_title_is_counted(self, tmp_path, output_dir, capsys):
        library = tmp_path / "lib"
        make_metadata_package(library, "Known.epub", title="Dune", file_as="Herbert")
        make_metadata_package(library / "x", "Untitled.epub", title="")

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

        assert "1 book(s) kept their folder name" in capsys.readouterr().err

    def test_nothing_is_said_when_every_book_was_named(
        self, tmp_path, output_dir, capsys
    ):
        library = tmp_path / "lib"
        make_metadata_package(library, "Known.epub", title="Dune", file_as="Herbert")

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

        assert "kept their folder name" not in capsys.readouterr().err

    def test_the_default_policy_says_nothing(self, tmp_path, output_dir, capsys):
        library = tmp_path / "lib"
        make_package(library, "Unparsable.epub")

        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])

        assert "kept their folder name" not in capsys.readouterr().err
