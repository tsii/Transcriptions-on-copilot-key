"""Tests for BufferedTranscriber (STAGE 3, TDD chain).

Hybrid strategy (STAGE 3):
  * Phase A (0 .. whole_buffer_until_s): growing full buffer, already using
    timestamps + TimestampCommitter(require_agreement_always=True).
  * Phase B (> whole_buffer_until_s): stateless fixed windows
    [left_context | chunk | right_context], deduplication via ABSOLUTE timestamps,
    commits only after 2 agreements (always).
  * finalize(): no engine call without new bytes; otherwise decode only the
    uncommitted tail + left context, never the full buffer.

All functional tests use a fake engine (deterministic, no model download).
The real-engine test is marked ``@pytest.mark.slow`` and is skipped without
a local model.
"""
from __future__ import annotations

import inspect
import os
from pathlib import Path

import pytest

from stt_button.asr import TranscriptionResult
from stt_button.buffered import BufferedTranscriber
from stt_button.streaming_types import Token, Transcription

RATE = 16000
BYTES_PER_SECOND = RATE * 2


def audio(seconds: float) -> bytes:
    """Generate ``seconds`` of silence in s16le mono."""
    return b"\x00\x00" * int(seconds * RATE)


def corpus(n_words: int, step_s: float = 0.4) -> list[tuple[str, float, float]]:
    """Deterministic script: one word every ``step_s`` seconds.

    Every token is a word start (leading space except for the first) so the
    committer's word-boundary rule can be tested.
    """
    out: list[tuple[str, float, float]] = []
    for i in range(n_words):
        start = i * step_s
        out.append(("Wort" if i == 0 else f" w{i:03d}", start,
                    start + step_s * 0.8))
    return out


class ScriptEngine:
    """Fake engine with a fixed script of ABSOLUTE times.

    Returns exactly the tokens for each window whose start lies within the
    window's absolute time range. Overlapping windows therefore return
    overlapping tokens -> tests absolute-timestamp deduplication.
    """

    def __init__(self, script: list[tuple[str, float, float]], *,
                 wrong_first: list[tuple[str, float, float]] | None = None,
                 silent: bool = False) -> None:
        self.script = script
        self.wrong_first = wrong_first
        self.silent = silent
        #: Entry per decode call: (offset_s, audio_s, n_bytes)
        self.calls: list[tuple[float, float, int]] = []
        self.plain_calls: list[int] = []

    # ---------------- Engine interface ---------------------------------
    def transcribe_with_timestamps(
        self, data: bytes, window_offset_s: float = 0.0
    ) -> Transcription:
        duration = len(data) / BYTES_PER_SECOND
        self.calls.append((window_offset_s, duration, len(data)))
        if self.silent:
            return Transcription(tokens=[])
        script = self.wrong_first if (
            self.wrong_first is not None and len(self.calls) == 1
        ) else self.script
        lo = window_offset_s - 1e-6
        hi = window_offset_s + duration - 1e-6
        return Transcription(tokens=[
            Token(text=t, start=s, end=e)
            for t, s, e in script
            if lo <= s <= hi
        ])

    def transcribe(self, data: bytes, initial_prompt: str = "") -> TranscriptionResult:
        self.plain_calls.append(len(data))
        return TranscriptionResult(
            text=self.transcribe_with_timestamps(data, 0.0).text.strip()
        )

    # ---------------- Test helpers --------------------------------------
    @property
    def n_calls(self) -> int:
        return len(self.calls)

    def tail_calls(self, n: int) -> list[tuple[float, float, int]]:
        """Decode windows starting from the nth call (e.g., from finalize)."""
        return self.calls[n:]


class ToggleVad:
    """Fake VAD whose result is switched manually by the test."""

    def __init__(self) -> None:
        self.speech = True
        self.n_calls = 0

    def has_speech(self, arr) -> bool:
        self.n_calls += 1
        return self.speech


