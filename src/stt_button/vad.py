"""Silero-VAD gate: filters silent audio tails to save compute and to
avoid hallucinations on pure noise.

Silero-VAD is already available via faster_whisper.vad (ONNX, CPU-only).
"""
from __future__ import annotations

from typing import Protocol

import numpy as np

from .streaming_types import Transcription  # noqa: F401


class VadGate(Protocol):
    def has_speech(self, audio_tail: np.ndarray) -> bool:
        """True if speech is detected in the audio tail."""


class SileroVadGate:
    """Uses Silero-VAD (faster_whisper.vad) to detect speech."""

    def __init__(self, threshold: float = 0.5) -> None:
        self.threshold = threshold
        self._model = None

    def _load(self) -> None:
        if self._model is not None:
            return
        from faster_whisper.vad import get_vad_model

        self._model = get_vad_model()

    def has_speech(self, audio_tail: np.ndarray) -> bool:
        """Check whether the audio tail (float32, 16 kHz) contains speech."""
        if len(audio_tail) < 16000:  # < 1s: too short for VAD
            return False
        self._load()
        from faster_whisper.vad import get_speech_timestamps

        # Silero expects float32, 16 kHz
        audio = np.asarray(audio_tail, dtype=np.float32)
        if audio.ndim != 1:
            audio = audio.reshape(-1)
        timestamps = get_speech_timestamps(audio, threshold=self.threshold)
        return len(timestamps) > 0
