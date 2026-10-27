"""Tests fuer die Projektdokumentation (docs/KONZEPT.md).

Die CapsLock-Praemisse im Konzept war falsch dokumentiert (LED invertiert).
Damit die korrigierte Regel nicht wieder verwischt, wird sie hier geprueft.
"""
from __future__ import annotations

from pathlib import Path

KONZEPT = Path(__file__).resolve().parent.parent / "docs" / "KONZEPT.md"


def text() -> str:
    return KONZEPT.read_text(encoding="utf-8")


class TestCapslockSection:
    def test_states_conventional_mapping(self) -> None:
        """Belegte (invertierte) Zuordnung: LED aus = CapsLock aktiv."""
        assert "LED aus = CapsLock aktiv" in text()
        assert "LED an = normale Schreibung" in text()

    def test_states_app_never_toggles(self) -> None:
        assert "Die App darf CapsLock niemals ungefragt togglen" in text()

    def test_states_lock_state_is_unreliable(self) -> None:
        body = text()
        assert "LED != State" in body
        assert "xset != State" in body

    def test_target_is_configurable(self) -> None:
        """Der Zielzustand ist ueber capslock_led_inverted konfigurierbar."""
        assert "capslock_led_inverted" in text()

    def test_mentions_reset_command(self) -> None:
        assert "reset-capslock" in text()

    def test_mentions_caps_diagnose_command(self) -> None:
        assert "caps-diagnose" in text()
