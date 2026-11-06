"""Focus-based sound logic (operator, October 31): completion signal only
with terminal focus (Kimi: no portable API without a shell extension;
extension GetFocus() returns app-id + title)."""
from __future__ import annotations

import pytest

from stt_button import focus


class TestCallFocusParsing:
    """_call_focus() must parse the REAL busctl --json output.

    busctl call returns {"type":"ss","data":[app_id,title]} -- not a
    'result' key. This was live-verified on the running extension
    (04.11.): the previous parser never matched and made
    terminal_focused() always return None (silent fallback).
    """

    class _Proc:
        def __init__(self, stdout: str, returncode: int = 0) -> None:
            self.stdout = stdout
            self.returncode = returncode

    def test_parses_real_busctl_json(self, monkeypatch) -> None:
        doc = '{"type":"ss","data":["org.gnome.Terminal","Terminal"]}'
        monkeypatch.setattr(
            focus.subprocess, "run",
            lambda *a, **kw: self._Proc(doc))
        assert focus._call_focus() == ("org.gnome.Terminal", "Terminal")

    def test_empty_data_none(self, monkeypatch) -> None:
        monkeypatch.setattr(
            focus.subprocess, "run",
            lambda *a, **kw: self._Proc('{"type":"ss","data":[]}'))
        assert focus._call_focus() is None

    def test_non_json_none(self, monkeypatch) -> None:
        monkeypatch.setattr(
            focus.subprocess, "run",
            lambda *a, **kw: self._Proc("not json", returncode=1))
        assert focus._call_focus() is None


class TestTerminalFocused:
    def test_terminal_app_id_true(self, monkeypatch) -> None:
        monkeypatch.setattr(focus, "_call_focus",
                            lambda: ("org.gnome.Terminal", "Terminal"))
        assert focus.terminal_focused() is True

    def test_other_app_false(self, monkeypatch) -> None:
        monkeypatch.setattr(focus, "_call_focus",
                            lambda: ("org.gnome.Nautilus", "Dokumente"))
        assert focus.terminal_focused() is False

    def test_unfocused_none(self, monkeypatch) -> None:
        monkeypatch.setattr(focus, "_call_focus",
                            lambda: ("none", ""))
        assert focus.terminal_focused() is None

    def test_call_failure_none(self, monkeypatch) -> None:
        monkeypatch.setattr(focus, "_call_focus", lambda: None)
        assert focus.terminal_focused() is None


