"""Tests fuer das VAD-Gate im whole_buffer-Pfad (StreamTranscriber).

Zielfehler: Im Modus ``streaming_mode="whole_buffer"`` war das VAD-Gate nicht
verdrahtet. Stille/Rauschen wurde transkribiert, die ASR halluzinierte und der
DeltaTyper tippte Unsinn und nahm ihn per Backspace zurueck (14x Backspace in
30s Leerlauf beobachtet).

Regel: Meldet das VAD im NEUEN Audio-Bereich keine Sprache, darf weder
transkribiert noch ein Ergebnis geliefert werden.
"""
from __future__ import annotations

import numpy as np

from stt_button.asr import StreamTranscriber, TranscriptionResult

RATE = 16000


def audio(seconds: float) -> bytes:
    """Erzeugt ``seconds`` Audio in s16le mono."""
    return b"\x00\x00" * int(seconds * RATE)


class FakeEngine:
    """Zeichnet Aufrufe und liefert einen festen Text."""

    def __init__(self, text: str = "Hallo Welt") -> None:
        self.text = text
        self.calls: list[int] = []

    def transcribe(self, data: bytes, initial_prompt: str = "") -> TranscriptionResult:
        self.calls.append(len(data))
        return TranscriptionResult(text=self.text)


class RecordingVad:
    """VAD-Fake: merkt sich die geprueften Laengen und antwortet konfiguriert."""

    def __init__(self, speech: bool) -> None:
        self.speech = speech
        self.checked: list[int] = []

    def has_speech(self, audio_tail: np.ndarray) -> bool:
        self.checked.append(len(audio_tail))
        return self.speech


class TestControllerIntegration:
    """Ende-zu-Ende: Stille darf den Typer nie erreichen."""

    def test_silence_types_nothing(self) -> None:
        from stt_button.controller import PushToTalkController
        from stt_button.typing import DeltaTyper

        class Source:
            def __init__(self) -> None:
                self.chunks = [audio(0.5)] * 8

            def start(self) -> None:
                pass

            def stop(self) -> bytes:
                return b""

            def read(self, block: bool = True) -> bytes:
                return self.chunks.pop(0) if self.chunks else b""

        class Typer:
            def __init__(self) -> None:
                self.calls: list[tuple[str, ...]] = []

            def type_text(self, text: str) -> None:
                self.calls.append(("type", text))

            def backspace(self, n: int) -> None:
                self.calls.append(("backspace", str(n)))

            def key(self, name: str) -> None:
                self.calls.append(("key", name))

        engine = FakeEngine()
        typer = Typer()

        def factory(eng, interval_s: float, min_audio_s: float):
            return StreamTranscriber(
                eng,
                interval_s=interval_s,
                min_audio_s=min_audio_s,
                vad=RecordingVad(speech=False),
            )

        ctrl = PushToTalkController(
            config=None,
            source_factory=Source,
            engine=engine,
            delta_typer=DeltaTyper(typer, append_space=True),
            min_audio_s=0.8,
            asr_interval_s=0.5,
            transcriber_factory=factory,
        )
        ctrl.start_recording()
        for _ in range(6):
            ctrl.run_once()
        assert engine.calls == [], "Stille wurde transkribiert"
        assert [c for c in typer.calls if c[0] == "backspace"] == []
        assert [c for c in typer.calls if c[0] == "type"] == []
        ctrl.shutdown()


