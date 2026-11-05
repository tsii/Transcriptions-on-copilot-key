"""Compute-device selection for the Qwen backend (auto | xpu | cpu).

torch is imported lazily inside :func:`xpu_available` only, so importing
this module never pulls in torch (the Parakeet production path stays
torch-free). Resolution never raises for an unavailable XPU: it falls
back to CPU with a log line.
"""
from __future__ import annotations

import logging
from typing import Callable

log = logging.getLogger(__name__)

VALID_QWEN_DEVICES = ("auto", "xpu", "cpu")


def xpu_available() -> tuple[bool, str]:
    """Returns (usable, reason) for an Intel XPU via torch."""
    try:
        import torch
    except Exception:  # noqa: BLE001
        return False, "torch not importable"
    try:
        if not hasattr(torch, "xpu"):
            return False, "torch build has no xpu"
        # is_available() alone is not enough: torch-xpu can report True
        # with zero devices when the GPU compute runtime is missing.
        if not torch.xpu.is_available() or torch.xpu.device_count() < 1:
            return False, f"no XPU device/runtime (torch {torch.__version__})"
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, repr(exc)


def resolve_qwen_device(
    requested: str, probe: Callable[[], tuple[bool, str]] = xpu_available
) -> str:
    """Maps the configured ``qwen_device`` to a concrete torch device."""
    if requested not in VALID_QWEN_DEVICES:
        raise ValueError(f"qwen_device must be one of {VALID_QWEN_DEVICES}, is {requested!r}")
    if requested == "cpu":
        return "cpu"
    try:
        ok, reason = probe()
    except Exception as exc:  # noqa: BLE001
        ok, reason = False, repr(exc)
    if ok:
        log.info("qwen_device=%s: using XPU", requested)
        return "xpu"
    if requested == "xpu":
        log.warning("qwen_device=xpu but XPU unavailable (%s), falling back to CPU", reason)
    else:
        log.info("qwen_device=auto: XPU unavailable (%s), using CPU "
                 "(XPU stack: tools/setup_qwen_xpu.sh -> .venv-xpu)", reason)
    return "cpu"
