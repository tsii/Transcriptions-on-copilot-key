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


class TestLivePausedSignal:
    """STUFE 4: Limit-Erreichung genau einmal melden (take_live_paused).

    Signatur (minimal, dokumentiert): ``take_live_paused() -> bool`` —
    liefert ``True`` genau einmal, nachdem ``maybe_transcribe()`` erstmals
    das Live-Limit erreicht hat (nur bei ``live_asr_until_s > 0``), und
    konsumiert die Meldung. Weitere Aufrufe liefern ``False`` bis ein
    ``reset()`` + erneutes Limit-Erreichen folgt.
    """

    def test_pending_true_exactly_once_after_limit(self) -> None:
        """(5a) Nach dem Limit genau EINE Meldung, spaeter keine mehr."""
        engine = FakeEngine(["Hallo Welt"])
        t = StreamTranscriber(
            engine, interval_s=0.1, min_audio_s=0.1, live_asr_until_s=1.0
        )
        t.feed(audio(0.5))
        assert t.maybe_transcribe() is not None
        assert t.take_live_paused() is False  # Limit noch nicht erreicht
        t.feed(audio(1.0))  # 1.5s Puffer > 1.0s Limit
        assert t.maybe_transcribe() is None
        assert t.take_live_paused() is True
        # Weitere Ticks ueber dem Limit melden NICHTS Neues mehr:
        t.feed(audio(1.0))
        assert t.maybe_transcribe() is None
        assert t.take_live_paused() is False

    def test_below_limit_never_pending(self) -> None:
        """(5b) Unter dem Limit: keine Meldung, Ticks laufen normal."""
        engine = FakeEngine(["Hallo Welt", "Hallo Welt und"])
        t = StreamTranscriber(
            engine, interval_s=0.1, min_audio_s=0.1, live_asr_until_s=10.0
        )
        t.feed(audio(0.5))
        assert t.maybe_transcribe() is not None
        t.feed(audio(0.5))
        assert t.maybe_transcribe() is not None
        assert t.take_live_paused() is False

    def test_default_zero_never_pending(self) -> None:
        """(7/5c) Default live_asr_until_s=0.0: NIE eine Limit-Meldung."""
        engine = FakeEngine(["Hallo Welt", "Hallo Welt und", "Hallo Welt und mehr"])
        t = StreamTranscriber(
            engine, interval_s=0.1, min_audio_s=0.1, live_asr_until_s=0.0
        )
        for _ in range(3):
            t.feed(audio(0.5))
            assert t.maybe_transcribe() is not None
            assert t.take_live_paused() is False

    def test_reset_rearms_the_signal(self) -> None:
        """(5d) reset() setzt das Signal zurueck: naechstes Limit meldet wieder."""
        engine = FakeEngine(["eins", "zwei"])
        t = StreamTranscriber(
            engine, interval_s=0.1, min_audio_s=0.1, live_asr_until_s=1.0
        )
        t.feed(audio(0.5))
        t.maybe_transcribe()
        t.feed(audio(1.0))
        assert t.maybe_transcribe() is None
        assert t.take_live_paused() is True
        t.reset()
        t.feed(audio(0.5))
        t.maybe_transcribe()
        t.feed(audio(1.0))
        assert t.maybe_transcribe() is None
        assert t.take_live_paused() is True  # wieder genau eine Meldung

    def test_feed_over_limit_signals_without_maybe_transcribe(self) -> None:
        """BUG 1 (ROT): feed() uebers Limit meldet auch OHNE maybe_transcribe().

        Realtest 11:56: Der letzte Live-Tick startet unter dem Limit, seine
        2.8s-Inferenz ueberdauert die Grenze; der Release kommt vor dem
        naechsten Tick - maybe_transcribe() sieht das Limit nie. Deshalb
        erkennt take_live_paused() das Limit SELBST (feed-Zeitpunkt egal):
        Puffer >= live_asr_until_s genuegt fuer die Meldung.
        """
        engine = FakeEngine(["Hallo Welt"])
        t = StreamTranscriber(
            engine, interval_s=0.1, min_audio_s=0.1, live_asr_until_s=1.0
        )
        # KEIN maybe_transcribe()-Aufruf - nur fuettern ueber das Limit:
        t.feed(audio(1.5))  # 1.5s Puffer > 1.0s Limit
        assert t.take_live_paused() is True, (
            "Limit-Erreichung ohne maybe_transcribe() nicht erkannt"
        )
        # Genau einmal: konsumiert, weitere Abrufe False.
        assert t.take_live_paused() is False
        t.feed(audio(1.0))  # weiterer Zuwachs: KEINE zweite Meldung
        assert t.take_live_paused() is False

    def test_feed_under_limit_never_signals_via_take(self) -> None:
        """BUG 1 (ROT): Unter dem Limit und bei 0.0 niemals eine Meldung.

        Auch bei Ueberfuetterung OHNE maybe_transcribe() bleibt die Grenze
        bindend: live_asr_until_s=0.0 ist deaktiviert.
        """
        engine = FakeEngine(["Hallo Welt"])
        t = StreamTranscriber(
            engine, interval_s=0.1, min_audio_s=0.1, live_asr_until_s=1.0
        )
        t.feed(audio(0.5))  # 0.5s < 1.0s Limit
        assert t.take_live_paused() is False
        t.feed(audio(0.4))  # 0.9s, weiterhin darunter
        assert t.take_live_paused() is False

        off = StreamTranscriber(
            FakeEngine(["x"]), interval_s=0.1, min_audio_s=0.1,
            live_asr_until_s=0.0,
        )
        off.feed(audio(10.0))  # Limit deaktiviert: NIE melden
        assert off.take_live_paused() is False


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


