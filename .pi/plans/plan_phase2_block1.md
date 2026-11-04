# Phase 2 – Block 1 (A1): Cooperative cancel hook in `ParakeetEngine`

Baseline: HEAD `52d3eac`, full suite green. `test_engine_error_aborts` is flaky under load (it passes when rerun alone) and is not part of this block.

## Goal
Another thread can stop `ParakeetEngine` (HF/FP32 path) **while `generate()` is running**:
- `request_cancel()` sets a `threading.Event`.
- Forward pre-hooks on the 24 encoder layers (`self._model.encoder.layers`; `ParakeetEncoder.layers`, `modeling_parakeet.py:551`) raise `InferenceCancelled` while the event is set.
- The encoder dominates the cost, so an abort lands after ≤ 1 layer (≈ tick/24).
- `clear_cancel()` resets the event.
- The ONNX/int8 path is **not** cancellable. That is documented, and it is not the current production path because FP32/HF is active.

### Decision for A: abort the live tick (chosen) vs. a second model instance (rejected)
| | Second instance | Cooperative abort (**chosen**) |
|---|---|---|
| RAM | +~2.5 GB (0.6B × 4 B FP32 ≈ 2.4 GB plus activations). Machine: 30 GB total, ~11 GB available → technically feasible | ±0 (one `Event`, 24 hooks) |
| CPU | `nproc`=8. Live and final would compute **at the same time** and share the 8 threads (`torch.set_num_threads`, asr.py:216) → the final runs ~1.5–2× slower. The wait is saved, but net p95 is uncertain | The final gets the full CPU immediately; the wait drops from ≤ 1 tick (up to ~3 s on a 15 s buffer) to ≤ 1 layer |
| Invariants | `_exec_lock` would have to be split per engine (DeltaTyper serialization) → high risk | `_exec_lock → _lock` unchanged; nothing runs in parallel |
| Effort | Engine factory, load time ×2, lock refactoring | ~40 LOC engine + ~15 StreamTranscriber + ~20 controller |

**Verdict:** abort. It gives the best benefit per unit of effort, costs 0 MB RAM, and keeps every invariant.

## Test first – `tests/test_engine_cancel.py` (new)
```python
"""Tests for the cooperative cancel hook of ParakeetEngine (Phase 2, A1)."""
from __future__ import annotations

import threading

import pytest

torch = pytest.importorskip("torch")

from stt_button.asr import InferenceCancelled, ParakeetEngine  # noqa: E402


class _Encoder(torch.nn.Module):
    def __init__(self) -> None:
        super().__init__()
        self.layers = torch.nn.ModuleList(torch.nn.Linear(4, 4) for _ in range(3))

    def forward(self, x):
        for layer in self.layers:
            x = layer(x)
        return x


class _Model(torch.nn.Module):
    def __init__(self) -> None:
        super().__init__()
        self.encoder = _Encoder()


def _engine_with_stub() -> ParakeetEngine:
    engine = ParakeetEngine()
    engine._model = _Model()
    engine._install_cancel_hooks()
    return engine


def test_forward_runs_when_not_cancelled() -> None:
    engine = _engine_with_stub()
    assert engine._model.encoder(torch.zeros(1, 4)).shape == (1, 4)


def test_request_cancel_aborts_next_layer() -> None:
    engine = _engine_with_stub()
    engine.request_cancel()
    with pytest.raises(InferenceCancelled):
        engine._model.encoder(torch.zeros(1, 4))


def test_clear_cancel_reenables_inference() -> None:
    engine = _engine_with_stub()
    engine.request_cancel()
    engine.clear_cancel()
    engine._model.encoder(torch.zeros(1, 4))  # must not raise


def test_cancel_from_other_thread_mid_forward() -> None:
    engine = _engine_with_stub()
    entered = threading.Event()
    go_on = threading.Event()

    def slow_hook(_module, _args):
        entered.set()
        go_on.wait(timeout=2.0)

    engine._model.encoder.layers[0].register_forward_pre_hook(slow_hook)
    errors: list[BaseException] = []

    def run() -> None:
        try:
            engine._model.encoder(torch.zeros(1, 4))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=run)
    t.start()
    assert entered.wait(timeout=2.0)
    engine.request_cancel()
    go_on.set()
    t.join(timeout=2.0)
    assert errors and isinstance(errors[0], InferenceCancelled)


def test_hooks_installed_once() -> None:
    engine = _engine_with_stub()
    engine._install_cancel_hooks()  # idempotent
    assert len(engine._cancel_hook_handles) == 3


def test_engine_without_model_cancel_is_noop() -> None:
    engine = ParakeetEngine()
    engine.request_cancel()
    engine.clear_cancel()
```

