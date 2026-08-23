"""Tests for strip naming, deterministic archives, interrupts and discovery."""

# Test names describe the behaviour under test; separate docstrings would only
# restate them. Explicit empty-list comparisons read better than truthiness here.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=use-implicit-booleaness-not-comparison,too-few-public-methods

import hashlib
import os
from pathlib import Path
from zipfile import ZIP_STORED, ZipFile

import pytest

from epubconvert import convert, naming
from tests.conftest import make_package

WINDOWS_ILLEGAL = set(naming.WINDOWS_ILLEGAL)


def digest(path: Path) -> str:
    """Hash a file's bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TestStripUnsafe:
    @pytest.mark.parametrize(
        "name",
        [
            "Sapiens: A Brief History.epub",
            "War and Peace (Vol. 1/2).epub",
            "Où sont les enfants ?.epub",
            'A "Quoted" Title.epub',
            "Pipe|Star*.epub",
        ],
    )
    def test_illegal_characters_are_removed(self, name):
        assert not WINDOWS_ILLEGAL & set(naming.strip_unsafe(name))

    def test_script_is_preserved(self):
        # The whole point of strip mode: every target filesystem stores these
        # correctly, so romanizing them would be gratuitous loss.
        assert naming.strip_unsafe("こころ.epub") == "こころ.epub"
        assert naming.strip_unsafe("Хождение.epub") == "Хождение.epub"
        assert naming.strip_unsafe("L'Étranger.epub") == "L'Étranger.epub"

    def test_clean_names_are_untouched(self):
        assert naming.strip_unsafe("The Hobbit.epub") == "The Hobbit.epub"

    def test_whitespace_runs_collapse(self):
        assert naming.strip_unsafe("A   B.epub") == "A B.epub"

    def test_reserved_device_names_are_escaped(self):
        assert naming.strip_unsafe("CON.epub").startswith("_")
        assert naming.strip_unsafe("LPT1.epub").startswith("_")
        assert naming.strip_unsafe("NUL").startswith("_")

    def test_trailing_dot_and_space_are_dropped(self):
        assert naming.strip_unsafe("Trailing .") == "Trailing"

    def test_result_fits_the_byte_budget(self):
        long_name = "Ä" * 400 + ".epub"

        result = naming.strip_unsafe(long_name)

        assert len(result.encode()) <= naming.MAX_FILENAME_BYTES
        assert result.endswith(".epub")

    def test_truncation_does_not_split_a_character(self):
        result = naming.strip_unsafe("é" * 400 + ".epub")

        result.encode().decode()  # would raise if a sequence were split
        assert len(result.encode()) <= naming.MAX_FILENAME_BYTES

    def test_empty_input_yields_a_usable_name(self):
        assert naming.strip_unsafe("") == "_"
        assert naming.strip_unsafe("///") != ""

    def test_is_idempotent(self):
        for name in ["Sapiens: A Brief.epub", "CON.epub", "Ä" * 400 + ".epub"]:
            once = naming.strip_unsafe(name)
            assert naming.strip_unsafe(once) == once


class TestStripNaming:
    def test_identity_folds_case(self):
        policy = naming.StripNaming()

        assert policy.identity("The Hobbit.epub") == policy.identity("THE HOBBIT.epub")

    def test_identity_round_trips_from_disk(self):
        policy = naming.StripNaming()
        on_disk = policy.filename("Sapiens: A Brief History.epub")

        assert policy.identity(on_disk) == policy.identity(
            policy.filename("Sapiens: A Brief History.epub")
        )

    def test_satisfies_the_protocol(self):
        assert isinstance(naming.StripNaming(), naming.NamingPolicy)

    def test_needs_no_disarm(self, monkeypatch):
        monkeypatch.setattr(naming, "disarm", None)

        policy = naming.build_policy(naming.STRIP)

        assert policy.filename("A: B.epub") == "A B.epub"

    def test_end_to_end_keeps_the_script(self, tmp_path, output_dir):
        source = tmp_path / "lib"
        make_package(source, "こころ: Kokoro.epub")

        convert.main(["-s", str(source), "-o", str(output_dir), "-m", "0", "-p", "-q"])

        names = [p.name for p in output_dir.glob("*.epub")]
        assert names == ["こころ Kokoro.epub"]


class TestDeterministicArchives:
    def test_re_export_is_byte_identical(self, library, output_dir):
        package = library / "Book One.epub"
        first = output_dir / "first.epub"
        second = output_dir / "second.epub"

        convert.zip_package(package, first)
        convert.zip_package(package, second)

        assert digest(first) == digest(second)

    def test_timestamps_are_normalized(self, library, output_dir):
        target = output_dir / "Book One.epub"

        convert.zip_package(library / "Book One.epub", target)

        with ZipFile(target) as archive:
            for info in archive.infolist():
                assert info.date_time == convert.ARCHIVE_TIMESTAMP

    def test_touching_the_source_does_not_change_the_bytes(self, library, output_dir):
        package = library / "Book One.epub"
        first = output_dir / "first.epub"
        convert.zip_package(package, first)
        before = digest(first)

        for path in package.rglob("*"):
            if path.is_file():
                os_stat = path.stat()
                os_utime = (os_stat.st_atime + 10_000, os_stat.st_mtime + 10_000)
                os.utime(path, os_utime)
        second = output_dir / "second.epub"
        convert.zip_package(package, second)

        assert digest(second) == before

    def test_archive_is_still_spec_valid(self, library, output_dir):
        target = output_dir / "Book One.epub"

        convert.zip_package(library / "Book One.epub", target)

        with ZipFile(target) as archive:
            names = archive.namelist()
            assert names[0] == "mimetype"
            assert archive.getinfo("mimetype").compress_type == ZIP_STORED
            assert archive.read("mimetype") == b"application/epub+zip"
            assert archive.testzip() is None

    def test_content_still_round_trips(self, library, output_dir):
        target = output_dir / "Book One.epub"

        convert.zip_package(library / "Book One.epub", target)

        with ZipFile(target) as archive:
            assert b"Chapter one" in archive.read("OEBPS/text/chapter1.xhtml")

    def test_members_are_readable_not_owner_only(self, library, output_dir):
        target = output_dir / "Book One.epub"

        convert.zip_package(library / "Book One.epub", target)

        with ZipFile(target) as archive:
            for info in archive.infolist():
                assert (info.external_attr >> 16) & 0o044


class TestInterrupt:
    def test_partial_counts_survive(self, library, output_dir, monkeypatch, capsys):
        real = convert.zip_package
        calls = {"n": 0}

        def stop_after_one(source: Path, target: Path) -> int:
            calls["n"] += 1
            if calls["n"] > 1:
                raise KeyboardInterrupt
            return real(source, target)

        monkeypatch.setattr(convert, "zip_package", stop_after_one)

        code = convert.main(
            [
                "-s",
                str(library),
                "-o",
                str(output_dir),
                "-m",
                "0",
                "--no-shuffle",
                "-w",
                "1",
                "-q",
            ]
        )

        out = capsys.readouterr().out
        assert code == 130
        assert "Interrupted." in out
        # The book that finished is reported, not silently discarded.
        assert "Exported 1" in out

    def test_finished_books_are_intact(self, library, output_dir, monkeypatch):
        real = convert.zip_package
        calls = {"n": 0}

        def stop_after_one(source: Path, target: Path) -> int:
            calls["n"] += 1
            if calls["n"] > 1:
                raise KeyboardInterrupt
            return real(source, target)

        monkeypatch.setattr(convert, "zip_package", stop_after_one)
        convert.main(
            [
                "-s",
                str(library),
                "-o",
                str(output_dir),
                "-m",
                "0",
                "--no-shuffle",
                "-w",
                "1",
                "-q",
            ]
        )

        written = list(output_dir.glob("*.epub"))
        assert len(written) == 1
        with ZipFile(written[0]) as archive:
            assert archive.testzip() is None
        assert list(output_dir.glob("*.part")) == []

    def test_rerun_after_interrupt_continues(self, library, output_dir, monkeypatch):
        real = convert.zip_package
        calls = {"n": 0}

        def stop_after_one(source: Path, target: Path) -> int:
            calls["n"] += 1
            if calls["n"] > 1:
                raise KeyboardInterrupt
            return real(source, target)

        monkeypatch.setattr(convert, "zip_package", stop_after_one)
        argv = [
            "-s",
            str(library),
            "-o",
            str(output_dir),
            "-m",
            "0",
            "--no-shuffle",
            "-w",
            "1",
            "-q",
        ]
        convert.main(argv)
        monkeypatch.setattr(convert, "zip_package", real)

        assert convert.main(argv) == 0
        assert len(list(output_dir.glob("*.epub"))) == 2


class TestSourceDiscovery:
    def test_prefers_a_candidate_holding_books(self, tmp_path, monkeypatch):
        empty = tmp_path / "empty"
        stocked = tmp_path / "stocked"
        empty.mkdir()
        make_package(stocked, "Dune.epub")
        monkeypatch.setattr(convert, "SOURCE_CANDIDATES", (empty, stocked))

        assert convert.discover_source() == stocked

    def test_falls_back_to_one_that_exists(self, tmp_path, monkeypatch):
        empty = tmp_path / "empty"
        empty.mkdir()
        missing = tmp_path / "missing"
        monkeypatch.setattr(convert, "SOURCE_CANDIDATES", (empty, missing))

        assert convert.discover_source() == empty

    def test_falls_back_to_the_default_when_nothing_exists(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            convert, "SOURCE_CANDIDATES", (tmp_path / "a", tmp_path / "b")
        )

        assert convert.discover_source() == convert.DEFAULT_SOURCE

    def test_explicit_source_skips_discovery(self, library):
        args = convert.parse_args(["-s", str(library)])

        assert args.source_dir == library
        assert args.source_auto is False


class TestLockDiagnostics:
    def test_lock_file_records_the_pid(self, output_dir):
        pytest.importorskip("fcntl", reason="advisory locking needs fcntl")
        with convert.output_lock(output_dir):
            contents = (output_dir / convert.LOCK_NAME).read_text(encoding="utf-8")

        assert f"pid={os.getpid()}" in contents

    def test_contended_error_names_the_holder(self, output_dir):
        pytest.importorskip("fcntl", reason="advisory locking needs fcntl")
        with (
            convert.output_lock(output_dir),
            pytest.raises(convert.OutputLockedError) as excinfo,
            convert.output_lock(output_dir),
        ):
            pass

        assert f"pid={os.getpid()}" in str(excinfo.value)

    def test_a_stale_lock_file_does_not_block(self, output_dir):
        # flock is released by the kernel when the holder dies, so a lock file
        # left behind by a killed run is inert. No PID liveness check needed.
        pytest.importorskip("fcntl", reason="advisory locking needs fcntl")
        (output_dir / convert.LOCK_NAME).write_text("pid=999999 host=ghost\n")

        with convert.output_lock(output_dir):
            pass  # acquired without complaint