# ---------------------------------------------------------------------------
# stitch_word_overlap -- Phase 1, isoliert (wird NICHT im Produktivpfad genutzt)
# ---------------------------------------------------------------------------

from stt_button.asr import stitch_word_overlap  # noqa: E402


class TestStitchWordOverlap:
    """Tests fuer stitch_word_overlap: haengt ein Fenster an exakte Wortanker."""

    def test_a_klassischer_anker(self):
        """Regel 2/5: exakter Anker 'geht es dir' am previous-Ende, neu 'gut heute'."""
        assert (
            stitch_word_overlap("Hallo wie geht es dir", "geht es dir gut heute")
            == "Hallo wie geht es dir gut heute"
        )

    def test_b_suffixfragment_kein_duplikat(self):
        """Regression: Fenster beginnt mit 'unkte' (SUFFIX-Fragment von 'Punkte').

        Selbst abgeleitete Erwartung aus Regel 4/5: Nur das ALLERERSTE
        Fensterwort darf echtes SUFFIX-Fragment sein; previous behaelt das
        volle Wort 'Punkte', das Fragment faellt ersatzlos weg. Anker sind
        'Punkte' (fragmentiert) + 'hier' (zeichengleich) als letzte zwei
        previous-Woerter, neu dahinter: 'sind schon'. Ergebnis also
        previous + 'sind schon' -- KEIN 'Punkte Punkte'-Duplikat und kein
        Fragmentrest 'unkte' im Ergebnis.
        """
        assert (
            stitch_word_overlap("das sind rote Punkte hier", "unkte hier sind schon")
            == "das sind rote Punkte hier sind schon"
        )

    def test_c1_ein_letztes_wort_ersetzt(self):
        """Regel 3: genau EIN letztes unvollstaendiges previous-Wort ('de')
        wird durch das Fensterwort ('deutlich') ersetzt."""
        assert stitch_word_overlap("das ist de", "deutlich besser") == "das ist deutlich besser"

    def test_c2_zwei_abweichende_alte_woerter(self):
        """Regel 3: 'gut' vs 'total' UND 'echt' vs 'daneben' -- zwei abweichende
        alte Woerter -> None."""
        assert stitch_word_overlap("das war echt gut", "total daneben") is None

    def test_d1_protected_prefix_verwirft_kandidaten(self):
        """Regel 6: Kandidat 'wie geht es dir' beginnt nicht mit 'Hallo wie'."""
        assert stitch_word_overlap("wie geht es", "geht es dir", protected_prefix="Hallo wie") is None
        # ohne Schutzpraefix liefert derselbe Fall den Kandidaten:
        assert stitch_word_overlap("wie geht es", "geht es dir") == "wie geht es dir"

    def test_d2_protected_prefix_konsistent(self):
        """Regel 6 erfuellt: Ergebnis beginnt zeichengleich mit 'Hallo wie'."""
        assert (
            stitch_word_overlap(
                "Hallo wie geht es", "wie geht es dir", protected_prefix="Hallo wie"
            )
            == "Hallo wie geht es dir"
        )

    def test_e_kein_fuzzy_ueber_komma(self):
        """Regel 1: 'Welt' != 'Welt,' -- Interpunktion exakt; ein angehaengtes
        Komma ist keine Wortvervollstaendigung -> None."""
        assert stitch_word_overlap("Ich sage Welt", "Welt, wie") is None

    def test_f_mehrdeutigkeit_zwei_anker(self):
        """Regel 7: 'b c ende neu' deckt zwei Anker mit UNTERSCHIEDLICHEN Ergebnissen.

        Anker 'b c' auf previous[1:3] schneidet previous ab -> 'a b c ende neu';
        Anker 'b c ende' auf previous[3:6] (previous-Ende) -> 'a b c b c ende neu'.
        Zwei unterschiedliche Kandidaten -> None.
        """
        assert stitch_word_overlap("a b c b c ende", "b c ende neu") is None

    def test_g_randfaelle(self):
        """Regel 8: previous='' -> window; window='' -> previous;
        previous='' mit nicht erfuelltem protected_prefix -> None."""
        assert stitch_word_overlap("", "hallo welt") == "hallo welt"
        assert stitch_word_overlap("hallo welt", "") == "hallo welt"
        assert stitch_word_overlap("", "hallo welt", protected_prefix="xyz") is None

    def test_h_praefixfragment_statt_suffix(self):
        """Regel 4: 'punkt' waere PRAEFIX-Fragment von 'Punkte' -- nur echte
        SUFFIX-Fragmente sind erlaubt -> None."""
        assert stitch_word_overlap("das sind rote Punkte hier", "punkt hier sind schon") is None



