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


class TestFinalPaste:
    """Final-Delta-Paste: finales Delta beim Abschluss per Zwischenablage."""

    def test_final_paste_defaults_and_toml(self, tmp_path: Path) -> None:
        """Defaults: final_paste aus, ctrl+v, wl-copy; TOML uebernimmt alle drei."""
        cfg = Config()
        assert cfg.final_paste is False
        assert cfg.paste_key == "ctrl+v"
        assert cfg.paste_binary == "wl-copy"

        p = write_config(tmp_path, """
            final_paste = true
            paste_key = "ctrl+shift+v"
            paste_binary = "wl-copy"
        """)
        cfg2 = load_config(p)
        assert cfg2.final_paste is True
        assert cfg2.paste_key == "ctrl+shift+v"
        assert cfg2.paste_binary == "wl-copy"

    def test_invalid_paste_key_is_rejected(self, tmp_path: Path) -> None:
        """Nur "ctrl+v" und "ctrl+shift+v" sind erlaubte Einfuege-Chords."""
        p = write_config(tmp_path, 'paste_key = "ctrl+b"')
        with pytest.raises(ConfigError) as exc:
            load_config(p)
        assert "paste_key" in str(exc.value)
        # Validierung (nicht nur "unbekannter Schluessel"): erlaubte Werte nennen.
        assert "ctrl+v" in str(exc.value)


class TestCapslockAutocorrect:
    """Die App darf CapsLock standardmaessig NICHT umschalten."""

    def test_default_is_off(self) -> None:
        assert Config().capslock_autocorrect is False

    def test_parse_true(self, tmp_path: Path) -> None:
        p = write_config(tmp_path, "capslock_autocorrect = true")
        assert load_config(p).capslock_autocorrect is True

    def test_wrong_type_is_rejected(self, tmp_path: Path) -> None:
        p = write_config(tmp_path, 'capslock_autocorrect = "ja"')
        with pytest.raises(ConfigError) as exc:
            load_config(p)
        assert "capslock_autocorrect" in str(exc.value)

    def test_capslock_led_inverted_key_accepted(self, tmp_path: Path) -> None:
        """Der Schluessel `capslock_led_inverted` ist bekannt und wird uebernommen."""
        p = write_config(tmp_path, "capslock_led_inverted = true")
        cfg = load_config(p)
        assert cfg.capslock_led_inverted is True

    def test_capslock_led_inverted_key_accepted_false(self, tmp_path: Path) -> None:
        """Auch `false` wird uebernommen (kein ConfigError)."""
        p = write_config(tmp_path, "capslock_led_inverted = false")
        cfg = load_config(p)
        assert cfg.capslock_led_inverted is False

    def test_config_has_led_inverted_attribute_default_true(self) -> None:
        assert Config().capslock_led_inverted is True


class TestLatencyStage1Keys:
    """STUFE 1: finalize_mode / audio_dump_mode / latency_trace."""

    def test_defaults_are_previous_behaviour(self) -> None:
        """Ohne Config gilt genau das bisherige Verhalten."""
        cfg = Config()
        assert cfg.finalize_mode == "full"
        assert cfg.audio_dump_mode == "sync"
        assert cfg.latency_trace is False

    def test_parse_all_valid_values(self, tmp_path: Path) -> None:
        p = write_config(
            tmp_path,
            """
            finalize_mode = "cached"
            audio_dump_mode = "async"
            latency_trace = true
            """,
        )
        cfg = load_config(p)
        assert cfg.finalize_mode == "cached"
        assert cfg.audio_dump_mode == "async"
        assert cfg.latency_trace is True

    @pytest.mark.parametrize("mode", ["instant", "volldampf"])
    def test_invalid_finalize_mode_is_rejected(self, tmp_path: Path, mode: str) -> None:
        p = write_config(tmp_path, f'finalize_mode = "{mode}"\n')
        with pytest.raises(ConfigError, match="finalize_mode"):
            load_config(p)

    def test_invalid_audio_dump_mode_is_rejected(self, tmp_path: Path) -> None:
        p = write_config(tmp_path, 'audio_dump_mode = "sofort"\n')
        with pytest.raises(ConfigError, match="audio_dump_mode"):
            load_config(p)

    def test_wrong_type_is_rejected(self, tmp_path: Path) -> None:
        p = write_config(tmp_path, "latency_trace = 1\n")
        with pytest.raises(ConfigError, match="latency_trace"):
            load_config(p)


