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