# =====================================================================
# PHASE 2: finalize_mode="cached_mid_tail"
# =====================================================================
def make_mid_tail(
    engine, *, limit_s=1.0, context_s=2.5, step_s=4.0, anchor_words=2, **kw
) -> StreamTranscriber:
    """Baut einen Transcriber im OPT-IN-Modus cached_mid_tail."""
    return StreamTranscriber(
        engine,
        interval_s=0.1,
        min_audio_s=0.1,
        live_asr_until_s=limit_s,
        finalize_mode="cached_mid_tail",
        mid_tail_context_s=context_s,
        mid_tail_step_s=step_s,
        mid_tail_anchor_words=anchor_words,
        **kw,
    )


class TestCachedMidTail:
    """PHASE 2: Hintergrund-Stitching nach dem Live-Limit (Opt-In)."""

    # --- 1) Throttling ---------------------------------------------------
    def test_throttle_kein_call_vor_step_s(self) -> None:
        """(2) <4s neues Audio seit Checkpoint -> KEIN Extra-Engine-Call."""
        engine = FakeEngine(["Hallo Welt", "Hallo Welt"])
        t = make_mid_tail(engine)
        t.feed(audio(0.5))
        assert t.maybe_transcribe() is not None  # Live-Snapshot
        t.feed(audio(1.0))  # 1.5s > Limit 1.0 -> Limit-Zweig
        assert t.maybe_transcribe() is None
        assert len(engine.calls) == 1, "unter step_s darf kein Mid-Tail-Call laufen"
        t.feed(audio(2.0))  # 3.5s: immer noch < 4.0s neu seit Checkpoint
        assert t.maybe_transcribe() is None
        assert len(engine.calls) == 1

    def test_throttle_genau_ein_call_und_slice_groesse(self) -> None:
        """(2) >=4s neues Audio -> genau EIN Call, Slice <= (4.0+2.5)s."""
        engine = FakeEngine(["Hallo Welt", "Hallo Welt und"])
        t = make_mid_tail(engine)
        t.feed(audio(0.5))
        assert t.maybe_transcribe() is not None  # Checkpoint bei 0.5s
        t.feed(audio(3.7))  # 4.2s Puffer: 3.7s neu -> noch kein Call
        assert t.maybe_transcribe() is None
        assert len(engine.calls) == 1
        t.feed(audio(0.6))  # 4.8s Puffer: 4.3s neu >= 4.0 -> Call
        assert t.maybe_transcribe() is None
        assert len(engine.calls) == 2
        nbytes = engine.calls[1][0]
        # Slice = Pufferende - (checkpoint.end - context), Start bei 0
        # geklemmt: 4.8s - max(0, 0.5-2.5)s = 4.8s.
        assert nbytes == int(4.8 * BYTES_PER_SECOND)
        assert nbytes <= int((4.0 + 2.5) * BYTES_PER_SECOND)

    def test_slice_startet_an_snapshot_ende_minus_context(self) -> None:
        """(4) Fall (i): erster Slice beginnt an snapshot_end - context."""
        engine = FakeEngine(["Hallo Welt", "Hallo Welt und mehr"])
        t = make_mid_tail(engine, limit_s=6.0, step_s=4.0, context_s=2.5)
        # Snapshot bei 5.5s (Live-Tick unter Limit 6.0), dann Limit erreicht.
        t.feed(audio(5.5))
        assert t.maybe_transcribe() is not None  # Snapshot end_nbytes=5.5s
        t.feed(audio(2.5))  # 8.0s > 6.0 Limit; 2.5s neu < 4.0 -> kein Call
        assert t.maybe_transcribe() is None
        assert len(engine.calls) == 1
        t.feed(audio(3.1))  # 11.1s: 5.6s neu >= 4.0 -> Call
        assert t.maybe_transcribe() is None
        assert len(engine.calls) == 2
        # Slice = 11.1s - (5.5s - 2.5s) = 8.1s, NICHT 11.1 (Vollpuffer) und
        # NICHT ~4.9 (ab einer Commit-Stelle).
        assert engine.calls[1][0] == int(8.1 * BYTES_PER_SECOND)

    # --- 2) Hintergrundlauf ohne Nebeneffekte ---------------------------
    def test_hintergrund_liefert_none_und_veraendert_nichts(self) -> None:
        """(3) Mid-Tail-Call: None, kein _update_commit, kein Cache-Update."""
        engine = FakeEngine(["Hallo Welt", "Hallo Welt und"])
        t = make_mid_tail(engine)
        t.feed(audio(0.5))
        assert t.maybe_transcribe() is not None
        committed_before = t._committed_text
        committed_len_before = t._committed_len
        history_before = list(t._history)
        last_text_before = t._last_text
        cache_bytes_before = t._last_completed_nbytes
        cache_text_before = t._last_completed_text
        t.feed(audio(5.0))  # 5.5s: 5.0s neu >= 4.0 -> Mid-Tail-Call
        result = t.maybe_transcribe()
        assert result is None, "Mid-Tail-Zweig muss None liefern"
        assert t._committed_text == committed_before
        assert t._committed_len == committed_len_before
        assert t._history == history_before
        assert t._last_text == last_text_before
        assert t._last_completed_nbytes == cache_bytes_before
        assert t._last_completed_text == cache_text_before
        # Checkpoint wurde aktualisiert (VOR dem Aufruf gesnapshottet: 5.5s).
        assert t._mid_tail_checkpoint is not None
        assert t._mid_tail_checkpoint.end_nbytes == int(5.5 * BYTES_PER_SECOND)
        assert t._mid_tail_checkpoint.text == "Hallo Welt und"

    # --- 3) Stitch-Verhalten ---------------------------------------------
    def test_normaler_stitch_checkpoint_waechst(self) -> None:
        """(5) Ueberlappender Text: Checkpoint waechst ohne Duplikat."""
        engine = FakeEngine(
            ["Hallo Welt", "Hallo Welt und mehr", "Hallo Welt und mehr noch was"]
        )
        t = make_mid_tail(engine, anchor_words=2)
        t.feed(audio(0.5))
        assert t.maybe_transcribe() is not None  # Checkpoint 'Hallo Welt'
        t.feed(audio(5.0))
        assert t.maybe_transcribe() is None
        assert t._mid_tail_checkpoint is not None
        assert t._mid_tail_checkpoint.text == "Hallo Welt und mehr"
        assert t._mid_tail_failed is False
        # Zweiter Hintergrundlauf: Fenster deckt das Checkpoint-Ende ab.
        t.feed(audio(4.5))
        assert t.maybe_transcribe() is None
        assert t._mid_tail_checkpoint.text == "Hallo Welt und mehr noch was"

    def test_regression_buffered_fragment(self) -> None:
        """(6) Fenster beginnt 'unkte ...' -> Stitch loest das Wortfragment
        ueber die Anker auf, ohne 'Punkte Punkte'-Duplikat."""
        engine = FakeEngine(
            [
                "das sind rote Punkte",
                "das sind rote Punkte hier sind schon",
                "unkte hier sind schon alles",
            ]
        )
        t = make_mid_tail(engine, anchor_words=2)
        t.feed(audio(0.5))
        assert t.maybe_transcribe() is not None  # Checkpoint bei 0.5s
        t.feed(audio(5.0))
        assert t.maybe_transcribe() is None  # 1. Hintergrundlauf
        assert t._mid_tail_checkpoint.text == "das sind rote Punkte hier sind schon"
        # 2. Hintergrundlauf: Fenster beginnt mitten in 'Punkte'.
        t.feed(audio(4.5))
        assert t.maybe_transcribe() is None
        assert t._mid_tail_checkpoint is not None
        assert t._mid_tail_checkpoint.text == (
            "das sind rote Punkte hier sind schon alles"
        )
        assert t._mid_tail_failed is False

    def test_stitch_none_setzt_failed(self) -> None:
        """(7) Stitch None -> failed, kein weiterer Versuch, Voll-Finalize."""
        engine = FakeEngine(["Hallo Welt", "voellig anders ohne Anker"])
        t = make_mid_tail(engine)
        t.feed(audio(0.5))
        assert t.maybe_transcribe() is not None
        t.feed(audio(5.0))
        assert t.maybe_transcribe() is None  # Stitch schlaegt fehl
        assert t._mid_tail_failed is True
        calls_after_fail = len(engine.calls)
        t.feed(audio(5.0))
        assert t.maybe_transcribe() is None
        assert len(engine.calls) == calls_after_fail, (
            "nach _mid_tail_failed darf kein weiterer Mid-Tail-Call laufen"
        )
        # Spaeteres Voll-Finalize: FakeEngine zaehlt den Vollpuffer-Call.
        result = t.finalize()
        assert result.text == "voellig anders ohne Anker"
        assert len(engine.calls) == calls_after_fail + 1
        # Der Fallback-Call sah den VOLLSTaendigen Puffer (10.5s).
        assert engine.calls[-1][0] == int(10.5 * BYTES_PER_SECOND)

    def test_engine_exception_kein_abort(self) -> None:
        """(8) Engine-Exception im Hintergrund -> failed, kein Abort,
        Finalize = genau EIN Vollpuffer-Call."""
        class FlakyEngine(FakeEngine):
            """Wirft GENAU EINMAL (auf dem Mid-Tail-Hintergrund-Call)."""

            def __init__(self) -> None:
                super().__init__(["Hallo Welt"])
                self.raised = False

            def transcribe(self, data, initial_prompt=""):
                if not self.raised and len(self.calls) >= 1:
                    self.raised = True
                    raise RuntimeError("boom")
                return super().transcribe(data, initial_prompt)

        engine = FlakyEngine()
        t = make_mid_tail(engine)
        t.feed(audio(0.5))
        assert t.maybe_transcribe() is not None
        t.feed(audio(5.0))
        assert t.maybe_transcribe() is None  # Engine-Exception im Hintergrund
        assert t._mid_tail_failed is True
        # Kein Abort: finalize() laeuft und macht GENAU EINEN Vollpuffer-Call.
        result = t.finalize()
        assert result.text == "Hallo Welt"
        assert len(engine.calls) == 2
        assert engine.calls[-1][0] == int(5.5 * BYTES_PER_SECOND)

    def test_ohne_live_snapshot_kein_mid_tail(self) -> None:
        """Kein erfolgreicher Live-Snapshot -> kein Mid-Tail-Versuch."""
        engine = FakeEngine(["Hallo Welt"])
        t = make_mid_tail(engine, limit_s=0.5, step_s=4.0)
        t.feed(audio(1.0))  # direkt ueber Limit: noch nie live transkribiert
        assert t.maybe_transcribe() is None
        assert len(engine.calls) == 0
        assert t._mid_tail_checkpoint is None
        t.feed(audio(5.0))
        assert t.maybe_transcribe() is None
        assert len(engine.calls) == 0, "kein Checkpoint -> kein Mid-Tail-Call"

    # --- 4) finalize ------------------------------------------------------
    def test_finalize_ohne_aenderung_kein_call(self) -> None:
        """(9) Puffer seit Checkpoint unveraendert -> KEIN Engine-Call,
        Text aus Checkpoint, committed_len = len(_committed_text)."""
        engine = FakeEngine(["Hallo Welt", "Hallo Welt und mehr"])
        t = make_mid_tail(engine)
        t.feed(audio(0.5))
        assert t.maybe_transcribe() is not None
        t.feed(audio(5.0))
        assert t.maybe_transcribe() is None  # Mid-Tail-Checkpoint gesetzt
        calls_before = len(engine.calls)
        result = t.finalize()
        assert len(engine.calls) == calls_before, (
            "Puffer unveraendert seit Checkpoint -> kein Engine-Call"
        )
        assert result.text == "Hallo Welt und mehr"
        assert result.committed_len == len(t._committed_text)

    def test_finalize_tail_stitch(self) -> None:
        """(10/Positiv) Kleiner Tail-Call, Stitch, committed_len gesetzt."""
        engine = FakeEngine(
            ["Hallo Welt", "Hallo Welt und mehr", "und mehr das Ende"]
        )
        t = make_mid_tail(engine)
        t.feed(audio(0.5))
        assert t.maybe_transcribe() is not None
        t.feed(audio(5.0))
        assert t.maybe_transcribe() is None  # Checkpoint bei 5.5s
        t.feed(audio(1.0))  # 6.5s: < 4.0s neu -> kein Hintergrundlauf
        calls_before = len(engine.calls)
        result = t.finalize()
        assert len(engine.calls) == calls_before + 1, "genau EIN Tail-Call"
        # Tail-Slice = 6.5s - (5.5 - 2.5)s = 3.5s
        assert engine.calls[-1][0] == int(3.5 * BYTES_PER_SECOND)
        assert result.text == "Hallo Welt und mehr das Ende"
        assert result.committed_len == len(t._committed_text)

    def test_finalize_tail_stitch_fehler_vollpuffer_fallback(self) -> None:
        """(10) Tail-Stitch schlaegt fehl -> EIN Vollpuffer-Call wie heute."""
        engine = FakeEngine(
            ["Hallo Welt", "Hallo Welt und mehr", "komplett anders"]
        )
        t = make_mid_tail(engine)
        t.feed(audio(0.5))
        assert t.maybe_transcribe() is not None
        t.feed(audio(5.0))
        assert t.maybe_transcribe() is None  # Checkpoint bei 5.5s
        t.feed(audio(1.0))
        result = t.finalize()
        assert result.text == "komplett anders"
        # Live + Mid-Tail + Tail-Call + GENAU EIN Vollpuffer-Fallback.
        assert len(engine.calls) == 4
        assert engine.calls[-1][0] == int(6.5 * BYTES_PER_SECOND), (
            "Fallback-Call sieht den VOLLSTaendigen Puffer"
        )
        assert result.committed_len == 0, "Vollpuffer-Fallback korrigiert voll"

    def test_finalize_ohne_checkpoint_vollpuffer(self) -> None:
        """Kein gueltiger Checkpoint -> Verhalten wie heute (Vollpuffer)."""
        engine = FakeEngine(["alles da"])
        t = make_mid_tail(engine, limit_s=0.5, step_s=4.0)
        t.feed(audio(1.0))  # direkt ueber Limit: noch nie live transkribiert
        assert t.maybe_transcribe() is None
        assert t._mid_tail_checkpoint is None
        t.feed(audio(5.0))
        result = t.finalize()
        assert result.text == "alles da"
        assert engine.calls[0][0] == int(6.0 * BYTES_PER_SECOND)
        assert result.committed_len == 0

    # --- 5) Bestandsschutz ------------------------------------------------
    def test_default_kein_mid_tail_pfad(self) -> None:
        """(11) Default finalize_mode='full': kein einziger Mid-Tail-Codepfad."""
        engine = FakeEngine(["Hallo Welt", "Hallo Welt und"])
        t = StreamTranscriber(
            engine, interval_s=0.1, min_audio_s=0.1, live_asr_until_s=1.0
        )
        t.feed(audio(0.5))
        assert t.maybe_transcribe() is not None
        t.feed(audio(10.0))
        assert t.maybe_transcribe() is None
        assert len(engine.calls) == 1, "keine Mid-Tail-Calls im Default-Modus"
        assert t._mid_tail_checkpoint is None
        assert t._mid_tail_failed is False
        result = t.finalize()
        assert result.text == "Hallo Welt und"
        assert len(engine.calls) == 2, "Finalize = genau EIN Vollpuffer-Call"
        assert engine.calls[-1][0] == int(10.5 * BYTES_PER_SECOND)

    def test_cached_kein_mid_tail_pfad(self) -> None:
        """(11b) finalize_mode='cached': Engine-Call-Zahlen wie Bestand."""
        engine = FakeEngine(["Hallo Welt", "Hallo Welt und"])
        t = StreamTranscriber(
            engine, interval_s=0.1, min_audio_s=0.1, live_asr_until_s=1.0,
            finalize_mode="cached",
        )
        t.feed(audio(0.5))
        assert t.maybe_transcribe() is not None
        t.feed(audio(10.0))
        assert t.maybe_transcribe() is None
        assert len(engine.calls) == 1
        assert t._mid_tail_checkpoint is None
        result = t.finalize()  # Puffer veraendert -> voller Call wie Bestand
        assert result.text == "Hallo Welt und"
        assert len(engine.calls) == 2

    def test_reset_macht_mid_tail_zustand_loser(self) -> None:
        """reset() leert Checkpoint und failed-Flag."""
        engine = FakeEngine(["Hallo Welt", "voellig anders ohne Anker"])
        t = make_mid_tail(engine)
        t.feed(audio(0.5))
        assert t.maybe_transcribe() is not None
        t.feed(audio(5.0))
        assert t.maybe_transcribe() is None
        assert t._mid_tail_failed is True
        t.reset()
        assert t._mid_tail_checkpoint is None
        assert t._mid_tail_failed is False

    def test_ungueltiger_snapshot_kein_checkpoint(self) -> None:
        """Snapshot-Text ohne Commit-Praefix darf KEIN Checkpoint werden."""
        engine = FakeEngine(["Hallo Welt", "Welt und mehr"])
        t = make_mid_tail(engine)
        t.feed(audio(0.5))
        assert t.maybe_transcribe() is not None
        # Commit-Zustand kuenstlich inkonsistent machen (simuliert
        # zurueckgesetzten Commit zwischen Snapshot und Mid-Tail-Tick).
        t._committed_text = "Xyz "
        t.feed(audio(5.0))
        assert t.maybe_transcribe() is None
        assert t._mid_tail_checkpoint is None, (
            "Snapshot-Text ohne Commit-Praefix darf KEIN Checkpoint werden"
        )
        assert len(engine.calls) == 1, "kein Mid-Tail-Call ohne Checkpoint"
        result = t.finalize()
        assert result.text == "Welt und mehr"
        assert len(engine.calls) == 2


