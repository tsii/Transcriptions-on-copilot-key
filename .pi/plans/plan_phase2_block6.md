# Phase 2 – Block 6 (C2): Hard cap `segment_max_s` and interplay with `live_asr_until_s`

Depends on block 5.

## Goal
- When `segment_max_s > 0` and the **segment** buffer is ≥ `segment_max_s`, the next due tick forces a cut **without** a punctuation requirement.
  - TalkType does the same with a buffer-based 35 s cap in the consumer.
- The cut position is the **quietest point** in the last 2 s, as a 100 ms RMS minimum. That avoids splitting mid-word.
  - Only if the window is too short does the cut land at the buffer end.
  - The remainder after the cut point stays in the buffer as the start of the new segment.
- Order in `maybe_transcribe`: the cap check comes **before** the `live_asr_until_s` check (asr.py:677). The live limit then refers to the *segment* length, because `audio_seconds` only counts the current segment buffer.
- **Migration/default of `live_asr_until_s`:**
  - Segmentation off: unchanged. The limit counts the whole recording (default 15 s).
  - Segmentation on: the limit counts **per segment**. Recommended config is `segment_max_s = 15.0` together with `live_asr_until_s = 15.0`. Because the cap fires first, the pause indicator never appears.
  - Validation (block 7): with `segment_pause_s > 0`, `segment_max_s` must be `> 0` and `≤ live_asr_until_s` (when `live_asr_until_s > 0`). Otherwise a segment would hit the limit before the cap, and live text would stop until release.
  - Rationale for 15 instead of TalkType's 35 s: at FP32 RTF ~0.2, a 35 s whole-buffer costs ~7 s per tick, which is exactly the latency problem from the report. 15 s caps the mini-final at ~3 s.

## Test first – append to `tests/test_segmenting.py`
```python
def _loud(seconds: float) -> bytes:
    import numpy as np
    n = int(seconds * RATE)
    return (np.full(n, 8000, dtype=np.int16)).tobytes()


def test_hard_cap_cuts_without_punctuation() -> None:
    t, engine, _ = make(["und dann weiter", "und dann weiter"], segment_max_s=6.0)
    speak(t, 4.0)
    res = speak(t, 2.5)                 # 6.5s >= cap, no punctuation, speaking
    assert t.closed_text == "und dann weiter "
    assert res.committed_len == len("und dann weiter ")


def test_hard_cap_cuts_at_quietest_point() -> None:
    t, engine, _ = make(["a", "a b"], segment_max_s=6.0)
    t.feed(_loud(4.0))
    t.maybe_transcribe()
    t.feed(_loud(1.0) + audio(0.2) + _loud(0.8))  # silence gap at 5.0-5.2s
    t.maybe_transcribe()
    cut = engine.calls[-1]
    assert 5.0 * BPS <= cut <= 5.2 * BPS
    remainder = t.audio_seconds
    assert 0.8 <= remainder <= 1.0


def test_cap_preempts_live_limit() -> None:
    t, _, _ = make(["x y", "x y"], segment_max_s=6.0, live_asr_until_s=6.0)
    speak(t, 4.0)
    speak(t, 2.5)
    assert t.closed_text == "x y "
    assert t.take_live_paused() is False


def test_cap_zero_means_no_cap() -> None:
    t, _, _ = make(["x y"] * 5)
    for _ in range(5):
        speak(t, 2.0)
    assert t.closed_text == ""
```

## Expected RED
`test_hard_cap_cuts_without_punctuation`: `closed_text == ""`. The cap is not implemented yet. The field exists from block 5, so this is an AssertionError, not a TypeError. `test_cap_zero_means_no_cap` is already green.

## Implementation, `src/stt_button/asr.py`

**1. `maybe_transcribe`.** Insert before the live-limit check (asr.py:677, `if self.live_asr_until_s > 0.0 and ...`). The cap must not run before `min_audio_s`, which is already checked at 671:
```python
        # PHASE 2 (C): hard cap per segment; precedes the live limit so a
        # segmented recording never reaches the pause indicator.
        if self.segment_max_s > 0.0 and self.audio_seconds >= self.segment_max_s:
            return self._cut_segment(self._quiet_cut_nbytes())
```

**2. New helper next to `_pause_cut_due`:**
```python
    def _quiet_cut_nbytes(self, window_s: float = 2.0, frame_s: float = 0.1) -> int:
        """Byte offset of the quietest 100 ms frame END in the last window_s.

        Falls back to the buffer end if the window is too short.
        """
        import numpy as np

        frame = int(frame_s * self.sample_rate)
        win = int(window_s * self.sample_rate)
        samples = np.frombuffer(bytes(self._buffer[: len(self._buffer) // 2 * 2]),
                                dtype=np.int16)
        if len(samples) < win + frame:
            return len(self._buffer)
        start = len(samples) - win
        best, best_rms = len(samples), None
        for pos in range(start, len(samples) - frame + 1, frame):
            rms = float(np.sqrt(np.mean(samples[pos:pos + frame].astype(np.float32) ** 2)))
            if best_rms is None or rms < best_rms:
                best, best_rms = pos + frame, rms
        return best * BYTES_PER_SAMPLE
```
`_cut_segment` from block 5 already closes every cut with `cut_nbytes < len(buffer)`. With an exact `cut == len` (fallback, or the quietest frame at the end), it would fall into the "no punctuation → normal tick" branch. To avoid that, add a parameter `force: bool = False`. The cap passes `force=True`, and the condition becomes `if not force and cut_nbytes == len(self._buffer) and not text.endswith(...)`.

## GREEN
```bash
nice -n 19 .venv/bin/python -m pytest tests/test_segmenting.py -q
nice -n 19 .venv/bin/python -m pytest -q
```

## Abort criterion
Gate block 8 shows > 0 lost or doubled words at **cap** seams (pause seams are fine).
- Then set `segment_max_s` to 0 and keep only pause cuts, with the live limit as before.
- If pause seams fail as well, drop C entirely.
