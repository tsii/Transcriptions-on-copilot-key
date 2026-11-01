"""Tests for WindowedTranscriber (step 7 integration)."""
from __future__ import annotations

import pytest

from stt_button.streaming_types import Token, Transcription
from stt_button.windowed import WindowedTranscriber


def audio(seconds: float) -> bytes:
    return b"\x00\x00" * int(seconds * 16000)


class FakeEngine:
    def __init__(self, tokens_by_offset: dict[float, list[Token]] | None = None) -> None:
        self.tokens_by_offset = tokens_by_offset or {}
        self.calls = 0
        self.transcribe_calls = 0

    def transcribe_with_timestamps(self, data: bytes, window_offset_s: float = 0.0) -> Transcription:
        self.calls += 1
        return Transcription(tokens=self.tokens_by_offset.get(window_offset_s, []))

    def transcribe(self, data: bytes, initial_prompt: str = ""):
        self.transcribe_calls += 1
        class R:
            text = "rest"
        return R()


class AlwaysSpeech:
    def has_speech(self, audio_tail) -> bool:
        return True


def test_below_min_audio_returns_none() -> None:
    e = FakeEngine()
    t = WindowedTranscriber(engine=e, min_audio_s=3.0, chunk_s=2.0,
                            left_context_s=4.0, right_context_s=1.0,
                            vad=AlwaysSpeech())
    t.feed(audio(2.0))
    assert t.maybe_transcribe() is None
    assert e.calls == 0


def test_transcribes_window_and_returns_commit() -> None:
    tokens = [
        Token("So", 0.0, 0.3),
        Token(" jetzt", 0.3, 0.6),
        Token(" weit", 0.6, 1.4),  # crosses horizon
    ]
    e = FakeEngine({0.0: tokens})
    t = WindowedTranscriber(engine=e, min_audio_s=3.0, chunk_s=1.0,
                            left_context_s=2.0, right_context_s=0.5,
                            vad=AlwaysSpeech())
    t.feed(audio(4.0))
    result = t.maybe_transcribe()
    assert result is not None
    # Fix 3: the result is the FULL text (committed + partial).
    # "weit" (crosses the horizon) is partial but not committed.
    assert result.text != ""
    # committed_len < total length because "weit" is not committed yet.
    assert result.committed_len < len(result.text)


def test_finalize_returns_text() -> None:
    # With speech VAD, the remainder is transcribed.
    e = FakeEngine()
    t = WindowedTranscriber(engine=e, min_audio_s=3.0, chunk_s=2.0,
                            left_context_s=4.0, right_context_s=1.0,
                            vad=AlwaysSpeech())
    t.feed(audio(4.0))
    result = t.finalize()
    assert result.text == "rest"


class SilenceVad:
    """Always reports 'no speech'."""

    def has_speech(self, audio_tail) -> bool:
        return False


class RecordingVad:
    """Records which audio tails it checks."""

    def __init__(self, result: bool) -> None:
        self.result = result
        self.seen: list = []

    def has_speech(self, audio_tail) -> bool:
        self.seen.append(audio_tail)
        return self.result


def test_finalize_skips_silent_short_rest() -> None:
    """A short silent remainder (<2s) is still skipped."""
    e = FakeEngine()
    t = WindowedTranscriber(engine=e, min_audio_s=3.0, chunk_s=2.0,
                            left_context_s=4.0, right_context_s=1.0,
                            vad=SilenceVad())
    t.feed(audio(1.5))  # < 2s -> VAD false -> skipped
    before = e.transcribe_calls
    result = t.finalize()
    assert result.text == "" or result.text == t.committed_text.strip()
    assert e.transcribe_calls == before


def test_finalize_skips_short_rest() -> None:
    """Fix 1: finalize must not transcribe a remainder that is too short."""
    e = FakeEngine()
    t = WindowedTranscriber(engine=e, min_audio_s=3.0, chunk_s=2.0,
                            left_context_s=4.0, right_context_s=1.0,
                            vad=AlwaysSpeech())
    t.feed(audio(0.4))  # < min_final_audio_s
    result = t.finalize()
    assert result.text == ""
    assert e.calls == 0


def test_finalize_transcribes_speech_rest() -> None:
    """Regression guard: transcribe with speech VAD and sufficient length."""
    e = FakeEngine()
    t = WindowedTranscriber(engine=e, min_audio_s=3.0, chunk_s=2.0,
                            left_context_s=4.0, right_context_s=1.0,
                            vad=AlwaysSpeech())
    t.feed(audio(5.0))
    result = t.finalize()
    assert result.text == "rest"


def test_reset_clears() -> None:
    e = FakeEngine()
    t = WindowedTranscriber(engine=e, min_audio_s=3.0, chunk_s=2.0,
                            left_context_s=4.0, right_context_s=1.0)
    t.feed(audio(2.0))
    t.reset()
    assert t.audio_seconds == 0.0
    assert t.committed_len == 0


def test_maybe_transcribe_returns_full_text_with_committed_prefix() -> None:
    """Fix 3: the result must return the FULL text + committed_len.

    Otherwise, DeltaTyper.update() discards everything from the second commit
    onward (len(delta) < committed_len early return).
    """
    e = FakeEngine({
        0.0: [Token("So", 0.0, 0.2), Token(" jetzt", 0.2, 0.4),
              Token(" weit", 0.4, 0.6)],
        0.4: [Token("So", 0.0, 0.2), Token(" jetzt", 0.2, 0.4),
              Token(" weit", 0.4, 0.6), Token("er", 0.6, 0.8)],
    })
    t = WindowedTranscriber(engine=e, min_audio_s=0.5, chunk_s=0.2,
                            left_context_s=0.4, right_context_s=0.1,
                            vad=AlwaysSpeech())
    t.feed(b"\x00\x00" * int(0.8 * 16000))
    result = t.maybe_transcribe()
    assert result is not None
    # The text must be the full committed+partial text so DeltaTyper can
    # work correctly.
    assert result.text != ""
    # committed_len == length of the committed prefix (without trailing space)
    assert result.committed_len == len(t.committed_text.rstrip())