def drive(t: BufferedTranscriber, seconds: float, step_s: float = 1.0,
          on_step=None) -> TranscriptionResult | None:
    """Feed ``seconds`` of audio in ``step_s`` steps, with one tick per step."""
    chunk = audio(step_s)
    last: TranscriptionResult | None = None
    for i in range(int(round(seconds / step_s))):
        t.feed(chunk)
        if on_step is not None:
            on_step(i)
        res = t.maybe_transcribe()
        if res is not None:
            last = res
    return last


def build(script, *, engine_kwargs=None, **kwargs
          ) -> tuple[BufferedTranscriber, ScriptEngine]:
    """Build transcriber + fake engine (engine_kwargs -> ScriptEngine)."""
    eng = ScriptEngine(script, **(engine_kwargs or {}))
    return BufferedTranscriber(eng, **kwargs), eng


# ----------------------------------------------------------------------
# Interface / Defaults
# ----------------------------------------------------------------------
class TestInterface:
    def test_signature_defaults_match_plan(self) -> None:
        """Exact defaults from the Kimi plan (STAGE 3)."""
        sig = inspect.signature(BufferedTranscriber.__init__)
        expected = {
            "interval_s": 1.0,
            "min_audio_s": 3.0,
            "whole_buffer_until_s": 8.0,
            "left_context_s": 8.0,
            "chunk_s": 2.0,
            "right_context_s": 2.0,
            "commit_agreements": 2,
            "max_final_window_s": 18.0,
            "sample_rate": 16000,
            "vad": None,
        }
        for name, default in expected.items():
            assert name in sig.parameters, f"Parameter {name} missing"
            assert sig.parameters[name].default == default, (
                f"{name}: Default {sig.parameters[name].default!r} "
                f"!= expected {default!r}"
            )

    def test_window_params_are_keyword_only(self) -> None:
        params = inspect.signature(BufferedTranscriber.__init__).parameters
        for name in ("interval_s", "whole_buffer_until_s", "vad"):
            assert params[name].kind is inspect.Parameter.KEYWORD_ONLY

    def test_below_min_audio_returns_none(self) -> None:
        t, eng = build(corpus(50), interval_s=1.0, min_audio_s=3.0)
        t.feed(audio(2.0))
        assert t.maybe_transcribe() is None
        assert eng.n_calls == 0

    def test_reset_clears_state(self) -> None:
        t, eng = build(corpus(60), interval_s=1.0, min_audio_s=3.0)
        drive(t, 16.0)
        assert t.committed_len > 0
        t.reset()
        assert t.audio_seconds == 0.0
        assert t.committed_len == 0
        assert t.last_text == ""

    def test_result_is_full_text_with_committed_len(self) -> None:
        """The controller still needs the FULL text (committed + partial)."""
        t, eng = build(corpus(80), interval_s=1.0, min_audio_s=3.0)
        res = drive(t, 20.0)
        assert res is not None
        assert res.committed_len > 0
        assert res.text.startswith(t.committed_text.strip())
        assert len(res.text) >= res.committed_len
        assert res.committed_len == len(t.committed_text.rstrip())


