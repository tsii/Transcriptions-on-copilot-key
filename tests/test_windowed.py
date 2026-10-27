"""Tests für den WindowedTranscriber (Schritt 7-Integration)."""
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
        Token(" weit", 0.6, 1.4),  # schneidet horizon
    ]
    e = FakeEngine({0.0: tokens})
    t = WindowedTranscriber(engine=e, min_audio_s=3.0, chunk_s=1.0,
                            left_context_s=2.0, right_context_s=0.5,
                            vad=AlwaysSpeech())
    t.feed(audio(4.0))
    result = t.maybe_transcribe()
    assert result is not None
    # Fix 3: Ergebnis ist der VOLLE Text (committed + partial).
    # "weit" (schneidet Horizont) ist im Partial, aber nicht committet.
    assert result.text != ""
    # committed_len < Gesamtlaenge, weil "weit" noch nicht committet ist.
    assert result.committed_len < len(result.text)


def test_finalize_returns_text() -> None:
    # Mit Sprach-VAD wird der Rest transkribiert.
    e = FakeEngine()
    t = WindowedTranscriber(engine=e, min_audio_s=3.0, chunk_s=2.0,
                            left_context_s=4.0, right_context_s=1.0,
                            vad=AlwaysSpeech())
    t.feed(audio(4.0))
    result = t.finalize()
    assert result.text == "rest"


class SilenceVad:
    """Meldet immer 'keine Sprache'."""

    def has_speech(self, audio_tail) -> bool:
        return False


class RecordingVad:
    """Zeichnet auf, welche Audio-Tails es prueft."""

    def __init__(self, result: bool) -> None:
        self.result = result
        self.seen: list = []

    def has_speech(self, audio_tail) -> bool:
        self.seen.append(audio_tail)
        return self.result


def test_finalize_skips_silent_short_rest() -> None:
    """Kurzer stummer Rest (<2s) wird weiterhin uebersprungen."""
    e = FakeEngine()
    t = WindowedTranscriber(engine=e, min_audio_s=3.0, chunk_s=2.0,
                            left_context_s=4.0, right_context_s=1.0,
                            vad=SilenceVad())
    t.feed(audio(1.5))  # < 2s -> VAD-False -> uebersprungen
    before = e.transcribe_calls
    result = t.finalize()
    assert result.text == "" or result.text == t.committed_text.strip()
    assert e.transcribe_calls == before


def test_finalize_skips_short_rest() -> None:
    """Fix 1: finalize darf zu kurzen Rest nicht transkribieren."""
    e = FakeEngine()
    t = WindowedTranscriber(engine=e, min_audio_s=3.0, chunk_s=2.0,
                            left_context_s=4.0, right_context_s=1.0,
                            vad=AlwaysSpeech())
    t.feed(audio(0.4))  # < min_final_audio_s
    result = t.finalize()
    assert result.text == ""
    assert e.calls == 0


