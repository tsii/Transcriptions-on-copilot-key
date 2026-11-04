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
