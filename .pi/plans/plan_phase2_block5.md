# Phase 2 – Block 5 (C1): Segmentation at sentence pauses in `StreamTranscriber` (core)

Depends on blocks 1–2 (A). The rationale is in the overview: the cut runs as a live engine call under `_exec_lock` and must be cancellable without leaving a half-cut state.

## Goal
Opt-in `segment_pause_s > 0` (default **0.0 = off = current behavior**).
- When a regular live tick is due (interval passed), first check for a **sentence pause**:
  - The last live hypothesis (`_last_text`) ends with `.`, `?` or `!` (TalkType: `lastHypothesisEndsWithPunct`).
  - **AND** the VAD reports no speech in the last `max(1.0, segment_pause_s)` seconds of the buffer.
  - **AND** the segment is ≥ `segment_min_s` (default 3.0) long.
- If all three hold, run a **mini-final**: one engine call on the full segment buffer.
  - Only if the mini-final text also ends with `.?!` is the segment **permanently** closed (hard cut, **no stitching**):
    - `_closed_text += text + " "`
    - append the segment bytes to `_closed_audio`
    - reset the buffer/commit/history/VAD/interval state (as in `reset()`, but without closed state and without the pause indicator)
  - Otherwise, treat the call as a normal tick (the result is returned as usual).
- Composition, everywhere:
  - live result: `text = _closed_text + seg_text`, `committed_len = len(_closed_text) + seg_committed_len`
  - `finalize()`: `text = _closed_text + final(seg)`, `committed_len = len(_closed_text)`
  - Closed segments are never revised again.
- State changes happen **only after** a successful engine call. `InferenceCancelled`/errors leave the state unchanged (A compatibility).
- Seams:
  - **Capitalization:** text is not rewritten. Parakeet sees the new segment start as a sentence start, which is correct because a cut only happens after `.?!`. The gate (block 8) checks this empirically.
  - **Trailing dots:** the cut only happens when the mini-final itself ends with punctuation, so a punctuation mark at the seam is never lost.
  - **Space:** `_closed_text` always ends with exactly one `" "`. An empty segment remainder at finalize gives `rstrip()` of the prefix (see finalize).
  - **Commit:** the next segment starts with `committed_len = len(_closed_text)` → in `live_typing_mode="committed"`, DeltaTyper types the closed prefix immediately (`stable.startswith(old)` holds, typing.py:733).

## Test first – `tests/test_segmenting.py` (new)
```python
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
```

## Expected RED
`TypeError: StreamTranscriber.__init__() got an unexpected keyword argument 'segment_pause_s'`. `test_default_off_never_cuts` fails with `AttributeError: segment_pause_s`.

## Implementation (before → after), `src/stt_button/asr.py`

**1. Fields after `mid_tail_anchor_words` (asr.py:507). If block 4 is already in, add them after `asr_interval_max_s`:**
```python
    #: PHASE 2 (C): hard-cut segmentation. >0: a VAD pause of at least this
    #: many seconds after a hypothesis ending in .?! closes the segment
    #: (mini-final, permanent commit, buffer reset). 0.0 (default) = off.
    segment_pause_s: float = 0.0
    #: PHASE 2 (C): minimum segment length (seconds) before a pause cut.
    segment_min_s: float = 3.0
    #: PHASE 2 (C): hard cap per segment (seconds, block 6); 0 = no cap.
    segment_max_s: float = 0.0
```
After `_mid_tail_failed` (asr.py:546):
```python
    #: PHASE 2 (C): text of closed segments, always ends with one space.
    _closed_text: str = field(default="", init=False, repr=False)
    #: PHASE 2 (C): audio of closed segments (for the debug dump).
    _closed_audio: bytearray = field(default_factory=bytearray, init=False, repr=False)
```

**2. Properties after `last_text` (asr.py ~566):**
```python
    @property
    def closed_text(self) -> str:
        """Text of permanently closed segments (Phase 2, C)."""
        return self._closed_text

    def full_audio(self) -> bytes:
        """Whole recording: closed segments followed by the current buffer."""
        return bytes(self._closed_audio) + bytes(self._buffer)
```

**3. `reset()` (asr.py:578-592).** Split it so the segment cut can reuse it:
```python
# before
    def reset(self) -> None:
        """..."""
        self._buffer.clear()
        ...
        self._mid_tail_failed = False
        self._invalidate_cache()
# after
    def reset(self) -> None:
        """...(docstring unchanged)..."""
        self._reset_segment()
        self._closed_text = ""
        self._closed_audio.clear()
        self._live_paused_seen = False
        self._live_paused_pending = False

    def _reset_segment(self) -> None:
        """Clear the CURRENT segment only (buffer, commit, history, cache)."""
        self._buffer.clear()
        self._last_transcribed_at = 0.0
        self._has_transcribed = False
        self._committed_text = ""
        self._committed_len = 0
        self._history = []
        self._last_text = ""
        self._vad_checked_at = 0.0
        self._mid_tail_checkpoint = None
        self._mid_tail_failed = False
        self._invalidate_cache()
```
If block 4 is in, also put `self._last_tick_s = 0.0` in `_reset_segment`.