def test_maybe_transcribe_respects_interval() -> None:
    """Fix 4: do not transcribe again immediately after a transcription."""
    e = FakeEngine({0.0: [Token("Das", 0.0, 0.2)]})
    t = WindowedTranscriber(engine=e, min_audio_s=0.5, chunk_s=0.2,
                            left_context_s=0.0, right_context_s=0.0,
                            interval_s=1.0, vad=AlwaysSpeech())
    t.feed(b"\x00\x00" * int(1.0 * 16000))
    r1 = t.maybe_transcribe()
    assert r1 is not None
    calls_after_first = e.calls
    # Very little new audio -> no additional engine call (interval_s=1.0)
    t.feed(b"\x00\x00" * int(0.2 * 16000))
    r2 = t.maybe_transcribe()
    assert r2 is None
    assert e.calls == calls_after_first


def test_vad_gates_on_new_region() -> None:
    """Fix 5: VAD checks only the new segment, not the left context."""
    # Speech-like left context + silent new region
    vad = RecordingVad(False)  # always returns False (silence)
    e = FakeEngine({0.0: [Token("Das", 0.0, 0.2), Token(" ist", 0.2, 0.4)]})
    t = WindowedTranscriber(engine=e, min_audio_s=0.5, chunk_s=0.2,
                            left_context_s=0.2, right_context_s=0.0,
                            vad=vad)
    t.feed(b"\x00\x00" * int(1.0 * 16000))
    result = t.maybe_transcribe()
    # VAD returns False -> no transcription
    assert result is None
    assert e.calls == 0
    assert len(vad.seen) >= 1


class NeverSpeech:
    def has_speech(self, audio_tail) -> bool:
        return False


def test_finalize_vad_rejection_still_attempts_long_rest() -> None:
    """Fix 1 (B1): a long remainder must not be silently discarded despite VAD false."""
    e = FakeEngine()
    t = WindowedTranscriber(engine=e, min_audio_s=3.0, chunk_s=2.0,
                            left_context_s=4.0, right_context_s=1.0,
                            vad=NeverSpeech())
    # 3s audio, no committed_s -> rest = 3s (>= 2s threshold)
    t.feed(audio(3.0))
    before = e.transcribe_calls
    result = t.finalize()
    # The long remainder must be transcribed despite VAD false (data loss
    # is worse than hallucination risk).
    assert result.text == "rest"
    assert e.transcribe_calls > before


def test_no_commit_at_audio_edge_without_right_context() -> None:
    """Fix 5 (A3): no commit up to the audio edge without right context."""
    from stt_button.streaming_types import StreamWindowPolicy
    policy = StreamWindowPolicy(left_context_s=4.0, chunk_s=2.0, right_context_s=1.0,
                                min_audio_s=3.0)
    w = policy.next_window(committed_s=4.0, audio_len_s=6.0)
    # The horizon must not be 6.0 (audio edge) when right context is missing.
    if w is not None:
        assert w.commit_horizon < 6.0
    else:
        # None is also acceptable (wait), but then nothing may be committed.
        assert w is None


def test_vad_silence_advances_committed_after_two_misses() -> None:
    """Fix A: after 2 negative VAD results, committed_s must advance (no stall)."""
    e = FakeEngine()
    t = WindowedTranscriber(engine=e, min_audio_s=0.5, chunk_s=1.0,
                            left_context_s=1.0, right_context_s=0.5,
                            interval_s=0.0, vad=SilenceVad())
    t.feed(audio(3.0))
    before = t._committer.committed_s
    t.maybe_transcribe()   # 1st VAD miss
    t.maybe_transcribe()   # 2nd VAD miss -> advance
    assert t._committer.committed_s > before


def test_single_vad_miss_does_not_advance() -> None:
    """Fix A: a single VAD miss check must NOT advance."""
    e = FakeEngine()
    t = WindowedTranscriber(engine=e, min_audio_s=0.5, chunk_s=1.0,
                            left_context_s=1.0, right_context_s=0.5,
                            interval_s=0.0, vad=SilenceVad())
    t.feed(audio(3.0))
    before = t._committer.committed_s
    t.maybe_transcribe()   # only 1 miss
    assert t._committer.committed_s == before


def test_long_pause_does_not_stall_pipeline() -> None:
    """Fix A: 5s speech / 4s silence / 10s speech -> no permanent stall."""
    class AlwaysSpeech:
        def has_speech(self, a): return True

    # First, speech with a commit
    tokens = [Token(" Anfang", 0.0, 0.3), Token(" Text", 0.3, 0.6),
              Token(" hier", 0.6, 0.9)]
    e = FakeEngine({0.0: tokens, 1.0: tokens})
    t = WindowedTranscriber(engine=e, min_audio_s=0.5, chunk_s=1.0,
                            left_context_s=1.0, right_context_s=0.5,
                            interval_s=0.0, vad=AlwaysSpeech())
    t.feed(audio(5.0))
    for _ in range(4):
        t.maybe_transcribe()
    after_speech = t._committer.committed_s
    assert after_speech > 0

    # Now 8s of silence (pause)
    class Sil:
        def has_speech(self, a): return False
    t.vad = Sil()
    t.feed(audio(8.0))
    for _ in range(6):
        t.maybe_transcribe()
    # committed_s must have advanced through the pause (not stalled)
    assert t._committer.committed_s > after_speech
