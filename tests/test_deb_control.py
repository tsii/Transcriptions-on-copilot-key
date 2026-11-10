"""Tests for packaging/debian/control (V5: no python3-venv, Weg B user-site install)."""

from pathlib import Path

CONTROL = Path(__file__).resolve().parents[1] / "packaging" / "debian" / "control"


def _depends() -> str:
    for line in CONTROL.read_text().splitlines():
        if line.startswith("Depends:"):
            return line
    raise AssertionError("Depends line not found")


def _description_block() -> str:
    lines = CONTROL.read_text().splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith("Description:"))
    return "\n".join(lines[start:])


def test_no_python3_venv_in_depends():
    deps = [d.strip() for d in _depends().split(":", 1)[1].split(",")]
    assert "python3-venv" not in deps


def test_python3_pip_in_depends():
    deps = [d.strip() for d in _depends().split(":", 1)[1].split(",")]
    assert "python3-pip" in deps


def test_description_contains_user_install():
    assert "--user" in _description_block()


def test_description_contains_no_venv_word():
    desc = _description_block().lower()
    assert "venv" not in desc
