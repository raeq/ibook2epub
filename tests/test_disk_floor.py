"""
Tests for how the --min-free floor is sampled while workers write at once.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=protected-access,too-few-public-methods

import errno
import threading
from typing import Any

import pytest

from epubconvert.run import convert, copying, run
from epubconvert.utils import exits
from tests.conftest import make_metadata_package


class TestTheFloorIsSampledOnce:
    def test_a_write_waits_while_a_due_measurement_is_taken(
        self, tmp_path, monkeypatch
    ):
        # The due measurement ran outside the lock, so while one worker was
        # asking a slow volume the others took their unsampled turns and
        # started writing onto a volume that had already crossed the floor.
        measuring, answer = threading.Event(), threading.Event()

        def slow_full_volume(*_: object) -> bool:
            measuring.set()
            answer.wait(5)
            return False

        monkeypatch.setattr(convert, "_has_room", slow_full_volume)
        progress = convert._Progress(total=4, interval=4)
        answers: dict[str, bool] = {}

        def ask(name: str) -> None:
            answers[name] = progress.has_room(tmp_path, 100)

        sampled = threading.Thread(target=ask, args=("sampled",))
        sampled.start()
        assert measuring.wait(5)
        unsampled = threading.Thread(target=ask, args=("unsampled",))
        unsampled.start()
        unsampled.join(0.2)
        answer.set()
        sampled.join(5)
        unsampled.join(5)

        assert answers == {"sampled": False, "unsampled": False}

    def test_a_measurement_that_raises_leaves_the_others_free_to_go(
        self, tmp_path, monkeypatch
    ):
        def broken(*_: object) -> bool:
            raise OSError(errno.EIO, "gone")

        monkeypatch.setattr(convert, "_has_room", broken)
        progress = convert._Progress(total=2, interval=2)

        with pytest.raises(OSError):
            progress.has_room(tmp_path, 100)

        assert progress.has_room(tmp_path, 100) is True


class _Interrupted(threading.Condition):
    """A condition whose first exit is cut short by Ctrl-C, before releasing."""

    fired = False

    def __exit__(self, *exc: Any) -> None:
        if not _Interrupted.fired:
            _Interrupted.fired = True
            raise KeyboardInterrupt
        super().__exit__(*exc)


class TestAnInterruptWhileTheFloorIsAsked:
    """
    The sampler's condition was built over the one lock that guards every
    run's report, which is not reentrant. A Ctrl-C landing in its exit, before
    the release, left that lock held for the life of the process, and the
    next run in it hung at its first count.
    """

    def test_leaves_the_next_run_free_to_go(self, tmp_path, monkeypatch):
        library = tmp_path / "lib"
        library.mkdir()
        (library / "Paper.pdf").write_bytes(b"%PDF-1.4\n")
        started = convert._Progress.__init__

        def interrupted(self: convert._Progress, *args: Any, **kwargs: Any) -> None:
            started(self, *args, **kwargs)
            self._measured = _Interrupted(self._measured._lock)  # type: ignore[attr-defined]

        monkeypatch.setattr(_Interrupted, "fired", False)
        monkeypatch.setattr(convert._Progress, "__init__", interrupted)
        argv = ["-s", str(library), "-o", str(tmp_path / "out"), "-m", "0"]

        first = _within(PATIENCE, argv)
        second = _within(PATIENCE, argv)

        assert first == exits.INTERRUPTED
        assert second == exits.SUCCESS
        assert (tmp_path / "out" / "Paper.pdf").is_file()


class _CutShort:
    """
    The report lock, whose first exit on the run's own thread is cut short by
    Ctrl-C before it releases. The workers' exits are left alone: they run on
    threads a Ctrl-C does not reach.
    """

    def __init__(self, lock: Any) -> None:
        self._lock = lock
        self.fired = False

    def __enter__(self) -> "_CutShort":
        self._lock.acquire()
        return self

    def __exit__(self, *exc: Any) -> None:
        worker = threading.current_thread().name.startswith(("zip", "copy"))
        if not worker and not self.fired:
            self.fired = True
            raise KeyboardInterrupt
        self._lock.release()

    def locked(self) -> bool:
        return bool(self._lock.locked())

    def release(self) -> None:
        self._lock.release()


class TestAnInterruptWhileTheReportIsCounted:
    """
    The run's own thread counted into the report under the one lock every
    run's workers share, which is not reentrant: the copies it could not make
    before the pool started, and the books whose worker raised once the pool
    had finished. A Ctrl-C landing in that lock's exit, before the release,
    left it held for the life of the process, and the next run in it hung.
    Neither count races a worker, so neither takes the lock.
    """

    @pytest.mark.parametrize(
        "book",
        [
            pytest.param("Paper.pdf", id="a-copy"),
            pytest.param("Alpha.epub", id="a-book-whose-worker-raised"),
        ],
    )
    def test_leaves_the_next_run_free_to_go(self, tmp_path, monkeypatch, book):
        library = tmp_path / "lib"
        if book.endswith(".pdf"):
            library.mkdir()
            (library / book).write_bytes(b"%PDF-1.4\n")
        else:
            make_metadata_package(library, book, title="Alpha")

            def raising(*_: object) -> bool:
                raise RuntimeError("escaped the worker")

            monkeypatch.setattr(convert._Progress, "has_room", raising)
        lock = _CutShort(threading.Lock())
        monkeypatch.setattr(convert, "_REPORT_LOCK", lock)
        monkeypatch.setattr(copying, "_REPORT_LOCK", lock)
        argv = ["-s", str(library), "-o", str(tmp_path / "out"), "-m", "0"]

        first = _within(PATIENCE, argv)
        second = _within(PATIENCE, argv)

        assert second is not None, "the second run hung"
        assert first == second
        assert not lock.fired


#: How long a run is given before it is taken to have hung.
PATIENCE = 10


def _within(seconds: float, argv: list[str]) -> int | None:
    """
    Run the tool on another thread, and give up on it after *seconds*.

    :return: Its exit code, or None when it did not finish in time. A lock
        it left held is released then, so the rest of the suite can go on.
    """
    codes: list[int] = []
    runner = threading.Thread(target=lambda: codes.append(run.main(argv)), daemon=True)
    runner.start()
    runner.join(seconds)
    if runner.is_alive():
        if convert._REPORT_LOCK.locked():
            convert._REPORT_LOCK.release()
        runner.join(seconds)
        return None
    return codes[0] if codes else None
