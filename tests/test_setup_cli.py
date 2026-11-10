"""Tests for ``stt-button setup`` (V4): no venv build, guarded unit install.

V4 rework (plan_no_venv_B): ``_cmd_setup`` verifies the runtime via
``deps_gate`` instead of building a venv, seeds the config, and only
writes the systemd user unit with an explicit ``--install-unit`` —
never silently over an existing unit with a different ExecStart.

All tests isolate ``$HOME`` into ``tmp_path``; the operator's real
``~/.config/systemd/user/stt-button.service`` is never written (its
existence+hash is asserted unchanged).
"""

import argparse
import hashlib
import inspect
from pathlib import Path

from stt_button import __main__ as cli
from stt_button import deps_gate


def _real_unit_path() -> Path:
    """Path of the REAL operator unit (before any HOME monkeypatch)."""
    import os
    return Path(os.environ.get("HOME", "/root")) / ".config/systemd/user/stt-button.service"


def _hash(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run_setup(monkeypatch, tmp_path, install_unit=False, gate_level=0):
    """Run _cmd_setup with $HOME isolated into tmp_path; gate stubbed."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(deps_gate, "main", lambda: gate_level)
    ns = argparse.Namespace(install_unit=install_unit)
    rc = cli._cmd_setup(ns)
    return rc, home


class TestNoSilentUnitWrite:
    def test_setup_without_flag_writes_no_unit(self, monkeypatch, tmp_path):
        rc, home = _run_setup(monkeypatch, tmp_path, install_unit=False)
        unit = home / ".config/systemd/user/stt-button.service"
        assert rc == 0
        assert not unit.exists()

    def test_setup_dry_run_message_no_file(self, monkeypatch, tmp_path, capsys):
        _run_setup(monkeypatch, tmp_path, install_unit=False)
        unit = tmp_path / "home/.config/systemd/user/stt-button.service"
        out = capsys.readouterr().out
        assert not unit.exists()
        assert "unit" in out.lower()


class TestUnitGuard:
    def test_diverging_existing_unit_aborts_exit1_unchanged(self, monkeypatch, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        unit_dir = home / ".config/systemd/user"
        unit_dir.mkdir(parents=True)
        unit = unit_dir / "stt-button.service"
        original = "[Service]\nExecStart=/something/else run\n"
        unit.write_text(original)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(deps_gate, "main", lambda: 0)
        rc = cli._cmd_setup(argparse.Namespace(install_unit=True))
        assert rc == 1
        assert unit.read_text() == original

    def test_matching_existing_unit_left_unchanged(self, monkeypatch, tmp_path):
        rc, home = _run_setup(monkeypatch, tmp_path, install_unit=True)
        unit = home / ".config/systemd/user/stt-button.service"
        assert rc == 0
        text = unit.read_text()
        assert "ExecStart=" in text
        assert "@EXEC@" not in text
        before = unit.read_text()
        # idempotent rerun
        monkeypatch.setenv("HOME", str(home))
        rc2 = cli._cmd_setup(argparse.Namespace(install_unit=True))
        assert rc2 == 0
        assert unit.read_text() == before

    def test_real_operator_unit_never_touched(self, monkeypatch, tmp_path):
        real = _real_unit_path()
        before = _hash(real)
        # exercise the abort path (most dangerous) inside an isolated HOME
        _run_setup(monkeypatch, tmp_path, install_unit=True)
        assert _hash(real) == before


class TestNoVenvBootstrap:
    def test_setup_body_has_no_bootstrap_call(self):
        src = inspect.getsource(cli._cmd_setup)
        assert "bootstrap" not in src


class TestGateAndConfig:
    def test_gate_failure_aborts_setup(self, monkeypatch, tmp_path):
        rc, home = _run_setup(monkeypatch, tmp_path, install_unit=True, gate_level=1)
        assert rc == 1
        unit = home / ".config/systemd/user/stt-button.service"
        assert not unit.exists()

    def test_seeds_missing_config(self, monkeypatch, tmp_path):
        rc, home = _run_setup(monkeypatch, tmp_path)
        cfg = home / ".config/stt-button/config.toml"
        assert rc == 0
        assert cfg.is_file()
        assert cfg.read_text().strip()

    def test_existing_config_unchanged(self, monkeypatch, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        cfg_dir = home / ".config/stt-button"
        cfg_dir.mkdir(parents=True)
        cfg = cfg_dir / "config.toml"
        cfg.write_text("# keep me\n")
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(deps_gate, "main", lambda: 0)
        rc = cli._cmd_setup(argparse.Namespace(install_unit=False))
        assert rc == 0
        assert cfg.read_text() == "# keep me\n"
