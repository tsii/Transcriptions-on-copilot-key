"""Deterministically generate the 'you_lose' sound asset.

Creates a short (~0.45 s) two-tone descending, quiet WAV file at
``src/stt_button/sounds/you_lose.wav`` using only numpy and the standard
library. The output is fully deterministic: identical calls produce
byte-identical files (no randomness, no time or locale dependence).

Run:  .venv/bin/python tools/make_you_lose.py
"""

from pathlib import Path

import wave

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "src" / "stt_button" / "sounds" / "you_lose.wav"

SAMPLE_RATE = 44100
DUR = 0.45
# Two falling tones (Hz), half the duration each.
FREQS = (880.0, 587.33)  # A5 -> D5
AMPLITUDE = 0.08  # quiet
FADE = 0.01  # seconds of fade-in/out per tone to avoid clicks


def _tone(freq: float, dur: float) -> np.ndarray:
    n = int(round(SAMPLE_RATE * dur))
    t = np.arange(n, dtype=np.float64) / SAMPLE_RATE
    tone = AMPLITUDE * np.sin(2.0 * np.pi * freq * t)
    # Linear fade-in/out window (deterministic).
    fade_n = max(1, int(round(SAMPLE_RATE * FADE)))
    env = np.ones(n, dtype=np.float64)
    env[:fade_n] = np.linspace(0.0, 1.0, fade_n, endpoint=True)
    env[-fade_n:] = np.linspace(1.0, 0.0, fade_n, endpoint=True)
    return tone * env


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    seg = [
        _tone(FREQS[0], DUR / 2.0),
        _tone(FREQS[1], DUR / 2.0),
    ]
    samples = np.concatenate(seg).astype(np.float64)
    # Convert to 16-bit PCM without introducing dither or randomness.
    pcm = np.round(samples * 32767.0).astype("<i2")
    with wave.open(str(OUT), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm.tobytes())
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes, {len(pcm) / SAMPLE_RATE:.3f} s)")


if __name__ == "__main__":
    main()
