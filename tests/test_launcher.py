"""Tests for the .deb launcher script (V2: system python, no bootstrap/venv).

The tests only read the launcher file and run `sh -n` on it. They never
start the service, never run `stt-button setup`, and never execute the
launcher's python step (pip is not installed on this machine - expected).
"""

import subprocess
from pathlib import Path

LAUNCHER = Path("systemd/stt-button-launcher.sh")


def _text() -> str:
    return LAUNCHER.read_text(encoding="utf-8")


def test_sh_n_syntax_ok():
    res = subprocess.run(
        ["sh", "-n", str(LAUNCHER)],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr


def test_no_bootstrap_or_venv_remnants():
    text = _text().lower()
    assert "run_bootstrap" not in text
    assert "venv" not in text


def test_execs_system_python_module():
    assert 'exec /usr/bin/python3 -m stt_button "$@"' in _text()


def test_pythonpath_only_root_src():
    lines = [
        ln.strip()
        for ln in _text().splitlines()
        if "PYTHONPATH" in ln
    ]
    assert lines, "PYTHONPATH line missing"
    for ln in lines:
        assert ln == 'export PYTHONPATH="$ROOT/src"' or ln.startswith(
            'PYTHONPATH="$ROOT/src"'
        ), f"unexpected PYTHONPATH line: {ln}"


def test_import_check_with_pip_hint():
    text = _text()
    # import check of the core modules (python -c 'import ...')
    assert "/usr/bin/python3 -c 'import" in text
    assert "stt_button" in text
    # pip hint for the user on missing dependencies
    assert "pip" in text.lower()
    assert "stt-button[stt]" in text or "requirements" in text
