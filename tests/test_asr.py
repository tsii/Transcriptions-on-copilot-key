"""Tests fuer den Streaming-Transkriber (TDD-Kette 3).

Getestet wird ausschliesslich die Logik - das Whisper-Modell wird durch
einen Fake-Engine ersetzt, damit die Tests schnell und ohne Download laufen.
"""
from __future__ import annotations

import pytest

from stt_button.asr import StreamTranscriber, TranscriptionResult

RATE = 16000
BYTES_PER_SECOND = RATE * 2  # s16le mono


def audio(seconds: float) -> bytes:
    """Erzeugt ``seconds`` Audio in s16le mono."""
    return b"\x00\x00" * int(seconds * RATE)


class FakeEngine:
    """Liefert Texte in fester Reihenfolge, abhaengig von der Aufrufnummer."""

    def __init__(self, texts: list[str] | None = None) -> None:
        self.texts = texts or []
        self.calls: list[tuple[int, str]] = []
        self.prompts: list[str] = []

    def transcribe(
        self, data: bytes, initial_prompt: str = ""
    ) -> TranscriptionResult:
        self.calls.append((len(data), initial_prompt))
        self.prompts.append(initial_prompt)
        idx = len(self.calls) - 1
        text = self.texts[idx] if idx < len(self.texts) else (
            self.texts[-1] if self.texts else ""
        )
        return TranscriptionResult(text=text)


class TestBuffering:
    def test_nothing_happens_below_min_audio(self) -> None:
        engine = FakeEngine(["Hallo"])
        t = StreamTranscriber(engine, interval_s=1.5, min_audio_s=0.8)
        t.feed(audio(0.4))
        assert t.maybe_transcribe() is None
        assert engine.calls == []

    def test_transcribes_above_min_audio(self) -> None:
        engine = FakeEngine(["Hallo Welt"])
        t = StreamTranscriber(engine, interval_s=1.5, min_audio_s=0.8)
        t.feed(audio(1.0))
        result = t.maybe_transcribe()
        assert result is not None
        assert result.text == "Hallo Welt"

    def test_respects_interval(self) -> None:
        engine = FakeEngine(["Hallo Welt"])
        t = StreamTranscriber(engine, interval_s=1.5, min_audio_s=0.8)
        t.feed(audio(1.0))
        assert t.maybe_transcribe() is not None
        # Direkt danach: noch nicht genug neue Audiodaten
        t.feed(audio(0.1))
        assert t.maybe_transcribe() is None
        assert len(engine.calls) == 1

    def test_transcribes_again_after_interval(self) -> None:
        engine = FakeEngine(["Hallo", "Hallo Welt"])
        t = StreamTranscriber(engine, interval_s=1.5, min_audio_s=0.8)
        t.feed(audio(1.0))
        t.maybe_transcribe()
        t.feed(audio(1.6))
        result = t.maybe_transcribe()
        assert result is not None
        assert result.text == "Hallo Welt"

    def test_buffer_grows(self) -> None:
        engine = FakeEngine()
        t = StreamTranscriber(engine, interval_s=1.5, min_audio_s=0.8)
        t.feed(audio(1.0))
        t.feed(audio(1.0))
        t.maybe_transcribe()
        assert engine.calls[0][0] == int(2.0 * BYTES_PER_SECOND)


