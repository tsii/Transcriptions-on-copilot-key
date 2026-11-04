"""Real-model check of the cooperative cancel (Phase 2, A3). Slow."""
from __future__ import annotations

import os
import threading
import time
import wave
from pathlib import Path

import pytest

CORPUS = Path.home() / "stt-korpus"


def _model_available() -> bool:
    root = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface"))
    return (root / "hub" / "models--nvidia--parakeet-tdt-0.6b-v3").is_dir()


pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not _model_available(), reason="Parakeet model missing"),
]


def _long_wav() -> bytes:
    wavs = sorted(CORPUS.glob("*.wav"), key=lambda p: p.stat().st_size)
    wavs = [p for p in wavs if p.stat().st_size > 15 * 32000]
    if not wavs:
        pytest.skip("no >15s clip in ~/stt-korpus")
    with wave.open(str(wavs[0]), "rb") as w:
        return w.readframes(w.getnframes())


@pytest.fixture(scope="module")
def engine():
    from stt_button.asr import ParakeetEngine

    eng = ParakeetEngine("nvidia/parakeet-tdt-0.6b-v3")
    eng.load()
    if not eng._cancel_hook_handles:
        pytest.skip("int8/ONNX path active - not cancellable")
    return eng


def test_cancel_is_fast_and_leaves_engine_clean(engine) -> None:
    from stt_button.asr import InferenceCancelled

    pcm = _long_wav()
    reference = engine.transcribe(pcm).text

    errors: list[BaseException] = []
    done_at: list[float] = []

    def run() -> None:
        try:
            engine.transcribe(pcm)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        done_at.append(time.monotonic())

    t = threading.Thread(target=run)
    t.start()
    time.sleep(0.3)  # inside the encoder
    t_cancel = time.monotonic()
    engine.request_cancel()
    t.join(timeout=10.0)
    engine.clear_cancel()

    latency = done_at[0] - t_cancel
    print(f"\nA3 cancel latency: {latency * 1000:.1f} ms")
    assert errors and isinstance(errors[0], InferenceCancelled), errors
    assert latency < 0.3, f"cancel latency {latency:.3f}s"
    again = engine.transcribe(pcm).text
    print(f"A3 text identical: {again == reference}")
    assert again == reference
