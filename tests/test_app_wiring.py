"""Tests for wiring in app.py.

These tests verify composition (not logic): does the transcriber receive a
VAD gate in default ``whole_buffer`` mode, and is the CapsLock setting passed
correctly to the typer?
"""
from __future__ import annotations

from stt_button.app import Application
from stt_button.asr import StreamTranscriber
from stt_button.config import Config


class TyperStub:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs


class VadStub:
    """Sentinel for the VAD gate."""


class ControllerStub:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs


class DeltaStub:
    """DeltaTyper stub: records all constructor arguments."""

    created: list["DeltaStub"] = []

    def __init__(self, typer, **kwargs) -> None:
        self.kwargs = kwargs
        self.typer = typer
        DeltaStub.created.append(self)

    def reset(self) -> None:
        pass

    def update(self, text: str, committed_len: int = 0, *, force_revision: bool = False) -> None:
        pass

    def finalize(self, final_text: str, committed_len: int = 0) -> None:
        pass

    @property
    def last_text(self) -> str:
        return ""


def _application(monkeypatch, **config_kwargs) -> tuple[Application, ControllerStub]:
    monkeypatch.setattr("stt_button.app.DotoolTyper", TyperStub)
    monkeypatch.setattr("stt_button.app.SileroVadGate", VadStub)
    monkeypatch.setattr("stt_button.app.PushToTalkController", ControllerStub)
    app = Application(Config(**config_kwargs))
    controller = app._build_controller()
    return app, controller


class TestTranscriberWiring:
    def test_segment_keys_wired(self, monkeypatch) -> None:
        _app, controller = _application(monkeypatch, segment_pause_s=1.2,
                                        segment_min_s=2.0, segment_max_s=15.0)
        t = controller.kwargs["transcriber_factory"](
            engine=object(), interval_s=1.0, min_audio_s=0.8)
        assert (t.segment_pause_s, t.segment_min_s, t.segment_max_s) == (1.2, 2.0, 15.0)

    def test_whole_buffer_gets_vad_gate(self, monkeypatch) -> None:
        """The VAD gate must be wired in default mode."""
        _app, controller = _application(monkeypatch, streaming_mode="whole_buffer")
        factory = controller.kwargs["transcriber_factory"]
        transcriber = factory(engine=object(), interval_s=1.0, min_audio_s=0.8)
        assert isinstance(transcriber, StreamTranscriber)
        assert isinstance(transcriber.vad, VadStub)

    def test_adaptive_interval_wired(self, monkeypatch) -> None:
        _app, controller = _application(monkeypatch, asr_interval_adaptive_k=2.0,
                                        asr_interval_max_s=5.0)
        t = controller.kwargs["transcriber_factory"](
            engine=object(), interval_s=1.0, min_audio_s=0.8)
        assert t.asr_interval_adaptive_k == 2.0
        assert t.asr_interval_max_s == 5.0


