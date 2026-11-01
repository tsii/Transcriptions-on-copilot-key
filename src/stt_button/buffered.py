"""BufferedTranscriber: Hybrid of a full buffer (Phase A) and bounded windows (Phase B).

STAGE 3 of the latency/quality plan. Implements the same interface as
``StreamTranscriber`` / ``WindowedTranscriber`` (``feed`` /
``maybe_transcribe`` / ``finalize`` / ``reset``), allowing the controller to
switch modes through configuration.

Strategy
--------
* **Phase A** (audio <= ``whole_buffer_until_s``, default 8s): transcribe the
  entire buffer, as in the proven ``StreamTranscriber``, but WITH timestamps
  and a single :class:`~stt_button.committer.TimestampCommitter`
  (``require_agreement_always=True``) from the outset. One committer, no second
  seeding: from the beginning, "two identical results at the same committed_s"
  is required so an early hallucination is never frozen into the output.
* **Phase B** (> ``whole_buffer_until_s``): exactly ONE strictly bounded window
  ``[committed_s - left_context_s | chunk_s | right_context_s]`` per tick, with
  the commit horizon at ``committed_s + chunk_s``. Stateless: no decoder or
  encoder state crosses window boundaries; each window is decoded independently.
  Overlapping tokens are deduplicated using their ABSOLUTE timestamps (the
  committer discards everything before ``committed_s``). The window is hard-
  capped at ``left + chunk + right``; if the commit point stalls at a pause,
  ``skip_silence`` ensures progress.
* **finalize()**: no new bytes -> ``committed + last_partial`` without an
  engine call. New bytes -> only the uncommitted tail plus left context in
  bounded windows (``max_final_window_s``); NEVER the full buffer.

The controller continues to receive the FULL text (committed + partial) along
with ``committed_len``, exactly as with ``WindowedTranscriber``.
"""
from __future__ import annotations

import logging
import numpy as np

from .committer import TimestampCommitter
from .streaming_types import StreamWindow

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000


def _to_float_audio(data: bytes) -> np.ndarray:
    """Reliably convert s16le bytes to float32 [-1,1], even for odd lengths."""
    if len(data) % 2:
        data = data[:-1]
    return np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0


