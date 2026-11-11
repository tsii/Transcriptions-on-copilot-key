"""V8 wiring guard: no runtime path uses bootstrap/venv (Weg B).

Each of the six seams (launcher, __main__, unit template, control,
postinst, bootstrap.py) is checked with its own named failure message.
Also pins tools/deps_gate.py as a thin shell (V4a).
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

import stt_button.bootstrap as bootstrap_mod
from stt_button.__main__ import _cmd_setup

ROOT = Path(__file__).resolve().parent.parent


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_launcher_system_python_no_bootstrap():
    text = _read("systemd/stt-button-launcher.sh")
    assert "bootstrap" not in text, (
        "systemd/stt-button-launcher.sh still mentions 'bootstrap'"
    )
    assert "venv" not in text, "launcher still mentions 'venv'"
    assert "exec /usr/bin/python3 -m stt_button" in text, (
        "launcher no longer execs /usr/bin/python3 -m stt_button"
    )


def test_cmd_setup_body_has_no_bootstrap():
    src = inspect.getsource(_cmd_setup)
    assert "bootstrap" not in src, (
        "src/stt_button/__main__.py: _cmd_setup still references bootstrap"
    )
    main_text = _read("src/stt_button/__main__.py")
    assert '"bootstrap"' in main_text, (
        "__main__.py must keep the 'stt-button bootstrap' subcommand"
    )


def test_unit_template_no_venv_exec_placeholder():
    text = _read("systemd/stt-button.service")
    assert "venv" not in text, "systemd/stt-button.service mentions 'venv'"
    assert "ExecStart=@EXEC@" in text, "unit template lost ExecStart=@EXEC@"


def test_control_no_venv_dep_selfhealing():
    text = _read("packaging/debian/control")
    depends = re.search(r"^Depends:.*$", text, re.M).group(0)
    assert "python3-venv" not in depends, "control Depends contains python3-venv"
    assert "python3-pip" in depends, "control Depends lost python3-pip"
    assert "venv" not in text, "control description mentions 'venv'"
    assert "self-healing" not in text.lower(), "control mentions 'self-healing'"


def test_postinst_no_venv_selfhealing():
    text = _read("tools/build-deb.sh")
    postinst = re.search(
        r"<<'POSTINST'\n(.*?)\nPOSTINST", text, re.S
    ).group(1)
    assert "--break-system-packages" in postinst, (
        "postinst text in tools/build-deb.sh lacks --break-system-packages"
    )
    assert "venv" not in postinst, "postinst text mentions 'venv'"
    assert "self-healing" not in postinst.lower(), (
        "postinst text mentions 'self-healing'"
    )


def test_bootstrap_module_importable_dev_only():
    assert "dev" in inspect.getdoc(bootstrap_mod).lower() and (
        "only" in inspect.getdoc(bootstrap_mod).lower()
    ), "bootstrap.py docstring must state it is dev-only"


def test_deps_gate_is_thin_shell():
    text = _read("tools/deps_gate.py")
    assert "from stt_button.deps_gate import main" in text, (
        "tools/deps_gate.py must forward to stt_button.deps_gate"
    )
    assert not re.search(r"^\s*def ", text, re.M), (
        "tools/deps_gate.py must contain no own logic (thin shell, V4a)"
    )
