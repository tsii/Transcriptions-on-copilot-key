"""Tests fuer die Verdrahtung in app.py.

Geprueft wird die Komposition (nicht die Logik): bekommt der Transcriber im
Default-Modus ``whole_buffer`` ein VAD-Gate, und wird die CapsLock-Einstellung
korrekt an den Typer durchgereicht?
"""
from __future__ import annotations

from stt_button.app import Application
from stt_button.asr import StreamTranscriber
from stt_button.config import Config


class TyperStub:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs


class VadStub:
    """Sentinel fuer das VAD-Gate."""


class ControllerStub:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs


def _application(monkeypatch, **config_kwargs) -> tuple[Application, ControllerStub]:
    monkeypatch.setattr("stt_button.app.DotoolTyper", TyperStub)
    monkeypatch.setattr("stt_button.app.SileroVadGate", VadStub)
    monkeypatch.setattr("stt_button.app.PushToTalkController", ControllerStub)
    app = Application(Config(**config_kwargs))
    controller = app._build_controller()
    return app, controller


class TestTranscriberWiring:
    def test_whole_buffer_gets_vad_gate(self, monkeypatch) -> None:
        """Im Default-Modus muss das VAD-Gate verdrahtet sein."""
        _app, controller = _application(monkeypatch, streaming_mode="whole_buffer")
        factory = controller.kwargs["transcriber_factory"]
        transcriber = factory(engine=object(), interval_s=1.0, min_audio_s=0.8)
        assert isinstance(transcriber, StreamTranscriber)
        assert isinstance(transcriber.vad, VadStub)


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