# ---------------------------------------------------------------------------
# INT8-Hauptpfad (Betreiber-Freigabe 29.10.): ParakeetEngine transkribiert
# bei compute_type='int8' über den bewiesenen onnx_asr/ORT-CPU-Weg
# (A/B-Log: RTF 0,043-0,060, WER verbessert), NICHT über OpenVINO-NPU
# (Ground-Truth: OV-Encoder statisch [1,128,3000] unvertraeglich mit
# echten Lengen, NPU unter Last 'No available devices', RTF = CPU).
# ---------------------------------------------------------------------------


class _FakeOrtSessionOpts:
    def __init__(self):
        self.intra_op_num_threads = 0
        self.inter_op_num_threads = 0


class _FakeOnnxAsr:
    """Mock des onnx_asr-Moduls: zaehlt recognize-Aufrufe."""

    def __init__(self, text="Hallo INT8 Welt"):
        self.text = text
        self.recognize_calls = []
        self.load_kwargs = None

    def load_model(self, kind, path=None, *, quantization=None,
                   sess_options=None, providers=None):
        self.load_kwargs = dict(kind=kind, path=path, quantization=quantization,
                                providers=providers)
        return self  # das Modell ist das Recognizer-Objekt

    def recognize(self, audio):
        self.recognize_calls.append(audio)
        return self.text


