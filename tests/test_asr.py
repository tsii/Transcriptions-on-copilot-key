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


class TestLiveAsrUntilLimit:
    """STUFE 3: live_asr_until_s stoppt Live-Ticks nach Pufferlaenge.

    Ab ``live_asr_until_s`` Sekunden Puffer-Audio liefert maybe_transcribe()
    kein Ergebnis mehr (keine Engine-Aufrufe) - der Whole-Buffer-Ansatz wird
    pro Tick teurer. feed() sammelt weiter, finalize() transkribiert den
    VOLLSTaendigen Puffer, reset() reaktiviert die Live-Ticks.
    """

    def test_below_limit_ticks_normally(self) -> None:
        """(1) Unter dem Limit laufen normale Live-Ticks."""
        engine = FakeEngine(["Hallo Welt", "Hallo Welt und"])
        t = StreamTranscriber(
            engine, interval_s=0.1, min_audio_s=0.1, live_asr_until_s=10.0
        )
        t.feed(audio(0.5))
        result = t.maybe_transcribe()
        assert result is not None
        assert result.text == "Hallo Welt"

    def test_above_limit_no_engine_calls(self) -> None:
        """(2) Ueber dem Limit: maybe_transcribe() ruft die Engine NICHT."""
        engine = FakeEngine(["Hallo Welt"])
        t = StreamTranscriber(
            engine, interval_s=0.1, min_audio_s=0.1, live_asr_until_s=1.0
        )
        t.feed(audio(0.5))
        assert t.maybe_transcribe() is not None  # unter dem Limit
        t.feed(audio(1.0))  # jetzt 1.5s > 1.0s Limit
        assert t.maybe_transcribe() is None
        assert len(engine.calls) == 1, (
            "Engine wurde trotz Live-Limit erneut gerufen"
        )
        # Noch mehr Audio aendert daran nichts.
        t.feed(audio(1.0))
        assert t.maybe_transcribe() is None
        assert len(engine.calls) == 1

    def test_feed_keeps_collecting(self) -> None:
        """(3) feed() nimmt Audio vollstaendig auf, auch ueber dem Limit."""
        engine = FakeEngine(["Hallo Welt"])
        t = StreamTranscriber(
            engine, interval_s=0.1, min_audio_s=0.1, live_asr_until_s=1.0
        )
        t.feed(audio(0.5))
        t.maybe_transcribe()
        t.feed(audio(2.0))
        assert t.audio_seconds == pytest.approx(2.5)
        assert t.maybe_transcribe() is None  # Live gestoppt

    def test_finalize_transcribes_full_buffer(self) -> None:
        """(4) finalize() transkribiert den VOLLSTaendigen Puffer (Cache aus).
        """
        engine = FakeEngine(["kurz", "alles da"])
        t = StreamTranscriber(
            engine, interval_s=0.1, min_audio_s=0.1, live_asr_until_s=1.0
        )
        t.feed(audio(0.5))
        t.maybe_transcribe()
        t.feed(audio(2.0))  # ueber Limit -> keine Live-Ticks mehr
        result = t.finalize()
        assert result.text == "alles da"
        # Engine sah den kompletten 2.5s-Puffer:
        assert engine.calls[-1][0] == int(2.5 * BYTES_PER_SECOND)

    def test_reset_reactivates_live_ticks(self) -> None:
        """(5) reset() setzt den Zaehler zurueck - Live-Ticks laufen wieder."""
        engine = FakeEngine(["eins", "zwei", "drei"])
        t = StreamTranscriber(
            engine, interval_s=0.1, min_audio_s=0.1, live_asr_until_s=1.0
        )
        t.feed(audio(0.5))
        t.maybe_transcribe()
        t.feed(audio(1.0))
        assert t.maybe_transcribe() is None  # Limit erreicht
        t.reset()
        t.feed(audio(0.5))
        result = t.maybe_transcribe()
        assert result is not None
        assert result.text == "zwei"  # FakeEngine: 2. Call -> texts[1]
        assert len(engine.calls) == 2

    def test_release_after_limit_finalize_starts_immediately(self) -> None:
        """(6) Release nach Limit ohne laufenden Tick: Finalize startet sofort.

        Der Finalizer nimmt ``_exec_lock``; ohne aktiven Live-Tick (die
        Live-Ticks sind ja ueber dem Limit ausgesetzt) steht das Lock frei -
        finalize() beginnt ohne Wartezeit.
        """
        engine = FakeEngine(["Hallo Welt"])
        t = StreamTranscriber(
            engine, interval_s=0.1, min_audio_s=0.1, live_asr_until_s=1.0
        )
        t.feed(audio(0.5))
        t.maybe_transcribe()
        t.feed(audio(2.0))
        assert t.maybe_transcribe() is None  # kein Tick mehr -> Lock frei
        result = t.finalize()
        assert result.text == "Hallo Welt"

    def test_zero_means_no_limit(self) -> None:
        """Default 0.0: Live-Ticks bleiben immer aktiv (bisheriges Verhalten)."""
        engine = FakeEngine(["eins", "zwei", "drei", "vier"])
        t = StreamTranscriber(
            engine, interval_s=0.1, min_audio_s=0.1, live_asr_until_s=0.0
        )
        for _ in range(4):
            t.feed(audio(0.5))
            assert t.maybe_transcribe() is not None


