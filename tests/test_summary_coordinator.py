"""Tests for SummaryCoordinator (B9a): state machine + idle wait.

Fake clock/sleep count polls; no real subprocess, no real keys.
"""
import threading

import pytest

from stt_button.summary import (
    ApplyResult,
    SummaryCoordinator,
    apply_summary,
)


class FakeController:
    """Counts has_pending_work() polls; scripted answers."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.polls = 0

    def has_pending_work(self):
        self.polls += 1
        if self.answers:
            return self.answers.pop(0)
        return False


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class FakeSleep:
    """Each sleep advances the fake clock by the poll interval."""

    def __init__(self, clock, step=0.1):
        self.clock = clock
        self.step = step
        self.calls = 0

    def __call__(self, seconds):
        self.calls += 1
        self.clock.now += self.step


class FakeTyper:
    def __init__(self):
        self.calls = []

    def backspace_and_type(self, n, text, trailing_key=None):
        self.calls.append((n, text, trailing_key))


class FakePaster:
    def __init__(self):
        self.calls = []

    def paste_combined(self, text, backspaces=0):
        self.calls.append((text, backspaces))
        return False


class FakeCfg:
    summary_wait_idle_s = 20
    summary_error_sound = "you_lose"
    summary_error_sound_volume = 24000


def _make_op(typer, paster):
    def op():
        return apply_summary(
            "summary text",
            original_text="original",
            n=8,
            typer=typer,
            paster=paster,
            restore_clipboard=lambda: None,
            restore_primary=lambda: None,
            paste_min_chars=40,
        )
    return op


def _make_coord(controller, *, cfg=None):
    clock = FakeClock()
    sleeper = FakeSleep(clock)
    sounds = []
    coord = SummaryCoordinator(
        controller,
        cfg or FakeCfg(),
        clock=clock,
        sleep=sleeper,
        play_error=lambda name, volume: sounds.append((name, volume)),
    )
    return coord, clock, sleeper, sounds


def test_waits_for_idle_before_typing():
    ctrl = FakeController([True, True, False])
    typer, paster = FakeTyper(), FakePaster()
    coord, clock, sleeper, sounds = _make_coord(ctrl)

    coord.submit(_make_op(typer, paster))
    coord.wait_for_result(timeout=5)

    assert ctrl.polls >= 3
    assert sleeper.calls >= 2  # two 0.1 s polls between the busy answers
    assert len(typer.calls) == 1
    assert paster.calls == []
    result = coord.result
    assert result.ok is True
    assert result.inserted_text == "summary text"
    assert sounds == []
    assert coord.state == SummaryCoordinator.IDLE


def test_timeout_busy_sound_no_keys():
    ctrl = FakeController([True] * 1000)  # never idle
    typer, paster = FakeTyper(), FakePaster()
    coord, clock, sleeper, sounds = _make_coord(ctrl)

    coord.submit(_make_op(typer, paster))
    coord.wait_for_result(timeout=5)

    assert ctrl.polls >= 1
    result = coord.result
    assert result.ok is False
    assert result.status == "busy"
    assert result.reason is not None
    assert len(sounds) == 1
    assert sounds[0] == ("you_lose", 24000)
    assert typer.calls == []
    assert paster.calls == []
    assert coord.state == SummaryCoordinator.IDLE


def test_states_and_single_job_rejection():
    ctrl = FakeController([False])
    typer, paster = FakeTyper(), FakePaster()
    coord, clock, sleeper, sounds = _make_coord(ctrl)

    started = threading.Event()
    release = threading.Event()

    def op():
        started.set()
        release.wait(5)
        return ApplyResult(ok=True, status="applied_typed", mode="typed",
                           removed_text="a", inserted_text="b", reason=None)

    assert coord.submit(op) is True
    assert started.wait(5)
    assert coord.state == SummaryCoordinator.WORKING
    # a second job is rejected while one is running (queue = B9c)
    assert coord.submit(_make_op(typer, paster)) is False

    release.set()
    coord.wait_for_result(timeout=5)
    assert coord.state == SummaryCoordinator.IDLE
    assert coord.result.ok is True
    assert typer.calls == []

    # undo jobs are flagged via the ACTIVE_UNDO state
    started2, release2 = threading.Event(), threading.Event()

    def op2():
        started2.set()
        release2.wait(5)
        return ApplyResult(ok=True, status="undone", mode="typed",
                           removed_text="b", inserted_text="a", reason=None)

    assert coord.submit(op2, undo=True) is True
    assert started2.wait(5)
    assert coord.state == SummaryCoordinator.ACTIVE_UNDO
    release2.set()
    coord.wait_for_result(timeout=5)
    assert coord.state == SummaryCoordinator.IDLE


def test_submit_is_noop_when_shut_down():
    ctrl = FakeController([False])
    coord, *_ = _make_coord(ctrl)
    coord.shutdown()
    assert coord.wait_for_result(timeout=0.3) is None
    assert coord.submit(_make_op(FakeTyper(), FakePaster())) is False
