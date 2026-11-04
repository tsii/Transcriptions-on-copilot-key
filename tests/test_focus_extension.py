"""Runs the gjs offline test of the GNOME Shell focus extension (F1)."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(
    shutil.which("gjs") is None or shutil.which("dbus-run-session") is None,
    reason="gjs/dbus-run-session not installed",
)
def test_extension_lifecycle_and_focus_hardening() -> None:
    proc = subprocess.run(
        ["dbus-run-session", "--", "gjs", "-m", "tools/test_focus_extension.js"],
        cwd=ROOT, env={**os.environ, "GIO_USE_VFS": "local"}, capture_output=True, text=True, timeout=30,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "OK" in proc.stdout.splitlines(), out