def _fake_ort_module():
    import types

    m = types.ModuleType("onnxruntime")
    m.SessionOptions = _FakeOrtSessionOpts
    # echtes __spec__: torch-dynamo ruft find_spec('onnxruntime') auf
    import importlib.machinery as _mach

    m.__spec__ = _mach.ModuleSpec("onnxruntime", loader=None)
    return m


class _NoHFModels:
    """Soll einen Fehler werfen, wenn doch HF geladen wuerde."""

    @staticmethod
    def from_pretrained(*a, **k):
        raise AssertionError("HF-Modell durfte bei int8-Hauptpfad nicht laden")


class TestInt8Hauptpfad:
    def _patch(self, monkeypatch, fake, tmp_path):
        import sys, types
        monkeypatch.setitem(sys.modules, "onnx_asr", fake)
        monkeypatch.setitem(sys.modules, "onnxruntime", _fake_ort_module())
        # int8-ONNX-Dateien existieren lassen (Fake-Dateien)
        (tmp_path / "encoder-model.int8.onnx").write_bytes(b"x")
        (tmp_path / "decoder_joint-model.int8.onnx").write_bytes(b"x")
        from stt_button.asr import ParakeetEngine as _PE
        monkeypatch.setattr(
            _PE, "_int8_dir_for_test", lambda self: tmp_path, raising=False,
        )

    def test_int8_laed_onnx_statt_hf(self, monkeypatch, tmp_path) -> None:
        """compute_type='int8' -> onnx_asr-Pfad, HF wird NIE geladen."""
        from stt_button.asr import ParakeetEngine

        fake = _FakeOnnxAsr()
        self._patch(monkeypatch, fake, tmp_path)
        # HF-Import abfangen: jeder Versuch wirft
        import transformers
        monkeypatch.setattr(transformers, "AutoProcessor", _NoHFModels)
        monkeypatch.setattr(transformers, "AutoModelForTDT", _NoHFModels)

        e = ParakeetEngine(compute_type="int8")
        e.load()
        assert e.loaded is True
        assert fake.load_kwargs is not None, "onnx_asr.load_model nicht aufgerufen"
        assert fake.load_kwargs["quantization"] == "int8"
        assert fake.load_kwargs["providers"] == ["CPUExecutionProvider"]

    def test_int8_transcribe_nutzt_onnx_ohne_hf(self, monkeypatch, tmp_path) -> None:
        """transcribe() bei int8 ruft recognize() mit float32-Mono-Audio."""
        from stt_button.asr import ParakeetEngine
        import numpy as np

        fake = _FakeOnnxAsr(text="Erkannter INT8 Text")
        self._patch(monkeypatch, fake, tmp_path)
        import transformers
        monkeypatch.setattr(transformers, "AutoProcessor", _NoHFModels)
        monkeypatch.setattr(transformers, "AutoModelForTDT", _NoHFModels)

        e = ParakeetEngine(compute_type="int8")
        pcm = (np.zeros(16000 * 2, dtype=np.int16)).tobytes()  # 2s Stille
        result = e.transcribe(pcm)
        assert result.text == "Erkannter INT8 Welt"[:0] + "Erkannter INT8 Text"
        assert len(fake.recognize_calls) == 1
        arg = fake.recognize_calls[0]
        assert arg.dtype == np.float32 and arg.ndim == 1

    def test_int8_datei_fehlt_fallback_auf_hf(self, monkeypatch, tmp_path) -> None:
        """Fehlen die int8-ONNX-Dateien, laedt der alte HF-FP32-Pfad."""
        from stt_button.asr import ParakeetEngine
        import sys

        fake = _FakeOnnxAsr()
        monkeypatch.setitem(sys.modules, "onnx_asr", fake)
        monkeypatch.setitem(sys.modules, "onnxruntime", _fake_ort_module())
        # tmp_path bleibt leer -> ONNX-Dateien fehlen
        from stt_button.asr import ParakeetEngine as _PE
        monkeypatch.setattr(
            _PE, "_int8_dir_for_test", lambda self: tmp_path, raising=False,
        )

        calls = {"n": 0}

        class _P:
            def __call__(self, a, sampling_rate):
                class B(dict):
                    def to(self, d, dtype=None):
                        return self
                return B({})

            def decode(self, s, skip_special_tokens=True):
                return ["FP32 Fallback"]

        class _M:
            dtype = "float32"
            def to(self, d): return self
            def eval(self): return self
            def generate(self, **k):
                return type("O", (), {"sequences": [[1]]})()

        monkeypatch.setattr("transformers.AutoProcessor.from_pretrained", lambda m: _P())
        monkeypatch.setattr("transformers.AutoModelForTDT.from_pretrained",
                            lambda m, dtype=None: _M())

        e = ParakeetEngine(compute_type="int8")
        e.load()
        assert fake.load_kwargs is None, "trotz fehlender Dateien onnx geladen"
        pcm = (np.zeros(16000 * 2, dtype=np.int16)).tobytes() if False else b"\0\0" * 16000 * 2
        result = e.transcribe(pcm)
        assert result.text == "FP32 Fallback"

    def test_int8_timestamps_ohne_hf(self, monkeypatch, tmp_path) -> None:
        """transcribe_with_timestamps darf bei int8 NICHT HF nachladen;
        ohne Token-Dauern liefert es einen leeren Transcription-Wrapper
        um denselben Text (windowed Streaming ist hier nicht in Betrieb)."""
        from stt_button.asr import ParakeetEngine
        import numpy as np

        fake = _FakeOnnxAsr(text="Timestamp INT8 Text")
        self._patch(monkeypatch, fake, tmp_path)
        import transformers
        monkeypatch.setattr(transformers, "AutoProcessor", _NoHFModels)
        monkeypatch.setattr(transformers, "AutoModelForTDT", _NoHFModels)

        e = ParakeetEngine(compute_type="int8")
        pcm = (np.zeros(16000 * 2, dtype=np.int16)).tobytes()
        tr = e.transcribe_with_timestamps(pcm)
        assert fake.load_kwargs is not None
        assert len(fake.recognize_calls) == 1
        joined = "".join(t.text for tok_row in [getattr(tr, "tokens", [])] for t in tok_row)
        # Tokens duerfen fehlen (leer okay) — Text muss trotzdem stimmen:
        assert getattr(tr, "text", None) == "Timestamp INT8 Text" or joined == "Timestamp INT8 Text"