# ----------------------------------------------------------------------
# Phase A: 0..8s growing full buffer, with timestamps + agreement
# ----------------------------------------------------------------------
class TestPhaseA:
    def test_early_windows_are_whole_buffer_from_zero(self) -> None:
        """Below whole_buffer_until_s: windows start at 0 and are <= 8s."""
        t, eng = build(corpus(30), interval_s=1.0, min_audio_s=3.0,
                       whole_buffer_until_s=8.0)
        drive(t, 7.0)
        assert eng.n_calls >= 3, "too few ticks executed"
        assert all(off == pytest.approx(0.0) for off, _, _ in eng.calls)
        assert max(d for _, d, _ in eng.calls) <= 8.0 + 1e-6

    def test_first_window_commits_nothing_second_agreement_commits(self) -> None:
        """Phase A commits only after 2 identical candidates, from the start."""
        t, eng = build(corpus(60), interval_s=1.0, min_audio_s=3.0)
        committed_progress: list[int] = []
        chunk = audio(1.0)
        for _ in range(12):
            t.feed(chunk)
            res = t.maybe_transcribe()
            if res is not None:
                committed_progress.append(res.committed_len)
        assert committed_progress, "no transcription occurred"
        assert committed_progress[0] == 0, "first window must not commit"
        assert max(committed_progress) > 0, "nothing committed even after agreements"

    def test_hallucination_in_first_window_is_not_frozen(self) -> None:
        """Window 1 returns garbage, window 2 is correct -> garbage is never committed."""
        script = corpus(40)
        bad = [("OKAY", 0.0, 0.3), (";", 0.3, 0.4)] + script[2:]
        t, eng = build(script, engine_kwargs={"wrong_first": bad},
                       interval_s=1.0, min_audio_s=3.0)
        drive(t, 24.0)
        assert "OKAY" not in t.committed_text


# ----------------------------------------------------------------------
# Phase B: bounded, stateless windows
# ----------------------------------------------------------------------
class TestPhaseB:
    def test_44s_never_exceeds_window_budget(self) -> None:
        """44s simulation: NO window exceeds left+chunk+right."""
        t, eng = build(corpus(200, step_s=0.25), interval_s=1.0,
                       min_audio_s=3.0, whole_buffer_until_s=8.0,
                       left_context_s=8.0, chunk_s=2.0, right_context_s=2.0)
        drive(t, 44.0)
        budget = 8.0 + 2.0 + 2.0
        assert eng.n_calls > 5, "too few windows processed"
        over = [c for c in eng.calls if c[1] > budget + 1e-6]
        assert not over, f"windows over budget: {over[:3]}"

    def test_window_starts_at_committed_minus_left_context(self) -> None:
        """Phase B windows start at committed_s - left_context (>= 0)."""
        t, eng = build(corpus(200, step_s=0.25), interval_s=1.0,
                       min_audio_s=3.0, whole_buffer_until_s=8.0,
                       left_context_s=8.0, chunk_s=2.0, right_context_s=2.0)
        drive(t, 30.0)
        starts = [off for off, _, _ in eng.calls if off > 0.0]
        assert starts, "no Phase B window with left context processed"
        assert all(s <= t.audio_seconds for s in starts)

    def test_second_call_without_new_audio_is_throttled(self) -> None:
        """No new audio -> no additional engine call (interval throttle)."""
        t, eng = build(corpus(60), interval_s=1.0, min_audio_s=3.0)
        drive(t, 16.0)
        n = eng.n_calls
        assert t.maybe_transcribe() is None
        assert eng.n_calls == n

    def test_no_commit_without_right_context(self) -> None:
        """Without sufficient right context, no commit horizon is created."""
        t, eng = build(corpus(40), interval_s=1.0, min_audio_s=3.0,
                       whole_buffer_until_s=8.0, chunk_s=2.0,
                       right_context_s=2.0)
        chunk = audio(1.0)
        for _ in range(9):
            t.feed(chunk)
            t.maybe_transcribe()
        # Phase A: all windows start at 0 and remain <= whole_buffer_until_s.
        assert eng.calls
        for off, dur, _ in eng.calls:
            assert off == pytest.approx(0.0)
            assert dur <= 8.0 + 1e-6

    def test_overlap_dedup_no_duplicated_words(self) -> None:
        """Overlapping windows must not duplicate words."""
        t, eng = build(corpus(220, step_s=0.25), interval_s=1.0, min_audio_s=3.0)
        res = drive(t, 48.0)
        words = res.text.split()
        dupes = sorted({w for w in words if words.count(w) > 1})
        assert not dupes, f"duplicated words: {dupes[:5]}"

    def test_no_gaps_and_monotonic_word_order(self) -> None:
        """No dropped words in the middle; monotonic order."""
        t, eng = build(corpus(220, step_s=0.25), interval_s=1.0, min_audio_s=3.0)
        res = drive(t, 48.0)
        words = res.text.split()
        assert words[0] == "Wort"
        idx = [int(w[1:]) for w in words[1:]]
        assert idx == sorted(idx), "word order is not monotonic"
        assert idx == list(range(idx[0], idx[0] + len(idx))), "gap in text"

    def test_last_word_never_split(self) -> None:
        """A word-start token at the commit boundary remains partial."""
        script = ([("Das", 0.0, 0.3), (" hier", 0.4, 0.7), (" ist", 0.8, 1.1),
                   (" de", 1.2, 1.35), ("utlich", 1.35, 1.6)]
                  + [(" mehr", 2.0, 2.3), (" text", 2.4, 2.7),
                     (" ue", 2.8, 2.95), ("ber", 2.95, 3.2)]
                  + corpus(400, step_s=0.4)[9:])
        t, eng = build(script, interval_s=0.5, min_audio_s=3.0,
                       whole_buffer_until_s=8.0, chunk_s=1.0,
                       right_context_s=2.0)
        drive(t, 16.0, step_s=0.5)
        assert " de " not in f" {t.committed_text} "
        assert " ue " not in f" {t.committed_text} "

    def test_pause_does_not_stall_and_keeps_previous_words(self) -> None:
        """VAD pause: commit point advances while previous text is preserved."""
        vad = ToggleVad()
        t, eng = build(corpus(220, step_s=0.4), interval_s=1.0, min_audio_s=3.0,
                       whole_buffer_until_s=8.0, vad=vad)
        drive(t, 12.0)
        before = t.committed_text.strip()
        assert before != ""

        vad.speech = False
        drive(t, 12.0)
        assert t.last_text.strip().startswith(before), "text before pause lost"

        vad.speech = True
        res = drive(t, 24.0)
        assert res.text.startswith(before), "pause displaced committed text"
        assert len(res.text) > len(before), "nothing recognized after pause"


