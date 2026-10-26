"""Tests für den SileroVadGate (Schritt 5)."""
from __future__ import annotations

import numpy as np

from stt_button.vad import SileroVadGate


class TestSileroVadGate:
    def test_short_audio_is_not_speech(self) -> None:
        gate = SileroVadGate()
        assert gate.has_speech(np.zeros(8000, dtype=np.float32)) is False

    def test_silence_is_not_speech(self) -> None:
        gate = SileroVadGate()
        assert gate.has_speech(np.zeros(16000, dtype=np.float32)) is False

    def test_interface_present(self) -> None:
        gate = SileroVadGate()
        assert hasattr(gate, "has_speech")