"""StreamingAsrSession: orchestrates windowed streaming.

Maintains a ring buffer, feeds audio into it, transcribes one window per
tick, and commits only stable tokens. On release, the remainder is
transcribed in a final pass.
"""
from __future__ import annotations

import logging
from typing import Protocol

import numpy as np

from .committer import TimestampCommitter
from .streaming_types import StreamWindowPolicy, Token, Transcription
from .vad import VadGate

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000


class Engine(Protocol):
    def transcribe_with_timestamps(self, data: bytes, window_offset_s: float = 0.0) -> Transcription: ...

    def transcribe(self, data: bytes, initial_prompt: str = "") -> object: ...


class Typer(Protocol):
    def commit(self, text: str) -> None: ...

    def revise_partial(self, old: str, new: str) -> None: ...


class StreamingAsrSession:
    """Connects the audio ring buffer, windowing policy, committer, and typer."""

    def __init__(
        self,
        engine: Engine,
        typer: Typer,
        *,
        vad: VadGate | None = None,
        policy: StreamWindowPolicy | None = None,
        sample_rate: int = SAMPLE_RATE,
    ) -> None:
        self.engine = engine
        self.typer = typer
        self.vad = vad
        self.policy = policy or StreamWindowPolicy()
        self.sample_rate = sample_rate
        self.committer = TimestampCommitter()
        self._buffer = bytearray()
        self._last_partial = ""

    @property
    def audio_seconds(self) -> float:
        return len(self._buffer) / (self.sample_rate * 2)

    def on_audio(self, chunk: bytes) -> None:
        """Adds audio data to the ring buffer."""
        if chunk:
            self._buffer.extend(chunk)

    def on_tick(self) -> None:
        """Transcribes the next window when one is due."""
        audio_len = self.audio_seconds
        window = self.policy.next_window(self.committer.committed_s, audio_len)
        if window is None:
            return

        # Audio slice for the window.
        start_byte = int(window.start * self.sample_rate * 2)
        end_byte = int(window.end * self.sample_rate * 2)
        slice_data = bytes(self._buffer[start_byte:end_byte])

        if not slice_data:
            return

        # VAD gate: transcribe only when speech is present.
        if self.vad is not None:
            arr = np.frombuffer(slice_data, dtype=np.int16).astype(np.float32) / 32768.0
            if not self.vad.has_speech(arr):
                return

        tr = self.engine.transcribe_with_timestamps(slice_data, window.start)
        commit_text, partial = self.committer.update(tr, window.commit_horizon)

        if commit_text:
            self.typer.commit(commit_text)
        if partial != self._last_partial:
            self.typer.revise_partial(self._last_partial, partial)
            self._last_partial = partial

    def on_release(self) -> None:
        """Final quality pass: transcribe the remainder from committed_s."""
        committed_byte = int(self.committer.committed_s * self.sample_rate * 2)
        rest = bytes(self._buffer[committed_byte:])
        if not rest:
            return
        result = self.engine.transcribe(rest)
        text = result.text if hasattr(result, "text") else str(result)
        if text.strip():
            self.typer.revise_partial(self._last_partial, text.strip())
            self._last_partial = text.strip()

    def reset(self) -> None:
        self._buffer.clear()
        self.committer = TimestampCommitter()
        self._last_partial = ""