"""
Tests for the work the tool does per book, and the work it declines to do.

These pin behaviour a measurement established, so a later change cannot quietly
undo it: which compression level is actually applied, how many threads the pool
takes by default, and which expensive checks run only for books that will
really be written.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them. Explicit empty-list comparisons read better than truthiness here.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=use-implicit-booleaness-not-comparison,too-few-public-methods

import asyncio
import os
import time
from collections import Counter
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

import pytest

from epubconvert.collect import annotations, source
from epubconvert.collect import package as package_reader
from epubconvert.export import archive, inspect_output
from epubconvert.export.naming import PassthroughNaming
from epubconvert.run import claims, convert, holders, placing, planning, run
from tests.conftest import make_metadata_package, make_package
from tests.test_annotations import highlight, library_row, make_databases
from tests.test_copy_claims import zipped_book

#: Font obfuscation, which is not protection, and a key-transport algorithm,
#: which is.
FONTS = "http://www.idpf.org/2008/embedding"
RSA = "http://www.w3.org/2001/04/xmlenc#rsa-1_5"


class TestCompressionLevelIsActuallyApplied:
    """The constant must reach zlib, and must be the level worth paying for."""

    def test_the_entry_carries_the_configured_level(self):
        # Regression (7.1): ZipFile(compresslevel=) is consulted only when
        # open() builds its own ZipInfo. _entry supplies a prebuilt one, so the
        # constant had never been applied and every member deflated at zlib's
        # default. Measured at level 9: 3.2x the CPU for 0.6% smaller.
        entry = archive.entry("OEBPS/text.xhtml", ZIP_DEFLATED)

        assert archive.level_of(entry) == archive.COMPRESS_LEVEL

    def test_the_level_is_the_one_measurement_supports(self):
        assert archive.COMPRESS_LEVEL == 6


class TestAlreadyCompressedMediaIsStored:
    """Deflating a JPEG spends CPU to save nothing."""

    def test_an_image_member_is_stored_not_deflated(self, tmp_path, output_dir):
        # Measured on an image-heavy book: storing media is 3.8x faster for
        # +0.02% size.
        package = make_package(tmp_path / "lib", "Book.epub")
        images = package / "OEBPS" / "images"
        images.mkdir(parents=True)
        (images / "cover.jpg").write_bytes(b"\xff\xd8\xff" + os.urandom(4096))

        target = output_dir / "Book.epub"
        archive.zip_package(package, target)

        with ZipFile(target) as opened:
            assert opened.getinfo("OEBPS/images/cover.jpg").compress_type == ZIP_STORED
            assert opened.getinfo("OEBPS/content.opf").compress_type == ZIP_DEFLATED

    def test_storing_media_keeps_exports_byte_identical(self, tmp_path, output_dir):
        package = make_package(tmp_path / "lib", "Book.epub")
        (package / "OEBPS" / "art.png").write_bytes(os.urandom(2048))
        first = output_dir / "a.epub"
        second = output_dir / "b.epub"

        archive.zip_package(package, first)
        archive.zip_package(package, second)

        assert first.read_bytes() == second.read_bytes()


class TestWorkerDefaultSuitsBlockingWork:
    """The pool is waiting on iCloud, not on the CPU."""

    def test_the_default_exceeds_the_cpu_count(self):
        # Measured under an iCloud stall model: 14 workers 19.2 s, 48 workers
        # 7.12 s, 64 workers 4.76 s. The CPU-bound cost of raising it is 6%.
        assert convert.default_workers() > (os.cpu_count() or 1)

    def test_an_explicit_count_still_wins(self):
        assert convert.default_workers(7) == 7


class TestTheStubWalkStaysBeforeTheCap:
    """Deferring it would be faster and would break the remaining count.

    Finding 5.1 proposed walking for undownloaded files only after
    ``--max-export-files`` has chosen its subset, which measured 801 scandirs
    and 3,696 stats to export 5 books out of 200. It was **not** taken.

    The planner can only know a book is undownloaded by walking it. Deferred
    past the cap, a book outside the subset is never walked, so it plans as
    pending on every run and ``N remaining`` never reaches zero -- which is
    precisely the defect an earlier review fixed, where ``count_pending``
    forced the check off and ``--skip-incomplete`` reported books remaining
    that it would never export.

    This test pins the correctness requirement so the optimisation cannot be
    reintroduced without noticing the conflict.
    """

    def test_an_undownloaded_book_is_excluded_from_the_remaining_count(
        self, tmp_path, output_dir, monkeypatch, capsys
    ):
        library = tmp_path / "lib"
        make_package(library, "Stub.epub")
        make_package(library, "Real.epub")
        monkeypatch.setattr(
            source, "has_dataless_files", lambda package: package.name == "Stub.epub"
        )

        run.main(
            [
                "-s",
                str(library),
                "-o",
                str(output_dir),
                "-m",
                "0",
                "--skip-incomplete",
                "-q",
            ]
        )

        out = capsys.readouterr().out
        assert "not downloaded" in out
        assert "remaining" not in out


class TestCollisionSearchDoesNotRescan:
    """A full group must not re-try every position for each later member."""

    def test_identity_is_not_recomputed_for_exhausted_names(self, tmp_path):
        # Regression (5.3): once a group used all MAX_SUFFIX positions, every
        # later package retried all 99 candidates before losing.
        packages = [make_package(tmp_path / str(i), "Same.epub") for i in range(4)]
        calls = {"n": 0}

        class Counting(PassthroughNaming):
            def identity(self, filename: str) -> str:
                calls["n"] += 1
                return super().identity(filename)

        planning.assign_names(packages, Counting(), planning.SUFFIX)

        assert calls["n"] < len(packages) * claims.MAX_SUFFIX


class TestVerifyChecksEveryArchive:
    """The read-back path pools its work but must miss nothing."""

    def test_every_archive_is_still_checked(self, output_dir):
        for index in range(5):
            with ZipFile(output_dir / f"Book{index}.epub", "w") as opened:
                opened.writestr("mimetype", "application/epub+zip")

        checked, damaged, _broken = inspect_output.verify_output(output_dir)

        assert checked == 5
        assert damaged == 5


class TestFreeSpaceIsSampled:
    """One statvfs per book is a syscall the floor does not need."""

    def test_the_volume_is_not_measured_once_per_book(
        self, tmp_path, output_dir, monkeypatch
    ):
        # Regression (7.8): --min-free defaults on, and its own help names the
        # volumes where statvfs is slowest.
        library = tmp_path / "lib"
        for index in range(8):
            make_package(library, f"Book{index}.epub")
        calls = {"n": 0}
        real = inspect_output.free_megabytes

        def counted(path):
            calls["n"] += 1
            return real(path)

        monkeypatch.setattr(convert, "free_megabytes", counted)

        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])

        assert calls["n"] < 8


class TestTheProductionSeamIsCovered:
    """plan -> cap -> export_planned is what main runs; test it directly."""

    def test_planned_decisions_can_be_exported_without_the_wrapper(
        self, tmp_path, output_dir
    ):
        # Regression (P9.6): both test helpers drove export_packages, which
        # production stopped calling when _run_export began planning once
        # itself, so the seam that replaced it had only end-to-end coverage.
        library = tmp_path / "lib"
        for index in range(3):
            make_package(library, f"Book{index}.epub")
        packages = archive.collect_package_dirs(library)

        decisions = planning.plan_exports(packages, output_dir, PassthroughNaming())
        selected = convert.cap_exports(decisions, 2, randomise=False)
        report = asyncio.run(convert.export_planned(selected, output_dir))

        assert convert.count_pending_decisions(decisions) == 3
        assert report.exported == 2
        assert len(list(output_dir.glob("*.epub"))) == 2

    def test_the_wrapper_still_plans_then_exports(self, tmp_path, output_dir):
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        packages = archive.collect_package_dirs(library)

        report = asyncio.run(convert.export_packages(packages, output_dir))

        assert report.exported == 1


class TestThePackageDocumentIsReadOnce:
    """
    ``find_orphans`` and ``plan_exports`` each named every package, so a
    metadata policy parsed every package document twice. Measured on a real
    2,805-book library: 5,610 reads, exactly 2.00x, about half of a 2.42s
    listing.

    The two callers see the same list on a full run and different lists under
    ``--match`` or ``-m``, so the work is shared only when the lists agree.
    """

    @staticmethod
    def _reads(monkeypatch, library: Path, output_dir: Path, *extra: str) -> list[Path]:
        seen: list[Path] = []
        original = package_reader.read_package_dir

        def counting(package: Path):
            seen.append(package)
            return original(package)

        monkeypatch.setattr(planning, "read_package_dir", counting)
        run.main(["-s", str(library), "-o", str(output_dir), "--list", "-q", *extra])
        return seen

    def test_a_full_run_reads_each_package_once(
        self, tmp_path, output_dir, monkeypatch
    ):
        library = tmp_path / "lib"
        for index in range(4):
            make_metadata_package(library, f"Book {index}.epub", title=f"Book {index}")

        seen = self._reads(
            monkeypatch, library, output_dir, "--name-by", "author-title"
        )

        assert len(seen) == 4
        assert len(set(seen)) == 4

    def test_the_default_policy_still_reads_nothing(
        self, tmp_path, output_dir, monkeypatch
    ):
        library = tmp_path / "lib"
        for index in range(4):
            make_metadata_package(library, f"Book {index}.epub", title=f"Book {index}")

        assert self._reads(monkeypatch, library, output_dir) == []

    def test_a_filtered_run_still_names_the_whole_library_for_orphans(
        self, tmp_path, output_dir, capsys
    ):
        # --match narrows the run, not the library: the books it excludes must
        # not be reported as abandoned. Sharing must not break that.
        library = tmp_path / "lib"
        make_metadata_package(library, "Dune.epub", title="Dune")
        make_metadata_package(library, "Hobbit.epub", title="The Hobbit")
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


def _named(file: object, directory: Path) -> Path:
    """
    Name what ZipFile was handed: a path, or an open file in *directory*.

    A book's metadata is read through a descriptor opened without blocking,
    so a FIFO cannot hang the open, and zipfile then knows it by no name.
    """
    if isinstance(file, (str, os.PathLike)):
        return Path(file)
    held = os.fstat(file.fileno())  # type: ignore[attr-defined]
    for path in directory.iterdir():
        if os.path.samestat(held, path.stat()):
            return path
    return Path(str(file))


class TestEachShelfArchiveIsReadOnce:
    """
    The orphan check and the plan each placed every book against the shelf,
    and each read the identifier of every archive under a book's name: on a
    200-book shelf under ``--name-by author-title``, 400 opens for a no-op
    rerun. Files copied through land between the two, so what one read is
    reused only while the archive is unchanged.
    """

    @staticmethod
    def _opens(
        monkeypatch, library: Path, output_dir: Path, *extra: str
    ) -> Counter[Path]:
        opened: Counter[Path] = Counter()
        original = ZipFile.__init__

        def counting(self, file, *args, **kwargs):
            path = _named(file, output_dir)
            if path.parent == output_dir:
                opened[path] += 1
            original(self, file, *args, **kwargs)

        monkeypatch.setattr(ZipFile, "__init__", counting)
        # -m 0 only for a conversion: --list shows every book whatever -m
        # says, and refuses it as a flag that changes nothing.
        cap = [] if "--list" in extra else ["-m", "0"]
        run.main(["-s", str(library), "-o", str(output_dir), *cap, "-q", *extra])
        return opened

    @pytest.mark.parametrize("listing", [[], ["--list"]])
    def test_a_rerun_reads_each_archive_once(
        self, tmp_path, output_dir, monkeypatch, listing
    ):
        library = tmp_path / "lib"
        for index in range(4):
            make_metadata_package(
                library,
                f"Book {index}.epub",
                title=f"Book {index}",
                identifier=f"urn:uuid:{index}",
            )
        flags = ("--name-by", "author-title")
        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q", *flags])

        opened = self._opens(monkeypatch, library, output_dir, *flags, *listing)

        assert len(opened) == 4
        assert set(opened.values()) == {1}

    def test_an_archive_replaced_between_asks_is_read_again(self, tmp_path, output_dir):
        library = tmp_path / "lib"
        for index in range(2):
            make_metadata_package(
                library,
                f"Book {index}.epub",
                title=f"Book {index}",
                identifier=f"urn:uuid:{index}",
            )
        run.main(
            ["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"]
            + ["--name-by", "author-title"]
        )
        first, second = sorted(output_dir.glob("*.epub"))
        assert holders.identifier_on_shelf(first) == "urn:uuid:0"

        second.replace(first)

        assert holders.identifier_on_shelf(first) == "urn:uuid:1"

    def test_the_default_policy_reads_no_archive(
        self, tmp_path, output_dir, monkeypatch
    ):
        library = tmp_path / "lib"
        for index in range(4):
            make_metadata_package(library, f"Book {index}.epub", title=f"Book {index}")
        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])

        assert self._opens(monkeypatch, library, output_dir) == Counter()


class TestARerunOverCopiesOpensNothing:
    """
    Whether a file on the shelf is a copy's own is settled by its size while
    no other book wants its name, a stat each; only two books wanting one
    name have their identifiers read.
    """

    def test_no_file_is_opened(self, tmp_path, output_dir, monkeypatch):
        library = tmp_path / "lib"
        for index in range(4):
            zipped_book(tmp_path, library / f"Book {index}.epub", f"urn:uuid:{index}")
        (library / "Paper.pdf").write_bytes(b"%PDF-1.4 fake")
        argv = ["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"]
        run.main(argv)
        opened: list[Path] = []
        original = ZipFile.__init__

        def counting(self, file, *args, **kwargs):
            opened.append(Path(str(file)))
            original(self, file, *args, **kwargs)

        monkeypatch.setattr(ZipFile, "__init__", counting)
        for mode in ("skip", "suffix"):
            run.main([*argv, "--on-collision", mode])

        assert not opened


def _nested_blocks(levels: int, algorithm: str = FONTS) -> bytes:
    """An encryption.xml of EncryptedData blocks nested *levels* deep."""
    return (
        b"<encryption>"
        + b"<EncryptedData>" * levels
        + f'<EncryptionMethod Algorithm="{algorithm}"/>'.encode("ascii")
        + b"</EncryptedData>" * levels
        + b"</encryption>"
    )


class TestEncryptionIsReadInOnePass:
    """
    Every EncryptedData block searched its whole subtree for a method, so
    nested blocks cost the square of their depth: 8,000 levels took 2.6s, and
    a file at the 1 MiB cap about 50s -- per book, on every run, before the
    book was even judged.
    """

    def test_deep_nesting_costs_linear_time(self, tmp_path):
        package = tmp_path / "Nested.epub"
        (package / "META-INF").mkdir(parents=True)
        (package / "META-INF" / "encryption.xml").write_bytes(_nested_blocks(12000))

        started = time.perf_counter()
        found = source.encryption_algorithms(package)
        elapsed = time.perf_counter() - started

        assert found == {FONTS}
        # Linear is a few milliseconds; quadratic was seconds here.
        assert elapsed < 1.0

    def test_a_method_deeper_in_a_block_still_names_its_algorithm(self, tmp_path):
        # The walk changed, not the rule: a method anywhere inside a block
        # still counts for it, so a protecting algorithm cannot hide below a
        # font-obfuscation one.
        package = tmp_path / "Deep.epub"
        (package / "META-INF").mkdir(parents=True)
        (package / "META-INF" / "encryption.xml").write_bytes(
            b"<encryption><EncryptedData>"
            + f'<EncryptionMethod Algorithm="{FONTS}"/>'.encode("ascii")
            + b"<KeyInfo><EncryptedKey>"
            + f'<EncryptionMethod Algorithm="{RSA}"/>'.encode("ascii")
            + b"</EncryptedKey></KeyInfo></EncryptedData></encryption>"
        )

        assert source.has_drm(package)[0]

    def test_a_nested_block_naming_nothing_still_fails_closed(self, tmp_path):
        package = tmp_path / "Silent.epub"
        (package / "META-INF").mkdir(parents=True)
        (package / "META-INF" / "encryption.xml").write_bytes(
            b"<encryption><EncryptedData>"
            + f'<EncryptionMethod Algorithm="{FONTS}"/>'.encode("ascii")
            + b"</EncryptedData><EncryptedData><EncryptedData/>"
            + f'<EncryptionMethod Algorithm="{FONTS}"/>'.encode("ascii")
            + b"</EncryptedData></encryption>"
        )

        assert source.has_drm(package)[0]


class TestARefreshReadsOnlyTheBooksItRewrites:
    """
    ``-ae -ar`` compares a folder-named book's identifier with its archive's
    before writing over it. It did so for every book on the shelf, though
    only a book with highlights is rewritten: 2,000 books with one highlight
    between them read 2,000 package documents and opened 2,002 archives,
    8.7x slower than the 0 and 2 before the comparison.
    """

    def test_only_the_highlighted_book_is_read(self, tmp_path, output_dir, monkeypatch):
        library = tmp_path / "lib"
        for index in range(4):
            make_metadata_package(
                library,
                f"Book {index}.epub",
                title=f"Book {index}",
                identifier=f"urn:uuid:{index}",
            )
        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])
        make_databases(
            tmp_path / "container",
            rows=[highlight()],
            books=[library_row(path="/x/Book 0.epub", title="Book 0")],
        )
        monkeypatch.setattr(
            "epubconvert.run.annotating.collect_annotations",
            lambda policy=None: annotations.collect(tmp_path / "container", policy),
        )
        sources: list[str] = []
        real_source = placing._identifier_of  # pylint: disable=protected-access

        def counting_source(package: Path) -> str | None:
            sources.append(package.name)
            return real_source(package)

        monkeypatch.setattr(placing, "_identifier_of", counting_source)
        opened: Counter[str] = Counter()
        original = ZipFile.__init__

        def counting(self, file, *args, **kwargs):
            # The rebuild's own temporary is not an archive on the shelf.
            path = _named(file, output_dir)
            if path.parent == output_dir and path.suffix == ".epub":
                opened[path.name] += 1
            original(self, file, *args, **kwargs)

        monkeypatch.setattr(ZipFile, "__init__", counting)
        holders._identifier_of.cache_clear()  # pylint: disable=protected-access

        code = run.main(["-s", str(library), "-o", str(output_dir), "-ae", "-ar", "-q"])

        assert code == 0
        assert sources == ["Book 0.epub"]
        assert set(opened) == {"Book 0.epub"}

    @pytest.mark.parametrize("detached", ["notes.json", "notes.csv", "-"])
    def test_a_document_beside_it_opens_no_zipped_book(
        self, tmp_path, output_dir, monkeypatch, detached
    ):
        # -ad FILE.json named every zipped book in the library for a document
        # that never uses the names, opening each to read its identifier:
        # and --skip-incomplete is refused there, so an evicted iCloud book
        # was downloaded to be named. Only a vault names its notes.
        library = tmp_path / "lib"
        make_metadata_package(library, "Pkg.epub", title="Pkg", identifier="urn:p")
        for index in range(3):
            zipped_book(tmp_path, library / f"Zipped {index}.epub", f"urn:{index}")
        # Named from the books' metadata, which is read from inside each.
        naming = ["--name-by", "author-title"]
        run.main(["-s", str(library), "-o", str(output_dir), *naming, "-m", "0", "-q"])
        make_databases(
            tmp_path / "container",
            rows=[highlight()],
            books=[library_row(path="/x/Pkg.epub", title="Pkg")],
        )
        monkeypatch.setattr(
            "epubconvert.run.annotating.collect_annotations",
            lambda policy=None: annotations.collect(tmp_path / "container", policy),
        )
        opened: list[str] = []
        original = ZipFile.__init__

        def counting(self, file, *args, **kwargs):
            if Path(str(file)).parent == library:
                opened.append(Path(str(file)).name)
            original(self, file, *args, **kwargs)

        monkeypatch.setattr(ZipFile, "__init__", counting)
        argv = ["-s", str(library), "-o", str(output_dir), *naming, "-ae", "-ar"]

        code = run.main([*argv, "-q", "-ad", str(tmp_path / detached)])

        assert code == 0
        assert opened == []
