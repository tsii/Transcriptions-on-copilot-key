"""Tests fuer die CLI-Subcommands (TDD-Kette 7).

Getestet werden ``stt-button reset-capslock`` (Ziel aus der Config abgeleitet,
standardmaessig invertiert: LED "an" = normale Schreibung) und
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
    """Zeichnet dotool-Payloads auf und simuliert die LED pro Device.

    ``inverted=True``: Semantik wie auf diesem System (LED aus = CapsLock
    aktiv), Ziel ist also "an". ``inverted=False``: konventionell, Ziel "aus".

    ``flips=True``: der Toggle bringt die LED auf das Ziel.
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
        self.led = self.other  # Start: falscher Zustand (CapsLock aktiv)
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
        # dotool-Payloads (key capslock) via stdin; sonst (z.B. xset) tolerieren.
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
        """Default (invertiert): Ziel ist LED an; einmal togglen genuegt."""
        env = CapslockEnv(monkeypatch)
        assert _cmd_reset_capslock(_args(tmp_path / "config.toml")) == 0
        out = capsys.readouterr().out
        assert env.toggles == 1
        assert "LED an = normale Schreibung" in out

    def test_success_mentions_physical_key(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
        tmp_path: Path,
    ) -> None:
        CapslockEnv(monkeypatch)
        _cmd_reset_capslock(_args(tmp_path / "config.toml"))
        out = capsys.readouterr().out
        assert "physische CapsLock-Taste" in out

    def test_conventional_config_targets_off(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
        tmp_path: Path,
    ) -> None:
        """capslock_led_inverted=false: Ziel ist LED aus."""
        cfg_file = tmp_path / "config.toml"
        cfg_file.write_text("capslock_led_inverted = false\n", encoding="utf-8")
        env = CapslockEnv(monkeypatch, inverted=False)
        assert _cmd_reset_capslock(_args(cfg_file)) == 0
        out = capsys.readouterr().out
        assert env.toggles == 1
        assert "LED aus = normale Schreibung" in out

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
        assert "melden" in out

    def test_old_config_key_is_now_valid(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
        tmp_path: Path,
    ) -> None:
        """capslock_led_inverted ist jetzt ein gueltiger Config-Schluessel."""
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
        """Ausgabe enthaelt beide Device-Namen und den gueltigen Zielzustand."""
        monkeypatch.setattr("stt_button.typing.shutil.which", lambda b: "/usr/bin/" + b)
        monkeypatch.setattr(
            "stt_button.__main__._capslock_leds",
            lambda: {"input3::capslock": "aus", "input9::capslock": "an"},
        )
        monkeypatch.setattr(
            "stt_button.__main__.subprocess.run",
            lambda cmd, **kw: subprocess_result(stdout="00: Caps Lock:   off\n"),
        )
        # Tippen des Markers darf subprocess.run (dotool) aufrufen; hier
        # einfach tolerieren (kein stdin). type_text verwendet typing.run,
        # nicht __main__.subprocess.run - deshalb separat mocken.
        monkeypatch.setattr(
            "stt_button.typing.subprocess.run",
            lambda cmd, **kw: subprocess_result(),
        )
        monkeypatch.setattr("stt_button.typing._capslock_state", lambda: "aus")
        assert _cmd_caps_diagnose(_args(tmp_path / "config.toml")) == 0
        out = capsys.readouterr().out
        assert "input3::capslock" in out
        assert "input9::capslock" in out
        assert "an" in out  # Zielzustand (Default invertiert: "an")

    def test_caps_diagnose_tolerates_xset_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
        tmp_path: Path,
    ) -> None:
        """xset q darf fehlschlagen, ohne dass das Kommando scheitert."""
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