"""
Tests for where the bytes of an archive sit, which the OCF specification fixes.

A reader identifies an epub by its first bytes: the ``mimetype`` member's local
header at offset 0, its name, and ``application/epub+zip`` at offset 38. The
central directory is an index written at the end, and its order is whatever
the writer chose, so it says nothing about what comes first in the file.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=use-implicit-booleaness-not-comparison,too-few-public-methods
# pylint: disable=protected-access

import io
import unicodedata
import warnings
from pathlib import Path
from zipfile import ZIP_STORED, ZipFile, ZipInfo

import pytest

from epubconvert.collect import validate
from epubconvert.collect.package import find_opf_path
from epubconvert.export.archive import replace_annotations, zip_package
from epubconvert.export.naming import filesystem_key
from epubconvert.run.run import main
from epubconvert.utils import exits
from epubconvert.utils.spec import fold_name
from tests.conftest import make_metadata_package
from tests.test_validate import MEMBERS, write_epub


class TestMimetypeIsPhysicallyFirst:
    def test_an_index_listing_mimetype_first_does_not_make_it_first(
        self, tmp_path: Path
    ):
        # _check_mimetype asked namelist(), which is central-directory order.
        # An archive whose index lists mimetype first while its bytes come
        # after another member's passed, and a reader sniffing offset 0 found
        # something else there.
        path = tmp_path / "Reindexed.epub"
        with ZipFile(path, "w") as archive:
            for name, body in MEMBERS.items():
                archive.writestr(name, body)
            archive.writestr(
                ZipInfo("mimetype"), "application/epub+zip", compress_type=ZIP_STORED
            )
            archive.filelist.sort(key=lambda info: info.filename != "mimetype")
        with ZipFile(path) as archive:
            assert archive.namelist()[0] == "mimetype"

        problems = validate.validate_archive(path)

        assert any("mimetype" in problem and "first" in problem for problem in problems)

    def test_bytes_ahead_of_the_archive_move_mimetype_off_the_start(
        self, tmp_path: Path
    ):
        # zipfile reads an archive with a stub prepended, and reports offsets
        # from the start of the file, so the stub is what a reader sees first.
        good = write_epub(tmp_path / "Good.epub")
        path = tmp_path / "Stubbed.epub"
        path.write_bytes(b"#!/bin/sh\n" + good.read_bytes())

        problems = validate.validate_archive(path)

        assert any("mimetype" in problem and "first" in problem for problem in problems)

    def test_mimetype_stored_first_but_indexed_later_is_first(self, tmp_path: Path):
        # The other half of the same mistake: the check asked the index which
        # member came first, so a sound archive whose writer happened to list
        # mimetype later in its central directory was reported damaged.
        path = tmp_path / "IndexedLater.epub"
        with ZipFile(path, "w") as archive:
            archive.writestr(
                ZipInfo("mimetype"), "application/epub+zip", compress_type=ZIP_STORED
            )
            for name, body in MEMBERS.items():
                if name != "mimetype":
                    archive.writestr(name, body)
            archive.filelist.sort(key=lambda info: info.filename == "mimetype")
        with ZipFile(path) as archive:
            assert archive.namelist()[-1] == "mimetype"
            assert archive.getinfo("mimetype").header_offset == 0

        assert validate.validate_archive(path) == []

    def test_a_refresh_keeps_mimetype_first_whatever_the_index_says(
        self, tmp_path: Path
    ):
        # The refresh copied members in the central directory's order, so a
        # book storing mimetype first but listing it last passed --verify,
        # and was rewritten with mimetype last, which --verify then rejected.
        path = tmp_path / "IndexedLater.epub"
        with ZipFile(path, "w") as archive:
            archive.writestr(
                ZipInfo("mimetype"), "application/epub+zip", compress_type=ZIP_STORED
            )
            for name, body in MEMBERS.items():
                archive.writestr(name, body)
            archive.filelist.append(archive.filelist.pop(0))
        assert validate.validate_archive(path) == []

        assert replace_annotations(path, [{"id": "A", "text": "hi"}])

        with ZipFile(path) as archive:
            first = min(archive.infolist(), key=lambda info: info.header_offset)
            assert first.filename == "mimetype"
            assert [info.filename for info in archive.infolist()][:-1] == [
                "mimetype",
                *MEMBERS,
            ]
        assert validate.validate_archive(path) == []

    def test_a_refresh_moves_mimetype_first_from_where_it_was_stored(
        self, tmp_path: Path
    ):
        # Copied in stored order, a book listing mimetype first but storing
        # it last was rewritten as damaged as it came; a refresh repairs it,
        # and the other members keep the order they were stored in.
        path = tmp_path / "StoredLater.epub"
        with ZipFile(path, "w") as archive:
            for name, body in MEMBERS.items():
                archive.writestr(name, body)
            archive.writestr(
                ZipInfo("mimetype"), "application/epub+zip", compress_type=ZIP_STORED
            )
            archive.filelist.sort(key=lambda info: info.filename != "mimetype")
        assert validate.validate_archive(path) != []

        assert replace_annotations(path, [{"id": "A", "text": "hi"}])

        with ZipFile(path) as archive:
            assert [info.filename for info in archive.infolist()][:-1] == [
                "mimetype",
                *MEMBERS,
            ]
        assert validate.validate_archive(path) == []

    def test_an_archive_written_properly_still_passes(self, tmp_path: Path):
        assert validate.validate_archive(write_epub(tmp_path / "Good.epub")) == []

    def test_what_this_tool_writes_still_passes(self, tmp_path: Path):
        package = make_metadata_package(tmp_path / "lib", "Book.epub", title="Book")
        target = tmp_path / "out" / "Book.epub"
        target.parent.mkdir()

        zip_package(package, target)

        with ZipFile(target) as archive:
            assert archive.getinfo("mimetype").header_offset == 0
        assert validate.validate_archive(target) == []


class TestMimetypeCarriesNoExtraField:
    """
    OCF asks for no extra field in ``mimetype``'s local header, and epubcheck
    fails a book for one (PKG-005), but readers open it: Info-ZIP given no
    ``-X`` writes one, which is how many books are zipped by hand. Such a book
    is copied through byte for byte, so calling it damaged sent --verify into
    a loop: move it aside, rerun, and the same bytes came back.
    """

    #: What Info-ZIP adds to every member unless given -X: a UT timestamp
    #: field with two times, and a ux field with a four-byte uid and gid.
    INFO_ZIP = (
        b"UT\x09\x00\x03"
        + bytes(8)
        + b"ux\x0b\x00\x01\x04"
        + bytes(4)
        + b"\x04"
        + bytes(4)
    )
    WARNING = (
        "mimetype carries a 28-byte extra field; OCF asks for none, readers open it"
    )

    def _written(self, path: Path, extra: bytes) -> Path:
        with ZipFile(path, "w") as archive:
            mimetype = ZipInfo("mimetype")
            mimetype.extra = extra
            archive.writestr(mimetype, "application/epub+zip")
            for name, body in MEMBERS.items():
                archive.writestr(name, body)
        return path

    def test_an_extra_field_is_a_warning_not_damage(self, tmp_path: Path):
        path = self._written(tmp_path / "Extra.epub", self.INFO_ZIP)
        assert path.read_bytes()[38:58] != b"application/epub+zip"

        verdict = validate.check_archive(path)

        assert verdict.problems == []
        assert verdict.warnings == [self.WARNING]
        assert validate.validate_archive(path) == []

    def test_the_local_header_is_the_one_judged(self, tmp_path: Path):
        # The central directory's copy is what zipfile reports, and the two
        # need not agree. The one at offset 0 is the one a reader sees.
        path = self._written(tmp_path / "LocalOnly.epub", self.INFO_ZIP)
        raw = bytearray(path.read_bytes())
        entry = raw.index(b"PK\x01\x02")  # mimetype's, the first listed
        raw[entry + 30 : entry + 32] = b"\x00\x00"
        raw[entry + 46 + 8 : entry + 46 + 8 + 28] = b""
        # The end record's directory size, 28 bytes shorter now.
        raw[-10:-6] = (int.from_bytes(raw[-10:-6], "little") - 28).to_bytes(4, "little")
        path.write_bytes(bytes(raw))
        with ZipFile(path) as archive:
            assert archive.getinfo("mimetype").extra == b""

        assert validate.check_archive(path).warnings == [self.WARNING]

    def test_a_damaged_local_header_is_left_to_the_read(self, tmp_path: Path):
        # No header, no extra field to measure: reading the member reports it.
        path = self._written(tmp_path / "Damaged.epub", b"")
        path.write_bytes(b"XX" + path.read_bytes()[2:])

        verdict = validate.check_archive(path)

        assert len(verdict.problems) == 1
        assert verdict.problems[0].startswith("not a readable zip archive")
        assert verdict.warnings == []

    def test_none_is_not_reported(self, tmp_path: Path):
        path = self._written(tmp_path / "Plain.epub", b"")

        assert validate.check_archive(path) == validate.Verdict([], [])

    def test_verify_says_it_once_and_passes_the_book(self, tmp_path: Path, capsys):
        library, shelf = tmp_path / "lib", tmp_path / "out"
        library.mkdir()
        self._written(library / "Hand Made.epub", self.INFO_ZIP)
        flags = ["-s", str(library), "-o", str(shelf), "-q"]
        assert main(flags) == exits.SUCCESS
        copied = (shelf / "Hand Made.epub").read_bytes()
        assert copied == (library / "Hand Made.epub").read_bytes()
        capsys.readouterr()

        code = main([*flags, "--verify"])

        out, err = capsys.readouterr()
        assert code == exits.SUCCESS
        assert err.count(f"Hand Made.epub: {self.WARNING}") == 1
        assert "0 damaged" in out
        assert "Move each" not in out
        assert main(flags) == exits.SUCCESS
        assert (shelf / "Hand Made.epub").read_bytes() == copied

    def test_an_export_checked_with_validate_warns_of_nothing(
        self, tmp_path: Path, capsys
    ):
        library, shelf = tmp_path / "lib", tmp_path / "out"
        make_metadata_package(library, "Book.epub", title="Book")

        code = main(["-s", str(library), "-o", str(shelf), "--validate", "-q"])

        assert code == exits.SUCCESS
        assert validate.check_archive(shelf / "Book.epub") == validate.Verdict([], [])
        assert "extra field" not in capsys.readouterr().err


class TestEveryMemberNameIsUnique:
    def test_two_members_with_one_name_are_reported(self, tmp_path: Path):
        # OCF requires unique names, and readers disagree about a duplicate:
        # some take the first local header, some the last directory entry. The
        # validator passed such an archive, and --verify called it sound.
        path = write_epub(tmp_path / "Doubled.epub")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)  # "Duplicate name"
            with ZipFile(path, "a") as archive:
                archive.writestr("OEBPS/text/chapter1.xhtml", "<html/>")

        problems = validate.validate_archive(path)

        assert problems == [
            "member name appears more than once: OEBPS/text/chapter1.xhtml"
        ]

    def test_every_duplicate_is_counted_but_five_are_named(self):
        names = [f"n{index}" for index in range(7)] * 2

        problems = validate._check_unique(names)

        assert problems[:5] == [
            f"member name appears more than once: n{index}" for index in range(5)
        ]
        assert problems[5:] == ["...and 2 more member name(s) appearing more than once"]

    def test_the_work_grows_with_the_names_not_their_square(self):
        # Each duplicate was looked for in a list of the duplicates found so
        # far: 80,000 names took 6.6 s. Counted by comparisons, not by time.
        compared = [0]

        class Name(str):
            __hash__ = str.__hash__

            def __eq__(self, other: object) -> bool:
                compared[0] += 1
                return str.__eq__(self, other)

        once: list[str] = [Name(f"d/{index:05d}") for index in range(2000)]
        names = once * 2

        validate._check_unique(names)

        assert compared[0] < 5 * len(names)


class TestNamesAFilesystemCannotTellApartAreReported:
    """
    OCF requires member names to stay unique after full case folding and
    NFC normalization, because a reader unpacking the book onto APFS, HFS+ or
    NTFS writes both names to one file. Only exact duplicates were reported,
    so ``chapter1.xhtml`` beside ``Chapter1.xhtml`` passed --verify.
    """

    @staticmethod
    def _with(path: Path, *extra: str) -> Path:
        write_epub(path)
        with ZipFile(path, "a") as archive:
            for name in extra:
                archive.writestr(name, "<html/>")
        return path

    def test_names_differing_only_by_case_are_reported(self, tmp_path: Path):
        path = self._with(tmp_path / "Case.epub", "OEBPS/text/Chapter1.xhtml")

        assert validate.validate_archive(path) == [
            "member names differ only by case or Unicode normalization: "
            "OEBPS/text/Chapter1.xhtml, OEBPS/text/chapter1.xhtml"
        ]

    def test_names_differing_only_by_normalization_are_reported(self, tmp_path: Path):
        composed, decomposed = "OEBPS/café.xhtml", "OEBPS/café.xhtml"
        path = self._with(tmp_path / "Nfd.epub", composed, decomposed)

        problems = validate.validate_archive(path)

        # Escaped, since both print as "café" and one name twice says nothing.
        assert problems == [
            "member names differ only by case or Unicode normalization: "
            "OEBPS/cafe\\u0301.xhtml, OEBPS/caf\\xe9.xhtml"
        ]

    def test_an_exact_duplicate_is_reported_once_and_as_such(self):
        names = ["a/x.xhtml", "a/x.xhtml"]

        assert validate._check_unique(names) == [
            "member name appears more than once: a/x.xhtml"
        ]

    def test_every_collision_is_counted_but_five_are_named(self):
        names = [f"n{index}" for index in range(7)]
        names += [name.upper() for name in names]

        problems = validate._check_unique(names)

        assert len(problems) == 6
        assert problems[0] == (
            "member names differ only by case or Unicode normalization: N0, n0"
        )
        assert problems[5] == (
            "...and 2 more member name(s) differing only by case or normalization"
        )

    def test_the_names_are_escaped(self):
        problems = validate._check_unique(["a\x1b[2K.xhtml", "A\x1b[2K.xhtml"])

        assert "\x1b" not in problems[0]

    def test_the_folding_is_the_one_naming_uses(self):
        # One rule, in utils/spec, for the writer and the validator alike.
        # keeps the two statements one rule.
        for name in ("Straße", "café", "İstanbul", "DUNE"):
            assert fold_name(name) == filesystem_key(name)

    def test_a_book_this_tool_writes_passes(self, tmp_path: Path):
        package = make_metadata_package(tmp_path / "src", "Book.epub", title="Book")
        zip_package(package, tmp_path / "Book.epub")

        assert validate.validate_archive(tmp_path / "Book.epub") == []


class TestFoldingIsCanonicalCaselessMatching:
    """
    ``casefold()`` of NFC text need not be NFC. Upper-case H-circumflex with a
    macron below folds to ``ĥ`` and a macron below, while the lower-case pair,
    written ``ẖ`` and a circumflex, stays apart: two names a case-insensitive
    filesystem cannot tell apart got two keys, so --verify passed both and the
    writer could replace one book with another.
    """

    @pytest.mark.parametrize(
        ("upper", "lower"),
        [("\u0124\u0331", "\u1e96\u0302"), ("\u0130\u0327", "i\u0327\u0307")],
    )
    def test_names_differing_only_by_case_share_a_key(self, upper, lower):
        assert fold_name(upper) == fold_name(lower)
        assert filesystem_key(f"{upper}.epub") == filesystem_key(f"{lower}.epub")

    def test_the_validator_reports_them(self, tmp_path: Path):
        path = TestNamesAFilesystemCannotTellApartAreReported._with(
            tmp_path / "Marks.epub", "\u0124\u0331.xhtml", "\u1e96\u0302.xhtml"
        )

        problems = validate.validate_archive(path)

        assert len(problems) == 1
        assert problems[0].startswith("member names differ only by case")

    @pytest.mark.parametrize(
        "name",
        ["Dune.epub", "Café Society.epub", "Straße.epub", "한국어 책.epub", "İstanbul"],
    )
    def test_an_ordinary_name_keeps_the_key_it_had(self, name):
        # What fold_name returned before, so a shelf's names still match.
        before = unicodedata.normalize("NFC", name).casefold()

        assert fold_name(name) == before

    def test_full_folding_is_kept(self):
        assert fold_name("Straße") == fold_name("STRASSE")


class TestTheContainerNamesThePackageDocument:
    """
    OCF names the package document as the first rootfile whose media type is
    ``application/oebps-package+xml``. The type was compared whole, so one
    carrying a parameter was missed, and without a match the first rootfile
    of any type was taken: a PDF listed ahead of an untyped package document.
    """

    PDF = '<rootfile full-path="book.pdf" media-type="application/pdf"/>'

    @staticmethod
    def _opf_path(rootfiles: str) -> str:
        container = (
            '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            f"<rootfiles>{rootfiles}</rootfiles></container>"
        )
        buffer = io.BytesIO()
        with ZipFile(buffer, "w") as archive:
            archive.writestr("META-INF/container.xml", container)
        with ZipFile(buffer) as archive:
            return find_opf_path(archive)

    @pytest.mark.parametrize(
        "media_type",
        [
            "application/oebps-package+xml; charset=utf-8",
            "Application/OEBPS-Package+XML ;charset=UTF-8",
            " application/oebps-package+xml ",
        ],
    )
    def test_a_parameter_or_case_does_not_hide_the_type(self, media_type):
        opf = f'<rootfile full-path="content.opf" media-type="{media_type}"/>'

        assert self._opf_path(self.PDF + opf) == "content.opf"

    def test_an_untyped_rootfile_is_preferred_to_one_of_another_type(self):
        untyped = '<rootfile full-path="content.opf"/>'

        assert self._opf_path(self.PDF + untyped) == "content.opf"

    def test_the_first_untyped_rootfile_is_the_one_taken(self):
        rootfiles = '<rootfile full-path="a.opf"/><rootfile full-path="b.opf"/>'

        assert self._opf_path(rootfiles) == "a.opf"

    def test_a_typed_one_still_wins_over_an_untyped_one_ahead(self):
        rootfiles = (
            '<rootfile full-path="a.opf"/><rootfile full-path="b.opf"'
            ' media-type="application/oebps-package+xml"/>'
        )

        assert self._opf_path(rootfiles) == "b.opf"

    def test_with_only_other_types_the_first_is_still_taken(self):
        other = '<rootfile full-path="c.xml" media-type="text/xml"/>'

        assert self._opf_path(self.PDF + other) == "book.pdf"
