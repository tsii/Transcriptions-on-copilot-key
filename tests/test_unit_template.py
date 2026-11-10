"""Tests for the systemd unit template (repo copy, not the live unit)."""

from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[1] / "systemd" / "stt-button.service"


def _content() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def test_no_bootstrap_timeout():
    assert "1800" not in _content()


def test_no_venv_mention():
    assert "venv" not in _content()


def test_hf_home_environment_present():
    assert "Environment=HF_HOME=%h/.cache/huggingface" in _content()


def test_execstart_placeholder_unchanged():
    assert "ExecStart=@EXEC@" in _content()
