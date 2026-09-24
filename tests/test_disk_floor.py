"""
Tests for how the --min-free floor is sampled while workers write at once.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=protected-access,too-few-public-methods

import errno
import threading

import pytest

from epubconvert.run import convert


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