class BufferedTranscriber:
    """Hybrid buffer/window transcription with timestamp-based commits."""

    def __init__(
        self,
        engine: object,
        *,
        interval_s: float = 1.0,
        min_audio_s: float = 3.0,
        whole_buffer_until_s: float = 8.0,
        left_context_s: float = 8.0,
        chunk_s: float = 2.0,
        right_context_s: float = 2.0,
        commit_agreements: int = 2,
        max_final_window_s: float = 18.0,
        sample_rate: int = SAMPLE_RATE,
        vad: object | None = None,
    ) -> None:
        #: Engine with transcribe_with_timestamps (+ transcribe as a fallback)
        self.engine = engine
        self.interval_s = interval_s
        self.min_audio_s = min_audio_s
        #: Transcribe the entire buffer up to this buffer length (Phase A).
        self.whole_buffer_until_s = whole_buffer_until_s
        #: Left context of the Phase B window (quality anchor before committed_s).
        self.left_context_s = left_context_s
        #: New stable segment per tick.
        self.chunk_s = chunk_s
        #: Right context (lookahead) beyond the commit horizon.
        self.right_context_s = right_context_s
        #: Consecutive identical results required before a candidate is committed.
        self.commit_agreements = commit_agreements
        #: Hard budget for finalize/catch-up windows (seconds of audio).
        self.max_final_window_s = max_final_window_s
        self.sample_rate = sample_rate
        #: Optional VAD gate (e.g., SileroVadGate).
        self.vad = vad

        self._buffer = bytearray()
        self._committer = self._new_committer()
        self._committed_text = ""
        self._last_partial = ""
        #: Audio position (seconds) at the last engine call (interval throttle).
        self._last_transcribed_at = 0.0
        self._has_transcribed = False
        #: Consecutive VAD misses in the new region (pause detection).
        self._silence_misses = 0
        #: True after the live gate reports "silent" in the new region.
        self._vad_live_silent = False
        #: Buffer size after the last successful engine call: evidence of
        #: "no new bytes" for the finalize cache.
        self._completed_nbytes = -1

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------
    @property
    def audio_seconds(self) -> float:
        return len(self._buffer) / (self.sample_rate * 2)

    @property
    def committed_len(self) -> int:
        """Length of the committed text prefix (for DeltaTyper)."""
        return len(self._committed_text.rstrip())

    @property
    def committed_text(self) -> str:
        return self._committed_text.rstrip()

    @property
    def last_partial(self) -> str:
        return self._last_partial

    @property
    def last_text(self) -> str:
        """Full last known text (committed + partial)."""
        return (self._committed_text + self._last_partial).strip()

    @property
    def committed_seconds(self) -> float:
        return self._committer.committed_s

    # ------------------------------------------------------------------
    # Control
    # ------------------------------------------------------------------
    def reset(self) -> None:
        """Clears the buffer, committer, and all counters (new recording)."""
        self._buffer.clear()
        self._committer = self._new_committer()
        self._committed_text = ""
        self._last_partial = ""
        self._last_transcribed_at = 0.0
        self._has_transcribed = False
        self._silence_misses = 0
        self._vad_live_silent = False
        self._completed_nbytes = -1

    def feed(self, chunk: bytes) -> None:
        """Adds audio to the buffer and invalidates the finalize cache."""
        if chunk:
            self._buffer.extend(chunk)
            self._vad_live_silent = False
            self._silence_misses = 0

    def _new_committer(self) -> TimestampCommitter:
        """One committer for both phases; agreement is always required."""
        return TimestampCommitter(
            commit_agreements=self.commit_agreements,
            require_agreement_always=True,
        )

    # ------------------------------------------------------------------
    # Live-Tick
    # ------------------------------------------------------------------
    def maybe_transcribe(self):
        """Transcribes the next due window, if any.

        Returns:
            ``TranscriptionResult`` containing the FULL text (committed +
            partial) and ``committed_len``, or ``None`` if nothing was due.
        """
        from .asr import TranscriptionResult

        audio_s = self.audio_seconds
        if audio_s < self.min_audio_s:
            return None
        if self._has_transcribed and \
                audio_s - self._last_transcribed_at < self.interval_s:
            return None

        window = self._next_window(audio_s)
        if window is None:
            return None
        if self._vad_blocks(window):
            return None

        tr = self._decode(window)
        self._last_transcribed_at = audio_s
        self._has_transcribed = True
        self._completed_nbytes = len(self._buffer)

        commit_text, _ = self._committer.update(tr, window.commit_horizon)
        if commit_text.strip():
            self._committed_text += commit_text.strip() + " "
        # Live partial = everything known AFTER the commit point: the candidate
        # not yet confirmed AND the tokens beyond the horizon. Using only the
        # committer's partial would omit the withheld candidate words from the
        # live text -> DeltaTyper would delete them with backspaces and retype
        # them later (visible flicker, lost words).
        if tr.tokens:
            fresh = [t for t in tr.tokens
                     if t.start >= self._committer.committed_s - 1e-3]
            self._last_partial = "".join(t.text for t in fresh).strip()
        else:
            self._last_partial = self._committer.last_partial.strip()

        committed_clean = self._committed_text.rstrip()
        full = (committed_clean + " " + self._last_partial).strip()
        log.debug(
            "buffered window %.1f-%.1fs horizon=%.1fs -> committed=%d text=%r",
            window.start, window.end, window.commit_horizon,
            len(committed_clean), full[:60],
        )
        return TranscriptionResult(text=full, committed_len=len(committed_clean))

    # ------------------------------------------------------------------
    # Finalization
    # ------------------------------------------------------------------
    def finalize(self):
        """Final pass: use cached audio if unchanged; otherwise, tail + context.

        The committed prefix is NEVER decoded again or overwritten;
        ``committed_len`` remains the prefix length so DeltaTyper neither
        modifies nor duplicates the committed portion.
        """
        from .asr import TranscriptionResult

        def _full(tail_text: str):
            committed_clean = self._committed_text.rstrip()
            text = (committed_clean + " " + tail_text).strip() if tail_text \
                else committed_clean
            return TranscriptionResult(text=text, committed_len=len(committed_clean))

        tail_start_byte = int(self._committer.committed_s * self.sample_rate * 2)
        tail = bytes(self._buffer[tail_start_byte:])

        if not tail:
            return _full(self._last_partial)
        if len(self._buffer) == self._completed_nbytes:
            log.debug("buffered finalize: no new bytes -> cache (%d bytes)",
                      len(self._buffer))
            return _full(self._last_partial)

        # Do not blindly discard VAD remainders: if the live gate reported
        # "silent" twice in the new region, the tail was a real pause, so keep
        # the last partial instead of inventing garbage.
        if self.vad is not None and self._vad_live_silent:
            log.info("buffered finalize: tail is silent according to VAD -> last "
                     "partial retained (no full buffer, no data loss)")
            return _full(self._last_partial)

        tail_s = len(tail) / (self.sample_rate * 2)
        log.debug("buffered finalize: decoding uncommitted tail %.2fs from %.1fs",
                  tail_s, self._committer.committed_s)

        pieces: list[str] = []
        # Deduplicate overlaps using absolute token timestamps: everything
        # before ``emitted_s`` has already been emitted.
        emitted_s = self._committer.committed_s
        for window in self._catchup_windows(self.audio_seconds):
            tr = self._decode(window)
            text = "".join(
                t.text for t in tr.tokens if t.start >= emitted_s - 1e-3
            ).strip()
            if text:
                pieces.append(text)
                emitted_s = max(t.end for t in tr.tokens) + 1e-3
        tail_text = " ".join(pieces).strip()
        if not pieces:
            # Fallback without timestamps: ONLY the uncommitted tail, never
            # the full buffer.
            result = self.engine.transcribe(tail)
            tail_text = getattr(result, "text", str(result)).strip()
        return _full(tail_text)

    # ------------------------------------------------------------------
    # Window calculation
    # ------------------------------------------------------------------
    def _next_window(self, audio_s: float) -> StreamWindow | None:
        """Next live window (Phase A: full buffer, Phase B: bounded)."""
        committed_s = self._committer.committed_s

        if audio_s <= self.whole_buffer_until_s:
            # Phase A: growing full buffer from 0 through the end of the audio.
            # Commit only what lies safely before the horizon and still has
            # right context beyond it (RC-A3, as in StreamWindowPolicy).
            horizon = min(audio_s - self.right_context_s,
                          committed_s + self.chunk_s)
            if horizon <= committed_s:
                return None
            return StreamWindow(start=0.0, end=audio_s, commit_horizon=horizon)

        # Phase B: exactly ONE strictly bounded window per tick, hard-capped at
        # left + chunk + right (constant inference costs).
        start = max(0.0, committed_s - self.left_context_s)
        horizon = committed_s + self.chunk_s
        if audio_s < horizon + self.right_context_s:
            # No right context beyond the horizon yet -> do not commit.
            return None
        end = horizon + self.right_context_s
        if end <= start:
            return None
        return StreamWindow(start=start, end=end, commit_horizon=horizon)

    def _catchup_windows(self, audio_s: float):
        """Bounded windows for finalize: tail + context, never full buffer."""
        budget = min(self.max_final_window_s,
                     self.left_context_s + self.chunk_s + self.right_context_s)
        ctx = min(self.left_context_s, self._committer.committed_s)
        pos = self._committer.committed_s
        while pos < audio_s - 1e-9:
            start = max(0.0, pos - ctx)
            end = min(audio_s, start + budget)
            if end <= pos:
                # Sacrifice left context to guarantee progress.
                end = min(audio_s, pos + max(1.0, budget - ctx))
                if end <= pos:
                    break
            yield StreamWindow(start=start, end=end, commit_horizon=None)
            pos = end

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------
    def _decode(self, window: StreamWindow):
        # Round to byte boundaries: round the start UP and the end DOWN so
        # the effective window length never exceeds the budget
        # (left+chunk+right).
        start_byte = -(-int(window.start * self.sample_rate * 2) // 1)
        start_byte = int(window.start * self.sample_rate * 2 + 0.999999)
        end_byte = int(window.end * self.sample_rate * 2)
        data = bytes(self._buffer[start_byte:end_byte])
        return self.engine.transcribe_with_timestamps(data, window.start)

    def _vad_blocks(self, window: StreamWindow) -> bool:
        """VAD gate on the NEW region; detects pauses and advances.

        As with ``WindowedTranscriber``, two consecutive "silent" reports
        trigger ``skip_silence``: without this, the commit point would stall
        at every pause and everything beyond it would be lost forever.
        Already recognized text is preserved (committed + last partial).
        """
        if self.vad is None:
            return False
        committed_byte = int(self._committer.committed_s * self.sample_rate * 2)
        end_byte = int(window.end * self.sample_rate * 2)
        data = bytes(self._buffer[committed_byte:end_byte])
        if not data:
            return False
        arr = _to_float_audio(data)
        if len(arr) < self.sample_rate:
            # Below 1s, the gate cannot make a determination -> do not block.
            self._silence_misses = 0
            return False
        try:
            speech = self.vad.has_speech(arr)
        except Exception as exc:  # noqa: BLE001 - Gate ist optional
            log.warning("buffered: VAD gate failed (%s) - transcribing anyway", exc)
            return False
        if speech:
            self._silence_misses = 0
            self._vad_live_silent = False
            return False

        self._silence_misses += 1
        self._vad_live_silent = True
        log.debug("buffered: VAD silent in new region (miss %d)",
                  self._silence_misses)
        if self._silence_misses >= 2:
            # Real pause: advance the commit point, but only through the
            # window end and only if the window was filled through that point.
            # The last partial is preserved as text (last_text).
            if self.audio_seconds >= window.end - 0.01:
                self._flush_pending_text()
                self._committer.skip_silence(window.end)
                self._silence_misses = 0
                log.info("buffered: pause detected -> committed_s advanced to %.1fs",
                         self._committer.committed_s)
        return True

    def _flush_pending_text(self) -> None:
        """Preserves the last partial in the committed text (pause protection).

        ``skip_silence`` clears the committer's partial; otherwise, already
        recognized text would be lost when the commit point advances.
        Separate it with a space, exactly as in a regular commit.
        """
        if self._last_partial.strip():
            self._committed_text += self._last_partial.strip() + " "
            self._last_partial = ""
