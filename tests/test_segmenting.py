"""Phase 2 (C1): hard-cut segmentation at sentence pauses (StreamTranscriber)."""
from __future__ import annotations

import pytest

from stt_button.asr import InferenceCancelled, StreamTranscriber, TranscriptionResult

RATE = 16000
BPS = RATE * 2


def audio(seconds: float) -> bytes:
    return b"\x00\x00" * int(seconds * RATE)


class ScriptEngine:
    """Returns texts in order; can raise on a chosen call index."""

    def __init__(self, texts, raise_on=None) -> None:
        self.texts = list(texts)
        self.calls: list[int] = []
        self.raise_on = raise_on

    def transcribe(self, data: bytes, initial_prompt: str = "") -> TranscriptionResult:
        self.calls.append(len(data))
        if self.raise_on is not None and len(self.calls) - 1 == self.raise_on:
            raise InferenceCancelled("cancelled")
        idx = min(len(self.calls) - 1, len(self.texts) - 1)
        return TranscriptionResult(text=self.texts[idx])


class SwitchVad:
    """VAD fake: speech unless `silent` is True."""

    def __init__(self) -> None:
        self.silent = False

    def has_speech(self, arr) -> bool:
        return not self.silent


def make(texts, **kw):
    vad = SwitchVad()
    engine = ScriptEngine(texts, raise_on=kw.pop("raise_on", None))
    t = StreamTranscriber(engine, interval_s=1.0, min_audio_s=0.5, vad=vad,
                          segment_pause_s=1.2, segment_min_s=3.0, **kw)
    return t, engine, vad


def speak(t, seconds):
    t.feed(audio(seconds))
    return t.maybe_transcribe()


def test_default_off_never_cuts() -> None:
    engine = ScriptEngine(["Hallo Welt."])
    vad = SwitchVad()
    t = StreamTranscriber(engine, interval_s=1.0, min_audio_s=0.5, vad=vad)
    assert t.segment_pause_s == 0.0
    speak(t, 4.0)
    vad.silent = True
    speak(t, 1.5)
    assert t.closed_text == ""
    assert t.audio_seconds == pytest.approx(5.5)


def test_pause_after_punctuation_cuts_segment() -> None:
    t, engine, vad = make(["Erster Satz.", "Erster Satz.", "Zweiter"])
    speak(t, 4.0)                      # live: "Erster Satz."
    vad.silent = True
    res = speak(t, 1.5)                # pause -> mini-final + cut
    assert t.closed_text == "Erster Satz. "
    assert t.audio_seconds == 0.0
    assert res.text == "Erster Satz. "
    assert res.committed_len == len("Erster Satz. ")
    assert engine.calls[-1] == int(5.5 * BPS)  # mini-final saw the whole segment


def test_no_cut_without_punctuation() -> None:
    t, _, vad = make(["Erster Satz und", "Erster Satz und"])
    speak(t, 4.0)
    vad.silent = True
    speak(t, 1.5)
    assert t.closed_text == ""


def test_no_cut_while_speaking() -> None:
    t, _, _ = make(["Erster Satz.", "Erster Satz. Und"])
    speak(t, 4.0)
    speak(t, 1.5)                      # VAD reports speech
    assert t.closed_text == ""


def test_no_cut_below_segment_min() -> None:
    t, _, vad = make(["Ja."])
    speak(t, 1.0)
    vad.silent = True
    speak(t, 1.5)                      # 2.5s < segment_min_s=3.0
    assert t.closed_text == ""


def test_no_cut_when_mini_final_lacks_punctuation() -> None:
    t, _, vad = make(["Erster Satz.", "Erster Satz und dann"])
    speak(t, 4.0)
    vad.silent = True
    res = speak(t, 1.5)
    assert t.closed_text == ""
    assert res.text == "Erster Satz und dann"   # treated as a normal tick


def test_next_segment_composes_with_prefix() -> None:
    t, _, vad = make(["Eins.", "Eins.", "Zwei drei", "Zwei drei vier"])
    speak(t, 4.0)
    vad.silent = True
    speak(t, 1.5)
    vad.silent = False
    res = speak(t, 1.0)
    assert res.text == "Eins. Zwei drei"
    assert res.committed_len >= len("Eins. ")


def test_finalize_returns_prefix_plus_segment_and_commits_prefix() -> None:
    t, _, vad = make(["Eins.", "Eins.", "Zwei."])
    speak(t, 4.0)
    vad.silent = True
    speak(t, 1.5)
    vad.silent = False
    t.feed(audio(2.0))
    res = t.finalize()
    assert res.text == "Eins. Zwei."
    assert res.committed_len == len("Eins. ")


def test_finalize_with_empty_remainder_has_no_trailing_space() -> None:
    t, _, vad = make(["Eins.", "Eins.", ""])
    speak(t, 4.0)
    vad.silent = True
    speak(t, 1.5)
    res = t.finalize()
    assert res.text == "Eins."


def test_cancelled_mini_final_leaves_state_untouched() -> None:
    t, _, vad = make(["Eins.", "never"], raise_on=1)
    speak(t, 4.0)
    vad.silent = True
    with pytest.raises(InferenceCancelled):
        speak(t, 1.5)
    assert t.closed_text == ""
    assert t.audio_seconds == pytest.approx(5.5)


def test_reset_clears_closed_segments() -> None:
    t, _, vad = make(["Eins.", "Eins."])
    speak(t, 4.0)
    vad.silent = True
    speak(t, 1.5)
    t.reset()
    assert t.closed_text == ""
    assert t.full_audio() == b""


def test_full_audio_contains_all_segments() -> None:
    t, _, vad = make(["Eins.", "Eins.", "Zwei"])
    speak(t, 4.0)
    vad.silent = True
    speak(t, 1.5)
    t.feed(audio(1.0))
    assert len(t.full_audio()) == int(6.5 * BPS)


def test_vad_error_never_cuts() -> None:
    class BoomVad:
        def has_speech(self, arr):
            raise RuntimeError("vad down")

    engine = ScriptEngine(["Eins."])
    t = StreamTranscriber(engine, interval_s=1.0, min_audio_s=0.5, vad=BoomVad(),
                          segment_pause_s=1.2, segment_min_s=3.0)
    speak(t, 4.0)
    speak(t, 1.5)
    assert t.closed_text == ""
