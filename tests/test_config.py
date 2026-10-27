"""Tests fuer die Konfiguration (TDD-Kette 1)."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from stt_button.config import Config, ConfigError, load_config


def write_config(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


class TestDefaults:
    def test_missing_file_yields_defaults(self, tmp_path: Path) -> None:
        cfg = load_config(tmp_path / "does-not-exist.toml")
        assert cfg.language == "auto"
        assert cfg.model == "nvidia/parakeet-tdt-0.6b-v3"
        assert cfg.backend == "parakeet"
        assert cfg.typer == "dotool"
        assert cfg.append_space is True
        assert cfg.asr_interval_s == 1.0
        assert cfg.min_audio_s == 3.0
        assert cfg.max_record_s == 300
        assert cfg.key_codes == ["KEY_F24"]
        assert cfg.input_device_name == "keyd virtual keyboard"

    def test_defaults_have_sane_audio_source(self) -> None:
        cfg = Config()
        assert "source" in cfg.audio_source or cfg.audio_source == "auto"

    def test_cpu_threads_positive(self) -> None:
        assert Config().cpu_threads >= 1


class TestParsing:
    def test_reads_all_known_keys(self, tmp_path: Path) -> None:
        p = write_config(tmp_path, """
            language = "auto"
            model = "large-v3-turbo"
            audio_source = "my-source"
            typer = "clipboard"
            append_space = false
            max_record_s = 30
            asr_interval_s = 2.0
            min_audio_s = 1.0
            cpu_threads = 4
            key_codes = ["KEY_F24", "KEY_F23"]
            input_device_name = "AT Translated Set 2 keyboard"
        """)
        cfg = load_config(p)
        assert cfg.language == "auto"
        assert cfg.audio_source == "my-source"
        assert cfg.typer == "clipboard"
        assert cfg.append_space is False
        assert cfg.max_record_s == 30
        assert cfg.asr_interval_s == 2.0
        assert cfg.min_audio_s == 1.0
        assert cfg.cpu_threads == 4
        assert cfg.key_codes == ["KEY_F24", "KEY_F23"]
        assert cfg.input_device_name == "AT Translated Set 2 keyboard"

    def test_partial_file_keeps_defaults(self, tmp_path: Path) -> None:
        p = write_config(tmp_path, """
            language = "en"
        """)
        cfg = load_config(p)
        assert cfg.language == "en"
        assert cfg.typer == "dotool"
        assert cfg.asr_interval_s == 1.0

    def test_empty_file_keeps_defaults(self, tmp_path: Path) -> None:
        p = write_config(tmp_path, "")
        cfg = load_config(p)
        assert cfg.language == "auto"


class TestValidation:
    def test_unknown_language_is_rejected(self, tmp_path: Path) -> None:
        p = write_config(tmp_path, 'language = "klingon"')
        with pytest.raises(ConfigError):
            load_config(p)

    def test_auto_language_is_allowed(self, tmp_path: Path) -> None:
        p = write_config(tmp_path, 'language = "auto"')
        assert load_config(p).language == "auto"

    def test_unknown_typer_is_rejected(self, tmp_path: Path) -> None:
        p = write_config(tmp_path, 'typer = "xdotool"')
        with pytest.raises(ConfigError):
            load_config(p)

    def test_negative_interval_is_rejected(self, tmp_path: Path) -> None:
        p = write_config(tmp_path, "asr_interval_s = -1.0")
        with pytest.raises(ConfigError):
            load_config(p)

    def test_zero_min_audio_is_rejected(self, tmp_path: Path) -> None:
        p = write_config(tmp_path, "min_audio_s = 0")
        with pytest.raises(ConfigError):
            load_config(p)

    def test_zero_cpu_threads_is_rejected(self, tmp_path: Path) -> None:
        p = write_config(tmp_path, "cpu_threads = 0")
        with pytest.raises(ConfigError):
            load_config(p)

    def test_empty_key_codes_is_rejected(self, tmp_path: Path) -> None:
        p = write_config(tmp_path, "key_codes = []")
        with pytest.raises(ConfigError):
            load_config(p)

    def test_negative_sample_rate_is_rejected(self, tmp_path: Path) -> None:
        p = write_config(tmp_path, "sample_rate = 800")
        with pytest.raises(ConfigError):
            load_config(p)

    def test_malformed_toml_is_reported(self, tmp_path: Path) -> None:
        p = tmp_path / "config.toml"
        p.write_text("language = = 3\n", encoding="utf-8")
        with pytest.raises(ConfigError):
            load_config(p)

    def test_error_message_mentions_the_key(self, tmp_path: Path) -> None:
        p = write_config(tmp_path, 'typer = "xdotool"')
        with pytest.raises(ConfigError) as exc:
            load_config(p)
        assert "typer" in str(exc.value)


class TestPathResolution:
    def test_explicit_path_wins(self, tmp_path: Path) -> None:
        p = write_config(tmp_path, 'language = "en"')
        assert load_config(p, override={"language": "auto"}).language == "auto"

    def test_override_ignores_unknown_keys(self, tmp_path: Path) -> None:
        p = write_config(tmp_path, 'language = "en"')
        with pytest.raises(ConfigError):
            load_config(p, override={"nonsense": 1})


class TestTypingDelays:
    def test_typing_delays_default(self) -> None:
        cfg = Config()
        assert cfg.typedelay_ms == 20
        assert cfg.typehold_ms == 15

    def test_typing_delays_parse(self, tmp_path: Path) -> None:
        p = write_config(tmp_path, "typedelay_ms = 30\ntypehold_ms = 25")
        cfg = load_config(p)
        assert cfg.typedelay_ms == 30
        assert cfg.typehold_ms == 25

    def test_negative_typedelay_rejected(self, tmp_path: Path) -> None:
        p = write_config(tmp_path, "typedelay_ms = -1")
        with pytest.raises(ConfigError):
            load_config(p)
