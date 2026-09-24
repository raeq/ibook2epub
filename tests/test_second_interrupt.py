"""
A second Ctrl-C waits for the books already being written, as the first does.

The first Ctrl-C drops the books not yet started and waits for the ones in
flight, so each finishes, replaces atomically and is counted. A second one
landed in that wait and broke out of it: the summary was printed before the
books in flight said they were done, and counted none of them; the lock was
released while they were still writing; and a book cut short at exit left a
full-size temporary behind.

Real signals, sent from a worker while the run waits for it, because where
an interrupt lands is the whole question: before Python 3.13 a thread join
that one broke into took a running thread for a stopped one, so joining again
returned at once. A stand-in raised in place of the join hid exactly that.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=too-few-public-methods

from __future__ import annotations

import contextlib
import os
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from epubconvert.run import convert, copying, run
from epubconvert.run.workers import WritingPool
from epubconvert.utils import exits
from tests.conftest import make_package

#: How long a test waits on another thread before giving up on it.
PATIENCE = 5

#: Between one interrupt and the next: long enough for the run to have
#: reached the wait the next one is meant to land in.
PAUSE = 0.5


def _interrupt() -> None:
    """Press Ctrl-C: the signal, which the main thread turns into an error."""
    os.kill(os.getpid(), signal.SIGINT)


class _TwoInterrupts:
    """
    Two Ctrl-Cs while the books are being written, held until both are in.

    The first stops the run; the second lands while it waits for the books
    in flight. Those are held until after it, so a run that stops waiting
    reports them unfinished.
    """

    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, workers: int, writer: tuple[object, str]
    ) -> None:
        module, name = writer
        self.started = threading.Barrier(workers)
        self.release = threading.Event()
        self._sent = threading.Event()
        self._sending = threading.Lock()
        self._real = getattr(module, name)
        monkeypatch.setattr(module, name, self.write)

    def write(self, *args: Any, **kwargs: Any) -> Any:
        # Broken only if a worker never started, and then the test fails.
        with contextlib.suppress(threading.BrokenBarrierError):
            self.started.wait(timeout=PATIENCE)
        with self._sending:
            first = not self._sent.is_set()
            self._sent.set()
        if first:
            _interrupt()
            time.sleep(PAUSE)
            _interrupt()
            time.sleep(PAUSE)
            self.release.set()
        self.release.wait(timeout=PATIENCE)
        return self._real(*args, **kwargs)


class TestASecondCtrlCWaitsForTheBooksInFlight:
    def test_the_books_in_flight_finish_and_are_counted(
        self, tmp_path, output_dir, monkeypatch, capsys
    ):
        library = tmp_path / "lib"
        for name in ("Alpha.epub", "Beta.epub"):
            make_package(library, name)
        _TwoInterrupts(monkeypatch, workers=2, writer=(convert, "zip_package"))

        code = run.main(
            ["-s", str(library), "-o", str(output_dir), "-m", "0", "-w", "2"]
        )

        out, err = capsys.readouterr()
        assert code == exits.INTERRUPTED
        assert out.startswith("Interrupted. Exported 2 epub file(s)")
        assert err.count("Finishing 2 book(s) already being written") == 1
        # Said before the summary, which is the last word on the run.
        assert err.index("Exported Alpha.epub") < err.index("Interrupted;")
        assert sorted(path.name for path in output_dir.iterdir()) == [
            convert.LOCK_NAME,
            "Alpha.epub",
            "Beta.epub",
        ]

    def test_the_files_copied_through_in_flight_too(
        self, tmp_path, output_dir, monkeypatch, capsys
    ):
        library = tmp_path / "lib"
        library.mkdir()
        for name in ("One.pdf", "Two.pdf"):
            (library / name).write_bytes(b"%PDF-1.4\n")
        _TwoInterrupts(monkeypatch, workers=2, writer=(copying, "copy_through"))

        code = run.main(
            ["-s", str(library), "-o", str(output_dir), "-m", "0", "-w", "2"]
        )

        out, err = capsys.readouterr()
        assert code == exits.INTERRUPTED
        assert "2 copied" in out
        assert err.count("Finishing 2 file(s) already being written") == 1
        assert sorted(path.name for path in output_dir.iterdir()) == [
            convert.LOCK_NAME,
            "One.pdf",
            "Two.pdf",
        ]


class TestWritingPoolFinish:
    """The wait itself, whatever the pool writes."""

    def test_every_interrupt_is_waited_out_and_then_raised(self, caplog):
        release = threading.Event()
        pool = WritingPool(max_workers=1, thread_name_prefix="test")
        running = pool.submit(release.wait, PATIENCE)
        queued = pool.submit(release.wait, PATIENCE)

        def interrupt_twice() -> None:
            for _ in range(2):
                time.sleep(PAUSE)
                _interrupt()
            time.sleep(PAUSE)
            release.set()

        threading.Thread(target=interrupt_twice).start()
        with pytest.raises(KeyboardInterrupt):
            pool.finish("file(s)")

        assert release.is_set()
        assert running.result() is True
        assert queued.cancelled()
        assert pool.running == 0
        assert caplog.text.count("Finishing 1 file(s) already being written") == 1

    def test_a_task_counted_although_submitting_it_was_interrupted(
        self, monkeypatch, caplog
    ):
        # The first Ctrl-C can land while a task is handed to the pool, after
        # it is queued; counted there, it was then counted as never queued,
        # and the second Ctrl-C found nothing to wait for.
        started = threading.Event()
        release = threading.Event()
        pool = WritingPool(max_workers=1)
        # pylint: disable-next=protected-access
        handing_over = ThreadPoolExecutor._adjust_thread_count  # noqa: SLF001

        def interrupted(self: ThreadPoolExecutor) -> None:
            handing_over(self)
            raise KeyboardInterrupt

        def task() -> bool:
            started.set()
            return release.wait(PATIENCE)

        monkeypatch.setattr(ThreadPoolExecutor, "_adjust_thread_count", interrupted)
        with pytest.raises(KeyboardInterrupt):
            pool.submit(task)
        monkeypatch.undo()
        assert started.wait(PATIENCE)

        def interrupt_then_release() -> None:
            time.sleep(PAUSE)
            _interrupt()
            time.sleep(PAUSE)
            release.set()

        threading.Thread(target=interrupt_then_release).start()
        with pytest.raises(KeyboardInterrupt):
            pool.finish("file(s)")

        assert release.is_set()
        assert "Finishing 1 file(s) already being written" in caplog.text

    def test_without_an_interrupt_it_only_waits(self, caplog):
        pool = WritingPool(max_workers=1)
        done = pool.submit(lambda: Path("x"))

        pool.finish("book(s)")

        assert done.result() == Path("x")
        assert pool.running == 0
        assert "Finishing" not in caplog.text

    def test_a_task_refused_after_shutdown_is_not_waited_for(self):
        pool = WritingPool(max_workers=1)
        pool.finish("book(s)")

        with pytest.raises(RuntimeError):
            pool.submit(lambda: None)

        assert pool.running == 0
