"""Tests for the .deb launcher script (V2: system python, no bootstrap/venv).

`sh -n` plus text checks cover the shape; since E2b the import check is also
EXECUTED against stub modules on a temporary PYTHONPATH, so a module list that
does not match the code fails here instead of killing the packaged service. The
tests never start the service, never run `stt-button setup`, and never touch the
host environment.
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


# --- E2/E3 (plan_review_sipgate_20261111): the import check must be correct
# AND actually executed. The V2-era tests above only read the file, which is how
# `sounddevice` (never used by the code) survived into the packaged launcher:
# after the unit switch the service would have died with ModuleNotFoundError.

import os
import re
import sys


def _import_check_code() -> str:
    """Return the code the launcher runs via `python3 -c '...'`."""
    match = re.search(r"-c '([^']+)'", _text())
    assert match, "launcher has no -c import check"
    return match.group(1)


def _checked_modules(code: str) -> set:
    return {name.strip() for name in
            re.search(r"import ([^;]+)", code).group(1).split(",")}


def test_import_check_requires_only_modules_the_code_imports():
    # `sounddevice` is not imported anywhere in src/ (capture is `pw-record`),
    # `faster_whisper`/`onnx_asr`/`requests` are pulled in by stt_button itself.
    assert _checked_modules(_import_check_code()) == {"numpy", "stt_button"}


def test_import_check_executes_with_stub_modules_only(tmp_path):
    """E2b (ii): stubs for EVERY checked module -> no dependency on the host."""
    code = _import_check_code()
    for module in _checked_modules(code):
        if module == "stt_button":
            package = tmp_path / "stt_button"
            package.mkdir()
            (package / "__init__.py").write_text("", encoding="utf-8")
        else:
            (tmp_path / f"{module}.py").write_text("", encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(tmp_path))
    res = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, env=env)
    assert res.returncode == 0, res.stderr


def test_pip_hint_points_at_the_shipped_requirements():
    text = _text()
    assert "packaging/runtime-requirements.txt" in text
    assert "requirements-stt.txt" not in text
    assert "[stt]" not in text
