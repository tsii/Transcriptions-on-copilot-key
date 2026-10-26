"""Silero-VAD-Gate: filtert stille Audio-Tails, um Rechenlast zu sparen und
Halluzinationen auf reinem Rauschen zu vermeiden.

Silero-VAD ist bereits über faster_whisper.vad vorhanden (ONNX, CPU-only).
"""
from __future__ import annotations

from typing import Protocol

import numpy as np

from .streaming_types import Transcription  # noqa: F401


class VadGate(Protocol):
    def has_speech(self, audio_tail: np.ndarray) -> bool:
        """True, wenn im Audio-Tail Sprache erkannt wird."""


class SileroVadGate:
    """Nutzt Silero-VAD (faster_whisper.vad), um Sprache zu erkennen."""

    def __init__(self, threshold: float = 0.5) -> None:
        self.threshold = threshold
        self._model = None

    def _load(self) -> None:
        if self._model is not None:
            return
        from faster_whisper.vad import get_vad_model

        self._model = get_vad_model()

    def has_speech(self, audio_tail: np.ndarray) -> bool:
        """Prüft, ob der Audio-Tail (float32, 16kHz) Sprache enthält."""
        if len(audio_tail) < 16000:  # < 1s: zu kurz für VAD
            return False
        self._load()
        from faster_whisper.vad import get_speech_timestamps

        # Silero erwartet float32, 16kHz
        audio = np.asarray(audio_tail, dtype=np.float32)
        if audio.ndim != 1:
            audio = audio.reshape(-1)
        timestamps = get_speech_timestamps(audio, threshold=self.threshold)
        return len(timestamps) > 0