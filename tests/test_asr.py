"""Tests for the streaming transcriber (TDD chain 3).

These tests cover only the logic; a fake engine replaces the Whisper model so
tests run quickly and without downloads.
"""
from __future__ import annotations

import pytest

from stt_button.asr import StreamTranscriber, TranscriptionResult

RATE = 16000
BYTES_PER_SECOND = RATE * 2  # s16le mono


def audio(seconds: float) -> bytes:
    """Generate ``seconds`` of audio in s16le mono."""
    return b"\x00\x00" * int(seconds * RATE)


class FakeEngine:
    """Return texts in a fixed order based on the call number."""

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
        # Immediately afterward: not enough new audio data yet
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
        # Commit only up to the word boundary "Hallo "; the incomplete word "Wel"
        # remains correctable.
        assert t.committed_len == len("Hallo ")

    def test_finalize_is_fully_correctable(self) -> None:
        engine = FakeEngine(["Hallo Welt", "Hallo Welt"])
        t = StreamTranscriber(engine, interval_s=0.1, min_audio_s=0.1)
        t.feed(audio(0.5))
        t.maybe_transcribe()
        t.feed(audio(0.5))
        t.maybe_transcribe()
        result = t.finalize()
        # The final transcription may correct everything.
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
        # The last word flickers -> only "Hallo " is committed
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
        # The model should actually be loaded only once.
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
        """Fix B: max_new_tokens must scale with audio length."""
        from stt_button.asr import _token_budget
        # 30s audio -> at least 360 tokens (12 tokens/s)
        assert _token_budget(30.0) >= 360
        # 60s -> at least 720
        assert _token_budget(60.0) >= 720
        # short audio retains the minimum budget
        assert _token_budget(1.0) >= 256

    def test_token_budget_capped(self) -> None:
        from stt_button.asr import _token_budget
        # cap against outliers
        assert _token_budget(10000.0) <= 4096


class TestLiveAsrUntilLimit:
    """STAGE 3: live_asr_until_s stops live ticks based on buffer length.

    Once the buffer contains ``live_asr_until_s`` seconds of audio,
    maybe_transcribe() returns no result (no engine calls) because whole-buffer
    processing becomes more expensive per tick. feed() keeps collecting,
    finalize() transcribes the COMPLETE buffer, and reset() reactivates live ticks.
    """

    def test_below_limit_ticks_normally(self) -> None:
        """(1) Normal live ticks run below the limit."""
        engine = FakeEngine(["Hallo Welt", "Hallo Welt und"])
        t = StreamTranscriber(
            engine, interval_s=0.1, min_audio_s=0.1, live_asr_until_s=10.0
        )
        t.feed(audio(0.5))
        result = t.maybe_transcribe()
        assert result is not None
        assert result.text == "Hallo Welt"

    def test_above_limit_no_engine_calls(self) -> None:
        """(2) Above the limit: maybe_transcribe() does NOT call the engine."""
        engine = FakeEngine(["Hallo Welt"])
        t = StreamTranscriber(
            engine, interval_s=0.1, min_audio_s=0.1, live_asr_until_s=1.0
        )
        t.feed(audio(0.5))
        assert t.maybe_transcribe() is not None  # below the limit
        t.feed(audio(1.0))  # now 1.5s > 1.0s limit
        assert t.maybe_transcribe() is None
        assert len(engine.calls) == 1, (
            "Engine wurde trotz Live-Limit erneut gerufen"
        )
        # Additional audio does not change this.
        t.feed(audio(1.0))
        assert t.maybe_transcribe() is None
        assert len(engine.calls) == 1

    def test_feed_keeps_collecting(self) -> None:
        """(3) feed() accepts all audio, even above the limit."""
        engine = FakeEngine(["Hallo Welt"])
        t = StreamTranscriber(
            engine, interval_s=0.1, min_audio_s=0.1, live_asr_until_s=1.0
        )
        t.feed(audio(0.5))
        t.maybe_transcribe()
        t.feed(audio(2.0))
        assert t.audio_seconds == pytest.approx(2.5)
        assert t.maybe_transcribe() is None  # live stopped

    def test_finalize_transcribes_full_buffer(self) -> None:
        """(4) finalize() transcribes the COMPLETE buffer (cache off).
        """
        engine = FakeEngine(["kurz", "alles da"])
        t = StreamTranscriber(
            engine, interval_s=0.1, min_audio_s=0.1, live_asr_until_s=1.0
        )
        t.feed(audio(0.5))
        t.maybe_transcribe()
        t.feed(audio(2.0))  # above limit -> no more live ticks
        result = t.finalize()
        assert result.text == "alles da"
        # engine saw the complete 2.5s buffer:
        assert engine.calls[-1][0] == int(2.5 * BYTES_PER_SECOND)

    def test_reset_reactivates_live_ticks(self) -> None:
        """(5) reset() resets the counter; live ticks run again."""
        engine = FakeEngine(["eins", "zwei", "drei"])
        t = StreamTranscriber(
            engine, interval_s=0.1, min_audio_s=0.1, live_asr_until_s=1.0
        )
        t.feed(audio(0.5))
        t.maybe_transcribe()
        t.feed(audio(1.0))
        assert t.maybe_transcribe() is None  # limit reached
        t.reset()
        t.feed(audio(0.5))
        result = t.maybe_transcribe()
        assert result is not None
        assert result.text == "zwei"  # FakeEngine: 2nd call -> texts[1]
        assert len(engine.calls) == 2

    def test_release_after_limit_finalize_starts_immediately(self) -> None:
        """(6) Release after limit with no active tick: finalize starts immediately.

        The finalizer acquires ``_exec_lock``; with no active live tick (live
        ticks are suspended above the limit), the lock is free and finalize()
        starts without waiting.
        """
        engine = FakeEngine(["Hallo Welt"])
        t = StreamTranscriber(
            engine, interval_s=0.1, min_audio_s=0.1, live_asr_until_s=1.0
        )
        t.feed(audio(0.5))
        t.maybe_transcribe()
        t.feed(audio(2.0))
        assert t.maybe_transcribe() is None  # no more ticks -> lock free
        result = t.finalize()
        assert result.text == "Hallo Welt"

    def test_zero_means_no_limit(self) -> None:
        """Default 0.0: live ticks always remain active (previous behavior)."""
        engine = FakeEngine(["eins", "zwei", "drei", "vier"])
        t = StreamTranscriber(
            engine, interval_s=0.1, min_audio_s=0.1, live_asr_until_s=0.0
        )
        for _ in range(4):
            t.feed(audio(0.5))
            assert t.maybe_transcribe() is not None