def test_finalize_transcribes_speech_rest() -> None:
    """Regressionsschutz: mit Sprach-VAD und genug Laenge wird transkribiert."""
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
    """Fix 3: Ergebnis muss den VOLLEN Text + committed_len liefern.

    Andernfalls verwirft DeltaTyper.update() ab dem 2. Commit alles
    (len(delta) < committed_len Early-Return).
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
    # Der Text muss der volle committed+partial Text sein, damit der
    # DeltaTyper korrekt arbeiten kann.
    assert result.text != ""
    # committed_len == Laenge des committeten Praefixes (ohne trailing space)
    assert result.committed_len == len(t.committed_text.rstrip())

def test_maybe_transcribe_respects_interval() -> None:
    """Fix 4: Nach einer Transkription wird nicht sofort wieder transkribiert."""
    e = FakeEngine({0.0: [Token("Das", 0.0, 0.2)]})
    t = WindowedTranscriber(engine=e, min_audio_s=0.5, chunk_s=0.2,
                            left_context_s=0.0, right_context_s=0.0,
                            interval_s=1.0, vad=AlwaysSpeech())
    t.feed(b"\x00\x00" * int(1.0 * 16000))
    r1 = t.maybe_transcribe()
    assert r1 is not None
    calls_after_first = e.calls
    # Kaum neues Audio -> kein weiterer Engine-Call (interval_s=1.0)
    t.feed(b"\x00\x00" * int(0.2 * 16000))
    r2 = t.maybe_transcribe()
    assert r2 is None
    assert e.calls == calls_after_first


def test_vad_gates_on_new_region() -> None:
    """Fix 5: VAD prüft nur das neue Segment, nicht den Links-Kontext."""
    # Spraechoider Links-Kontext + stille neue Region
    vad = RecordingVad(False)  # liefert immer False (Stille)
    e = FakeEngine({0.0: [Token("Das", 0.0, 0.2), Token(" ist", 0.2, 0.4)]})
    t = WindowedTranscriber(engine=e, min_audio_s=0.5, chunk_s=0.2,
                            left_context_s=0.2, right_context_s=0.0,
                            vad=vad)
    t.feed(b"\x00\x00" * int(1.0 * 16000))
    result = t.maybe_transcribe()
    # VAD liefert False -> keine Transkription
    assert result is None
    assert e.calls == 0
    assert len(vad.seen) >= 1


class NeverSpeech:
    def has_speech(self, audio_tail) -> bool:
        return False


def test_finalize_vad_rejection_still_attempts_long_rest() -> None:
    """Fix 1 (B1): langer Rest darf trotz VAD-False nicht still verworfen werden."""
    e = FakeEngine()
    t = WindowedTranscriber(engine=e, min_audio_s=3.0, chunk_s=2.0,
                            left_context_s=4.0, right_context_s=1.0,
                            vad=NeverSpeech())
    # 3s Audio, kein committed_s -> rest = 3s (>= 2s Schwelle)
    t.feed(audio(3.0))
    before = e.transcribe_calls
    result = t.finalize()
    # Der lange Rest muss trotz VAD-False transkribiert werden (Datenverlust
    # ist schlimmer als Halluzinationsrisiko).
    assert result.text == "rest"
    assert e.transcribe_calls > before


def test_no_commit_at_audio_edge_without_right_context() -> None:
    """Fix 5 (A3): kein Commit bis an die Audio-Kante ohne Rechts-Kontext."""
    from stt_button.streaming_types import StreamWindowPolicy
    policy = StreamWindowPolicy(left_context_s=4.0, chunk_s=2.0, right_context_s=1.0,
                                min_audio_s=3.0)
    w = policy.next_window(committed_s=4.0, audio_len_s=6.0)
    # Horizont darf nicht 6.0 (Audio-Kante) sein, wenn Rechts-Kontext fehlt.
    if w is not None:
        assert w.commit_horizon < 6.0
    else:
        # None ist auch akzeptabel (warten), aber dann darf nichts committen.
        assert w is None


def test_vad_silence_advances_committed_after_two_misses() -> None:
    """Fix A: Nach 2x VAD-negativ muss committed_s vorruecken (kein Stall)."""
    e = FakeEngine()
    t = WindowedTranscriber(engine=e, min_audio_s=0.5, chunk_s=1.0,
                            left_context_s=1.0, right_context_s=0.5,
                            interval_s=0.0, vad=SilenceVad())
    t.feed(audio(3.0))
    before = t._committer.committed_s
    t.maybe_transcribe()   # 1. VAD-Miss
    t.maybe_transcribe()   # 2. VAD-Miss -> vorruecken
    assert t._committer.committed_s > before


def test_single_vad_miss_does_not_advance() -> None:
    """Fix A: Eine einzelne VAD-Miss-Prüfung darf NICHT vorrücken."""
    e = FakeEngine()
    t = WindowedTranscriber(engine=e, min_audio_s=0.5, chunk_s=1.0,
                            left_context_s=1.0, right_context_s=0.5,
                            interval_s=0.0, vad=SilenceVad())
    t.feed(audio(3.0))
    before = t._committer.committed_s
    t.maybe_transcribe()   # nur 1 Miss
    assert t._committer.committed_s == before


def test_long_pause_does_not_stall_pipeline() -> None:
    """Fix A: 5s Sprache / 4s Stille / 10s Sprache -> kein ewiger Stall."""
    class AlwaysSpeech:
        def has_speech(self, a): return True

    # Erst Sprache mit Commit
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

    # Jetzt 8s Stille (Pause)
    class Sil:
        def has_speech(self, a): return False
    t.vad = Sil()
    t.feed(audio(8.0))
    for _ in range(6):
        t.maybe_transcribe()
    # committed_s muss durch die Pause vorgerueckt sein (nicht steckengeblieben)
    assert t._committer.committed_s > after_speech
