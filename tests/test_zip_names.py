"""
Tests for member names an archive does not flag as UTF-8.

OCF requires every member name in UTF-8, but the zip format says so only with
general-purpose bit 11, and Info-ZIP -- ``zip -X0 book.epub mimetype`` then
``zip -rX9 book.epub META-INF OEBPS``, the recipe every guide to making an
epub by hand gives -- writes the UTF-8 bytes of ``第1章.xhtml`` without it.
zipfile reads an unflagged name as cp437, so it handed back ``τ¼¼1τ½á.xhtml``,
and each reader that compared that with a name the book declares called the
member missing. A refresh rewrote it under the cp437 reading, flagged UTF-8,
and so renamed it for good.

The archives here are written by zipfile and the flag cleared afterwards, so
nothing depends on a ``zip`` binary being installed.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them. Explicit empty-list comparisons read better than truthiness here.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=use-implicit-booleaness-not-comparison,too-few-public-methods

import ast
import warnings
from pathlib import Path
from zipfile import ZIP_BZIP2, ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

import pytest

from epubconvert.collect import annotations
from epubconvert.collect.package import member_name, read_archive_package
from epubconvert.collect.validate import ArchiveInvalidError, validate_archive
from epubconvert.export import archive as shelf
from epubconvert.run import annotating, run
from epubconvert.utils import exits
from tests.test_annotations import highlight, library_row, make_databases

#: The member whose name has bytes past ASCII.
CHAPTER = "OEBPS/第1章.xhtml"

#: A small sound book, in the order Info-ZIP's recipe writes it.
BOOK = {
    "mimetype": "application/epub+zip",
    "META-INF/container.xml": (
        '<container version="1.0"'
        ' xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
        '<rootfile full-path="OEBPS/content.opf"'
        ' media-type="application/oebps-package+xml"/>'
        "</rootfiles></container>"
    ),
    "OEBPS/content.opf": (
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0"'
        ' unique-identifier="id"><metadata'
        ' xmlns:dc="http://purl.org/dc/elements/1.1/">'
        '<dc:identifier id="id">urn:uuid:12345678-1234-1234-1234-123456789abc'
        "</dc:identifier><dc:title>T</dc:title><dc:language>ja</dc:language>"
        '</metadata><manifest><item id="c1" href="第1章.xhtml"'
        ' media-type="application/xhtml+xml"/></manifest>'
        '<spine><itemref idref="c1"/></spine></package>'
    ),
    CHAPTER: (
        '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>x</title>'
        "</head><body><p>x</p></body></html>"
    ),
}


def unflagged(
    path: Path,
    members: dict[str, str] | list[tuple[str, str]],
    *,
    method: int = ZIP_DEFLATED,
    only: int | None = None,
) -> Path:
    """
    Write an archive whose member names are UTF-8 but not flagged as such.

    zipfile flags every name past ASCII, so the flag is cleared afterwards, in
    each local header and in each central directory entry: Info-ZIP leaves it
    clear in both, and zipfile refuses an archive whose two disagree.

    :param members: Names and contents, in order; a list may repeat a name.
    :param method: How every member but ``mimetype`` is compressed.
    :param only: The position of the one entry to clear, or None for all.
    """
    listed = list(members.items()) if isinstance(members, dict) else members
    with warnings.catch_warnings(), ZipFile(path, "w") as writing:
        warnings.simplefilter("ignore")  # "Duplicate name", when one is meant
        for name, text in listed:
            chosen = ZIP_STORED if name == "mimetype" else method
            writing.writestr(name, text, compress_type=chosen)
    raw = bytearray(path.read_bytes())
    with ZipFile(path) as reading:
        entries = reading.infolist()
    end = raw.rindex(b"PK\x05\x06")
    offset = int.from_bytes(raw[end + 16 : end + 20], "little")
    for position, info in enumerate(entries):
        # Bit 11 of the flags is bit 3 of their second byte: offset 7 of a
        # local header, 9 of a directory entry.
        if only is None or position == only:
            raw[info.header_offset + 7] &= ~0x08 & 0xFF
            raw[offset + 9] &= ~0x08 & 0xFF
        lengths = (raw[offset + 28 : offset + 30], raw[offset + 30 : offset + 32])
        comment = raw[offset + 32 : offset + 34]
        offset += 46 + sum(int.from_bytes(n, "little") for n in (*lengths, comment))
    path.write_bytes(bytes(raw))
    return path


def names(path: Path) -> list[str]:
    """Member names as zipfile hands them back, flagged or not."""
    with ZipFile(path) as reading:
        return reading.namelist()


class TestTheHelperBuildsWhatInfoZipWrites:
    def test_the_name_is_read_as_cp437(self, tmp_path):
        # The premise of every test below: zipfile does not see the real name.
        path = unflagged(tmp_path / "Book.epub", BOOK)

        assert CHAPTER not in names(path)
        assert CHAPTER.encode("utf-8").decode("cp437") in names(path)


class TestAnUnflaggedNameIsReadAsUTF8:
    def test_utf8_bytes_are_read_as_utf8(self):
        info = ZipInfo(CHAPTER.encode("utf-8").decode("cp437"))

        assert member_name(info) == CHAPTER

    def test_a_flagged_name_is_left_as_zipfile_read_it(self):
        info = ZipInfo(CHAPTER)
        info.flag_bits |= 0x800

        assert member_name(info) == CHAPTER

    def test_bytes_that_are_not_utf8_keep_the_cp437_reading(self):
        # b"caf\x82" is café in cp437 and no UTF-8 at all: a name some old
        # DOS-era zip wrote, which there is no better reading of.
        info = ZipInfo("café.xhtml")

        assert member_name(info) == "café.xhtml"


class TestARefreshKeepsTheRealName:
    def test_the_rebuild_writes_the_member_under_its_utf8_name(self, tmp_path):
        path = unflagged(tmp_path / "Book.epub", BOOK)
        note: dict[str, object] = {"id": "u1", "text": "x", "book": {"title": "T"}}

        assert shelf.replace_annotations(path, [note])

        assert CHAPTER in names(path)
        assert validate_archive(path) == []

    def test_a_refresh_through_the_command_leaves_the_book_sound(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ):
        library, out = tmp_path / "lib", tmp_path / "out"
        package = library / "Book.epub"
        for name, text in BOOK.items():
            (package / name).parent.mkdir(parents=True, exist_ok=True)
            (package / name).write_text(text, encoding="utf-8")
        out.mkdir()
        # The book as exported earlier by the shell recipe, not by this tool.
        book = unflagged(out / "Book.epub", BOOK)
        make_databases(
            tmp_path / "container",
            rows=[highlight(asset="A1")],
            books=[library_row(asset="A1", path=str(package))],
        )
        monkeypatch.setattr(
            annotating,
            "collect_annotations",
            lambda policy=None: annotations.collect(tmp_path / "container", policy),
        )

        code = run.main(["-s", str(library), "-o", str(out), "-ae", "-ar", "-q"])

        assert code == exits.SUCCESS
        assert CHAPTER in names(book)
        assert annotations.EMBEDDED_PATH in names(book)
        assert validate_archive(book) == []


#: A note to refresh a book with.
NOTE: dict[str, object] = {
    "id": "u1",
    "text": "x",
    "book": {"title": "T", "source": "B.epub"},
}


class TestRuleEveryReaderKnowsAMemberByItsUTF8Name:
    """
    A member is named by ``member_name``, wherever an archive is read.

    Sites: ``validate_archive``'s names, and through them the manifest,
    duplicate and case-fold checks; ``_check_mimetype``; ``_first_corrupt``;
    ``_check_methods``; ``repeated_entries``; ``open_member``'s refusal;
    ``_ArchiveMembers``, which every read of a package document goes through;
    and ``replace_annotations``.
    """

    def test_no_reader_asks_zipfile_for_a_name(self):
        # zipfile's own name for a member is its cp437 reading when the flag
        # is clear, so a lookup by name, a listing of names or a ZipInfo's
        # filename is a reader this rule missed. Derived, so a new one fails.
        offenders = []
        for path in sorted(Path("epubconvert").rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            reads_archives = "from zipfile import" in text
            tree = ast.parse(text)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Attribute):
                    continue
                if node.attr in _ASKED_BY_NAME:
                    offenders.append(f"{path}:{node.lineno} {node.attr}")
                if node.attr != "filename" or not reads_archives:
                    continue
                # An OSError's filename, in archive._shown, is a path on disk.
                if _inside(tree, node, "_shown") or _inside(tree, node, "member_name"):
                    continue
                offenders.append(f"{path}:{node.lineno} filename")

        assert offenders == [], f"name a member through member_name: {offenders}"

    def test_verify_finds_every_manifest_item(self, tmp_path):
        assert validate_archive(unflagged(tmp_path / "Book.epub", BOOK)) == []

    def test_a_package_document_named_past_ascii_is_found(self, tmp_path):
        book = {
            name.replace("OEBPS/", "本/"): text.replace("OEBPS/", "本/")
            for name, text in BOOK.items()
        }
        path = unflagged(tmp_path / "Book.epub", book)

        package = read_archive_package(path)

        assert package.opf_path == "本/content.opf"
        assert package.manifest == {"c1": "本/第1章.xhtml"}

    def test_a_name_flagged_once_and_once_not_is_a_duplicate(self, tmp_path):
        # The same bytes twice, which zipfile reads two ways. OCF sees one name.
        listed = [*BOOK.items(), (CHAPTER, "again")]
        path = unflagged(tmp_path / "Book.epub", listed, only=len(listed) - 1)

        problems = validate_archive(path)

        assert f"member name appears more than once: {CHAPTER}" in problems

    def test_names_differing_by_case_are_found_on_the_real_names(self, tmp_path):
        listed = [*BOOK.items(), ("OEBPS/Ä.xhtml", "x"), ("OEBPS/ä.xhtml", "y")]
        path = unflagged(tmp_path / "Book.epub", listed, only=len(listed) - 1)

        problems = validate_archive(path)

        assert any("differ only by case" in problem for problem in problems)

    def test_the_first_member_is_named_as_it_is(self, tmp_path):
        listed = [(CHAPTER, BOOK[CHAPTER]), *list(BOOK.items())[:-1]]
        path = unflagged(tmp_path / "Book.epub", listed)

        problems = validate_archive(path)

        assert f"first member is {CHAPTER!r}, not 'mimetype'" in problems

    def test_a_corrupt_member_is_named_as_it_is(self, tmp_path):
        path = unflagged(tmp_path / "Book.epub", BOOK, method=ZIP_STORED)
        raw = path.read_bytes()
        at = raw.rindex(b"<p>x</p>")  # the chapter's text, stored
        path.write_bytes(raw[:at] + b"<p>y</p>" + raw[at + 8 :])

        assert f"corrupt member: {CHAPTER}" in validate_archive(path)

    def test_a_member_in_a_forbidden_method_is_named_as_it_is(self, tmp_path):
        path = unflagged(tmp_path / "Book.epub", BOOK, method=ZIP_BZIP2)

        problems = validate_archive(path)

        assert any(problem.endswith(f": {CHAPTER}") for problem in problems)

    def test_a_refresh_refuses_a_name_listed_twice(self, tmp_path):
        # Rebuilt, both would be written under the one real name.
        listed = [*BOOK.items(), (CHAPTER, "again")]
        path = unflagged(tmp_path / "Book.epub", listed, only=len(listed) - 1)

        with pytest.raises(ArchiveInvalidError, match="more than once"):
            shelf.replace_annotations(path, [NOTE])

    def test_a_refresh_names_a_member_it_may_not_inflate(self, tmp_path):
        # The chapter straight after mimetype, so it is the first refused.
        first, *rest = BOOK.items()
        listed = [first, (CHAPTER, BOOK[CHAPTER]), *rest[:-1]]
        path = unflagged(tmp_path / "Book.epub", listed, method=ZIP_BZIP2)

        with pytest.raises(NotImplementedError, match=CHAPTER):
            shelf.replace_annotations(path, [NOTE])


#: What asks zipfile for a member by its own reading of the name.
_ASKED_BY_NAME = frozenset({"namelist", "getinfo", "NameToInfo"})


def _inside(tree: ast.Module, node: ast.AST, function: str) -> bool:
    """Whether *node* lies within the named function of *tree*."""
    for candidate in ast.walk(tree):
        if isinstance(candidate, ast.FunctionDef) and candidate.name == function:
            return any(child is node for child in ast.walk(candidate))
    return False