class TestCommittedPrefix:
    def test_prefix_commits_after_two_agreements(self) -> None:
        engine = FakeEngine(["Hallo Welt", "Hallo Welt"])
        t = StreamTranscriber(engine, interval_s=0.1, min_audio_s=0.1)
        t.feed(audio(0.5))
        t.maybe_transcribe()
        t.feed(audio(0.5))
        t.maybe_transcribe()
        assert t.committed_len == len("Hallo ")

    def test_prefix_not_committed_after_one_agreement(self) -> None:
        engine = FakeEngine(["Hallo Welt"])
        t = StreamTranscriber(engine, interval_s=0.1, min_audio_s=0.1)
        t.feed(audio(0.5))
        t.maybe_transcribe()
        assert t.committed_len == 0

    def test_committed_prefix_never_shrinks(self) -> None:
        engine = FakeEngine(["Hallo Welt", "Hallo Welt", "Nichts"])
        t = StreamTranscriber(engine, interval_s=0.1, min_audio_s=0.1)
        t.feed(audio(0.5))
        t.maybe_transcribe()
        t.feed(audio(0.5))
        t.maybe_transcribe()
        t.feed(audio(0.5))
        t.maybe_transcribe()
        assert t.committed_len == len("Hallo ")

    def test_result_carries_committed_len(self) -> None:
        engine = FakeEngine(["Hallo Welt", "Hallo Welt"])
        t = StreamTranscriber(engine, interval_s=0.1, min_audio_s=0.1)
        t.feed(audio(0.5))
        t.maybe_transcribe()
        t.feed(audio(0.5))
        result = t.maybe_transcribe()
        assert result is not None
        assert result.committed_len == len("Hallo ")


    def test_multiple_words_commit_up_to_last_space(self) -> None:
        engine = FakeEngine(["Hallo Welt foo", "Hallo Welt foo"])
        t = StreamTranscriber(engine, interval_s=0.1, min_audio_s=0.1)
        t.feed(audio(0.5))
        t.maybe_transcribe()
        t.feed(audio(0.5))
        t.maybe_transcribe()
        assert t.committed_len == len("Hallo Welt ")

    def test_incomplete_word_is_not_committed(self) -> None:
        engine = FakeEngine(["Hallo Wel", "Hallo Wel"])
        t = StreamTranscriber(engine, interval_s=0.1, min_audio_s=0.1)
        t.feed(audio(0.5))
        t.maybe_transcribe()
        t.feed(audio(0.5))
        t.maybe_transcribe()
        # Committet wird nur bis zur Wortgrenze "Hallo "; das unvollstaendige
        # Wort "Wel" bleibt korrigierbar.
        assert t.committed_len == len("Hallo ")

    def test_finalize_is_fully_correctable(self) -> None:
        engine = FakeEngine(["Hallo Welt", "Hallo Welt"])
        t = StreamTranscriber(engine, interval_s=0.1, min_audio_s=0.1)
        t.feed(audio(0.5))
        t.maybe_transcribe()
        t.feed(audio(0.5))
        t.maybe_transcribe()
        result = t.finalize()
        # Die Schluss-Transkription darf alles korrigieren.
        assert result.committed_len == 0

    def test_finalize_does_not_shrink_commit_state(self) -> None:
        engine = FakeEngine(["Hallo Welt", "Hallo Welt"])
        t = StreamTranscriber(engine, interval_s=0.1, min_audio_s=0.1)
        t.feed(audio(0.5))
        t.maybe_transcribe()
        t.feed(audio(0.5))
        t.maybe_transcribe()
        t.finalize()
        assert t.committed_len == len("Hallo ")


    def test_commit_agreements_three(self) -> None:
        engine = FakeEngine(["Hallo Welt", "Hallo Welt", "Hallo Welt"])
        t = StreamTranscriber(engine, interval_s=0.1, min_audio_s=0.1,
                              commit_agreements=3)
        t.feed(audio(0.5))
        t.maybe_transcribe()
        t.feed(audio(0.5))
        t.maybe_transcribe()
        t.feed(audio(0.5))
        t.maybe_transcribe()
        assert t.committed_len == len("Hallo ")

    def test_commit_agreements_three_divergent_middle(self) -> None:
        engine = FakeEngine(["Hallo Welt", "Hallo Weld", "Hallo Welt"])
        t = StreamTranscriber(engine, interval_s=0.1, min_audio_s=0.1,
                              commit_agreements=3)
        for _ in range(3):
            t.feed(audio(0.5))
            t.maybe_transcribe()
        # Das letzte Wort flackert -> nur "Hallo " wird committet
        assert t.committed_len == len("Hallo ")


class TestFinalize:
    def test_finalize_returns_text(self) -> None:
        engine = FakeEngine(["Hallo Welt"])
        t = StreamTranscriber(engine, interval_s=1.5, min_audio_s=0.8)
        t.feed(audio(1.0))
        result = t.finalize()
        assert result.text == "Hallo Welt"

    def test_finalize_uses_committed_prefix_as_prompt(self) -> None:
        engine = FakeEngine(["Hallo Welt", "Hallo Welt", "Hallo Welt final"])
        t = StreamTranscriber(engine, interval_s=0.1, min_audio_s=0.1)
        t.feed(audio(0.5))
        t.maybe_transcribe()
        t.feed(audio(0.5))
        t.maybe_transcribe()
        t.feed(audio(0.5))
        t.maybe_transcribe()
        t.finalize()
        assert engine.prompts[-1] == "Hallo "

    def test_finalize_with_empty_buffer(self) -> None:
        engine = FakeEngine()
        t = StreamTranscriber(engine, interval_s=1.5, min_audio_s=0.8)
        result = t.finalize()
        assert result.text == ""

    def test_finalize_below_min_audio_still_transcribes(self) -> None:
        engine = FakeEngine(["Hallo"])
        t = StreamTranscriber(engine, interval_s=1.5, min_audio_s=5.0)
        t.feed(audio(0.5))
        result = t.finalize()
        assert result.text == "Hallo"


class TestReset:
    def test_reset_clears_buffer_and_commit(self) -> None:
        engine = FakeEngine(["Hallo Welt", "Hallo Welt"])
        t = StreamTranscriber(engine, interval_s=0.1, min_audio_s=0.1)
        t.feed(audio(0.5))
        t.maybe_transcribe()
        t.feed(audio(0.5))
        t.maybe_transcribe()
        assert t.committed_len > 0
        t.reset()
        assert t.committed_len == 0
        assert t.audio_seconds == 0.0

    def test_reset_makes_next_call_transcribe_again(self) -> None:
        engine = FakeEngine(["Hallo", "Hallo Welt"])
        t = StreamTranscriber(engine, interval_s=0.1, min_audio_s=0.1)
        t.feed(audio(0.5))
        t.maybe_transcribe()
        t.reset()
        t.feed(audio(0.5))
        assert t.maybe_transcribe() is not None
        assert len(engine.calls) == 2


