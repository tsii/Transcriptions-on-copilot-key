# Phase 2 – Block 3 (A3): Real-model verification of the cancel (slow test)

Depends on blocks 1 and 2.

## Goal
Show with the real Parakeet HF model that:
1. A cancel mid-inference raises `InferenceCancelled` in < 0.3 s.
2. The **next** `transcribe()` on the same buffer gives a result character-identical to an uncancelled reference. That means no corrupted state: no decoder/LSTM state or hooks left in a bad state.

## Test first – `tests/test_engine_cancel_slow.py` (new, `@pytest.mark.slow`)
```python
"""Real-model check of the cooperative cancel (Phase 2, A3). Slow."""
from __future__ import annotations

import threading
import time
import wave
from pathlib import Path

import pytest

pytestmark = pytest.mark.slow

CORPUS = Path.home() / "stt-korpus"


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

    assert errors and isinstance(errors[0], InferenceCancelled)
    assert done_at[0] - t_cancel < 0.3, f"cancel latency {done_at[0] - t_cancel:.3f}s"
    assert engine.transcribe(pcm).text == reference
```

## Expected RED
- Before block 1: ImportError.
- After block 1, with the hooks wired: expected GREEN right away. This block is a **verification block**, so RED is not expected.
- If the test fails, that triggers the block 1 abort criterion.

## Implementation
None. This is a pure verification block.
- If the cancel latency is > 0.3 s (encoder layers too coarse), add an extra hook on `self._model.decoder` (`ParakeetRNNTDecoder`, modeling_parakeet.py:821) with the same `_check` function inside `_install_cancel_hooks`:
```python
        decoder = getattr(self._model, "decoder", None)
        if decoder is not None:
            self._cancel_hook_handles.append(decoder.register_forward_pre_hook(_check))
```
- The block 1 test `test_hooks_installed_once` then expects `3`, because the stub has no decoder. That stays correct.

## GREEN
```bash
nice -n 19 OMP_NUM_THREADS=4 .venv/bin/python -m pytest tests/test_engine_cancel_slow.py -q -m slow
```

## Abort criterion
Stop and drop idea A (OPT2 documentation) if either holds:
- The text after a cancel ≠ the reference.
- The cancel latency is > 0.5 s even with the decoder hook.