class TestDoneSoundFocusMode:
    """Controller semantics: done_sound + done_sound_mode.

    'terminal' (default): play sound when terminal_focused() is (True, None);
    NO sound when it is False (browser focus). 'always': always play sound.
    """

    def _play_calls(self, monkeypatch):
        import stt_button.controller as ctrl_mod

        calls = []

        def fake_popen(cmd, *a, **kw):
            calls.append(list(cmd))

            class _P:
                def poll(self):
                    return 0

            return _P()

        monkeypatch.setattr(ctrl_mod.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(ctrl_mod.shutil, "which",
                            lambda b: f"/usr/bin/{b}")
        monkeypatch.setattr(ctrl_mod.os.path, "exists",
                            lambda p: p.endswith(".oga"))
        return calls

    def _make(self, monkeypatch, tmp_path, mode, term_state):
        from stt_button.config import Config

        import stt_button.controller as ctrl_mod

        monkeypatch.setattr(ctrl_mod.focus, "terminal_focused",
                            lambda: term_state)
        recorder = RecordingTranscriber()
        source = RestAudioSource([b"\x00" * 16000] * 4, rest=b"")
        ctrl = PushToTalkController(
            config=Config(done_sound="complete", done_sound_mode=mode),
            source_factory=lambda: source,
            engine=FakeEngine(),
            delta_typer=DeltaTyper(FakeTyper(), append_space=True),
            transcriber_factory=lambda *a, **kw: recorder,
            dump_path=str(tmp_path / "last.wav"),
        )
        return ctrl

    def _run_case(self, ctrl, tmp_path):
        ctrl.start_recording()
        import time

        time.sleep(0.2)
        ctrl.stop_recording()
        _wait_state(ctrl, ControllerState.IDLE)

    def test_terminal_mode_plays_for_terminal(
        self, monkeypatch, tmp_path
    ) -> None:
        calls = self._play_calls(monkeypatch)
        self._run_case(
            self._make(monkeypatch, tmp_path, "terminal", True), tmp_path
        )
        assert len(calls) == 1, "Terminal focus: sound must play"

    def test_terminal_mode_silent_for_browser(
        self, monkeypatch, tmp_path
    ) -> None:
        calls = self._play_calls(monkeypatch)
        self._run_case(
            self._make(monkeypatch, tmp_path, "terminal", False), tmp_path
        )
        assert calls == [], "Browser focus (False): NO sound"

    def test_terminal_mode_unknown_plays(self, monkeypatch, tmp_path) -> None:
        """None = extension missing/not responding: prefer sound over
        silence (feedback principle; operator default)."""
        calls = self._play_calls(monkeypatch)
        self._run_case(
            self._make(monkeypatch, tmp_path, "terminal", None), tmp_path
        )
        assert len(calls) == 1

    def test_always_mode_ignores_focus(self, monkeypatch, tmp_path) -> None:
        calls = self._play_calls(monkeypatch)
        self._run_case(
            self._make(monkeypatch, tmp_path, "always", False), tmp_path
        )
        assert len(calls) == 1, "always: focus is irrelevant"


from stt_button.controller import ControllerState, PushToTalkController  # noqa: E402
from stt_button.typing import DeltaTyper  # noqa: E402
from tests.test_controller import (  # noqa: E402
    ControllerState as _CS,
    DeltaTyper as _DT,
    FakeEngine,
    FakeTyper,
    PushToTalkController,
    RecordingTranscriber,
    RestAudioSource,
    _wait_state,
)


class TestFocusState:
    """F2b: distinguish dead extension ('unreachable') from 'no_window'."""

    class _Proc:
        def __init__(self, stdout: str = "", returncode: int = 0) -> None:
            self.stdout = stdout
            self.returncode = returncode

    def _run(self, monkeypatch, proc=None, exc=None):
        def fake(*a, **kw):
            if exc is not None:
                raise exc
            return proc
        monkeypatch.setattr(focus.subprocess, "run", fake)

    def test_rc_nonzero_unreachable(self, monkeypatch) -> None:
        self._run(monkeypatch, self._Proc("", 1))
        assert focus.focus_state() == "unreachable"

    def test_timeout_unreachable(self, monkeypatch) -> None:
        import subprocess
        self._run(monkeypatch, exc=subprocess.TimeoutExpired("busctl", 1))
        assert focus.focus_state() == "unreachable"

    def test_none_app_no_window(self, monkeypatch) -> None:
        self._run(monkeypatch, self._Proc('{"type":"ss","data":["none",""]}'))
        assert focus.focus_state() == "no_window"

    def test_terminal(self, monkeypatch) -> None:
        self._run(monkeypatch, self._Proc(
            '{"type":"ss","data":["org.gnome.Terminal",""]}'))
        assert focus.focus_state() == "terminal"

    def test_other(self, monkeypatch) -> None:
        self._run(monkeypatch, self._Proc('{"type":"ss","data":["firefox","x"]}'))
        assert focus.focus_state() == "other"

    @pytest.mark.parametrize("state,expected", [
        ("terminal", True), ("other", False),
        ("no_window", None), ("unreachable", None),
    ])
    def test_terminal_focused_mapping(self, monkeypatch, state, expected) -> None:
        monkeypatch.setattr(focus, "focus_state", lambda: state)
        assert focus.terminal_focused() is expected


class TestWindowIdentity:
    """B10a: window_identity() is a thin public wrapper over _call_focus()."""

    def test_passthrough_tuple(self, monkeypatch) -> None:
        monkeypatch.setattr(
            focus, "_call_focus", lambda: ("org.kde.dolphin", "Notiz"))
        assert focus.window_identity() == ("org.kde.dolphin", "Notiz")

    def test_passthrough_none(self, monkeypatch) -> None:
        monkeypatch.setattr(focus, "_call_focus", lambda: None)
        assert focus.window_identity() is None

    def test_exception_returns_none(self, monkeypatch) -> None:
        def boom():
            raise RuntimeError("bus dead")
        monkeypatch.setattr(focus, "_call_focus", boom)
        assert focus.window_identity() is None
