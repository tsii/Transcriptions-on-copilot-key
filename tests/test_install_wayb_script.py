"""Tests for the Way B install script (release 0.2.1).

Validates syntax, dry-run sudo-safety, and the key safety strings.
Perform NO installation, NO sudo/apt/pip/systemctl/dpkg and NO network.
"""

import os
import re
import stat
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / ".pi" / "tools" / "install_wayb_0.2.1.sh"


def test_bash_syntax_ok():
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def _make_fake_sudo(bindir: Path, marker: Path) -> None:
    sudo = bindir / "sudo"
    sudo.write_text("#!/bin/sh\necho called >> %s\nexit 1\n" % marker)
    sudo.chmod(sudo.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def test_dry_run_does_not_call_sudo(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    marker = tmp_path / "sudo.called"
    _make_fake_sudo(bindir, marker)

    home = tmp_path / "home"
    home.mkdir()

    env = dict(os.environ)
    env["PATH"] = "%s:%s" % (bindir, env["PATH"])
    env["HOME"] = str(home)

    result = subprocess.run(
        ["bash", str(SCRIPT), "--dry-run"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert not marker.exists(), "dry-run must never invoke sudo"


def test_key_strings_present_and_no_rm_rf():
    text = SCRIPT.read_text()
    for needle in (
        "set -euo pipefail",
        "--break-system-packages",
        "https://download.pytorch.org/whl/cpu",
        "--purge",
        ".bak",
        "daemon-reload",
    ):
        assert needle in text, "missing key string: %s" % needle
    assert "rm -rf" not in text


def test_gate_level_check_runs_before_r5_install():
    text = SCRIPT.read_text()
    level_pos = text.find("not Level 0")
    install_pos = text.find('apt-get install -y "$DEB"')
    assert level_pos != -1, "gate Level-0 check missing"
    assert install_pos != -1, "R5 apt install line missing"
    assert level_pos < install_pos, "gate Level check must precede R5 install"


def test_rollback_branch_has_no_purge():
    text = SCRIPT.read_text()
    m = re.search(r"do_rollback\(\)\s*\{.*?\n\}", text, flags=re.S)
    assert m, "rollback function not found"
    body = m.group(0)
    assert "cp -a" in body
    assert "daemon-reload" in body
    assert "dpkg --purge" not in body


def test_permissive_is_opt_in_not_default():
    """--permissive must be explicit; it is off unless the flag is passed."""
    text = SCRIPT.read_text()
    assert "PERMISSIVE=0" in text
    assert "--permissive) PERMISSIVE=1" in text


def test_permissive_scoped_to_pyaudio_only():
    """The permissive acceptance must gate on exactly the pyaudio finding:
    one 'not importable' line matching 'pyaudio' and zero version mismatches.
    """
    text = SCRIPT.read_text()
    assert 'not importable: pyaudio' in text
    assert 'version mismatch:' in text
    assert "n_imp" in text and "n_ver" in text
    assert 'pw-record' in text  # justification for tolerating pyaudio


def test_dry_run_permissive_does_not_call_sudo(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    marker = tmp_path / "sudo.called"
    _make_fake_sudo(bindir, marker)

    home = tmp_path / "home"
    home.mkdir()

    env = dict(os.environ)
    env["PATH"] = "%s:%s" % (bindir, env["PATH"])
    env["HOME"] = str(home)

    result = subprocess.run(
        ["bash", str(SCRIPT), "--dry-run", "--permissive"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert not marker.exists(), "dry-run must never invoke sudo"


def test_r4_and_r5_both_recheck_the_gate():
    """Befund D: R5 must re-run the gate itself instead of trusting R4's
    result, so no state is carried between --only invocations.
    """
    text = SCRIPT.read_text()
    r4 = text.index("step_r4()")
    r5 = text.index("step_r5()")
    r6 = text.index("step_r6()")
    assert "run_gate" in text[r4:r5]
    assert "run_gate" in text[r5:r6]

def test_deb_version_is_overridable_not_baked_only():
    """R5 must not be pinned to one version string: the 0.2.1 build got a
    follow-up fix (0.2.2) and a hardcoded path would silently install the
    launcher with the broken import check again."""
    text = SCRIPT.read_text(encoding="utf-8")
    assert "STT_DEB" in text
    assert re.search(r'DEB="\$\{STT_DEB:-[^"]+\}"', text), \
        "DEB must be ${STT_DEB:-<default>} so the operator can pick the artifact"