**4. `maybe_transcribe` (asr.py:665-697).** Insert after the interval check (`if self._has_transcribed: ... return None`, asr.py:665-667) and **before** `# VAD gate:`:
```python
        # PHASE 2 (C): sentence-pause cut takes precedence over the VAD skip
        # (the pause IS silence; the normal gate would return None here).
        if self.segment_pause_s > 0.0 and self._pause_cut_due():
            return self._cut_segment(len(self._buffer))
```
`return self._transcribe()` (asr.py:697) becomes `return self._compose(self._transcribe())`.

**5. `finalize()` (asr.py:713-736).** Wrap every return path:
- the cache return (asr.py:722-724): `return self._finalize_compose(self._last_completed_text)`
- the mid-tail return: unchanged, because block 7 validation forbids `cached_mid_tail` together with segmentation
- the end (asr.py:735-736):
```python
# before
        result = self._transcribe()
        return TranscriptionResult(text=result.text, committed_len=0)
# after
        result = self._transcribe()
        return self._finalize_compose(result.text)
```

**6. New helpers before `_new_audio` (asr.py:874):**
```python
    _SENTENCE_END = (".", "?", "!")

    def _compose(self, result: TranscriptionResult) -> TranscriptionResult:
        """Prefix closed segments to a live result (Phase 2, C)."""
        if not self._closed_text:
            return result
        return TranscriptionResult(
            text=self._closed_text + result.text,
            committed_len=len(self._closed_text) + result.committed_len,
        )

    def _finalize_compose(self, text: str) -> TranscriptionResult:
        """Final result: closed prefix is committed, the segment is open."""
        if not self._closed_text:
            return TranscriptionResult(text=text, committed_len=0)
        if not text:
            return TranscriptionResult(text=self._closed_text.rstrip(),
                                       committed_len=0)
        return TranscriptionResult(text=self._closed_text + text,
                                   committed_len=len(self._closed_text))

    def _pause_cut_due(self) -> bool:
        """Sentence pause: punctuation at the hypothesis end + silent tail."""
        if self.vad is None or not self._last_text.endswith(self._SENTENCE_END):
            return False
        if self.audio_seconds < self.segment_min_s:
            return False
        tail_s = max(1.0, self.segment_pause_s)
        n = int(tail_s * self.sample_rate) * BYTES_PER_SAMPLE
        if len(self._buffer) < n:
            return False
        try:
            return not self.vad.has_speech(_pcm_to_float(bytes(self._buffer[-n:])))
        except Exception as exc:  # noqa: BLE001 - gate is optional
            log.warning("VAD pause check failed (%s) - no segment cut", exc)
            return False

    def _cut_segment(self, cut_nbytes: int) -> TranscriptionResult:
        """Mini-final on buffer[:cut_nbytes]; close it if it ends a sentence.

        State is changed ONLY after the engine call succeeded (an
        InferenceCancelled or engine error leaves everything untouched).
        """
        seg = bytes(self._buffer[:cut_nbytes])
        result = self.engine.transcribe(seg, initial_prompt=self._committed_text)
        text = result.text.strip()
        if cut_nbytes == len(self._buffer) and not text.endswith(self._SENTENCE_END):
            # Not a sentence end after all: behave like a normal tick.
            self._last_transcribed_at = self.audio_seconds
            self._has_transcribed = True
            self._invalidate_cache()
            self._last_text = text
            self._update_commit(text)
            return self._compose(
                TranscriptionResult(text=text, committed_len=self._committed_len)
            )
        rest = bytes(self._buffer[cut_nbytes:])
        if text:
            self._closed_text += text + " "
        self._closed_audio.extend(seg)
        self._reset_segment()
        self._buffer.extend(rest)
        self._feed_epoch += 1
        log.debug("Segment closed (%d bytes) -> %r", len(seg), text)
        return TranscriptionResult(text=self._closed_text,
                                   committed_len=len(self._closed_text))
```
Note: `_cut_segment(cut_nbytes < len)` is only reached from block 6 (hard cap). Cap cuts are always closed, even without punctuation.

## GREEN
```bash
nice -n 19 .venv/bin/python -m pytest tests/test_segmenting.py -q
nice -n 19 .venv/bin/python -m pytest -q
```
All existing `test_asr.py`/`test_stream_vad.py` tests must stay green. `reset()` behaves the same for callers.

## Abort criterion
- An existing StreamTranscriber test fails and the only fix would be changing its semantics.
- Or the pause check needs more than one VAD run per due tick, i.e. a CPU regression visible in `latency_trace`.

Then stop C at C1 and keep A+B (see the rollback in the overview).