class TestFinalPasteWiring:
    """The paster is created and wired only when final_paste=True."""

    def test_final_paste_wiring_respects_flag_and_config(self, monkeypatch) -> None:
        """Without flag: no paster. With flag: ClipboardPaster with paste_key/paste_binary."""
        from stt_button.typing import ClipboardPaster, DeltaTyper

        captured: dict[str, object] = {}

        class DeltaStub:
            def __init__(self, typer, **kwargs):
                captured["typer"] = typer
                captured.update(kwargs)
                captured["self"] = self

        class PasterStub:
            def __init__(self, typer, **kwargs):
                captured["paster_kwargs"] = kwargs

        monkeypatch.setattr("stt_button.app.DeltaTyper", DeltaStub)
        monkeypatch.setattr("stt_button.app.DotoolTyper", TyperStub)
        monkeypatch.setattr("stt_button.app.SileroVadGate", VadStub)
        monkeypatch.setattr("stt_button.app.PushToTalkController", ControllerStub)

        # Without flag: final_paster=None (previous behavior)
        app = Application(Config())
        app._build_controller()
        assert captured.get("final_paster", "fehlend") is None

        # With flag: paster created and configured with paste_key/paste_binary
        monkeypatch.setattr("stt_button.app.ClipboardPaster", PasterStub)
        app2 = Application(Config(final_paste=True, paste_key="ctrl+shift+v"))
        app2._build_controller()
        assert isinstance(captured.get("paster_kwargs"), dict)
        assert captured["paster_kwargs"] == {
            "copy_binary": "wl-copy",
            "paste_key": "ctrl+shift+v",
        }


    def test_final_paste_injects_terminal_focus_probe(self, monkeypatch) -> None:
        from stt_button import focus

        captured: dict[str, object] = {}

        class DeltaStub:
            def __init__(self, typer, **kwargs):
                captured.update(kwargs)

        class PasterStub:
            def __init__(self, typer, **kwargs):
                pass

        monkeypatch.setattr("stt_button.app.DeltaTyper", DeltaStub)
        monkeypatch.setattr("stt_button.app.ClipboardPaster", PasterStub)
        monkeypatch.setattr("stt_button.app.DotoolTyper", TyperStub)
        monkeypatch.setattr("stt_button.app.SileroVadGate", VadStub)
        monkeypatch.setattr("stt_button.app.PushToTalkController", ControllerStub)

        Application(Config(final_paste=True))._build_controller()
        assert captured["focus_probe"] is focus.focus_state

        captured.clear()
        Application(Config())._build_controller()
        assert captured.get("focus_probe") is None

    def test_focus_unknown_fallback_wired(self, monkeypatch) -> None:
        from stt_button import app as app_mod

        captured: dict[str, object] = {}

        class DeltaStub:
            def __init__(self, typer, **kwargs):
                captured.update(kwargs)

        monkeypatch.setattr("stt_button.app.DeltaTyper", DeltaStub)
        monkeypatch.setattr("stt_button.app.ClipboardPaster", lambda *a, **k: None)
        monkeypatch.setattr("stt_button.app.DotoolTyper", TyperStub)
        monkeypatch.setattr("stt_button.app.SileroVadGate", VadStub)
        monkeypatch.setattr("stt_button.app.PushToTalkController", ControllerStub)

        Application(Config(final_paste=True,
                           focus_unknown_fallback="type"))._build_controller()
        assert captured["focus_unknown_fallback"] == "type"
        assert captured["on_focus_unknown"] is app_mod._notify_focus_unknown


class TestTyperWiring:
    def test_typer_receives_autocorrect_flag(self, monkeypatch) -> None:
        monkeypatch.setattr("stt_button.app.DotoolTyper", TyperStub)
        created: list[TyperStub] = []

        def factory(**kwargs):
            created.append(TyperStub(**kwargs))
            return created[-1]

        monkeypatch.setattr("stt_button.app.DotoolTyper", factory)
        app = Application(Config(capslock_autocorrect=True))
        app._build_typer()
        assert created[0].kwargs["capslock_autocorrect"] is True


class TestFinalizeModeWiring:
    """STAGE 1: Application passes finalize_mode to StreamTranscriber."""

    def test_finalize_mode_passed_to_transcriber(self, monkeypatch) -> None:
        _app, controller = _application(
            monkeypatch, streaming_mode="whole_buffer", finalize_mode="cached"
        )
        factory = controller.kwargs["transcriber_factory"]
        transcriber = factory(engine=object(), interval_s=1.0, min_audio_s=0.8)
        assert isinstance(transcriber, StreamTranscriber)
        assert transcriber.finalize_mode == "cached"

    def test_finalize_mode_default_full(self, monkeypatch) -> None:
        _app, controller = _application(monkeypatch, streaming_mode="whole_buffer")
        factory = controller.kwargs["transcriber_factory"]
        transcriber = factory(engine=object(), interval_s=1.0, min_audio_s=0.8)
        assert transcriber.finalize_mode == "full"


