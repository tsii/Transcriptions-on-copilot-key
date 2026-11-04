"""Unit tests for tools/focus_check.py (F3 one-shot diagnostic)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parent.parent / "tools" / "focus_check.py"
_spec = importlib.util.spec_from_file_location("focus_check", _PATH)
focus_check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(focus_check)


class _Proc:
    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


def _fake_run(state: str, name_ok: bool, focus_doc: str | None,
              errors: str = ""):
    def run(cmd, *a, **kw):
        if cmd[0] == "gnome-extensions":
            return _Proc(f"stt-button-focus@salmon.io\n  State: {state}\n")
        if cmd[0] == "busctl" and "status" in cmd:
            return _Proc("PID=1\n", 0 if name_ok else 1)
        if cmd[0] == "busctl" and "call" in cmd:
            return _Proc(focus_doc or "", 0 if focus_doc else 1)
        if cmd[0] == "gdbus":
            return _Proc(errors)
        raise AssertionError(cmd)
    return run


def test_ok(monkeypatch, capsys) -> None:
    monkeypatch.setattr(focus_check.subprocess, "run", _fake_run(
        "ACTIVE", True, '{"type":"ss","data":["firefox","x"]}'))
    assert focus_check.main([]) == 0
    out = capsys.readouterr().out
    assert "OK" in out and "other" in out


def test_error_zombie(monkeypatch, capsys) -> None:
    monkeypatch.setattr(focus_check.subprocess, "run", _fake_run(
        "ERROR", False, None,
        "(['An object is already exported for the interface'],)"))
    assert focus_check.main([]) == 2
    out = capsys.readouterr().out
    assert "already exported" in out
    assert "Zombie" in out and "Logout" in out


def test_name_missing(monkeypatch, capsys) -> None:
    monkeypatch.setattr(focus_check.subprocess, "run", _fake_run(
        "ACTIVE", False, None))
    assert focus_check.main([]) == 1
    assert "not on the bus" in capsys.readouterr().out


def test_quiet_ok_prints_nothing(monkeypatch, capsys) -> None:
    monkeypatch.setattr(focus_check.subprocess, "run", _fake_run(
        "ACTIVE", True, '{"type":"ss","data":["none",""]}'))
    assert focus_check.main(["--quiet"]) == 0
    assert capsys.readouterr().out == ""


def test_missing_binaries_do_not_crash(monkeypatch, capsys) -> None:
    def run(cmd, *a, **kw):
        raise FileNotFoundError(cmd[0])
    monkeypatch.setattr(focus_check.subprocess, "run", run)
    assert focus_check.main([]) in (1, 2)
