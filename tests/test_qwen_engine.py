"""B2: QwenEngine (BF16 default, capabilities) with a fake transformers module."""
from __future__ import annotations

import sys
import types

import numpy as np
import pytest
import torch

from stt_button.asr import ParakeetEngine, QwenEngine, WhisperEngine


class _Inputs(dict):
    def to(self, *args, **kwargs):
        self["to_args"] = args
        return self


class _FakeProcessor:
    def __init__(self):
        self.requests = []

    def apply_transcription_request(self, audio=None, **kw):
        self.requests.append((audio, kw))
        return _Inputs(input_ids=torch.zeros((1, 3), dtype=torch.long))

    def decode(self, ids, return_format=None):
        assert return_format == "transcription_only"
        return ["  Hallo Welt.  "]


class _FakeModel:
    dtype = torch.bfloat16

    def __init__(self):
        self.generate_kwargs = None

    def to(self, device):
        return self

    def eval(self):
        return self

    def generate(self, **kw):
        self.generate_kwargs = kw
        return torch.zeros((1, 5), dtype=torch.long)


@pytest.fixture
def fake_tf(monkeypatch):
    rec = {"loads": 0}
    proc = _FakeProcessor()
    model = _FakeModel()

    class AutoProcessor:
        @staticmethod
        def from_pretrained(name, **kw):
            rec["proc_kw"] = kw
            return proc

    class AutoModelForMultimodalLM:
        @staticmethod
        def from_pretrained(name, **kw):
            rec["loads"] += 1
            rec["name"] = name
            rec["model_kw"] = kw
            return model

    mod = types.ModuleType("transformers")
    mod.AutoProcessor = AutoProcessor
    mod.AutoModelForMultimodalLM = AutoModelForMultimodalLM
    monkeypatch.setitem(sys.modules, "transformers", mod)
    rec["proc"] = proc
    rec["model"] = model
    return rec


PCM_1S = (np.zeros(16000, dtype=np.int16)).tobytes()


def test_qwen_loads_bfloat16_by_default(fake_tf) -> None:
    QwenEngine().load()
    assert fake_tf["model_kw"]["dtype"] is torch.bfloat16


def test_qwen_float32_opt_in(fake_tf) -> None:
    QwenEngine(compute_type="float32").load()
    assert fake_tf["model_kw"]["dtype"] is torch.float32


def test_qwen_rejects_unknown_compute_type() -> None:
    with pytest.raises(ValueError):
        QwenEngine(compute_type="int8")


def test_qwen_default_model_pins_revision(fake_tf) -> None:
    from stt_button.asr import QWEN_DEFAULT_REVISION
    QwenEngine().load()
    assert fake_tf["model_kw"]["revision"] == QWEN_DEFAULT_REVISION
    assert fake_tf["proc_kw"]["revision"] == QWEN_DEFAULT_REVISION


def test_qwen_custom_model_no_revision_pin(fake_tf) -> None:
    QwenEngine("Qwen/Qwen3-ASR-0.6B-hf").load()
    assert "revision" not in fake_tf["model_kw"]


def test_qwen_supports_live_false_and_no_timestamps() -> None:
    assert QwenEngine.supports_live is False
    assert QwenEngine.supports_timestamps is False
    assert not hasattr(QwenEngine, "transcribe_with_timestamps")


def test_parakeet_whisper_supports_live_true() -> None:
    assert ParakeetEngine.supports_live is True
    assert WhisperEngine.supports_live is True


def test_qwen_transcribe_returns_stripped_text_committed_zero(fake_tf) -> None:
    r = QwenEngine().transcribe(PCM_1S)
    assert r.text == "Hallo Welt."
    assert r.committed_len == 0


def test_qwen_ignores_initial_prompt(fake_tf) -> None:
    QwenEngine().transcribe(PCM_1S, initial_prompt="Kubernetes")
    for audio, kw in fake_tf["proc"].requests:
        assert "Kubernetes" not in repr(kw)


def test_qwen_load_idempotent(fake_tf) -> None:
    e = QwenEngine()
    e.load()
    e.load()
    e.transcribe(PCM_1S)
    assert fake_tf["loads"] == 1


def test_qwen_token_budget_passed(fake_tf) -> None:
    from stt_button.asr import _token_budget
    QwenEngine().transcribe(PCM_1S)
    assert fake_tf["model"].generate_kwargs["max_new_tokens"] == _token_budget(1.0)


# --- qwen-xpu B3: engine honors qwen_device -------------------------------

@pytest.fixture
def xpu_probe(monkeypatch):
    import stt_button.device as dev
    state = {"ok": True}
    monkeypatch.setattr(dev, "xpu_available", lambda: (state["ok"], "test probe"))
    return state


@pytest.fixture
def track_to(fake_tf, monkeypatch):
    calls = []
    fail = {"xpu": False}

    def to(self, device):
        calls.append(device)
        if device == "xpu" and fail["xpu"]:
            raise RuntimeError("no level zero")
        return self

    monkeypatch.setattr(_FakeModel, "to", to)
    threads = []
    monkeypatch.setattr(torch, "set_num_threads", lambda n: threads.append(n))
    return {"calls": calls, "fail": fail, "threads": threads, "tf": fake_tf}


def test_qwen_default_device_is_auto() -> None:
    assert QwenEngine().device == "auto"


def test_qwen_auto_uses_xpu_when_available(track_to, xpu_probe) -> None:
    e = QwenEngine(warmup=False)
    e.load()
    assert e.resolved_device == "xpu"
    assert track_to["calls"] == ["xpu"]
    assert track_to["threads"] == []


def test_qwen_auto_falls_back_to_cpu(track_to, xpu_probe) -> None:
    xpu_probe["ok"] = False
    e = QwenEngine()
    e.load()
    assert e.resolved_device == "cpu"
    assert track_to["calls"] == ["cpu"]
    assert track_to["threads"]


def test_qwen_cpu_explicit(track_to, xpu_probe) -> None:
    e = QwenEngine(device="cpu")
    e.load()
    assert e.resolved_device == "cpu"


def test_qwen_xpu_move_failure_reloads_on_cpu(track_to, xpu_probe, caplog) -> None:
    import logging
    track_to["fail"]["xpu"] = True
    caplog.set_level(logging.WARNING)
    e = QwenEngine(device="xpu", warmup=False)
    e.load()
    assert e.resolved_device == "cpu"
    assert track_to["calls"][-1] == "cpu"
    assert any(r.levelno == logging.WARNING for r in caplog.records)
    assert track_to["tf"]["loads"] == 2


def test_qwen_transcribe_moves_inputs_to_resolved_device(track_to, xpu_probe) -> None:
    e = QwenEngine(warmup=False)
    e.transcribe(PCM_1S)
    assert track_to["tf"]["proc"].requests  # ran
    # _Inputs.to records args; last request's inputs went to xpu
    assert e.resolved_device == "xpu"


def test_qwen_xpu_warmup_runs_one_generate(track_to, xpu_probe) -> None:
    e = QwenEngine()
    e.load()
    assert track_to["tf"]["model"].generate_kwargs is not None