class TestFinalizeCached:
    """STUFE 1: finalize_mode="cached" spart den doppelten Engine-Call.

    War der Puffer seit dem letzten ERFOLGREICHEN Live-Tick unveraendert,
    ist das Finale ergebnislos: der Text kommt aus dem Cache, die Engine
    wird nicht noch einmal gerufen (identische Eingabe -> identische
    Ausgabe, da die Engine-Determinismus-Parameter fest sind).
    """

    def test_finalize_cached_unchanged_buffer_skips_engine(self) -> None:
        engine = FakeEngine(["Hallo Welt"])
        t = StreamTranscriber(
            engine, interval_s=0.1, min_audio_s=0.1, finalize_mode="cached"
        )
        t.feed(audio(1.0))
        live = t.maybe_transcribe()
        assert live is not None
        calls_before = len(engine.calls)

        result = t.finalize()

        assert len(engine.calls) == calls_before, (
            "cached-Finalize rief die Engine trotz unveraendertem Puffer"
        )
        assert result.text == "Hallo Welt"
        # Volle Korrekturfaehigkeit bleibt erhalten (wie beim vollen Pfad).
        assert result.committed_len == 0

    def test_cached_finalize_does_not_add_agreement(self) -> None:
        """Die Agreement-History darf durch den Cache-Pfad nicht wachsen."""
        engine = FakeEngine(["Hallo Welt", "Hallo Welt"])
        t = StreamTranscriber(
            engine, interval_s=0.1, min_audio_s=0.1, finalize_mode="cached"
        )
        t.feed(audio(0.5))
        t.maybe_transcribe()
        t.feed(audio(0.5))
        t.maybe_transcribe()
        history_before = list(t._history)

        t.finalize()

        assert t._history == history_before, (
            "cached-Finalize veraendert die Agreement-History"
        )

    def test_cached_finalize_invalidated_by_feed_and_reset(self) -> None:
        """Neues feed() oder reset() entwertet den Cache -> voller Engine-Call."""
        engine = FakeEngine(["Hallo", "Hallo Welt"])
        t = StreamTranscriber(
            engine, interval_s=0.1, min_audio_s=0.1, finalize_mode="cached"
        )
        t.feed(audio(1.0))
        t.maybe_transcribe()
        # Neues Audio seit dem letzten erfolgreichen Tick -> Cache ungueltig.
        t.feed(audio(0.5))
        calls_before = len(engine.calls)
        result = t.finalize()
        assert len(engine.calls) == calls_before + 1
        assert result.text == "Hallo Welt"

        # Nach reset() gibt es keinen gueltigen Cache mehr.
        engine2 = FakeEngine(["Neustext"])
        t2 = StreamTranscriber(
            engine2, interval_s=0.1, min_audio_s=0.1, finalize_mode="cached"
        )
        t2.feed(audio(1.0))
        t2.maybe_transcribe()
        t2.reset()
        t2.feed(audio(1.0))
        assert t2.finalize() is not None
        assert len(engine2.calls) == 2  # 1 Live + 1 final (kein Treffer)

    def test_cached_finalize_falls_back_after_failed_tick(self) -> None:
        """Exception im Live-Tick setzt keinen Abgeschlossen-Vermerk."""
        class FlakyEngine:
            def __init__(self) -> None:
                self.calls = 0

            def transcribe(self, data, initial_prompt=""):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError(" Modell-Boom")
                return TranscriptionResult(text="spaeter Text")

        engine = FlakyEngine()
        t = StreamTranscriber(
            engine, interval_s=0.1, min_audio_s=0.1, finalize_mode="cached"
        )
        t.feed(audio(1.0))
        with pytest.raises(RuntimeError):
            t.maybe_transcribe()
        # Kein erfolgreicher Tick -> Finalize MUSS die Engine rufen.
        result = t.finalize()
        assert engine.calls == 2
        assert result.text == "spaeter Text"

    def test_default_finalize_mode_is_full(self) -> None:
        """Default bleibt "full": Finalize transkribiert wie bisher neu."""
        engine = FakeEngine(["Hallo Welt"])
        t = StreamTranscriber(engine, interval_s=0.1, min_audio_s=0.1)
        t.feed(audio(1.0))
        t.maybe_transcribe()
        calls_before = len(engine.calls)
        t.finalize()
        assert len(engine.calls) == calls_before + 1