class TestLivePausedSignal:
    """STAGE 4: report reaching the limit exactly once (take_live_paused).

    Minimal documented signature: ``take_live_paused() -> bool`` returns
    ``True`` exactly once after ``maybe_transcribe()`` first reaches the live
    limit (only when ``live_asr_until_s > 0``), and consumes the notification.
    Further calls return ``False`` until ``reset()`` and another limit event.
    """

    def test_pending_true_exactly_once_after_limit(self) -> None:
        """(5a) Exactly ONE notification after the limit; none afterward."""
        engine = FakeEngine(["Hallo Welt"])
        t = StreamTranscriber(
            engine, interval_s=0.1, min_audio_s=0.1, live_asr_until_s=1.0
        )
        t.feed(audio(0.5))
        assert t.maybe_transcribe() is not None
        assert t.take_live_paused() is False  # limit not reached yet
        t.feed(audio(1.0))  # 1.5s buffer > 1.0s limit
        assert t.maybe_transcribe() is None
        assert t.take_live_paused() is True
        # Further ticks above the limit report NOTHING new:
        t.feed(audio(1.0))
        assert t.maybe_transcribe() is None
        assert t.take_live_paused() is False

    def test_below_limit_never_pending(self) -> None:
        """(5b) Below the limit: no notification; ticks run normally."""
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
        """(7/5c) Default live_asr_until_s=0.0: NEVER a limit notification."""
        engine = FakeEngine(["Hallo Welt", "Hallo Welt und", "Hallo Welt und mehr"])
        t = StreamTranscriber(
            engine, interval_s=0.1, min_audio_s=0.1, live_asr_until_s=0.0
        )
        for _ in range(3):
            t.feed(audio(0.5))
            assert t.maybe_transcribe() is not None
            assert t.take_live_paused() is False

    def test_reset_rearms_the_signal(self) -> None:
        """(5d) reset() resets the signal: the next limit notifies again."""
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
        assert t.take_live_paused() is True  # exactly one notification again

    def test_feed_over_limit_signals_without_maybe_transcribe(self) -> None:
        """BUG 1 (RED): feed() beyond the limit notifies even WITHOUT maybe_transcribe().

        Real test 11:56: the last live tick starts below the limit, and its
        2.8s inference crosses the threshold; release occurs before the next
        tick, so maybe_transcribe() never sees the limit. Therefore,
        take_live_paused() detects the limit ITSELF (regardless of feed time):
        buffer >= live_asr_until_s is sufficient for the notification.
        """
        engine = FakeEngine(["Hallo Welt"])
        t = StreamTranscriber(
            engine, interval_s=0.1, min_audio_s=0.1, live_asr_until_s=1.0
        )
        # NO maybe_transcribe() call—only feed beyond the limit:
        t.feed(audio(1.5))  # 1.5s buffer > 1.0s limit
        assert t.take_live_paused() is True, (
            "Limit-Erreichung ohne maybe_transcribe() nicht erkannt"
        )
        # Exactly once: consumed; further reads return False.
        assert t.take_live_paused() is False
        t.feed(audio(1.0))  # further growth: NO second notification
        assert t.take_live_paused() is False

    def test_feed_under_limit_never_signals_via_take(self) -> None:
        """BUG 1 (RED): never notify below the limit or at 0.0.

        Even when overfed WITHOUT maybe_transcribe(), the threshold remains
        binding: live_asr_until_s=0.0 is disabled.
        """
        engine = FakeEngine(["Hallo Welt"])
        t = StreamTranscriber(
            engine, interval_s=0.1, min_audio_s=0.1, live_asr_until_s=1.0
        )
        t.feed(audio(0.5))  # 0.5s < 1.0s limit
        assert t.take_live_paused() is False
        t.feed(audio(0.4))  # 0.9s, still below it
        assert t.take_live_paused() is False

        off = StreamTranscriber(
            FakeEngine(["x"]), interval_s=0.1, min_audio_s=0.1,
            live_asr_until_s=0.0,
        )
        off.feed(audio(10.0))  # limit disabled: NEVER notify
        assert off.take_live_paused() is False


