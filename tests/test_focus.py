"""Fokus-Ton-Logik (Betreiber 31.10.): fertig-Signal nur bei
Terminal-Fokus (Kimi: ohne Shell-Erweiterung keine portable API;
Extension GetFocus() liefert app-id + Titel)."""
from __future__ import annotations

import pytest

from stt_button import focus


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
    """Controller-Semantik: done_sound + done_sound_mode.

    'terminal' (Default): Ton bei terminal_focused() in (True, None);
    KEIN Ton bei False (Browser-Fokus). 'always': Ton immer.
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
        assert len(calls) == 1, "Terminal-Fokus: Ton muss klingen"

    def test_terminal_mode_silent_for_browser(
        self, monkeypatch, tmp_path
    ) -> None:
        calls = self._play_calls(monkeypatch)
        self._run_case(
            self._make(monkeypatch, tmp_path, "terminal", False), tmp_path
        )
        assert calls == [], "Browser-Fokus (False): KEIN Ton"

    def test_terminal_mode_unknown_plays(self, monkeypatch, tmp_path) -> None:
        """None = Extension fehlt/nicht antwortet: lieber Toenen als
        still sein (Feedback-Prinzip; Betaetiger-Default)."""
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
        assert len(calls) == 1, "always: Fokus ist irrelevant"


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
