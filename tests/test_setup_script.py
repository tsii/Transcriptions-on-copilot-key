"""qwen-xpu B5: tools/setup_qwen_xpu.sh (user-space XPU venv)."""
from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools" / "setup_qwen_xpu.sh"
BUILD_DEB = ROOT / "tools" / "build-deb.sh"


def _postinst_block() -> str:
    s = BUILD_DEB.read_text()
    start = s.index("<<'POSTINST'")
    end = s.index("POSTINST", start + 12)
    return s[start:end]


def test_script_exists_and_parses() -> None:
    assert SCRIPT.is_file()
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def test_script_pins_and_indexes() -> None:
    s = SCRIPT.read_text()
    assert "--index-url https://download.pytorch.org/whl/xpu" in s
    assert "torch==2.14.1+xpu" in s
    assert "curl -4" in s


def test_script_has_no_sudo_or_target() -> None:
    code = "\n".join(l for l in SCRIPT.read_text().splitlines()
                     if not l.lstrip().startswith(("#", "echo")))
    assert "sudo" not in code
    assert "--target" not in code
    assert ".venv/bin/pip install" not in code  # never touches production venv


def test_gitignore_lists_xpu_venv() -> None:
    assert ".venv-xpu/" in (ROOT / ".gitignore").read_text().splitlines()


# --- V6: postinst hint guides the user-site install (Weg B) ------------------

def test_build_deb_script_parses() -> None:
    assert BUILD_DEB.is_file()
    subprocess.run(["bash", "-n", str(BUILD_DEB)], check=True)


def test_postinst_mentions_break_system_packages() -> None:
    assert "--break-system-packages" in _postinst_block()


def test_postinst_has_no_venv_hint() -> None:
    assert "venv" not in _postinst_block()