class TestFinalizeCached:
    """STAGE 1: finalize_mode="cached" avoids the duplicate engine call.

    If the buffer is unchanged since the last SUCCESSFUL live tick,
    finalization requires no new result: text comes from the cache and the
    engine is not called again (identical input -> identical output because
    engine determinism parameters are fixed).
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
        # Full correction capability is preserved (as in the full path).
        assert result.committed_len == 0

    def test_cached_finalize_does_not_add_agreement(self) -> None:
        """The cache path must not grow the agreement history."""
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
        """A new feed() or reset() invalidates the cache -> full engine call."""
        engine = FakeEngine(["Hallo", "Hallo Welt"])
        t = StreamTranscriber(
            engine, interval_s=0.1, min_audio_s=0.1, finalize_mode="cached"
        )
        t.feed(audio(1.0))
        t.maybe_transcribe()
        # New audio since the last successful tick -> cache invalid.
        t.feed(audio(0.5))
        calls_before = len(engine.calls)
        result = t.finalize()
        assert len(engine.calls) == calls_before + 1
        assert result.text == "Hallo Welt"

        # After reset(), there is no valid cache.
        engine2 = FakeEngine(["Neustext"])
        t2 = StreamTranscriber(
            engine2, interval_s=0.1, min_audio_s=0.1, finalize_mode="cached"
        )
        t2.feed(audio(1.0))
        t2.maybe_transcribe()
        t2.reset()
        t2.feed(audio(1.0))
        assert t2.finalize() is not None
        assert len(engine2.calls) == 2  # 1 live + 1 final (no hit)

    def test_cached_finalize_falls_back_after_failed_tick(self) -> None:
        """An exception in a live tick does not set a completion marker."""
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
        # No successful tick -> finalize MUST call the engine.
        result = t.finalize()
        assert engine.calls == 2
        assert result.text == "spaeter Text"

    def test_default_finalize_mode_is_full(self) -> None:
        """Default remains "full": finalize retranscribes as before."""
        engine = FakeEngine(["Hallo Welt"])
        t = StreamTranscriber(engine, interval_s=0.1, min_audio_s=0.1)
        t.feed(audio(1.0))
        t.maybe_transcribe()
        calls_before = len(engine.calls)
        t.finalize()
        assert len(engine.calls) == calls_before + 1


# ---------------------------------------------------------------------------
# stitch_word_overlap -- Phase 1, isolated (NOT used in the production path)
# ---------------------------------------------------------------------------

from stt_button.asr import stitch_word_overlap  # noqa: E402


class TestStitchWordOverlap:
    """Tests for stitch_word_overlap: attach a window at exact word anchors."""

    def test_a_klassischer_anker(self):
        """Rule 2/5: exact anchor 'geht es dir' at the previous end, new 'gut heute'."""
        assert (
            stitch_word_overlap("Hallo wie geht es dir", "geht es dir gut heute")
            == "Hallo wie geht es dir gut heute"
        )

    def test_b_suffixfragment_kein_duplikat(self):
        """Regression: window starts with 'unkte' (SUFFIX fragment of 'Punkte').

        Independently derived expectation from rule 4/5: only the VERY FIRST
        window word may be a true SUFFIX fragment; previous retains the full
        word 'Punkte', and the fragment is dropped entirely. Anchors are the
        last two previous words, 'Punkte' (fragmented) + 'hier' (character
        identical), followed by new 'sind schon'. The result is therefore
        previous + 'sind schon'—NO 'Punkte Punkte' duplicate and no 'unkte'
        fragment remaining.
        """
        assert (
            stitch_word_overlap("das sind rote Punkte hier", "unkte hier sind schon")
            == "das sind rote Punkte hier sind schon"
        )

    def test_c1_ein_letztes_wort_ersetzt(self):
        """Rule 3: exactly ONE final incomplete previous word ('de') is
        replaced by the window word ('deutlich')."""
        assert stitch_word_overlap("das ist de", "deutlich besser") == "das ist deutlich besser"

    def test_c2_zwei_abweichende_alte_woerter(self):
        """Rule 3: 'gut' vs 'total' AND 'echt' vs 'daneben'—two differing
        old words -> None."""
        assert stitch_word_overlap("das war echt gut", "total daneben") is None

    def test_d1_protected_prefix_verwirft_kandidaten(self):
        """Rule 6: candidate 'wie geht es dir' does not start with 'Hallo wie'."""
        assert stitch_word_overlap("wie geht es", "geht es dir", protected_prefix="Hallo wie") is None
        # without a protected prefix, the same case returns the candidate:
        assert stitch_word_overlap("wie geht es", "geht es dir") == "wie geht es dir"

    def test_d2_protected_prefix_konsistent(self):
        """Rule 6 satisfied: result starts character-identically with 'Hallo wie'."""
        assert (
            stitch_word_overlap(
                "Hallo wie geht es", "wie geht es dir", protected_prefix="Hallo wie"
            )
            == "Hallo wie geht es dir"
        )

    def test_e_kein_fuzzy_ueber_komma(self):
        """Rule 1: 'Welt' != 'Welt,'—punctuation is exact; an appended comma
        is not word completion -> None."""
        assert stitch_word_overlap("Ich sage Welt", "Welt, wie") is None

    def test_f_mehrdeutigkeit_zwei_anker(self):
        """Rule 7: 'b c ende neu' covers two anchors with DIFFERENT results.

        Anchor 'b c' at previous[1:3] truncates previous -> 'a b c ende neu';
        anchor 'b c ende' at previous[3:6] (previous end) -> 'a b c b c ende neu'.
        Two different candidates -> None.
        """
        assert stitch_word_overlap("a b c b c ende", "b c ende neu") is None

    def test_g_randfaelle(self):
        """Rule 8: previous='' -> window; window='' -> previous;
        previous='' with unsatisfied protected_prefix -> None."""
        assert stitch_word_overlap("", "hallo welt") == "hallo welt"
        assert stitch_word_overlap("hallo welt", "") == "hallo welt"
        assert stitch_word_overlap("", "hallo welt", protected_prefix="xyz") is None

    def test_h_praefixfragment_statt_suffix(self):
        """Rule 4: 'punkt' would be a PREFIX fragment of 'Punkte'; only true
        SUFFIX fragments are allowed -> None."""
        assert stitch_word_overlap("das sind rote Punkte hier", "punkt hier sind schon") is None



# =====================================================================
# PHASE 2: finalize_mode="cached_mid_tail"
# =====================================================================
def make_mid_tail(
    engine, *, limit_s=1.0, context_s=2.5, step_s=4.0, anchor_words=2, **kw
) -> StreamTranscriber:
    """Build a transcriber in OPT-IN cached_mid_tail mode."""
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
    """PHASE 2: background stitching after the live limit (opt-in)."""

    # --- 1) Throttling ---------------------------------------------------
    def test_throttle_kein_call_vor_step_s(self) -> None:
        """(2) <4s new audio since checkpoint -> NO extra engine call."""
        engine = FakeEngine(["Hallo Welt", "Hallo Welt"])
        t = make_mid_tail(engine)
        t.feed(audio(0.5))
        assert t.maybe_transcribe() is not None  # live snapshot
        t.feed(audio(1.0))  # 1.5s > limit 1.0 -> limit branch
        assert t.maybe_transcribe() is None
        assert len(engine.calls) == 1, "unter step_s darf kein Mid-Tail-Call laufen"
        t.feed(audio(2.0))  # 3.5s: still < 4.0s new since checkpoint
        assert t.maybe_transcribe() is None
        assert len(engine.calls) == 1

    def test_throttle_genau_ein_call_und_slice_groesse(self) -> None:
        """(2) >=4s new audio -> exactly ONE call, slice <= (4.0+2.5)s."""
        engine = FakeEngine(["Hallo Welt", "Hallo Welt und"])
        t = make_mid_tail(engine)
        t.feed(audio(0.5))
        assert t.maybe_transcribe() is not None  # checkpoint at 0.5s
        t.feed(audio(3.7))  # 4.2s buffer: 3.7s new -> no call yet
        assert t.maybe_transcribe() is None
        assert len(engine.calls) == 1
        t.feed(audio(0.6))  # 4.8s buffer: 4.3s new >= 4.0 -> call
        assert t.maybe_transcribe() is None
        assert len(engine.calls) == 2
        nbytes = engine.calls[1][0]
        # Slice = buffer end - (checkpoint.end - context), start clamped to 0:
        # 4.8s - max(0, 0.5-2.5)s = 4.8s.
        assert nbytes == int(4.8 * BYTES_PER_SECOND)
        assert nbytes <= int((4.0 + 2.5) * BYTES_PER_SECOND)

    def test_slice_startet_an_snapshot_ende_minus_context(self) -> None:
        """(4) Case (i): first slice starts at snapshot_end - context."""
        engine = FakeEngine(["Hallo Welt", "Hallo Welt und mehr"])
        t = make_mid_tail(engine, limit_s=6.0, step_s=4.0, context_s=2.5)
        # Snapshot at 5.5s (live tick below limit 6.0), then limit reached.
        t.feed(audio(5.5))
        assert t.maybe_transcribe() is not None  # snapshot end_nbytes=5.5s
        t.feed(audio(2.5))  # 8.0s > 6.0 limit; 2.5s new < 4.0 -> no call
        assert t.maybe_transcribe() is None
        assert len(engine.calls) == 1
        t.feed(audio(3.1))  # 11.1s: 5.6s new >= 4.0 -> call
        assert t.maybe_transcribe() is None
        assert len(engine.calls) == 2
        # Slice = 11.1s - (5.5s - 2.5s) = 8.1s, NOT 11.1 (full buffer) and
        # NOT ~4.9 (from a commit point).
        assert engine.calls[1][0] == int(8.1 * BYTES_PER_SECOND)

    # --- 2) Background run without side effects -------------------------
    def test_hintergrund_liefert_none_und_veraendert_nichts(self) -> None:
        """(3) Mid-tail call: None, no _update_commit, no cache update."""
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
        t.feed(audio(5.0))  # 5.5s: 5.0s new >= 4.0 -> mid-tail call
        result = t.maybe_transcribe()
        assert result is None, "Mid-Tail-Zweig muss None liefern"
        assert t._committed_text == committed_before
        assert t._committed_len == committed_len_before
        assert t._history == history_before
        assert t._last_text == last_text_before
        assert t._last_completed_nbytes == cache_bytes_before
        assert t._last_completed_text == cache_text_before
        # Checkpoint was updated (snapshotted BEFORE the call: 5.5s).
        assert t._mid_tail_checkpoint is not None
        assert t._mid_tail_checkpoint.end_nbytes == int(5.5 * BYTES_PER_SECOND)
        assert t._mid_tail_checkpoint.text == "Hallo Welt und"

    # --- 3) Stitch behavior --------------------------------------------
    def test_normaler_stitch_checkpoint_waechst(self) -> None:
        """(5) Overlapping text: checkpoint grows without duplication."""
        engine = FakeEngine(
            ["Hallo Welt", "Hallo Welt und mehr", "Hallo Welt und mehr noch was"]
        )
        t = make_mid_tail(engine, anchor_words=2)
        t.feed(audio(0.5))
        assert t.maybe_transcribe() is not None  # checkpoint 'Hallo Welt'
        t.feed(audio(5.0))
        assert t.maybe_transcribe() is None
        assert t._mid_tail_checkpoint is not None
        assert t._mid_tail_checkpoint.text == "Hallo Welt und mehr"
        assert t._mid_tail_failed is False
        # Second background run: window covers the checkpoint end.
        t.feed(audio(4.5))
        assert t.maybe_transcribe() is None
        assert t._mid_tail_checkpoint.text == "Hallo Welt und mehr noch was"

    def test_regression_buffered_fragment(self) -> None:
        """(6) Window starts 'unkte ...' -> stitch resolves the word fragment
        via anchors without a 'Punkte Punkte' duplicate."""
        engine = FakeEngine(
            [
                "das sind rote Punkte",
                "das sind rote Punkte hier sind schon",
                "unkte hier sind schon alles",
            ]
        )
        t = make_mid_tail(engine, anchor_words=2)
        t.feed(audio(0.5))
        assert t.maybe_transcribe() is not None  # checkpoint at 0.5s
        t.feed(audio(5.0))
        assert t.maybe_transcribe() is None  # 1st background run
        assert t._mid_tail_checkpoint.text == "das sind rote Punkte hier sind schon"
        # 2nd background run: window starts in the middle of 'Punkte'.
        t.feed(audio(4.5))
        assert t.maybe_transcribe() is None
        assert t._mid_tail_checkpoint is not None
        assert t._mid_tail_checkpoint.text == (
            "das sind rote Punkte hier sind schon alles"
        )
        assert t._mid_tail_failed is False

    def test_stitch_none_setzt_failed(self) -> None:
        """(7) Stitch None -> failed, no further attempt, full finalize."""
        engine = FakeEngine(["Hallo Welt", "voellig anders ohne Anker"])
        t = make_mid_tail(engine)
        t.feed(audio(0.5))
        assert t.maybe_transcribe() is not None
        t.feed(audio(5.0))
        assert t.maybe_transcribe() is None  # stitch fails
        assert t._mid_tail_failed is True
        calls_after_fail = len(engine.calls)
        t.feed(audio(5.0))
        assert t.maybe_transcribe() is None
        assert len(engine.calls) == calls_after_fail, (
            "nach _mid_tail_failed darf kein weiterer Mid-Tail-Call laufen"
        )
        # Later full finalize: FakeEngine counts the full-buffer call.
        result = t.finalize()
        assert result.text == "voellig anders ohne Anker"
        assert len(engine.calls) == calls_after_fail + 1
        # The fallback call saw the COMPLETE buffer (10.5s).
        assert engine.calls[-1][0] == int(10.5 * BYTES_PER_SECOND)

    def test_engine_exception_kein_abort(self) -> None:
        """(8) Engine exception in background -> failed, no abort;
        finalize = exactly ONE full-buffer call."""
        class FlakyEngine(FakeEngine):
            """Raise EXACTLY ONCE (on the mid-tail background call)."""

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
        assert t.maybe_transcribe() is None  # engine exception in background
        assert t._mid_tail_failed is True
        # No abort: finalize() runs and makes EXACTLY ONE full-buffer call.
        result = t.finalize()
        assert result.text == "Hallo Welt"
        assert len(engine.calls) == 2
        assert engine.calls[-1][0] == int(5.5 * BYTES_PER_SECOND)

    def test_ohne_live_snapshot_kein_mid_tail(self) -> None:
        """No successful live snapshot -> no mid-tail attempt."""
        engine = FakeEngine(["Hallo Welt"])
        t = make_mid_tail(engine, limit_s=0.5, step_s=4.0)
        t.feed(audio(1.0))  # directly above limit: never transcribed live
        assert t.maybe_transcribe() is None
        assert len(engine.calls) == 0
        assert t._mid_tail_checkpoint is None
        t.feed(audio(5.0))
        assert t.maybe_transcribe() is None
        assert len(engine.calls) == 0, "kein Checkpoint -> kein Mid-Tail-Call"

    # --- 4) finalize ------------------------------------------------------
    def test_finalize_ohne_aenderung_kein_call(self) -> None:
        """(9) Buffer unchanged since checkpoint -> NO engine call;
        text from checkpoint, committed_len = len(_committed_text)."""
        engine = FakeEngine(["Hallo Welt", "Hallo Welt und mehr"])
        t = make_mid_tail(engine)
        t.feed(audio(0.5))
        assert t.maybe_transcribe() is not None
        t.feed(audio(5.0))
        assert t.maybe_transcribe() is None  # mid-tail checkpoint set
        calls_before = len(engine.calls)
        result = t.finalize()
        assert len(engine.calls) == calls_before, (
            "Puffer unveraendert seit Checkpoint -> kein Engine-Call"
        )
        assert result.text == "Hallo Welt und mehr"
        assert result.committed_len == len(t._committed_text)

    def test_finalize_tail_stitch(self) -> None:
        """(10/positive) Small tail call, stitch, committed_len set."""
        engine = FakeEngine(
            ["Hallo Welt", "Hallo Welt und mehr", "und mehr das Ende"]
        )
        t = make_mid_tail(engine)
        t.feed(audio(0.5))
        assert t.maybe_transcribe() is not None
        t.feed(audio(5.0))
        assert t.maybe_transcribe() is None  # checkpoint at 5.5s
        t.feed(audio(1.0))  # 6.5s: < 4.0s new -> no background run
        calls_before = len(engine.calls)
        result = t.finalize()
        assert len(engine.calls) == calls_before + 1, "genau EIN Tail-Call"
        # tail slice = 6.5s - (5.5 - 2.5)s = 3.5s
        assert engine.calls[-1][0] == int(3.5 * BYTES_PER_SECOND)
        assert result.text == "Hallo Welt und mehr das Ende"
        assert result.committed_len == len(t._committed_text)

    def test_finalize_tail_stitch_fehler_vollpuffer_fallback(self) -> None:
        """(10) Tail stitch fails -> ONE full-buffer call as before."""
        engine = FakeEngine(
            ["Hallo Welt", "Hallo Welt und mehr", "komplett anders"]
        )
        t = make_mid_tail(engine)
        t.feed(audio(0.5))
        assert t.maybe_transcribe() is not None
        t.feed(audio(5.0))
        assert t.maybe_transcribe() is None  # checkpoint at 5.5s
        t.feed(audio(1.0))
        result = t.finalize()
        assert result.text == "komplett anders"
        # Live + mid-tail + tail call + EXACTLY ONE full-buffer fallback.
        assert len(engine.calls) == 4
        assert engine.calls[-1][0] == int(6.5 * BYTES_PER_SECOND), (
            "Fallback-Call sieht den VOLLSTaendigen Puffer"
        )
        assert result.committed_len == 0, "Vollpuffer-Fallback korrigiert voll"

    def test_finalize_ohne_checkpoint_vollpuffer(self) -> None:
        """No valid checkpoint -> previous behavior (full buffer)."""
        engine = FakeEngine(["alles da"])
        t = make_mid_tail(engine, limit_s=0.5, step_s=4.0)
        t.feed(audio(1.0))  # directly above limit: never transcribed live
        assert t.maybe_transcribe() is None
        assert t._mid_tail_checkpoint is None
        t.feed(audio(5.0))
        result = t.finalize()
        assert result.text == "alles da"
        assert engine.calls[0][0] == int(6.0 * BYTES_PER_SECOND)
        assert result.committed_len == 0

    # --- 5) Backward compatibility ----------------------------------------
    def test_default_kein_mid_tail_pfad(self) -> None:
        """(11) Default finalize_mode='full': no mid-tail code path at all."""
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
        """(11b) finalize_mode='cached': engine call counts unchanged."""
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
        result = t.finalize()  # buffer changed -> full call as before
        assert result.text == "Hallo Welt und"
        assert len(engine.calls) == 2

    def test_reset_macht_mid_tail_zustand_loser(self) -> None:
        """reset() clears the checkpoint and failed flag."""
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
        """Snapshot text without a commit prefix must NOT become a checkpoint."""
        engine = FakeEngine(["Hallo Welt", "Welt und mehr"])
        t = make_mid_tail(engine)
        t.feed(audio(0.5))
        assert t.maybe_transcribe() is not None
        # Artificially make commit state inconsistent (simulates a commit reset
        # between the snapshot and mid-tail tick).
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
# INT8 main path (operator approval, October 29): ParakeetEngine transcribes
# with compute_type='int8' via the proven onnx_asr/ORT CPU path (A/B log:
# RTF 0.043-0.060, improved WER), NOT via OpenVINO NPU (ground truth: static
# OV encoder [1,128,3000] incompatible with real lengths; NPU under load:
# 'No available devices'; RTF = CPU).
# ---------------------------------------------------------------------------


class _FakeOrtSessionOpts:
    def __init__(self):
        self.intra_op_num_threads = 0
        self.inter_op_num_threads = 0


class _FakeOnnxAsr:
    """Mock of the onnx_asr module: counts recognize calls."""

    def __init__(self, text="Hallo INT8 Welt"):
        self.text = text
        self.recognize_calls = []
        self.load_kwargs = None

    def load_model(self, kind, path=None, *, quantization=None,
                   sess_options=None, providers=None):
        self.load_kwargs = dict(kind=kind, path=path, quantization=quantization,
                                providers=providers)
        return self  # the model is the recognizer object

    def recognize(self, audio):
        self.recognize_calls.append(audio)
        return self.text


def _fake_ort_module():
    import types

    m = types.ModuleType("onnxruntime")
    m.SessionOptions = _FakeOrtSessionOpts
    # real __spec__: torch-dynamo calls find_spec('onnxruntime')
    import importlib.machinery as _mach

    m.__spec__ = _mach.ModuleSpec("onnxruntime", loader=None)
    return m


class _NoHFModels:
    """Raise an error if HF is loaded despite expectations."""

    @staticmethod
    def from_pretrained(*a, **k):
        raise AssertionError("HF-Modell durfte bei int8-Hauptpfad nicht laden")


class TestInt8Hauptpfad:
    def _patch(self, monkeypatch, fake, tmp_path):
        import sys, types
        monkeypatch.setitem(sys.modules, "onnx_asr", fake)
        monkeypatch.setitem(sys.modules, "onnxruntime", _fake_ort_module())
        # make int8 ONNX files exist (fake files)
        (tmp_path / "encoder-model.int8.onnx").write_bytes(b"x")
        (tmp_path / "decoder_joint-model.int8.onnx").write_bytes(b"x")
        from stt_button.asr import ParakeetEngine as _PE
        monkeypatch.setattr(
            _PE, "_int8_dir_for_test", lambda self: tmp_path, raising=False,
        )

    def test_int8_laed_onnx_statt_hf(self, monkeypatch, tmp_path) -> None:
        """compute_type='int8' -> onnx_asr path; HF is NEVER loaded."""
        from stt_button.asr import ParakeetEngine

        fake = _FakeOnnxAsr()
        self._patch(monkeypatch, fake, tmp_path)
        # intercept HF import: every attempt raises
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
        """transcribe() with int8 calls recognize() with float32 mono audio."""
        from stt_button.asr import ParakeetEngine
        import numpy as np

        fake = _FakeOnnxAsr(text="Erkannter INT8 Text")
        self._patch(monkeypatch, fake, tmp_path)
        import transformers
        monkeypatch.setattr(transformers, "AutoProcessor", _NoHFModels)
        monkeypatch.setattr(transformers, "AutoModelForTDT", _NoHFModels)

        e = ParakeetEngine(compute_type="int8")
        pcm = (np.zeros(16000 * 2, dtype=np.int16)).tobytes()  # 2s silence
        result = e.transcribe(pcm)
        assert result.text == "Erkannter INT8 Welt"[:0] + "Erkannter INT8 Text"
        assert len(fake.recognize_calls) == 1
        arg = fake.recognize_calls[0]
        assert arg.dtype == np.float32 and arg.ndim == 1

    def test_int8_datei_fehlt_fallback_auf_hf(self, monkeypatch, tmp_path) -> None:
        """If int8 ONNX files are missing, the legacy HF FP32 path loads."""
        from stt_button.asr import ParakeetEngine
        import sys

        fake = _FakeOnnxAsr()
        monkeypatch.setitem(sys.modules, "onnx_asr", fake)
        monkeypatch.setitem(sys.modules, "onnxruntime", _fake_ort_module())
        # tmp_path remains empty -> ONNX files missing
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
        """transcribe_with_timestamps must NOT load HF with int8;
        without token durations, it returns an empty Transcription wrapper
        around the same text (windowed streaming is not active here)."""
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
        # Tokens may be missing (empty is okay); text must still match:
        assert getattr(tr, "text", None) == "Timestamp INT8 Text" or joined == "Timestamp INT8 Text"


# ---------------------------------------------------------------------------
# OPT2 (operator, October 29; Kimi requirements): allow mid-tail snapshots for
# pure punctuation/capitalization drift in the committed region.
# Ground truth 07:08: committed='TT, die ' (typed with comma), later live text
# 'TT die Sau...' (comma removed) -> character startswith fails -> checkpoint
# never created -> finalizer transcribed the full buffer.
# Safety constraints (Kimi b): fixed word positions, subsequent comparison ONLY
# after stripping punctuation, case EXACT, no fuzzy matching, remaining text
# word by word (never mid-word), result must carry committed_text as a
# character-exact prefix -> all downstream invariants remain intact.
# ---------------------------------------------------------------------------


class TestOpt2SnapshotPunctuationDrift:
    def _make(self, engine) -> StreamTranscriber:
        return make_mid_tail(engine)

    def _snapshot(self, t, completed: str, committed: str, nbytes: int):
        t._last_completed_ok = True
        t._last_completed_text = completed
        t._last_completed_nbytes = nbytes
        t._committed_text = committed
        t._committed_len = len(committed)
        return t._mid_tail_valid_snapshot()

    def test_pure_punctuation_drift_accepted(self) -> None:
        """'TT, die ' committed, later 'TT die Sau...' -> snapshot written back
        with committed prefix; remainder adopted word by word."""
        t = self._make(FakeEngine([]))
        snap = self._snapshot(
            t, "TT die Sau wuerde laufen.", "TT, die ", 80000
        )
        assert snap is not None
        assert snap.text == "TT, die Sau wuerde laufen."
        assert snap.text.startswith("TT, die "), "Downstream-startswith muss gelten"
        assert snap.end_nbytes == 80000

    def test_capitalization_drift_accepted(self) -> None:
        """Case drift alone is not punctuation interaction: here 'die'->'Die'
        in the prefix -> NO (characters exact except punctuation)."""
        t = self._make(FakeEngine([]))
        snap = self._snapshot(t, "TT Die Sau", "TT, die ", 80000)
        assert snap is None, "Buchstaben-Gross/klein darf nicht toleriert werden"

    def test_word_change_rejected(self) -> None:
        """A real word substitution in the prefix -> still None (no forced stitch)."""
        t = self._make(FakeEngine([]))
        snap = self._snapshot(t, "TT die Sau", "TT, hund ", 80000)
        assert snap is None

    def test_word_stem_change_rejected(self) -> None:
        """'Hunde' vs 'Hunden': inflection is NOT punctuation -> None."""
        t = self._make(FakeEngine([]))
        snap = self._snapshot(t, "TT, hunden Sau", "TT, hunde ", 80000)
        assert snap is None

    def test_short_completed_rejected(self) -> None:
        """Window older/shorter than the prefix (fewer words) -> None;
        never cut in the middle of a word."""
        t = self._make(FakeEngine([]))
        snap = self._snapshot(t, "TT, die", "TT, die sau ", 80000)
        assert snap is None

    def test_committed_without_trailing_space_rejected(self) -> None:
        """Commit in the middle of a word (no trailing space) -> None."""
        t = self._make(FakeEngine([]))
        snap = self._snapshot(t, "TT die Sau", "TT, di", 80000)
        assert snap is None

    def test_identical_text_behaves_as_before(self) -> None:
        """Character-exact prefix (the former special case): behavior unchanged."""
        t = self._make(FakeEngine([]))
        snap = self._snapshot(t, "TT, die Sau", "TT, die ", 12345)
        assert snap is not None and snap.text == "TT, die Sau"

    def test_repeated_anchor_words_position_fixed(self) -> None:
        """Repeated words (Kimi: ambiguity): matching is POSITION-BASED, not a
        search—'ja ja ' must match words 1:1."""
        t = self._make(FakeEngine([]))
        snap = self._snapshot(t, "Ja, ja Ja, ja mehr", "ja, ja ", 80000)
        assert snap is None, "Gross/klein im Anker ('Ja' vs 'ja') -> None"
        snap2 = self._snapshot(t, "ja, ja ja, ja mehr", "ja, ja ", 80000)
        assert snap2 is not None
        assert snap2.text == "ja, ja ja, ja mehr"


class TestAdaptiveInterval:
    """Phase 2 (B): interval = clamp(k * last tick, interval_s, interval_max_s)."""

    def _t(self, durations, **kw):
        engine = FakeEngine(["a b c"])
        t = StreamTranscriber(engine, interval_s=1.0, min_audio_s=0.5, **kw)
        dur = iter(durations)
        state = {"now": 0.0}

        def fake_monotonic():
            return state["now"]

        orig = engine.transcribe

        def timed(data, initial_prompt=""):
            state["now"] += next(dur)
            return orig(data, initial_prompt)

        engine.transcribe = timed
        t._clock = fake_monotonic
        return t, engine

    def test_default_k_zero_keeps_fixed_interval(self) -> None:
        t, engine = self._t([5.0] * 10)
        assert t.asr_interval_adaptive_k == 0.0
        t.feed(audio(1.0)); t.maybe_transcribe()
        assert t.effective_interval_s == t.interval_s
        t.feed(audio(1.0)); t.maybe_transcribe()
        assert t.effective_interval_s == t.interval_s
        assert len(engine.calls) == 2  # 1s interval despite a 5s tick

    def test_slow_tick_stretches_interval(self) -> None:
        t, engine = self._t([2.0] * 10, asr_interval_adaptive_k=2.0,
                            asr_interval_max_s=6.0)
        t.feed(audio(1.0)); t.maybe_transcribe()          # tick 1: 2s -> 4s
        assert t.effective_interval_s == pytest.approx(4.0)
        t.feed(audio(3.0)); assert t.maybe_transcribe() is None
        t.feed(audio(1.0)); assert t.maybe_transcribe() is not None
        assert len(engine.calls) == 2

    def test_clamped_to_max(self) -> None:
        t, _ = self._t([10.0], asr_interval_adaptive_k=2.0, asr_interval_max_s=6.0)
        t.feed(audio(1.0)); t.maybe_transcribe()
        assert t.effective_interval_s == pytest.approx(6.0)

    def test_clamped_to_min(self) -> None:
        t, _ = self._t([0.1], asr_interval_adaptive_k=2.0, asr_interval_max_s=6.0)
        t.feed(audio(1.0)); t.maybe_transcribe()
        assert t.effective_interval_s == pytest.approx(1.0)

    def test_reset_restores_base_interval(self) -> None:
        t, _ = self._t([3.0], asr_interval_adaptive_k=2.0)
        t.feed(audio(1.0)); t.maybe_transcribe()
        t.reset()
        assert t.effective_interval_s == pytest.approx(1.0)

    def test_failed_engine_call_keeps_last_tick(self) -> None:
        t, engine = self._t([2.0, 9.0], asr_interval_adaptive_k=2.0,
                            asr_interval_max_s=6.0)
        t.feed(audio(1.0)); t.maybe_transcribe()
        assert t.effective_interval_s == pytest.approx(4.0)

        def boom(data, initial_prompt=""):
            raise RuntimeError("engine down")

        engine.transcribe = boom
        t.feed(audio(4.0))
        with pytest.raises(RuntimeError):
            t.maybe_transcribe()
        assert t.effective_interval_s == pytest.approx(4.0)
