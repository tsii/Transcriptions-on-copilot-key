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

    def test_readme_documents_qwen_xpu(self) -> None:
        """qwen-xpu B7: GPU path, separate venv and operator command."""
        body = README.read_text(encoding="utf-8")
        assert "qwen_device" in body
        assert ".venv-xpu" in body
        assert "tools/setup_qwen_xpu.sh" in body
        assert "intel-opencl-icd libze-intel-gpu1" in body

    def test_changelog_mentions_qwen_xpu(self) -> None:
        body = (README.parent / "CHANGELOG.md").read_text(encoding="utf-8")
        assert "qwen_device" in body

    def test_konzept_has_qwen_bf16_measurement(self) -> None:
        body = text()
        assert "Qwen3-ASR 1.7B (BF16)" in body


CHANGELOG = KONZEPT.parent.parent / "CHANGELOG.md"
BUILD_DEB = KONZEPT.parent.parent / "tools" / "build-deb.sh"


class TestSummaryDocs:
    """B12: README documents the opt-in double-press summary mode."""

    def test_readme_has_summary_section(self) -> None:
        assert "## Zusammenfassen mit Doppelklick" in README.read_text(encoding="utf-8")

    def test_readme_documents_summary_enabled(self) -> None:
        assert "summary_enabled" in README.read_text(encoding="utf-8")

    def test_readme_documents_summary_model(self) -> None:
        assert "my-qwen38-flash-next" in README.read_text(encoding="utf-8")

    def test_readme_warns_text_leaves_machine(self) -> None:
        assert "Text verlässt den Rechner" in README.read_text(encoding="utf-8")

    def test_readme_documents_caret_not_at_end(self) -> None:
        body = README.read_text(encoding="utf-8")
        assert "caret_not_at_end" in body
        assert "Abbruch" in body  # abort without deleting

    def test_readme_documents_dictated_or_typed_scope(self) -> None:
        """Rest scope §2: dictated OR typed text is summarized; undo restores."""
        body = README.read_text(encoding="utf-8")
        assert "diktiert oder getippt" in body
        assert "Rückgängig" in body
        assert "zweimal dieselbe Taste" in body

    def test_changelog_mentions_summary_mode(self) -> None:
        body = CHANGELOG.read_text(encoding="utf-8")
        assert "Zusammenfassen mit Doppelklick" in body
        assert "summary_enabled" in body

    def test_build_deb_ships_sounds(self) -> None:
        """B12: the .deb must ship sounds/you_lose.wav."""
        body = BUILD_DEB.read_text(encoding="utf-8")
        assert "sounds/" in body
        assert "you_lose.wav" in body


BOOTSTRAP = KONZEPT.parent.parent / "src" / "stt_button" / "bootstrap.py"


class TestVenvFreeWayBDocs:
    """V7: README documents the venv-free installation (Weg B)."""

    def test_readme_has_weg_b_section(self) -> None:
        assert "## Installation ohne venv (Weg B)" in README.read_text(encoding="utf-8")

    def test_readme_documents_break_system_packages(self) -> None:
        body = README.read_text(encoding="utf-8")
        assert "pip install --user --break-system-packages -r /opt/stt-button/packaging/runtime-requirements.txt" in body

    def test_readme_documents_pythonuserbase_shadow_recipe(self) -> None:
        body = README.read_text(encoding="utf-8")
        assert "PYTHONUSERBASE" in body
        assert "Environment=PYTHONUSERBASE=%h/.local/stt" in body

    def test_changelog_has_021_entry(self) -> None:
        body = CHANGELOG.read_text(encoding="utf-8")
        assert "[0.2.1]" in body
        assert "2026-11-10" in body

    def test_build_deb_header_no_first_run_claim(self) -> None:
        header = "\n".join(
            BUILD_DEB.read_text(encoding="utf-8").splitlines()[:15]
        )
        assert "built on the" not in header
        assert "no .venv" in header

    def test_bootstrap_docstring_is_dev_only(self) -> None:
        body = BOOTSTRAP.read_text(encoding="utf-8")
        docstring = body.split('"""')[1]
        assert "dev" in docstring
        assert "first start" not in docstring
        assert "builds the Python runtime" not in docstring
        assert "called by ``app.run``" not in docstring


CONTROL = KONZEPT.parent.parent / "packaging" / "debian" / "control"


class TestV7bLegacyVenvDocsRemoved:
    """V7b: the venv-era installation text is rewritten (Weg B only)."""

    def test_readme_no_local_venv_path(self) -> None:
        assert "~/.local/stt-button/venv" not in README.read_text(encoding="utf-8")

    def test_readme_no_python3_venv_dep(self) -> None:
        assert "python3-venv" not in README.read_text(encoding="utf-8")

    def test_readme_no_build_venv_claim(self) -> None:
        assert "build venv" not in README.read_text(encoding="utf-8")

    def test_no_self_healing_anywhere(self) -> None:
        for p in (README, CONTROL, BUILD_DEB):
            assert "self-healing" not in p.read_text(encoding="utf-8").lower()

    def test_weg_b_section_still_present(self) -> None:
        assert "## Installation ohne venv (Weg B)" in README.read_text(encoding="utf-8")

    def test_build_deb_header_no_venv_claim_still_present(self) -> None:
        header = "\n".join(BUILD_DEB.read_text(encoding="utf-8").splitlines()[:15])
        assert "no .venv" in header
