"""B6: tools/model_switch_check.py (fake engine; real model only when cached + opted in)."""
from __future__ import annotations

import importlib.util
import os
import wave
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parent.parent / "tools" / "model_switch_check.py"
_spec = importlib.util.spec_from_file_location("model_switch_check", _PATH)
msc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(msc)

from stt_button.asr import TranscriptionResult  # noqa: E402


def _wav(path: Path, seconds: float) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * int(16000 * seconds))
    return path


class FakeEngine:
    loaded = False

    def __init__(self, text="Hallo."):
        self.text = text

    def load(self):
        self.loaded = True

    def transcribe(self, data, initial_prompt=""):
        return TranscriptionResult(text=self.text)


def test_check_builds_engine_from_config(tmp_path) -> None:
    cfg = tmp_path / "c.toml"
    cfg.write_text('backend = "qwen"\ncompute_type = "bfloat16"\n')
    engine = msc.engine_from_config(cfg)
    assert type(engine).__name__ == "QwenEngine"
    assert engine.loaded is False


def test_check_report_lines(tmp_path) -> None:
    clip = _wav(tmp_path / "a.wav", 2.0)
    rows, ok = msc.run_check(FakeEngine(), [clip], rss_fn=lambda: 1.0)
    assert ok
    line = msc.format_row(rows[0])
    assert "a.wav" in line and "RTF" in line and "Hallo." in line


def test_check_fails_on_rss_gate(tmp_path) -> None:
    clip = _wav(tmp_path / "a.wav", 2.0)
    rows, ok = msc.run_check(FakeEngine(), [clip], rss_fn=lambda: 7.5)
    assert not ok


def test_check_fails_on_rtf_gate_for_20s_class(tmp_path, monkeypatch) -> None:
    clip = _wav(tmp_path / "b.wav", 20.0)
    t = iter([0.0, 40.0])
    rows, ok = msc.run_check(FakeEngine(), [clip], rss_fn=lambda: 1.0,
                             clock=lambda: next(t))
    assert rows[0]["rtf"] == pytest.approx(2.0)
    assert not ok


def test_main_exit_code_nonzero_on_missing_clip(tmp_path, capsys) -> None:
    assert msc.main(["--clips", str(tmp_path / "missing.wav"), "--dry-run"]) == 2


_CACHE = Path.home() / ".cache/huggingface/hub/models--Qwen--Qwen3-ASR-1.7B-hf"


@pytest.mark.skipif(
    not (_CACHE.is_dir() and os.environ.get("STT_REAL_MODEL") == "1"),
    reason="real Qwen model: needs HF cache and STT_REAL_MODEL=1 (~5 GB RAM)",
)
def test_real_qwen_smoke(tmp_path) -> None:
    cfg = tmp_path / "c.toml"
    cfg.write_text('backend = "qwen"\ncompute_type = "bfloat16"\n')
    clip = Path.home() / "stt-korpus" / msc.DEFAULT_CLIPS[0]
    rows, ok = msc.run_check(msc.engine_from_config(cfg), [clip])
    assert ok and rows[0]["text"]


# --- qwen-xpu B6: --device reporting -------------------------------------

def test_engine_from_config_device_override(tmp_path) -> None:
    cfg = tmp_path / "c.toml"
    cfg.write_text('backend = "qwen"\ncompute_type = "bfloat16"\n')
    assert msc.engine_from_config(cfg).device == "auto"
    assert msc.engine_from_config(cfg, device="cpu").device == "cpu"


def test_device_report_line(monkeypatch) -> None:
    import stt_button.device as dev
    monkeypatch.setattr(dev, "xpu_available", lambda: (False, "no XPU device/runtime"))

    class E:
        device = "auto"
        resolved_device = "cpu"

    line = msc.device_report(E())
    assert line == "device: requested=auto resolved=cpu reason=no XPU device/runtime"


def test_device_report_none_for_non_qwen() -> None:
    assert msc.device_report(FakeEngine()) is None


def test_main_dry_run_prints_device(tmp_path, capsys, monkeypatch) -> None:
    import stt_button.device as dev
    monkeypatch.setattr(dev, "xpu_available", lambda: (True, ""))
    cfg = tmp_path / "c.toml"
    cfg.write_text('backend = "qwen"\ncompute_type = "bfloat16"\n')
    clip = _wav(tmp_path / "a.wav", 1.0)
    assert msc.main(["--config", str(cfg), "--device", "xpu", "--clips", str(clip), "--dry-run"]) == 0
    assert "device: requested=xpu" in capsys.readouterr().out


@pytest.mark.skipif(
    os.environ.get("STT_QWEN_XPU_REAL") != "1",
    reason="real XPU run: needs .venv-xpu + GPU runtime and STT_QWEN_XPU_REAL=1",
)
def test_real_qwen_xpu(tmp_path) -> None:
    cfg = tmp_path / "c.toml"
    cfg.write_text('backend = "qwen"\ncompute_type = "bfloat16"\nqwen_device = "xpu"\n')
    eng = msc.engine_from_config(cfg)
    eng.load()
    assert eng.resolved_device == "xpu"
    clip = Path.home() / "stt-korpus" / "20261028-211208_305138.wav"
    rows, ok = msc.run_check(eng, [clip])
    assert rows[0]["text"]
