"""Tests for SummaryCoordinator (B9a): state machine + idle wait.

Fake clock/sleep count polls; no real subprocess, no real keys.
"""
import threading

import pytest

from stt_button.summary import (
    ApplyResult,
    Chunk,
    ModelUnavailable,
    SummaryCoordinator,
    apply_summary,
    play_error_sound,
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
        self.keys = []

    def backspace_and_type(self, n, text, trailing_key=None):
        self.calls.append((n, text, trailing_key))

    def key(self, name):
        self.keys.append(name)


class FakePaster:
    def __init__(self, result=False):
        self.result = result
        self.calls = []

    def paste_combined(self, text, backspaces=0):
        self.calls.append((text, backspaces))
        return self.result


class FakeFocus:
    """Scripted focus classification."""

    def __init__(self, state):
        self.state = state
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.state


class FakeClipboard:
    """Scripted wl-paste/wl-copy collaborator with a call log."""

    def __init__(self, clipboard_texts=None, primary_texts=None):
        self.clipboard_texts = list(clipboard_texts or [])
        self.primary_texts = list(primary_texts or [])
        self.reads = []  # "clipboard" | "primary"
        self.writes = []  # ("clipboard"|"primary", text)

    def read_clipboard(self):
        self.reads.append("clipboard")
        return self.clipboard_texts.pop(0) if self.clipboard_texts else ""

    def read_primary(self):
        self.reads.append("primary")
        return self.primary_texts.pop(0) if self.primary_texts else ""

    def copy_clipboard(self, text):
        self.writes.append(("clipboard", text))

    def copy_primary(self, text):
        self.writes.append(("primary", text))


class FakeCfg:
    summary_wait_idle_s = 20
    summary_error_sound = "you_lose"
    summary_error_sound_volume = 24000
    summary_enabled = True


class FakeCfgDisabled(FakeCfg):
    summary_enabled = False


def chunk(text):
    return Chunk(text, epoch=0, ts=0.0)


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


def _make_coord(controller, *, cfg=None, focus=None, clipboard=None,
                 typer=None, paster=None):
    clock = FakeClock()
    sleeper = FakeSleep(clock)
    sounds = []
    coord = SummaryCoordinator(
        controller,
        cfg or FakeCfg(),
        clock=clock,
        sleep=sleeper,
        play_error=lambda name, volume: sounds.append((name, volume)),
        focus_state=focus,
        clipboard=clipboard,
        typer=typer,
        paster=paster,
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


# -- B9b: focus gate, selection path, roundtrip cleanup ------------------

@pytest.mark.parametrize("state", ["terminal", "unreachable", "no_window"])
def test_focus_gate_blocks_all_emissions(state):
    ctrl = FakeController([False])
    typer, paster = FakeTyper(), FakePaster()
    clip = FakeClipboard()
    focus = FakeFocus(state)
    coord, clock, sleeper, sounds = _make_coord(
        ctrl, focus=focus, clipboard=clip, typer=typer, paster=paster)

    assert coord.submit_apply("summary text", [chunk("anchor")]) is True
    result = coord.wait_for_result(timeout=5)

    assert result is not None
    assert result.ok is False
    assert result.status == "focus_not_editable"
    assert typer.calls == [] and typer.keys == []
    assert paster.calls == []
    assert clip.reads == [] and clip.writes == []
    assert len(sounds) == 1
    assert coord.state == SummaryCoordinator.IDLE


def test_primary_selection_paste_chord_without_roundtrip():
    ctrl = FakeController([False])
    typer = FakeTyper()
    paster = FakePaster(result=True)
    clip = FakeClipboard(clipboard_texts=["old clip"],
                         primary_texts=["selected words"])
    coord, clock, sleeper, sounds = _make_coord(
        ctrl, focus=FakeFocus("other"), clipboard=clip,
        typer=typer, paster=paster)

    coord.submit_apply("new summary", [chunk("anchor")])
    result = coord.wait_for_result(timeout=5)

    assert result is not None and result.ok is True
    assert result.status == "pasted"
    assert result.removed_text == "selected words"
    assert result.inserted_text == "new summary"
    # selection path: NO locate roundtrip, NO ctrl+shift+Home, no keys
    assert "ctrl+shift+Home" not in typer.keys
    assert typer.keys == []
    assert typer.calls == []
    # exactly one clipboard read (the D4 backup), i.e. no roundtrip read
    assert clip.reads.count("clipboard") == 1
    assert paster.calls == [("new summary", 0)]
    # D4: both selections are restored, clipboard first
    assert clip.writes[0] == ("clipboard", "old clip")
    assert clip.writes[1] == ("primary", "selected words")
    assert sounds == []


def test_d3_roundtrip_ends_with_key_right_when_aborted():
    ctrl = FakeController([False])
    typer, paster = FakeTyper(), FakePaster()
    # read ends NOT with the anchor -> caret_not_at_end abort
    clip = FakeClipboard(clipboard_texts=["old clip"],
                         primary_texts=["", "anchor typed-on"])
    coord, clock, sleeper, sounds = _make_coord(
        ctrl, focus=FakeFocus("other"), clipboard=clip,
        typer=typer, paster=paster)

    coord.submit_apply("new summary", [chunk("anchor")])
    result = coord.wait_for_result(timeout=5)

    assert result is not None and result.ok is False
    assert result.reason == "caret_not_at_end"
    # roundtrip cleanup rule: the last emitted act is `key right`
    assert typer.keys == ["ctrl+shift+Home", "right"]
    assert typer.keys[-1] == "right"
    assert typer.calls == []
    assert paster.calls == []
    # restore happened even on abort (D4, clipboard first)
    assert clip.writes[0] == ("clipboard", "old clip")
    assert sounds == []


def test_d3_roundtrip_mutates_block_after_key_right():
    ctrl = FakeController([False])
    typer, paster = FakeTyper(), FakePaster()
    read = "pre anchor one anchor two"  # ends with the last anchor
    clip = FakeClipboard(clipboard_texts=["old clip"],
                         primary_texts=["", read])
    coord, clock, sleeper, sounds = _make_coord(
        ctrl, focus=FakeFocus("other"), clipboard=clip,
        typer=typer, paster=paster)

    coord.submit_apply("new summary",
                       [chunk("anchor one"), chunk("anchor two")])
    result = coord.wait_for_result(timeout=5)

    assert result is not None and result.ok is True
    assert result.status == "typed"
    assert clip.writes[0] == ("clipboard", "old clip")
    assert sounds == []


def test_d3_roundtrip_reads_primary_not_clipboard():
    ctrl = FakeController([False])
    typer, paster = FakeTyper(), FakePaster()
    read = "pre anchor one anchor two"
    # roundtrip read must come from PRIMARY; clipboard only holds the
    # pre-flow snapshot and is never popped for the roundtrip
    clip = FakeClipboard(clipboard_texts=["old clip"],
                         primary_texts=["", read])
    coord, clock, sleeper, sounds = _make_coord(
        ctrl, focus=FakeFocus("other"), clipboard=clip,
        typer=typer, paster=paster)

    coord.submit_apply("new summary",
                       [chunk("anchor one"), chunk("anchor two")])
    result = coord.wait_for_result(timeout=5)

    assert result is not None and result.ok is True
    assert result.status == "typed"
    assert typer.keys == ["ctrl+shift+Home", "right"]
    assert clip.reads.count("clipboard") == 1  # snapshot only
    assert clip.reads.count("primary") == 2  # D4 check + roundtrip
    assert sounds == []


def test_d3_roundtrip_empty_read_aborts_with_empty_read_and_tone():
    ctrl = FakeController([False])
    typer, paster = FakeTyper(), FakePaster()
    clip = FakeClipboard(clipboard_texts=["old clip"],
                         primary_texts=[""])
    coord, clock, sleeper, sounds = _make_coord(
        ctrl, focus=FakeFocus("other"), clipboard=clip,
        typer=typer, paster=paster)

    coord.submit_apply("new summary", [chunk("anchor")])
    result = coord.wait_for_result(timeout=5)

    assert result is not None and result.ok is False
    assert result.status == "aborted"
    assert result.mode == ""
    assert result.reason == "empty_read"
    # cleanup still happened and the last emitted act is `key right`
    assert typer.keys == ["ctrl+shift+Home", "right"]
    assert typer.calls == []
    assert paster.calls == []
    assert clip.writes[0] == ("clipboard", "old clip")
    # not silent: exactly one error tone
    assert len(sounds) == 1
    assert sounds[0] == ("you_lose", 24000)


# -- B9c: single-slot queue discard tone + enabled gate ------------------

def test_second_job_discarded_with_single_tone():
    ctrl = FakeController([False])
    typer2, paster2 = FakeTyper(), FakePaster()
    coord, clock, sleeper, sounds = _make_coord(ctrl)
    started, release = threading.Event(), threading.Event()

    def op():
        started.set()
        release.wait(5)
        return ApplyResult(ok=True, status="applied_typed", mode="typed",
                           removed_text="a", inserted_text="b", reason=None)

    assert coord.submit(op) is True
    assert started.wait(5)
    # second job while one is running: discarded, never queued
    assert coord.submit(_make_op(typer2, paster2)) is False
    assert typer2.calls == []  # no parallel typing from the second job
    assert len(sounds) == 1  # exactly ONE tone for the discarded job
    assert sounds[0] == ("you_lose", 24000)

    release.set()
    result = coord.wait_for_result(timeout=5)
    assert result is not None and result.ok is True
    # the later success of the first job adds NO further tone
    assert len(sounds) == 1
    assert coord.state == SummaryCoordinator.IDLE


def _coord_with_popen(controller, cfg, popens):
    clock = FakeClock()
    sleeper = FakeSleep(clock)

    def fake_popen(cmd, **kwargs):
        popens.append(cmd)
        return object()

    coord = SummaryCoordinator(
        controller, cfg, clock=clock, sleep=sleeper,
        play_error=lambda name, volume:
            play_error_sound(name, volume, popen=fake_popen))
    return coord


def test_disabled_gate_zero_popen_no_client_call():
    ctrl = FakeController([False])
    popens, client_calls = [], []
    coord = _coord_with_popen(ctrl, FakeCfgDisabled(), popens)

    def op():
        client_calls.append(1)
        raise ModelUnavailable("http 500")  # forced client failure

    assert coord.submit(op) is True
    result = coord.wait_for_result(timeout=5)

    assert result is not None and result.ok is False
    assert result.reason == "summary_disabled"
    # the coordinator is the ONLY switch holder: the op (and thus the
    # client) never runs, play_error_sound is never reached
    assert client_calls == []
    assert popens == []  # zero Popen calls
    assert coord.state == SummaryCoordinator.IDLE


def test_enabled_model_unavailable_exactly_one_popen():
    ctrl = FakeController([False])
    popens = []
    coord = _coord_with_popen(ctrl, FakeCfg(), popens)

    def op():
        raise ModelUnavailable("http 500")  # forced client failure

    assert coord.submit(op) is True
    result = coord.wait_for_result(timeout=5)

    assert result is not None and result.ok is False
    assert result.status == "failed"
    assert result.reason == "model_unavailable"
    assert len(popens) == 1  # exactly one tone
    assert coord.state == SummaryCoordinator.IDLE
