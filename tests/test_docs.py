"""Tests for the project documentation (docs/KONZEPT.md).

The CapsLock premise in the concept was documented incorrectly (LED inverted).
These tests ensure the corrected rule is not obscured again.
"""
from __future__ import annotations

from pathlib import Path

KONZEPT = Path(__file__).resolve().parent.parent / "docs" / "KONZEPT.md"


def text() -> str:
    return KONZEPT.read_text(encoding="utf-8")


class TestCapslockSection:
    def test_states_conventional_mapping(self) -> None:
        """Verified (inverted) mapping: LED off = CapsLock active."""
        assert "LED off = CapsLock active" in text()
        assert "LED on = normal" in text()

    def test_states_app_never_toggles(self) -> None:
        assert "never toggle CapsLock" in text()

    def test_states_lock_state_is_unreliable(self) -> None:
        body = text()
        assert "LED != State" in body
        assert "xset != State" in body

    def test_target_is_configurable(self) -> None:
        """The target state is configurable via capslock_led_inverted."""
        assert "capslock_led_inverted" in text()

    def test_mentions_reset_command(self) -> None:
        assert "reset-capslock" in text()

    def test_mentions_caps_diagnose_command(self) -> None:
        assert "caps-diagnose" in text()


README = KONZEPT.parent.parent / "README.md"


class TestModelSwitchDocs:
    """B5: README documents switching ASR models."""

    def test_readme_has_modelle_umschalten_section(self) -> None:
        assert "## Modelle umschalten" in README.read_text(encoding="utf-8")

    def test_readme_documents_qwen_bfloat16_switch(self) -> None:
        body = README.read_text(encoding="utf-8")
        assert 'backend = "qwen"' in body
        assert 'compute_type = "bfloat16"' in body
        assert "final-only" in body

    def test_konzept_has_qwen_bf16_measurement(self) -> None:
        body = text()
        assert "Qwen3-ASR 1.7B (BF16)" in body