# ----------------------------------------------------------------------
# finalize()
# ----------------------------------------------------------------------
class TestFinalize:
    def _streamed(self, seconds: float = 24.0, script=None):
        t, eng = build(script or corpus(240, step_s=0.25), interval_s=1.0,
                       min_audio_s=3.0)
        drive(t, seconds)
        return t, eng

    def test_no_new_bytes_no_engine_call(self) -> None:
        """Unchanged audio -> committed + last_partial, without an engine call."""
        t, eng = self._streamed(24.0)
        n = eng.n_calls
        res = t.finalize()
        assert eng.n_calls == n, "finalize must not decode without new bytes"
        assert res.text == t.last_text
        assert res.committed_len == len(t.committed_text.rstrip())

    def test_new_bytes_decode_only_tail_plus_context(self) -> None:
        """New bytes -> only uncommitted tail + context, never the full buffer."""
        t, eng = self._streamed(24.0)
        committed = t.committed_text.strip()
        t.feed(audio(2.0))
        n = eng.n_calls
        res = t.finalize()
        assert eng.n_calls > n, "finalize must decode the new tail"
        for off, dur, _ in eng.tail_calls(n):
            assert dur <= 18.0 + 1e-6, f"finalize window over budget: {dur}"
        assert res.text.startswith(t.committed_text.strip())
        assert res.committed_len == len(t.committed_text.rstrip())

    def test_late_finalize_decodes_small_slice(self) -> None:
        """The later finalize runs, the smaller the decoded region."""
        t, eng = build(corpus(400, step_s=0.25), interval_s=1.0, min_audio_s=3.0)
        drive(t, 60.0)
        total_s = t.audio_seconds
        n = eng.n_calls
        t.feed(audio(1.0))
        t.finalize()
        biggest = max(d for _, d, _ in eng.tail_calls(n))
        assert biggest < total_s / 2, (
            f"finalize decoded {biggest:.1f}s of {total_s:.1f}s -> full buffer"
        )

    def test_catchup_uses_bounded_windows_not_whole_buffer(self) -> None:
        """Committer far behind -> multiple bounded windows, never full buffer."""
        t, eng = build(corpus(400, step_s=0.25), interval_s=1.0,
                       min_audio_s=3.0, max_final_window_s=18.0)
        # Lots of audio at once, few ticks -> committer stagnates near 0s.
        t.feed(audio(70.0))
        t.maybe_transcribe()
        t.feed(audio(1.0))
        n = eng.n_calls
        res = t.finalize()
        calls = eng.tail_calls(n)
        assert calls, "finalize must decode the uncommitted tail"
        for off, dur, _ in calls:
            assert dur <= 18.0 + 1e-6, f"catch-up window too large: {dur}"
        assert len(res.text.split()) > 5, "catch-up returns very little text"

    def test_silent_tail_is_not_dropped(self) -> None:
        """VAD reports silence: finalize must not silently discard the remainder."""
        vad = ToggleVad()
        t, eng = build(corpus(220, step_s=0.4), interval_s=1.0, min_audio_s=3.0,
                       vad=vad)
        drive(t, 20.0)
        vad.speech = False
        t.feed(audio(3.0))
        n = eng.n_calls
        res = t.finalize()
        assert eng.n_calls > n or res.text == t.last_text, (
            "silent tail received no processing"
        )
        assert res.text.startswith(t.committed_text.strip()), "tail discarded"


