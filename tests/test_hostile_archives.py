"""
Tests for archives built to cost more to read than they are worth.

An ``*.epub`` archive arrives from a library, a sideload or a shelf this tool
did not write, so what its central directory declares is a claim, not a bound,
and the directory itself may not even be readable. These cases cover what
believing either cost.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them.
# pylint: disable=missing-function-docstring,missing-class-docstring

import io
import json
import struct
import tracemalloc
import warnings
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from zipfile import ZIP_BZIP2, ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

import pytest

from epubconvert.collect import package as package_reader
from epubconvert.collect import validate
from epubconvert.collect.annotations import EMBEDDED_PATH
from epubconvert.collect.validate import ArchiveInvalidError
from epubconvert.export import archive
from epubconvert.run import run
from tests.conftest import make_metadata_package
from tests.test_validate import MEMBERS, write_epub

#: How much padding a bomb inflates to. Enough that decompressing it whole
#: stands far above :data:`PEAK`, small enough that the old behaviour costs a
#: test run nothing.
BOMB_BYTES = 32 * 1024 * 1024

#: What reading a bomb may allocate at most, once reads are bounded.
PEAK = 4 * 1024 * 1024

_CHUNK = b" " * (1024 * 1024)


def _epub_with_bomb(
    path: Path, member: str, method: int, *, declared: int | None = None
) -> Path:
    """
    Write an epub whose *member* inflates to :data:`BOMB_BYTES` of padding.

    With *declared*, the central directory claims the member is that size, as
    a hostile archive would, rather than the size it really inflates to.
    """
    bodies = {"mimetype": "application/epub+zip", **MEMBERS}
    bodies.setdefault(member, "")
    with ZipFile(path, "w") as opened:
        for name, body in bodies.items():
            if name != member:
                compressed = ZIP_STORED if name == "mimetype" else ZIP_DEFLATED
                opened.writestr(ZipInfo(name), body, compress_type=compressed)
                continue
            info = ZipInfo(name)
            info.compress_type = method
            with opened.open(info, "w") as writing:
                writing.write(body.encode())
                for _ in range(BOMB_BYTES // len(_CHUNK)):
                    writing.write(_CHUNK)
            if declared is not None:
                opened.getinfo(name).file_size = declared
    return path


@contextmanager
def _allocations() -> Iterator[Callable[[], int]]:
    """Trace allocations inside the block; the callable returns their peak."""
    peak = [0]
    tracemalloc.start()
    try:
        yield lambda: peak[0]
    finally:
        peak[0] = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()


class TestAMemberIsNeverInflatedWhole:
    """
    ``ZipFile.read`` decompresses a member's whole stream before it checks the
    size the directory declared: bzip2 and LZMA without any bound, deflate up
    to 1 GiB a call. A 2.4 KB epub made the reader allocate 1.9 GiB and raise a
    MemoryError nothing caught.
    """

    def test_a_package_document_compressed_with_bzip2_is_refused_unread(self, tmp_path):
        # OCF allows stored and deflate only, so no book needs another method.
        path = _epub_with_bomb(
            tmp_path / "Bomb.epub", "META-INF/container.xml", ZIP_BZIP2, declared=180
        )

        with (
            _allocations() as peak,
            ZipFile(path) as opened,
            pytest.raises(package_reader.ValidationError, match="bzip2"),
        ):
            package_reader.read_package(opened)

        assert peak() < PEAK

    def test_a_deflated_package_document_is_read_only_as_far_as_the_cap(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(package_reader, "MAX_XML_BYTES", 64 * 1024)
        path = _epub_with_bomb(
            tmp_path / "Bomb.epub", "META-INF/container.xml", ZIP_DEFLATED, declared=180
        )

        with (
            _allocations() as peak,
            ZipFile(path) as opened,
            pytest.raises(package_reader.ValidationError),
        ):
            package_reader.read_package(opened)

        assert peak() < PEAK

    def test_a_member_holding_more_than_the_cap_is_refused(self, tmp_path, monkeypatch):
        # zipfile stops at the declared size itself, so this is the backstop
        # for a reader that does not.
        path = tmp_path / "Book.epub"
        with ZipFile(path, "w") as opened:
            for name, body in MEMBERS.items():
                opened.writestr(name, body)
        monkeypatch.setattr(package_reader, "MAX_XML_BYTES", 1024)
        monkeypatch.setattr(
            ZipFile, "open", lambda *_args, **_kwargs: io.BytesIO(b" " * 1025)
        )

        with (
            ZipFile(path) as opened,
            pytest.raises(
                package_reader.ValidationError, match="larger than it declares"
            ),
        ):
            package_reader.read_package(opened)


class TestVerifyInflatesNothingItNeedNot:
    def test_a_member_compressed_with_bzip2_is_reported_not_inflated(self, tmp_path):
        # testzip() inflated every member, and bzip2 has no bound at all.
        path = _epub_with_bomb(
            tmp_path / "Bomb.epub", "OEBPS/text/chapter1.xhtml", ZIP_BZIP2, declared=64
        )

        with _allocations() as peak:
            problems = validate.validate_archive(path)

        assert peak() < PEAK
        assert problems == [
            "member is compressed with bzip2, which an epub may not use: "
            "OEBPS/text/chapter1.xhtml"
        ]

    def test_every_disallowed_member_is_counted_but_five_are_named(self, tmp_path):
        path = tmp_path / "Many.epub"
        with ZipFile(path, "w") as opened:
            opened.writestr(ZipInfo("mimetype"), "application/epub+zip")
            for index in range(7):
                opened.writestr(f"x{index}", "x", compress_type=ZIP_BZIP2)

        problems = validate.validate_archive(path)

        assert len(problems) == 6
        assert problems[-1] == "...and 2 more member(s) compressed a way OCF forbids"

    def test_a_compressed_mimetype_is_not_inflated_to_be_compared(self, tmp_path):
        path = _epub_with_bomb(
            tmp_path / "Bomb.epub", "mimetype", ZIP_DEFLATED, declared=20
        )

        with _allocations() as peak:
            problems = validate.validate_archive(path)

        assert peak() < PEAK
        assert "mimetype is compressed; it must be stored" in problems


class TestARefreshInflatesNothingWhole:
    def test_the_embedded_set_is_read_only_as_far_as_its_cap(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(archive, "MAX_EMBEDDED_BYTES", 64 * 1024)
        target = _epub_with_bomb(
            tmp_path / "Bomb.epub", EMBEDDED_PATH, ZIP_DEFLATED, declared=64
        )

        with (
            _allocations() as peak,
            pytest.raises(package_reader.UNREADABLE_MEMBER),
        ):
            archive.replace_annotations(target, [{"id": "mine"}])

        assert peak() < PEAK

    def test_an_embedded_set_larger_than_its_cap_is_replaced(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(archive, "MAX_EMBEDDED_BYTES", 1024)
        target = _epub_with_bomb(tmp_path / "Book.epub", EMBEDDED_PATH, ZIP_DEFLATED)

        assert archive.replace_annotations(target, [{"id": "mine"}]) is True
        with ZipFile(target) as reading:
            assert reading.read(EMBEDDED_PATH).startswith(b"{")

    def test_a_set_too_large_to_be_equal_is_replaced_unparsed(self, tmp_path):
        # Within the cap, but json.loads built a dict per "{}": the 64 MiB
        # the cap allows cost 1.6 GiB to compare against one annotation.
        held = '{"annotations": [' + "{}, " * (128 * 1024) + "{}]}"
        target = write_epub(tmp_path / "Book.epub", {**MEMBERS, EMBEDDED_PATH: held})

        with _allocations() as peak:
            rewritten = archive.replace_annotations(target, [{"id": "mine"}])

        assert peak() < PEAK
        assert rewritten is True
        with ZipFile(target) as reading:
            assert json.loads(reading.read(EMBEDDED_PATH))["annotations"] == [
                {"id": "mine"}
            ]

    def test_a_set_this_tool_wrote_is_current_without_being_parsed(
        self, tmp_path, monkeypatch
    ):
        target = write_epub(tmp_path / "Book.epub")
        archive.replace_annotations(target, [{"id": "a"}])
        before = target.read_bytes()
        monkeypatch.setattr(json, "loads", pytest.fail)

        assert archive.replace_annotations(target, [{"id": "a"}]) is False
        assert target.read_bytes() == before

    def test_an_equal_set_written_another_way_is_still_current(self, tmp_path):
        # Only the annotations are compared: an older release's generator, or
        # other spacing, is no reason to rewrite every book on the shelf.
        held = json.dumps({"generator": {"version": "0"}, "annotations": [{"id": "a"}]})
        target = write_epub(tmp_path / "Book.epub", {**MEMBERS, EMBEDDED_PATH: held})
        before = target.read_bytes()

        assert archive.replace_annotations(target, [{"id": "a"}]) is False
        assert target.read_bytes() == before

    @pytest.mark.parametrize(
        "member", [EMBEDDED_PATH, "OEBPS/text/chapter1.xhtml"], ids=["held", "other"]
    )
    def test_a_member_compressed_with_bzip2_is_refused_unread(self, tmp_path, member):
        # A rebuild streams each member across, but bzip2 inflates a whole
        # compressed chunk in one call however little is asked for.
        target = _epub_with_bomb(tmp_path / "Bomb.epub", member, ZIP_BZIP2)
        before = target.read_bytes()

        with (
            _allocations() as peak,
            pytest.raises(package_reader.UNREADABLE_MEMBER, match="bzip2"),
        ):
            archive.replace_annotations(target, [{"id": "mine"}])

        assert peak() < PEAK
        assert target.read_bytes() == before
        assert sorted(path.name for path in tmp_path.iterdir()) == ["Bomb.epub"]


def _crafted(path: Path, flaw: str) -> Path:
    """
    Write a sound epub, then break its last central-directory entry.

    ``utf8`` flags the entry's name as UTF-8 when it is not, and ``version``
    says reading it needs a zip version newer than zipfile implements. Both
    are refused while the archive is opened, before any member is read.
    """
    data = bytearray(write_epub(path).read_bytes())
    entry = data.rfind(b"PK\x01\x02")
    if flaw == "utf8":
        flags = int.from_bytes(data[entry + 8 : entry + 10], "little") | 0x800
        data[entry + 8 : entry + 10] = flags.to_bytes(2, "little")
        length = int.from_bytes(data[entry + 28 : entry + 30], "little")
        data[entry + 46 + length - 1] = 0xFF
    else:
        data[entry + 6 : entry + 8] = (64).to_bytes(2, "little")
    path.write_bytes(bytes(data))
    return path


FLAWS = pytest.mark.parametrize("flaw", ["utf8", "version"])


class TestAnArchiveThatCannotBeOpenedCostsOneBook:
    """
    zipfile raises UnicodeDecodeError for a name flagged UTF-8 that is not,
    and NotImplementedError for a zip version it does not implement. The
    three readers of an already-zipped book's metadata caught ValidationError,
    BadZipFile and OSError, so either ended a ``--name-by author-title`` run
    with a traceback.
    """

    @FLAWS
    def test_it_is_a_validation_error(self, tmp_path, flaw):
        path = _crafted(tmp_path / "Hostile.epub", flaw)

        with pytest.raises(package_reader.ValidationError):
            package_reader.read_archive_package(path)

    def test_a_sound_archive_is_read(self, tmp_path):
        path = write_epub(tmp_path / "Book.epub")

        assert package_reader.read_archive_package(path).title == (
            "A Wizard of Earthsea"
        )

    def test_a_missing_archive_is_a_validation_error(self, tmp_path):
        with pytest.raises(package_reader.ValidationError):
            package_reader.read_archive_package(tmp_path / "Absent.epub")

    @FLAWS
    def test_one_in_the_library_is_copied_under_its_own_name(
        self, tmp_path, output_dir, flaw
    ):
        library = tmp_path / "lib"
        library.mkdir()
        _crafted(library / "Hostile.epub", flaw)

        code = run.main(
            ["-s", str(library), "-o", str(output_dir), "--name-by", "author-title"]
            + ["-m", "0", "-q"]
        )

        assert code == 0
        assert [path.name for path in output_dir.glob("*.epub")] == ["Hostile.epub"]

    @FLAWS
    def test_one_on_the_shelf_under_a_wanted_name_does_not_end_the_run(
        self, tmp_path, output_dir, flaw
    ):
        library = tmp_path / "lib"
        make_metadata_package(
            library, "Dune.epub", title="Dune", file_as="Herbert, Frank"
        )
        held = _crafted(output_dir / "Herbert, Frank - Dune.epub", flaw)
        before = held.read_bytes()

        code = run.main(
            ["-s", str(library), "-o", str(output_dir), "--name-by", "author-title"]
            + ["-q"]
        )

        assert code == 0
        assert held.read_bytes() == before


#: The member :func:`_repeating_last` repeats, and what it inflates to.
PADDING = "OEBPS/padding.bin"
PADDING_BYTES = 1024 * 1024


def _repeating_last(path: Path, copies: int, *, method: int = ZIP_DEFLATED) -> Path:
    """
    Write a sound epub whose central directory lists its last member *copies* times.

    Every copy points at the one local header, so the archive grows by a
    directory record per copy while each copy inflates the whole member.
    """
    write_epub(path)
    with ZipFile(path, "a") as opened:
        opened.writestr(PADDING, b"\0" * PADDING_BYTES, compress_type=method)
    data = path.read_bytes()
    end = data.rfind(b"PK\x05\x06")
    count, size, offset = struct.unpack("<HII", data[end + 10 : end + 20])
    directory = data[offset : offset + size]
    directory += directory[directory.rfind(b"PK\x01\x02") :] * (copies - 1)
    total = count + copies - 1
    tail = struct.pack(
        "<4sHHHHIIH", b"PK\x05\x06", 0, 0, total, total, len(directory), offset, 0
    )
    path.write_bytes(data[:offset] + directory + tail)
    return path


@contextmanager
def _inflations(monkeypatch) -> Iterator[Counter[str]]:
    """Count every member opened for reading, by name, inside the block."""
    opened: Counter[str] = Counter()
    original = ZipFile.open

    def counting(self, name, *args, **kwargs):
        opened[getattr(name, "filename", name)] += 1
        return original(self, name, *args, **kwargs)

    monkeypatch.setattr(ZipFile, "open", counting)
    with warnings.catch_warnings():
        # 3.13 and later warn about an entry sharing a header, not refuse it.
        warnings.simplefilter("ignore")
        yield opened


class TestARepeatedDirectoryEntryIsNotInflatedAgain:
    """
    A central directory may list one local header any number of times. 3.13
    and later only warn about it, and ``testzip()`` opens every entry by name,
    so each copy inflated the member again: a 258 KiB archive took 13 s to
    verify. A refresh rebuilt a 4 MiB book with 200 copies into 800 MiB and
    moved it over the original. 3.10 to 3.12 refuse the second copy instead,
    so what is pinned here is that the archive is refused before any of it is
    inflated, whichever zipfile reads it.
    """

    def test_verify_reports_it_and_inflates_none_of_it(self, tmp_path, monkeypatch):
        path = _repeating_last(tmp_path / "Repeated.epub", 40)

        with _inflations(monkeypatch) as opened:
            problems = validate.validate_archive(path)

        assert "members share a local header (possible zip bomb)" in problems
        assert f"member name appears more than once: {PADDING}" in problems
        assert opened[PADDING] == 0

    def test_verify_inflates_no_member_named_twice(self, tmp_path, monkeypatch):
        # Two local headers under one name: testzip() opened both by name,
        # which is the last of them twice.
        path = write_epub(tmp_path / "Doubled.epub")
        with warnings.catch_warnings(), ZipFile(path, "a") as opened:
            warnings.simplefilter("ignore")  # "Duplicate name"
            for _ in range(2):
                opened.writestr(PADDING, b"\0" * PADDING_BYTES, ZIP_DEFLATED)

        with _inflations(monkeypatch) as inflated:
            problems = validate.validate_archive(path)

        assert problems == [f"member name appears more than once: {PADDING}"]
        assert inflated[PADDING] == 0

    def test_verify_still_inflates_every_member_of_a_sound_archive_once(
        self, tmp_path, monkeypatch
    ):
        path = write_epub(tmp_path / "Sound.epub")
        with ZipFile(path, "a") as opened:
            opened.writestr(PADDING, b"\0" * PADDING_BYTES, ZIP_DEFLATED)

        with _inflations(monkeypatch) as inflated:
            problems = validate.validate_archive(path)

        assert not problems
        assert inflated[PADDING] == 1

    @pytest.mark.parametrize("method", [ZIP_STORED, ZIP_DEFLATED])
    def test_a_refresh_refuses_to_rebuild_it(self, tmp_path, monkeypatch, method):
        target = _repeating_last(tmp_path / "Repeated.epub", 40, method=method)
        before = target.read_bytes()

        with (
            _inflations(monkeypatch) as opened,
            pytest.raises(ArchiveInvalidError, match="share a local header"),
        ):
            archive.replace_annotations(target, [{"id": "mine"}])

        assert opened[PADDING] == 0
        assert target.read_bytes() == before
        assert sorted(path.name for path in tmp_path.iterdir()) == ["Repeated.epub"]

    def test_a_refresh_refuses_a_member_named_twice(self, tmp_path):
        target = write_epub(tmp_path / "Doubled.epub")
        with warnings.catch_warnings(), ZipFile(target, "a") as opened:
            warnings.simplefilter("ignore")  # "Duplicate name"
            opened.writestr("OEBPS/text/chapter1.xhtml", "<html/>")
        before = target.read_bytes()

        with pytest.raises(ArchiveInvalidError, match="more than once"):
            archive.replace_annotations(target, [{"id": "mine"}])

        assert target.read_bytes() == before


def test_a_repeated_member_is_named_escaped(tmp_path: Path):
    # A refresh refusing an archive whose directory repeats a name said only
    # that "member names appear more than once", so nobody could tell which.
    path = tmp_path / "Twice.epub"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # zipfile warns of the duplicate
        with ZipFile(path, "w") as opened:
            opened.writestr(ZipInfo("mimetype"), "application/epub+zip")
            opened.writestr(ZipInfo("OEBPS/\x1b[2Kch1.xhtml"), "one")
            opened.writestr(ZipInfo("OEBPS/\x1b[2Kch1.xhtml"), "two")

    with ZipFile(path) as reading:
        said = package_reader.repeated_entries(reading)

    assert said is not None
    assert "ch1.xhtml" in said
    assert "\x1b" not in said
