"""WindowedTranscriber: windowed streaming with the StreamTranscriber interface.

Implements the same interface as ``StreamTranscriber`` (``feed`` /
``maybe_transcribe`` / ``finalize`` / ``reset``), allowing the controller to
switch modes through configuration.

Internally: sliding windows [left|chunk|right] + timestamp-based commits via
:class:`TimestampCommitter`. No direct typer access; the controller continues
to handle output through ``DeltaTyper.update()``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

from .committer import TimestampCommitter
from .streaming_types import StreamWindowPolicy

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000


def _to_float_audio(data: bytes) -> np.ndarray:
    """Reliably convert s16le bytes to float32 [-1,1], even for odd lengths."""
    if len(data) % 2:
        data = data[:-1]
    return np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0


@dataclass
class WindowedTranscriber:
    """Window-based streaming transcription object."""

    engine: object  # provides transcribe_with_timestamps + transcribe
    interval_s: float = 1.0
    min_audio_s: float = 3.0
    min_final_audio_s: float = 0.5
    #: Maximum segment length for final remainder transcription (seconds).
    #: Long remainders are split to prevent the token budget or encoder memory
    #: from becoming a bottleneck.
    max_final_segment_s: float = 30.0
    sample_rate: int = SAMPLE_RATE
    commit_agreements: int = 2
    left_context_s: float = 4.0
    chunk_s: float = 2.0
    right_context_s: float = 1.0
    vad: object | None = None

    _buffer: bytearray = field(default_factory=bytearray, init=False, repr=False)
    _committer: TimestampCommitter = field(
        default_factory=TimestampCommitter, init=False, repr=False
    )
    _last_partial: str = field(default="", init=False, repr=False)
    _committed_text: str = field(default="", init=False, repr=False)
    _last_transcribed_audio_s: float = field(default=0.0, init=False, repr=False)
    _silence_misses: int = field(default=0, init=False, repr=False)

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------
    @property
    def audio_seconds(self) -> float:
        return len(self._buffer) / (self.sample_rate * 2)

    @property
    def committed_len(self) -> int:
        """Length of the committed text prefix (for DeltaTyper)."""
        return len(self._committed_text)

    @property
    def committed_text(self) -> str:
        return self._committed_text

    @property
    def last_text(self) -> str:
        return self._committed_text + self._last_partial

    def _policy(self) -> StreamWindowPolicy:
        return StreamWindowPolicy(
            left_context_s=self.left_context_s,
            chunk_s=self.chunk_s,
            right_context_s=self.right_context_s,
            min_audio_s=self.min_audio_s,
        )

    # ------------------------------------------------------------------
    # Control
    # ------------------------------------------------------------------
    def reset(self) -> None:
        self._buffer.clear()
        self._committer = TimestampCommitter(commit_agreements=self.commit_agreements)
        self._last_partial = ""
        self._committed_text = ""
        self._last_transcribed_audio_s = 0.0

    def feed(self, chunk: bytes) -> None:
        if chunk:
            self._buffer.extend(chunk)

    def _flush_pending_text(self) -> None:
        """Preserves the last partial in the committed text.

        Called before skipping a pause so that already recognized text is not
        lost when committed_s advances.
        """
        if self._last_partial.strip():
            self._committed_text += self._last_partial.strip() + " "
            self._last_partial = ""

    def maybe_transcribe(self):
        """Transcribes the next window when one is due.

        Returns:
            A ``TranscriptionResult`` containing the FULL text (committed +
            partial) and ``committed_len``, or None.
        """
        # Fix 4: Enforce interval_s; do not transcribe on every tick.
        if self._last_transcribed_audio_s > 0:
            if self.audio_seconds - self._last_transcribed_audio_s < self.interval_s:
                return None

        policy = self._policy()
        window = policy.next_window(self._committer.committed_s, self.audio_seconds)
        if window is None:
            return None

        start_byte = int(window.start * self.sample_rate * 2)
        end_byte = int(window.end * self.sample_rate * 2)
        slice_data = bytes(self._buffer[start_byte:end_byte])
        if not slice_data:
            return None

        # Fix 5: Run VAD only on the NEW segment (from committed_s), not on
        # the entire slice including left speech context.
        if self.vad is not None:
            committed_byte = int(self._committer.committed_s * self.sample_rate * 2)
            vad_data = bytes(self._buffer[committed_byte:end_byte])
            if vad_data:
                arr = _to_float_audio(vad_data)
                if len(arr) and not self.vad.has_speech(arr):
                    # Fix A (pause stall): After 2 consecutive VAD misses,
                    # this is a real pause -> advance committed_s; otherwise,
                    # the commit point stalls indefinitely and everything
                    # after the pause is lost.
                    self._silence_misses += 1
                    if self._silence_misses >= 2:
                        # Safeguard: skip only when the window contains
                        # absolutely NO speech AND the buffer already extends
                        # farther (otherwise, speech that simply has not been
                        # reached yet would be skipped).
                        self._flush_pending_text()
                        skip_to = min(
                            self._committer.committed_s + self.chunk_s,
                            self.audio_seconds,
                        )
                        # Advance only when the buffer is filled at least
                        # through the window end (no premature skip).
                        if self.audio_seconds >= window.end - 0.01:
                            self._committer.skip_silence(skip_to)
                            log.info(
                                "Pause detected -> committed_s advanced to %.1fs",
                                self._committer.committed_s,
                            )
                        self._silence_misses = 0
                        log.info(
                            "Pause detected -> committed_s advanced to %.1fs",
                            self._committer.committed_s,
                        )
                    else:
                        log.debug(
                            "Window %.1f-%.1fs: VAD silent (miss %d)",
                            window.start, window.end, self._silence_misses,
                        )
                    return None
            # Speech detected -> reset miss counter
            self._silence_misses = 0

        tr = self.engine.transcribe_with_timestamps(slice_data, window.start)
        self._last_transcribed_audio_s = self.audio_seconds
        commit_text, partial = self._committer.update(tr, window.commit_horizon)
        log.debug(
            "Window %.1f-%.1fs -> commit=%r partial=%r",
            window.start, window.end, commit_text.strip(), partial.strip(),
        )

        if commit_text.strip():
            self._committed_text += commit_text.strip() + " "
        self._last_partial = partial.strip()

        from .asr import TranscriptionResult

        # Fix 3 (RC-3): Give DeltaTyper the FULL text (committed + partial),
        # not just the window delta. Otherwise, DeltaTyper.update() discards
        # everything from the 2nd commit onward (len(delta) < committed_len).
        committed_clean = self._committed_text.rstrip()
        full_text = (committed_clean + " " + self._last_partial).strip()
        return TranscriptionResult(
            text=full_text,
            committed_len=len(committed_clean),
        )

    def finalize(self):
        """Final quality pass: transcribe the remainder from committed_s.

        Fix 1 (RC-1): Transcribe the remainder ONLY if it is long enough and
        contains speech. Otherwise, silence/noise (with committed_s==0) would
        be transcribed blindly and hallucinated (UPPERCASE, semicolons).
        """
        from .asr import TranscriptionResult

        # P1 review fix: ALWAYS combine the remainder with the committed
        # prefix so DeltaTyper receives the FULL text (otherwise, data loss).
        # committed_len consistently = len(rstrip(prefix)).
        def _full(text: str) -> TranscriptionResult:
            committed_clean = self._committed_text.rstrip()
            full = (committed_clean + " " + text).strip() if text else committed_clean
            return TranscriptionResult(
                text=full, committed_len=len(committed_clean)
            )

        committed_byte = int(self._committer.committed_s * self.sample_rate * 2)
        rest = bytes(self._buffer[committed_byte:])
        if not rest:
            return _full(self._last_partial)

        # 1) Length guard: do not transcribe a remainder that is too short.
        min_bytes = int(self.min_final_audio_s * self.sample_rate * 2)
        if len(rest) < min_bytes:
            log.debug("finalize: remainder %.2fs too short -> skipped",
                      len(rest) / (self.sample_rate * 2))
            return _full(self._last_partial)

        # 2) VAD guard: Discard only SHORT remainders (< 2s) when VAD is false.
        #    A long remainder is very likely real speech; data loss is worse
        #    than the risk of hallucination (RC-B1).
        if self.vad is not None and len(rest) < 2 * self.sample_rate * 2:
            arr = _to_float_audio(rest)
            if len(arr) and not self.vad.has_speech(arr):
                log.info("finalize: short remainder is silence -> skipped")
                return _full(self._last_partial)
        elif self.vad is not None and len(rest) >= 2 * self.sample_rate * 2:
            # Long remainder: transcribe even if VAD is false, but warn.
            arr = _to_float_audio(rest)
            if len(arr) and not self.vad.has_speech(arr):
                log.warning(
                    "finalize: long remainder (%.1fs) is silent according to VAD; "
                    "transcribing anyway (avoid data loss)",
                    len(rest) / (self.sample_rate * 2),
                )

        log.debug("finalize: transcribing remainder %.2fs",
                  len(rest) / (self.sample_rate * 2))

        # RC-A2 fix: Transcribe the remainder with left context and timestamps
        # instead of without context (prevents UPPERCASE at the start of a
        # remainder in the middle of a sentence).
        # Fix B: Split very long remainders into <=30s segments so neither the
        # token budget nor encoder memory becomes a bottleneck.
        ctx_byte = int(self.left_context_s * self.sample_rate * 2)
        slice_start = max(0, committed_byte - ctx_byte)
        max_seg_bytes = int(self.max_final_segment_s * self.sample_rate * 2)

        try:
            texts: list[str] = []
            seg_start = slice_start
            total = len(self._buffer)
            while seg_start < total:
                seg_end = min(seg_start + max_seg_bytes, total)
                seg_data = bytes(self._buffer[seg_start:seg_end])
                offset = seg_start / (self.sample_rate * 2)
                tr = self.engine.transcribe_with_timestamps(seg_data, offset)
                rest_tokens = [
                    t for t in tr.tokens
                    if t.start >= self._committer.committed_s - 1e-3
                ]
                seg_text = "".join(t.text for t in rest_tokens).strip()
                if seg_text:
                    texts.append(seg_text)
                # next segment without left-context overlap
                seg_start = seg_end

            text = " ".join(texts).strip()
            if not text:
                # Fall back to whole-transcribe if no timestamps were returned.
                result = self.engine.transcribe(rest)
                text = getattr(result, "text", str(result)).strip()
        except Exception as exc:  # noqa: BLE001
            log.warning("finalize: timestamp path failed (%s), falling back", exc)
            result = self.engine.transcribe(rest)
            text = getattr(result, "text", str(result)).strip()

        return _full(text)