class TestVadGate:
    def test_silence_yields_no_result_and_no_engine_call(self) -> None:
        engine = FakeEngine()
        vad = RecordingVad(speech=False)
        t = StreamTranscriber(engine, interval_s=1.0, min_audio_s=0.8, vad=vad)
        t.feed(audio(1.0))
        assert t.maybe_transcribe() is None
        assert engine.calls == []
        assert vad.checked, "VAD wurde nicht befragt"

    def test_speech_transcribes(self) -> None:
        engine = FakeEngine()
        vad = RecordingVad(speech=True)
        t = StreamTranscriber(engine, interval_s=1.0, min_audio_s=0.8, vad=vad)
        t.feed(audio(1.0))
        result = t.maybe_transcribe()
        assert result is not None
        assert result.text == "Hallo Welt"
        assert len(engine.calls) == 1

    def test_no_vad_keeps_old_behaviour(self) -> None:
        engine = FakeEngine()
        t = StreamTranscriber(engine, interval_s=1.0, min_audio_s=0.8)
        t.feed(audio(1.0))
        assert t.maybe_transcribe() is not None
        assert len(engine.calls) == 1

    def test_vad_checks_only_new_audio(self) -> None:
        """Nach einer Transkription wird nur das Delta geprueft (Kosten)."""
        engine = FakeEngine()
        vad = RecordingVad(speech=True)
        t = StreamTranscriber(engine, interval_s=1.0, min_audio_s=0.8, vad=vad)
        t.feed(audio(1.0))
        t.maybe_transcribe()
        first_len = vad.checked[-1]
        t.feed(audio(1.0))
        t.maybe_transcribe()
        second_len = vad.checked[-1]
        assert first_len == int(1.0 * RATE)
        # Nur das neue 1s-Segment, nicht der ganze 2s-Puffer.
        assert second_len == int(1.0 * RATE)

    def test_silence_does_not_advance_commit_state(self) -> None:
        """VAD-Stille darf den Transkriptions-Zeitpunkt nicht vorschieben."""
        engine = FakeEngine()
        vad = RecordingVad(speech=False)
        t = StreamTranscriber(engine, interval_s=1.0, min_audio_s=0.8, vad=vad)
        t.feed(audio(1.0))
        assert t.maybe_transcribe() is None
        assert t.last_text == ""
        assert t.committed_len == 0

    def test_recovery_after_silence(self) -> None:
        """Stille uebersprungen, danach Sprache -> wird transkribiert."""
        engine = FakeEngine()
        vad = RecordingVad(speech=False)
        t = StreamTranscriber(engine, interval_s=1.0, min_audio_s=0.8, vad=vad)
        t.feed(audio(1.0))
        assert t.maybe_transcribe() is None
        vad.speech = True
        t.feed(audio(1.0))
        assert t.maybe_transcribe() is not None
        assert len(engine.calls) == 1

    def test_vad_is_throttled_to_interval(self) -> None:
        """VAD darf nicht bei jedem Tick laufen (Kosten bei Stille).

        Ohne Drosselung pruefte das Gate den ganzen Puffer im 20-ms-Takt, weil
        bei Stille ``_has_transcribed`` nie gesetzt wird.
        """
        engine = FakeEngine()
        vad = RecordingVad(speech=False)
        t = StreamTranscriber(engine, interval_s=1.0, min_audio_s=0.8, vad=vad)
        t.feed(audio(1.0))
        assert t.maybe_transcribe() is None
        assert len(vad.checked) == 1
        # Wenig neues Audio -> kein erneuter VAD-Lauf.
        for _ in range(3):
            t.feed(audio(0.1))
            assert t.maybe_transcribe() is None
        assert len(vad.checked) == 1
        # Ein volles Intervall neues Audio -> wieder pruefen.
        t.feed(audio(1.0))
        assert t.maybe_transcribe() is None
        assert len(vad.checked) == 2

    def test_reset_clears_vad_throttle(self) -> None:
        engine = FakeEngine()
        vad = RecordingVad(speech=False)
        t = StreamTranscriber(engine, interval_s=1.0, min_audio_s=0.8, vad=vad)
        t.feed(audio(1.0))
        t.maybe_transcribe()
        t.reset()
        t.feed(audio(1.0))
        assert t.maybe_transcribe() is None
        assert len(vad.checked) == 2

    def test_short_new_audio_is_not_blocked(self) -> None:
        """Zu kurzes Fenster (< 1s) kann das VAD nicht beurteilen -> nicht blocken.

        Mit ``interval_s`` < 1.0 wuerde ein VAD-Nein ("zu kurz fuer VAD")
        sonst jede Transkription verhindern.
        """
        engine = FakeEngine()
        vad = RecordingVad(speech=False)
        t = StreamTranscriber(engine, interval_s=0.2, min_audio_s=0.1, vad=vad)
        t.feed(audio(0.5))
        assert t.maybe_transcribe() is not None
        assert len(engine.calls) == 1
        # Fuer zu kurzes Audio wird das VAD gar nicht erst befragt.
        assert vad.checked == []

    def test_unknown_vad_state_does_not_crash(self) -> None:
        """VAD-Fehler duerfen das Diktat nicht verhindern."""

        class BrokenVad:
            def has_speech(self, audio_tail: np.ndarray) -> bool:
                raise RuntimeError("VAD kaputt")

        engine = FakeEngine()
        t = StreamTranscriber(
            engine, interval_s=1.0, min_audio_s=0.8, vad=BrokenVad()
        )
        t.feed(audio(1.0))
        # Absichtlich tolerant: das Gate ist eine Optimierung, kein Muss.
        assert t.maybe_transcribe() is not None

    def test_finalize_still_transcribes(self) -> None:
        """Die Schluss-Transkription bleibt ungefiltert (kein Datenverlust)."""
        engine = FakeEngine()
        vad = RecordingVad(speech=False)
        t = StreamTranscriber(engine, interval_s=1.0, min_audio_s=0.8, vad=vad)
        t.feed(audio(1.0))
        assert t.maybe_transcribe() is None
        assert t.finalize().text == "Hallo Welt"
        assert len(engine.calls) == 1