class TestLiveTypingModeKeys:
    """STUFE 2: live_typing_mode 'full' (Default) | 'committed'."""

    def test_default_is_full(self) -> None:
        """Ohne Config gilt exakt das bisherige Verhalten."""
        cfg = Config()
        assert cfg.live_typing_mode == "full"

    def test_parse_committed(self, tmp_path: Path) -> None:
        p = write_config(tmp_path, 'live_typing_mode = "committed"\n')
        cfg = load_config(p)
        assert cfg.live_typing_mode == "committed"

    @pytest.mark.parametrize("mode", ["partiell", "instant", 3])
    def test_invalid_mode_is_rejected(self, tmp_path: Path, mode) -> None:
        p = write_config(tmp_path, f'live_typing_mode = {mode!r}\n')
        if isinstance(mode, int):
            p.write_text("live_typing_mode = 3\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="live_typing_mode"):
            load_config(p)


class TestLiveAsrUntilKeys:
    """STUFE 3: live_asr_until_s (Default 0.0 = aus)."""

    def test_default_is_off(self) -> None:
        assert Config().live_asr_until_s == 0.0

    def test_parse_value(self, tmp_path: Path) -> None:
        p = write_config(tmp_path, "live_asr_until_s = 6.5\n")
        assert load_config(p).live_asr_until_s == 6.5

    def test_negative_is_rejected(self, tmp_path: Path) -> None:
        p = write_config(tmp_path, "live_asr_until_s = -1.0\n")
        with pytest.raises(ConfigError, match="live_asr_until_s"):
            load_config(p)

    def test_wrong_type_is_rejected(self, tmp_path: Path) -> None:
        p = write_config(tmp_path, 'live_asr_until_s = "spaeter"\n')
        with pytest.raises(ConfigError, match="live_asr_until_s"):
            load_config(p)


class TestBufferedMode:
    """STUFE 3: streaming_mode='buffered' + buffered_* Felder (Default AUS)."""

    def test_buffered_is_valid_streaming_mode(self) -> None:
        cfg = Config(streaming_mode="buffered")
        cfg.validate()
        assert cfg.streaming_mode == "buffered"

    def test_default_streaming_mode_stays_whole_buffer(self) -> None:
        """Feature-Flag bleibt Default AUS - kein Produktiv-Umschalten."""
        assert Config().streaming_mode == "whole_buffer"

    def test_buffered_defaults_match_transcriber_signature(self) -> None:
        """Config-Defaults == BufferedTranscriber-Defaults (Kimi-Plan)."""
        import inspect

        from stt_button.buffered import BufferedTranscriber

        params = inspect.signature(BufferedTranscriber.__init__).parameters
        cfg = Config()
        assert cfg.buffered_whole_buffer_until_s == params["whole_buffer_until_s"].default
        assert cfg.buffered_left_context_s == params["left_context_s"].default
        assert cfg.buffered_chunk_s == params["chunk_s"].default
        assert cfg.buffered_right_context_s == params["right_context_s"].default
        assert cfg.buffered_max_final_window_s == params["max_final_window_s"].default

    def test_parse_from_toml(self, tmp_path: Path) -> None:
        p = write_config(tmp_path, """
            streaming_mode = "buffered"
            buffered_whole_buffer_until_s = 6.0
            buffered_left_context_s = 5.0
            buffered_chunk_s = 1.5
            buffered_right_context_s = 1.0
            buffered_max_final_window_s = 12.0
            """,
        )
        cfg = load_config(p)
        assert cfg.streaming_mode == "buffered"
        assert cfg.buffered_whole_buffer_until_s == 6.0
        assert cfg.buffered_left_context_s == 5.0
        assert cfg.buffered_chunk_s == 1.5
        assert cfg.buffered_right_context_s == 1.0
        assert cfg.buffered_max_final_window_s == 12.0

    @pytest.mark.parametrize("key", [
        "buffered_whole_buffer_until_s",
        "buffered_left_context_s",
        "buffered_chunk_s",
        "buffered_right_context_s",
        "buffered_max_final_window_s",
    ])
    def test_nonpositive_values_rejected(self, tmp_path: Path, key: str) -> None:
        p = write_config(tmp_path, f'{key} = 0\n')
        with pytest.raises(ConfigError, match=key):
            load_config(p)

    def test_right_context_larger_than_chunk_allowed(self) -> None:
        """Nur positive Werte sind Pflicht; Groessenverhaeltnisse sind frei."""
        cfg = Config(buffered_chunk_s=0.5, buffered_right_context_s=4.0)
        cfg.validate()

    def test_wrong_type_rejected(self, tmp_path: Path) -> None:
        p = write_config(tmp_path, 'buffered_chunk_s = "zwei"\n')
        with pytest.raises(ConfigError, match="buffered_chunk_s"):
            load_config(p)

    def test_unknown_streaming_mode_still_rejected(self, tmp_path: Path) -> None:
        p = write_config(tmp_path, 'streaming_mode = "pufferlos"\n')
        with pytest.raises(ConfigError, match="streaming_mode"):
            load_config(p)


class TestCachedMidTailConfig:
    """PHASE 2: finalize_mode='cached_mid_tail' (Opt-In)."""

    def test_cached_mid_tail_ist_gueltig(self, tmp_path: Path) -> None:
        p = write_config(tmp_path, 'finalize_mode = "cached_mid_tail"\n')
        cfg = load_config(p)
        assert cfg.finalize_mode == "cached_mid_tail"

    def test_defaults_unveraendert(self) -> None:
        assert Config().finalize_mode == "full"

    def test_ungueltige_weiterhin_abgelehnt(self, tmp_path: Path) -> None:
        p = write_config(tmp_path, 'finalize_mode = "mitte_schwanz"\n')
        with pytest.raises(ConfigError, match="finalize_mode"):
            load_config(p)
