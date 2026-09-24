"""
The pool that writes onto the shelf, and how a run waits for it.

A Ctrl-C drops the writes not yet started and waits for the ones in flight:
each finishes, replaces atomically and records itself, so the summary agrees
with the shelf and the lock is held until the last write is done. Kept apart
from :mod:`epubconvert.run.convert` because the copies wait the same way.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import ParamSpec, TypeVar

from ..utils.app_logger import logger

_P = ParamSpec("_P")
_T = TypeVar("_T")


class WritingPool(ThreadPoolExecutor):
    """A thread pool that knows how many of its tasks are running."""

    def __init__(self, max_workers: int | None = None, thread_name_prefix: str = ""):
        super().__init__(max_workers=max_workers, thread_name_prefix=thread_name_prefix)
        self._running = 0
        self._settled = threading.Condition()

    def submit(
        self, fn: Callable[_P, _T], /, *args: _P.args, **kwargs: _P.kwargs
    ) -> Future[_T]:
        """
        Run *fn* in the pool, counted while it runs.

        Counted by the worker that runs it, not here: an interrupt can land
        anywhere in this thread, including after the task is queued and
        before it is counted, and a count taken here then disagreed with
        what was running.
        """

        def counted() -> _T:
            with self._settled:
                self._running += 1
            try:
                return fn(*args, **kwargs)
            finally:
                with self._settled:
                    self._running -= 1
                    self._settled.notify_all()

        return super().submit(counted)

    @property
    def running(self) -> int:
        """How many tasks are running now."""
        with self._settled:
            return self._running

    def finish(self, what: str) -> None:
        """
        Drop the tasks not yet started, and wait for the ones running.

        A second Ctrl-C landed in the wait and broke out of it: the summary
        was printed before the books in flight were done and counted none of
        them, the lock was released while they were still writing, and one
        cut short at exit left a full-size temporary behind. The wait is now
        tried until it completes, however often it is interrupted, and the
        interrupt is raised once it has, so the run still ends as stopped.

        Waited out on the pool's own count of running tasks before its
        threads are joined: before Python 3.13, a ``Thread.join`` that an
        interrupt broke into marks a thread still running as stopped, so a
        second join returned at once, and so did the interpreter's at exit.
        The join that follows only waits for idle threads to leave, and for a
        task a worker took from the queue just before it was emptied.

        :param what: What the tasks write, for the one line said while
            waiting: ``book(s)``, say.

        :raises KeyboardInterrupt: If the wait was interrupted.
        """
        interrupted = said = False
        while True:
            try:
                # Inside the guard, so an interrupt while saying it is waited
                # out as well; said first, so it is said once.
                if interrupted and not said:
                    said = True
                    logger.warning(
                        "Finishing %d %s already being written...",
                        self.running,
                        what,
                    )
                self.shutdown(wait=False, cancel_futures=True)
                with self._settled:
                    self._settled.wait_for(lambda: self._running == 0)
                self.shutdown(wait=True)
                break
            except KeyboardInterrupt:
                interrupted = True
        if interrupted:
            raise KeyboardInterrupt
