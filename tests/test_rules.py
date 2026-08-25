"""
Tests written per rule, at every call site of that rule.

Four of the five most serious findings in the second review were introduced by
fixes for the first: the right change, applied to one of the two or three places
that needed it, with a regression test that only covered the place it went.

So each class here names a rule, and holds one test per site where that rule
applies -- including sites that were already correct. A rule with three call
sites gets three tests. When a later change breaks the rule somewhere new, the
test for that site fails rather than the rule quietly holding in one place.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them. Explicit empty-list comparisons read better than truthiness here.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=use-implicit-booleaness-not-comparison,too-few-public-methods

import errno
import os
import sys
import threading
from pathlib import Path
from zipfile import ZipFile

import pytest

from epubconvert import archive, convert, display, naming, planning, run, source
from epubconvert.naming import PassthroughNaming, StripNaming
from tests.conftest import make_package

SURROGATE = "Bad\udce9Name.epub"


class TestRuleInspectBeforeWriting:
    """A book is inspected for DRM and stubs before anything is written.

    Sites: the three paths in ``_decide`` that can return PENDING --
    plain, ``--refresh`` and ``--force``.
    """

    @staticmethod
    def _drm_package(root: Path) -> Path:
        package = make_package(root, "Book.epub")
        (package / "META-INF" / "sinf.xml").write_text("<sinf/>", encoding="utf-8")
        return package

    def test_plain_path_inspects(self, tmp_path, output_dir):
        self._drm_package(tmp_path / "lib")
        packages = archive.collect_package_dirs(tmp_path / "lib")

        decisions = planning.plan_exports(packages, output_dir, PassthroughNaming())

        assert [d.status for d in decisions] == [planning.DRM]

    def test_refresh_path_inspects(self, tmp_path, output_dir):
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])
        (library / "Book.epub" / "META-INF" / "sinf.xml").write_text(
            "<sinf/>", encoding="utf-8"
        )
        future = (output_dir / "Book.epub").stat().st_mtime + 100
        os.utime(library / "Book.epub", (future, future))
        packages = archive.collect_package_dirs(library)

        decisions = planning.plan_exports(
            packages,
            output_dir,
            PassthroughNaming(),
            planning.PlanOptions(refresh=True),
        )

        assert [d.status for d in decisions] == [planning.DRM]

    def test_force_path_inspects(self, tmp_path, output_dir):
        # The critical. --force returned PENDING before inspect_package ran, so
        # a good archive was overwritten by a DRM-protected, truncated one.
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])
        before = (output_dir / "Book.epub").read_bytes()
        (library / "Book.epub" / "META-INF" / "sinf.xml").write_text(
            "<sinf/>", encoding="utf-8"
        )

        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-f", "-q"])

        assert (output_dir / "Book.epub").read_bytes() == before
        with ZipFile(output_dir / "Book.epub") as opened:
            assert "META-INF/sinf.xml" not in opened.namelist()


class TestRuleNothingIsSilentlySkipped:
    """A package tree that cannot be fully read fails; it never half-succeeds.

    Sites: ``archive._members`` (files and directories),
    ``archive.collect_package_dirs`` (directories), and
    ``source.has_dataless_files`` (files and directories).
    """

    @staticmethod
    def _symlinked_tree(root: Path) -> Path:
        package = make_package(root, "Book.epub")
        content = root.parent / "elsewhere"
        content.mkdir(parents=True, exist_ok=True)
        (content / "chapter.xhtml").write_text("<html/>", encoding="utf-8")
        target = package / "OEBPS"
        for child in sorted(target.rglob("*"), reverse=True):
            if child.is_file():
                child.unlink()
            else:
                child.rmdir()
        target.rmdir()
        target.symlink_to(content)
        return package

    def test_members_refuses_a_symlinked_directory(self, tmp_path, output_dir):
        # os.walk never descends a symlinked directory, so the package
        # contributed nothing -- and the warning covered symlinked *files*
        # only, so this case logged nothing at all.
        package = self._symlinked_tree(tmp_path / "lib")

        with pytest.raises(OSError):
            archive.zip_package(package, output_dir / "Book.epub")

    def test_members_refuses_a_symlinked_file(self, tmp_path, output_dir):
        package = make_package(tmp_path / "lib", "Book.epub")
        (tmp_path / "outside.txt").write_text("SECRET", encoding="utf-8")
        (package / "OEBPS" / "linked.xhtml").symlink_to(tmp_path / "outside.txt")

        with pytest.raises(OSError):
            archive.zip_package(package, output_dir / "Book.epub")

    def test_collect_refuses_a_symlinked_package(self, tmp_path):
        library = tmp_path / "lib"
        library.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (library / "Fake.epub").symlink_to(elsewhere)

        assert archive.collect_package_dirs(library) == []

    def test_the_stub_walk_refuses_a_symlinked_directory(self, tmp_path):
        # Same rule, third site. A symlinked content directory means the walk
        # sees nothing, so --skip-incomplete answered "downloaded" for a
        # package it never examined.
        package = self._symlinked_tree(tmp_path / "lib")

        assert source.has_dataless_files(package) is True

    def test_the_stub_walk_fails_closed_on_an_unreadable_directory(self, tmp_path):
        package = make_package(tmp_path / "lib", "Book.epub")
        locked = package / "OEBPS" / "locked"
        locked.mkdir()
        locked.chmod(0o000)
        try:
            assert source.has_dataless_files(package) is True
        finally:
            locked.chmod(0o755)


class TestRuleTheUmaskIsReadWithoutMutatingIt:
    """Reading the umask must be safe from every worker at once.

    Sites: ``archive._file_mode``. The pool reaches 64 threads.
    """

    def test_concurrent_reads_agree_and_do_not_disturb_the_process(self):
        # The fix read the umask by setting it to 0 and restoring it, twice
        # per book, from up to 64 threads. A thread landing in another's
        # window saw 0o666, and the process umask could be left at 0 -- so
        # every file the process created afterwards was world-writable.
        before = os.umask(0)
        os.umask(before)
        switch = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)
        seen: list[int] = []
        try:

            def hammer() -> None:
                for _ in range(2000):
                    seen.append(archive.file_mode())

            threads = [threading.Thread(target=hammer) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            after = os.umask(0)
            os.umask(after)
        finally:
            sys.setswitchinterval(switch)

        assert len(set(seen)) == 1
        assert after == before

    def test_the_mode_honours_the_umask_in_force_at_import(self):
        assert archive.file_mode() == 0o666 & ~archive.UMASK


class TestRuleTheFilesystemKeyDecidesSameness:
    """Two names the filesystem cannot tell apart are one file.

    Sites: ``assign_names`` when claiming a name, and the ``existing`` map plus
    its lookup in ``_decide`` when recognising completed work.
    """

    def test_assignment_treats_case_variants_as_one_name(self, tmp_path, output_dir):
        library = tmp_path / "lib"
        make_package(library / "a", "Book.epub")
        make_package(library / "b", "BOOK.epub")
        packages = archive.collect_package_dirs(library)

        decisions = planning.plan_exports(packages, output_dir, PassthroughNaming())

        assert sorted(d.status for d in decisions) == [
            planning.COLLISION,
            planning.PENDING,
        ]

    def test_an_existing_file_is_recognised_across_case(self, tmp_path, output_dir):
        # The half-applied fix: the fold went into assign_names but not into
        # the existing map, so a book already exported under another case was
        # re-exported on every run for ever.
        library = tmp_path / "lib"
        make_package(library, "The Hobbit.epub")
        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])
        (output_dir / "The Hobbit.epub").rename(output_dir / "THE HOBBIT.epub")
        packages = archive.collect_package_dirs(library)

        decisions = planning.plan_exports(packages, output_dir, PassthroughNaming())

        assert [d.status for d in decisions] == [planning.EXPORTED]

    def test_a_rerun_converges(self, tmp_path, output_dir, capsys):
        library = tmp_path / "lib"
        make_package(library, "The Hobbit.epub")
        argv = ["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"]
        run.main(argv)
        (output_dir / "The Hobbit.epub").rename(output_dir / "THE HOBBIT.epub")
        capsys.readouterr()

        run.main(argv)

        assert "Exported 0" in capsys.readouterr().out
        assert len(list(output_dir.glob("*.epub"))) == 1


class TestRuleLengthUsesTheSurrogateSafeEncoder:
    """Every byte-budget measurement survives an undecodable name.

    Sites: ``truncate_bytes``, ``strip_unsafe``, ``PortableNaming.filename``
    and ``planning.suffixed`` -- the last had three plain encodes.
    """

    def test_truncate_bytes_survives(self):
        assert naming.truncate_bytes(SURROGATE, 200) == SURROGATE

    def test_strip_unsafe_survives(self):
        assert StripNaming().filename(SURROGATE).endswith(".epub")

    def test_suffixed_survives(self):
        assert planning.suffixed(SURROGATE, 2, naming.MAX_FILENAME_BYTES)

    def test_planning_survives_a_colliding_surrogate_name(self, tmp_path):
        # Paths only -- APFS refuses to create a file whose name carries a
        # surrogate, but os.walk on a share that allows it hands one back, and
        # assign_names never touches the filesystem.
        packages = [tmp_path / "a" / SURROGATE, tmp_path / "b" / SURROGATE]

        assigned = planning.assign_names(packages, StripNaming(), planning.SUFFIX)

        assert len(assigned) == 2


class TestRuleUserFacingNamesArePrintable:
    """A name from a book cannot steer a terminal, anywhere it is shown.

    Sites: every render of a package or archive name -- the export lines, the
    planner's listing and tallies, the dry-run line, and the skip warnings.
    """

    def test_printable_escapes_control_characters(self):
        assert "\x1b" not in display.printable("a\x1b[2Kb")

    def test_printable_escapes_lone_surrogates(self):
        # A surrogate survives printable() and then makes the log handler
        # raise while emitting the record, losing the line entirely.
        rendered = display.printable(SURROGATE)

        rendered.encode("utf-8")

    def test_the_dry_run_line_is_escaped(self, tmp_path, output_dir, capsys):
        library = tmp_path / "lib"
        make_package(library, "Innocent\x1b[2KDONE.epub")

        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-d"])

        assert "\x1b" not in capsys.readouterr().err

    def test_the_listing_is_escaped(self, tmp_path, output_dir, capsys):
        library = tmp_path / "lib"
        make_package(library, "Innocent\x1b[2KDONE.epub")

        run.main(["-s", str(library), "-o", str(output_dir), "--list", "-q"])

        assert "\x1b" not in capsys.readouterr().out


class TestRuleSurprisesAreCountedNotSwallowed:
    """A failure the worker did not catch still reaches the summary.

    Sites: the ``asyncio.gather`` in ``export_planned``.
    """

    def test_an_escaping_exception_is_counted(self, tmp_path, output_dir, monkeypatch):
        def boom(*_args, **_kwargs):
            raise MemoryError("out of memory")

        monkeypatch.setattr(convert, "_zip_and_record", boom)
        library = tmp_path / "lib"
        make_package(library, "Book.epub")

        code = run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])

        assert code != 0


class TestRuleTheSweepNeedsARealLock:
    """Temporaries are only swept when the lock was actually taken.

    Sites: ``output_lock``'s unlocked fallback, and ``_run_export``'s call.
    """

    def test_the_sweep_is_skipped_when_locking_is_unsupported(
        self, tmp_path, output_dir, monkeypatch
    ):
        def unsupported(*_args, **_kwargs):
            raise OSError(errno.ENOTSUP, "not supported")

        monkeypatch.setattr("epubconvert.convert.fcntl.flock", unsupported)
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        # A temporary another concurrent run may still be writing.
        inflight = output_dir / f"{archive.PARTIAL_PREFIX}other{archive.PARTIAL_SUFFIX}"
        inflight.write_bytes(b"another run is writing this")

        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])

        assert inflight.exists()

    def test_the_sweep_still_runs_when_the_lock_is_held(self, tmp_path, output_dir):
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        stale = output_dir / f"{archive.PARTIAL_PREFIX}stale{archive.PARTIAL_SUFFIX}"
        stale.write_bytes(b"abandoned")

        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])

        assert not stale.exists()