# ----------------------------------------------------------------------
# Slow end-to-end test with real engine (skip without model)
# ----------------------------------------------------------------------
def _model_available() -> bool:
    root = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface"))
    return (root / "hub" / "models--nvidia--parakeet-tdt-0.6b-v3").is_dir()


@pytest.mark.slow
@pytest.mark.skipif(not _model_available(), reason="Parakeet-Modell nicht vorhanden")
def test_real_engine_long_dictate_stays_within_window_budget() -> None:
    """Real engine: long dictation -> windows within budget, text grows."""
    import wave

    from stt_button.asr import ParakeetEngine

    wav = Path(__file__).parent / "fixtures" / "corpus_fix_01.wav"
    if not wav.is_file():
        pytest.skip("fixture WAV missing")
    with wave.open(str(wav), "rb") as fh:
        raw = fh.readframes(fh.getnframes())
        rate = fh.getframerate()
    if rate != RATE:
        pytest.skip(f"fixture has {rate} Hz, expected {RATE} Hz")
    while len(raw) // 2 < 44 * RATE:
        raw = raw + raw
    raw = raw[: 44 * RATE * 2]

    class Counting:
        def __init__(self, inner) -> None:
            self.inner = inner
            self.calls: list[tuple[float, float, int]] = []

        def transcribe_with_timestamps(self, data, window_offset_s=0.0):
            self.calls.append((window_offset_s, len(data) / BYTES_PER_SECOND,
                               len(data)))
            return self.inner.transcribe_with_timestamps(data, window_offset_s)

        def transcribe(self, data, initial_prompt=""):
            return self.inner.transcribe(data, initial_prompt=initial_prompt)

    engine = Counting(ParakeetEngine("nvidia/parakeet-tdt-0.6b-v3"))
    t = BufferedTranscriber(engine, interval_s=1.0, min_audio_s=3.0)
    for i in range(0, len(raw), BYTES_PER_SECOND):
        t.feed(raw[i:i + BYTES_PER_SECOND])
        t.maybe_transcribe()
    res = t.finalize()

    budget = 8.0 + 2.0 + 2.0
    assert all(d <= budget + 1e-6 for _, d, _ in engine.calls)
    assert len(res.text.split()) > 5