class TestErrors:
    def test_engine_exception_propagates(self) -> None:
        class Boom:
            def transcribe(self, data, initial_prompt=""):
                raise RuntimeError("Modell kaputt")

        t = StreamTranscriber(Boom(), interval_s=0.1, min_audio_s=0.1)
        t.feed(audio(0.5))
        with pytest.raises(RuntimeError):
            t.maybe_transcribe()

    def test_state_stays_consistent_after_error(self) -> None:
        class Boom:
            def transcribe(self, data, initial_prompt=""):
                raise RuntimeError("Modell kaputt")

        t = StreamTranscriber(Boom(), interval_s=0.1, min_audio_s=0.1)
        t.feed(audio(0.5))
        with pytest.raises(RuntimeError):
            t.maybe_transcribe()
        assert t.audio_seconds == pytest.approx(0.5)


class TestAudioSeconds:
    def test_tracks_seconds(self) -> None:
        engine = FakeEngine()
        t = StreamTranscriber(engine, interval_s=1.5, min_audio_s=0.8, sample_rate=RATE)
        t.feed(audio(1.0))
        assert t.audio_seconds == pytest.approx(1.0)
        t.feed(audio(0.5))
        assert t.audio_seconds == pytest.approx(1.5)


class TestParakeetEngine:
    def test_load_is_idempotent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from stt_button.asr import ParakeetEngine

        calls = {"n": 0}

        def fake_processor_from_pretrained(model):
            return object()

        def fake_model_from_pretrained(model, dtype=None):
            calls["n"] += 1
            fake = type("M", (), {"dtype": "float32", "to": lambda s, d: s,
                                      "eval": lambda s: s, "generate": lambda *a, **k: None})()
            return fake

        monkeypatch.setattr(
            "transformers.AutoProcessor.from_pretrained", fake_processor_from_pretrained
        )
        monkeypatch.setattr(
            "transformers.AutoModelForTDT.from_pretrained", fake_model_from_pretrained
        )

        e = ParakeetEngine()
        e.load()
        e.load()
        # Modell sollte nur einmal tatsaechlich geladen werden.
        assert calls["n"] == 1
        assert e.loaded is True

    def test_transcribe_returns_result(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from stt_button.asr import ParakeetEngine

        class FakeProcessor:
            def __call__(self, audio, sampling_rate):
                class Batch(dict):
                    def to(self, device, dtype=None):
                        return self

                return Batch({"input": audio})

            def decode(self, sequences, skip_special_tokens=True):
                return ["Hallo Welt"]

        class FakeModel:
            dtype = "float32"

            def to(self, d):
                return self

            def eval(self):
                return self

            def generate(self, **kwargs):
                return type("O", (), {"sequences": [[1, 2, 3]]})()

        monkeypatch.setattr(
            "transformers.AutoProcessor.from_pretrained",
            lambda m: FakeProcessor(),
        )
        monkeypatch.setattr(
            "transformers.AutoModelForTDT.from_pretrained",
            lambda m, dtype=None: FakeModel(),
        )

        e = ParakeetEngine()
        result = e.transcribe(b"\x00\x00" * 16000)
        assert result.text == "Hallo Welt"

    def test_transcribe_strips_ansi(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from stt_button.asr import ParakeetEngine

        class FakeProcessor:
            def __call__(self, audio, sampling_rate):
                class Batch(dict):
                    def to(self, device, dtype=None):
                        return self

                return Batch({"input": audio})

            def decode(self, sequences, skip_special_tokens=True):
                return ["\x1b[2KHallo\x1b[2K"]

        class FakeModel:
            dtype = "float32"

            def to(self, d):
                return self

            def eval(self):
                return self

            def generate(self, **kwargs):
                return type("O", (), {"sequences": [[1]]})()

        monkeypatch.setattr(
            "transformers.AutoProcessor.from_pretrained", lambda m: FakeProcessor()
        )
        monkeypatch.setattr(
            "transformers.AutoModelForTDT.from_pretrained",
            lambda m, dtype=None: FakeModel(),
        )

        e = ParakeetEngine()
        result = e.transcribe(b"\x00\x00" * 16000)
        assert result.text == "Hallo"


class TestTokenBudget:
    def test_token_budget_scales_with_audio(self) -> None:
        """Fix B: max_new_tokens muss mit der Audiolaenge skalieren."""
        from stt_button.asr import _token_budget
        # 30s Audio -> mind. 360 Tokens (12 Tokens/s)
        assert _token_budget(30.0) >= 360
        # 60s -> mind. 720
        assert _token_budget(60.0) >= 720
        # kurze Audio bleibt beim Mindestbudget
        assert _token_budget(1.0) >= 256

    def test_token_budget_capped(self) -> None:
        from stt_button.asr import _token_budget
        # Deckel gegen Ausreisser
        assert _token_budget(10000.0) <= 4096
