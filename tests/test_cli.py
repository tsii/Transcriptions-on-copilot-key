"""Tests for CLI subcommands (TDD chain 7).

These tests cover ``stt-button reset-capslock`` (target derived from config,
inverted by default: LED "on" = normal typing) and
``stt-button caps-diagnose``.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from stt_button.__main__ import _cmd_caps_diagnose, _cmd_reset_capslock


def subprocess_result(stdout_text: str = "") -> object:
    class R:
        returncode = 0
        stdout = stdout_text
        stderr = ""

    return R()


class CapslockEnv:
    """Record dotool payloads and simulate the LED per device.

    ``inverted=True``: semantics as on this system (LED off = CapsLock
    active), so the target is "on". ``inverted=False``: conventional, target "off".

    ``flips=True``: the toggle moves the LED to the target.
    """

    DEVICES = ["input3::capslock"]

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        inverted: bool = True,
        flips: bool = True,
    ) -> None:
        self.inverted = inverted
        self.flips = flips
        self.payloads: list[str] = []
        self.target = "an" if inverted else "aus"
        self.other = "aus" if inverted else "an"
        self.led = self.other  # Start: incorrect state (CapsLock active)
        self.calls = 0

        monkeypatch.setattr("stt_button.typing.shutil.which", lambda b: "/usr/bin/" + b)
        monkeypatch.setattr(
            "stt_button.typing.subprocess.run",
            self._run,
        )
        monkeypatch.setattr(
            "stt_button.__main__._capslock_leds", self._leds
        )
        monkeypatch.setattr(
            "stt_button.__main__._capslock_state", self._state
        )

    def _run(self, cmd, **kw):
        # Accept dotool payloads (key capslock) via stdin; tolerate others (e.g., xset).
        if kw.get("input"):
            self.payloads.append(kw.get("input", ""))
            if "key capslock" in kw.get("input", ""):
                if self.flips:
                    self.led = self.target
                self.calls += 1
        return subprocess_result()

    def _leds(self) -> dict[str, str]:
        return {d: self.led for d in self.DEVICES}

    def _state(self) -> str:
        return self.led

    @property
    def toggles(self) -> int:
        return sum("key capslock" in p for p in self.payloads)


def _args(config: Path | None = None) -> argparse.Namespace:
    return argparse.Namespace(config=None if config is None else str(config))


class TestResetCapslock:
    def test_toggles_led_to_target_inverted(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
        tmp_path: Path,
    ) -> None:
        """Default (inverted): target is LED on; one toggle is sufficient."""
        env = CapslockEnv(monkeypatch)
        assert _cmd_reset_capslock(_args(tmp_path / "config.toml")) == 0
        out = capsys.readouterr().out
        assert env.toggles == 1
        assert "LED on = normal typing" in out

    def test_success_mentions_physical_key(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
        tmp_path: Path,
    ) -> None:
        CapslockEnv(monkeypatch)
        _cmd_reset_capslock(_args(tmp_path / "config.toml"))
        out = capsys.readouterr().out
        assert "physical CapsLock key" in out

    def test_conventional_config_targets_off(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
        tmp_path: Path,
    ) -> None:
        """capslock_led_inverted=false: target is LED off."""
        cfg_file = tmp_path / "config.toml"
        cfg_file.write_text("capslock_led_inverted = false\n", encoding="utf-8")
        env = CapslockEnv(monkeypatch, inverted=False)
        assert _cmd_reset_capslock(_args(cfg_file)) == 0
        out = capsys.readouterr().out
        assert env.toggles == 1
        assert "LED off = normal typing" in out

    def test_prints_per_device(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
        tmp_path: Path,
    ) -> None:
        env = CapslockEnv(monkeypatch)
        _cmd_reset_capslock(_args(tmp_path / "config.toml"))
        out = capsys.readouterr().out
        assert "input3::capslock" in out

    def test_gives_up_after_three_toggles(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
        tmp_path: Path,
    ) -> None:
        env = CapslockEnv(monkeypatch, flips=False)
        assert _cmd_reset_capslock(_args(tmp_path / "config.toml")) == 1
        out = capsys.readouterr().out
        assert env.toggles == 3
        assert "keyd" in out
        assert "log out/in" in out

    def test_old_config_key_is_now_valid(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
        tmp_path: Path,
    ) -> None:
        """capslock_led_inverted is now a valid config key."""
        cfg_file = tmp_path / "config.toml"
        cfg_file.write_text("capslock_led_inverted = true\n", encoding="utf-8")
        env = CapslockEnv(monkeypatch)
        assert _cmd_reset_capslock(_args(cfg_file)) == 0
        assert env.toggles == 1


class TestCapsDiagnose:
    def test_caps_diagnose_prints_per_device_leds(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
        tmp_path: Path,
    ) -> None:
        """Output contains both device names and the valid target state."""
        monkeypatch.setattr("stt_button.typing.shutil.which", lambda b: "/usr/bin/" + b)
        monkeypatch.setattr(
            "stt_button.__main__._capslock_leds",
            lambda: {"input3::capslock": "aus", "input9::capslock": "an"},
        )
        monkeypatch.setattr(
            "stt_button.__main__.subprocess.run",
            lambda cmd, **kw: subprocess_result(stdout="00: Caps Lock:   off\n"),
        )
        # Typing the marker may call subprocess.run (dotool); simply tolerate
        # it here (no stdin). type_text uses typing.run, not
        # __main__.subprocess.run, so mock it separately.
        monkeypatch.setattr(
            "stt_button.typing.subprocess.run",
            lambda cmd, **kw: subprocess_result(),
        )
        monkeypatch.setattr("stt_button.typing._capslock_state", lambda: "aus")
        assert _cmd_caps_diagnose(_args(tmp_path / "config.toml")) == 0
        out = capsys.readouterr().out
        assert "input3::capslock" in out
        assert "input9::capslock" in out
        assert "an" in out  # target state (default inverted: "on")

    def test_caps_diagnose_tolerates_xset_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
        tmp_path: Path,
    ) -> None:
        """xset q may fail without causing the command to fail."""
        monkeypatch.setattr("stt_button.typing.shutil.which", lambda b: "/usr/bin/" + b)
        monkeypatch.setattr(
            "stt_button.__main__._capslock_leds",
            lambda: {"input3::capslock": "an"},
        )
        monkeypatch.setattr(
            "stt_button.__main__.subprocess.run",
            lambda cmd, **kw: (_ for _ in ()).throw(FileNotFoundError("xset")),
        )
        monkeypatch.setattr(
            "stt_button.typing.subprocess.run",
            lambda cmd, **kw: subprocess_result(),
        )
        monkeypatch.setattr("stt_button.typing._capslock_state", lambda: "an")
        assert _cmd_caps_diagnose(_args(tmp_path / "config.toml")) == 0