## Expected RED
`ImportError: cannot import name 'InferenceCancelled' from 'stt_button.asr'` (collection error, 1 error).

## Implementation (before → after)

**1. `src/stt_button/asr.py:17-19` imports**
```python
# before
import logging
import os
import re
# after
import logging
import os
import re
import threading
```

**2. `src/stt_button/asr.py:64`. Insert after `_token_budget`, before `@dataclass(frozen=True)` at 66:**
```python
class InferenceCancelled(Exception):
    """Raised inside engine inference when a cooperative cancel was requested.

    Only live ticks are ever cancelled (Phase 2, idea A): on release the
    controller cancels a live tick whose result would be discarded anyway,
    so the finalizer does not queue behind it on _exec_lock.
    """
```

**3. `src/stt_button/asr.py:183-185` (`ParakeetEngine.__init__`)**
```python
# before
        self._model = None
        self._processor = None
        self._onnx_model = None
# after
        self._model = None
        self._processor = None
        self._onnx_model = None
        #: Phase 2 (A): cooperative cancel, checked before every encoder layer.
        self._cancel_event = threading.Event()
        self._cancel_hook_handles: list = []
```

**4. `src/stt_button/asr.py:214-217` (end of `load()`)**
```python
# before
        if self.device == "cpu":
            torch.set_num_threads(self.cpu_threads)
        log.info("Parakeet model loaded.")
# after
        if self.device == "cpu":
            torch.set_num_threads(self.cpu_threads)
        self._install_cancel_hooks()
        log.info("Parakeet model loaded.")
```

**5. New methods after `load()`, before `def _load_int8` (asr.py:230):**
```python
    def _install_cancel_hooks(self) -> None:
        """Register one forward pre-hook per encoder layer (idempotent).

        The hook raises InferenceCancelled while the cancel event is set;
        generate() unwinds like on any exception (each call builds its own
        tensors, so no shared state is left half-updated).
        """
        if self._cancel_hook_handles or self._model is None:
            return
        layers = getattr(getattr(self._model, "encoder", None), "layers", None)
        if layers is None:
            log.debug("cancel hooks: no encoder.layers - inference not cancellable")
            return

        def _check(_module, _args):
            if self._cancel_event.is_set():
                raise InferenceCancelled("live inference cancelled")

        self._cancel_hook_handles = [
            layer.register_forward_pre_hook(_check) for layer in layers
        ]

    def request_cancel(self) -> None:
        """Ask a running HF inference to stop at the next encoder layer."""
        self._cancel_event.set()

    def clear_cancel(self) -> None:
        """Re-enable inference after a cancel."""
        self._cancel_event.clear()
```

**6. ONNX path, asr.py:279.** Add a comment above `onnx_model = getattr(...)`: `# Not cancellable (Phase 2 A): an ORT run is atomic.`

## GREEN
```bash
nice -n 19 .venv/bin/python -m pytest tests/test_engine_cancel.py -q
nice -n 19 .venv/bin/python -m pytest -q
```

## Abort criterion
Stop if either of these holds:
- The real HF model has no `encoder.layers`, or it has fewer than 10. Check with `.venv/bin/python -c "from transformers import AutoModelForTDT as M; print(len(M.from_pretrained('nvidia/parakeet-tdt-0.6b-v3').encoder.layers))"`, which is expected to print 24.
- The slow test in block 3 shows that a run after a cancel deviates from an uncancelled run.

If so, stop idea A completely and document it like OPT2 (CHANGELOG + ABSCHLUSSBERICHT).