class TestLiveModeWiring:
    """STAGE 2: Application passes live_typing_mode to DeltaTyper."""

    def test_live_typing_mode_passed_to_delta_typer(self, monkeypatch) -> None:
        monkeypatch.setattr("stt_button.app.DeltaTyper", DeltaStub)
        app = Application(Config(live_typing_mode="committed"))
        app._build_controller()
        assert DeltaStub.created[-1].kwargs["live_typing_mode"] == "committed"

    def test_live_typing_mode_default_committed(self, monkeypatch) -> None:
        monkeypatch.setattr("stt_button.app.DeltaTyper", DeltaStub)
        app = Application(Config())
        app._build_controller()
        assert DeltaStub.created[-1].kwargs["live_typing_mode"] == "committed"

    def test_live_typing_mode_explicit_full_passed_to_delta_typer(
        self, monkeypatch
    ) -> None:
        """Opt-in guard: explicitly setting 'full' must reach DeltaTyper."""
        monkeypatch.setattr("stt_button.app.DeltaTyper", DeltaStub)
        app = Application(Config(live_typing_mode="full"))
        app._build_controller()
        assert DeltaStub.created[-1].kwargs["live_typing_mode"] == "full"


class TestBufferedWiring:
    """STAGE 3: Application selects BufferedTranscriber ONLY for streaming_mode."""

    def test_buffered_mode_selects_buffered_transcriber(self, monkeypatch) -> None:
        from stt_button.buffered import BufferedTranscriber

        _app, controller = _application(monkeypatch, streaming_mode="buffered")
        factory = controller.kwargs["transcriber_factory"]
        transcriber = factory(engine=object(), interval_s=1.0, min_audio_s=3.0)
        assert isinstance(transcriber, BufferedTranscriber)
        assert transcriber.interval_s == 1.0
        assert transcriber.min_audio_s == 3.0
        assert transcriber.vad is not None  # SileroVadGate as in the other modes

    def test_buffered_config_values_reach_transcriber(self, monkeypatch) -> None:
        from stt_button.buffered import BufferedTranscriber

        _app, controller = _application(
            monkeypatch, streaming_mode="buffered",
            buffered_chunk_s=1.5, buffered_left_context_s=6.0,
            buffered_right_context_s=3.0, buffered_whole_buffer_until_s=10.0,
            buffered_max_final_window_s=20.0, commit_agreements=3,
        )
        factory = controller.kwargs["transcriber_factory"]
        t = factory(engine=object(), interval_s=1.0, min_audio_s=3.0)
        assert isinstance(t, BufferedTranscriber)
        assert t.chunk_s == 1.5
        assert t.left_context_s == 6.0
        assert t.right_context_s == 3.0
        assert t.whole_buffer_until_s == 10.0
        assert t.max_final_window_s == 20.0
        assert t.commit_agreements == 3

    def test_default_mode_still_stream_transcriber(self, monkeypatch) -> None:
        """Default (whole_buffer) still returns StreamTranscriber."""
        from stt_button.asr import StreamTranscriber
        from stt_button.buffered import BufferedTranscriber

        _app, controller = _application(monkeypatch)
        factory = controller.kwargs["transcriber_factory"]
        t = factory(engine=object(), interval_s=1.0, min_audio_s=3.0)
        assert isinstance(t, StreamTranscriber)
        assert not isinstance(t, BufferedTranscriber)

    def test_windowed_mode_untouched(self, monkeypatch) -> None:
        from stt_button.windowed import WindowedTranscriber

        _app, controller = _application(monkeypatch, streaming_mode="windowed")
        factory = controller.kwargs["transcriber_factory"]
        t = factory(engine=object(), interval_s=1.0, min_audio_s=3.0)
        assert isinstance(t, WindowedTranscriber)


class TestCachedMidTailWiring:
    """PHASE 2: Application passes finalize_mode='cached_mid_tail' through
    (existing generic finalize_mode pass-through)."""

    def test_cached_mid_tail_passed_to_transcriber(self, monkeypatch) -> None:
        _app, controller = _application(
            monkeypatch, streaming_mode="whole_buffer",
            finalize_mode="cached_mid_tail",
        )
        factory = controller.kwargs["transcriber_factory"]
        transcriber = factory(engine=object(), interval_s=1.0, min_audio_s=0.8)
        assert isinstance(transcriber, StreamTranscriber)
        assert transcriber.finalize_mode == "cached_mid_tail"
