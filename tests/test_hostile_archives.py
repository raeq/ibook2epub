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
import tracemalloc
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from zipfile import ZIP_BZIP2, ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

import pytest

from epubconvert.collect import package as package_reader
from epubconvert.collect import validate
from epubconvert.collect.annotations import EMBEDDED_PATH
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
