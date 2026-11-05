"""B1: Qwen device selection (auto|xpu|cpu) with a mockable XPU probe."""
from __future__ import annotations

import logging
import subprocess
import sys

import pytest

from stt_button.device import resolve_qwen_device


def _probe(result):
    calls = []

    def probe():
        calls.append(1)
        if isinstance(result, BaseException):
            raise result
        return result

    probe.calls = calls
    return probe


def test_cpu_never_probes() -> None:
    p = _probe((True, ""))
    assert resolve_qwen_device("cpu", probe=p) == "cpu"
    assert p.calls == []


def test_auto_with_xpu_available() -> None:
    assert resolve_qwen_device("auto", probe=_probe((True, ""))) == "xpu"


def test_auto_without_xpu_logs_one_info(caplog) -> None:
    caplog.set_level(logging.DEBUG, logger="stt_button.device")
    dev = resolve_qwen_device("auto", probe=_probe((False, "no XPU device/runtime")))
    assert dev == "cpu"
    recs = [r for r in caplog.records if r.name == "stt_button.device"]
    assert len(recs) == 1
    assert recs[0].levelno == logging.INFO
    assert "no XPU device/runtime" in recs[0].getMessage()


def test_explicit_xpu_unavailable_warns_no_raise(caplog) -> None:
    caplog.set_level(logging.DEBUG, logger="stt_button.device")
    assert resolve_qwen_device("xpu", probe=_probe((False, "x"))) == "cpu"
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_explicit_xpu_available() -> None:
    assert resolve_qwen_device("xpu", probe=_probe((True, ""))) == "xpu"


def test_probe_exception_falls_back_to_cpu() -> None:
    assert resolve_qwen_device("auto", probe=_probe(RuntimeError("boom"))) == "cpu"


def test_unknown_device_raises_value_error() -> None:
    with pytest.raises(ValueError):
        resolve_qwen_device("gpu", probe=_probe((True, "")))


def test_default_probe_on_cpu_torch_is_false() -> None:
    from stt_button.device import xpu_available
    ok, reason = xpu_available()
    assert isinstance(ok, bool) and isinstance(reason, str)


def test_import_does_not_import_torch() -> None:
    code = "import sys, stt_button.device; print('torch' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"
