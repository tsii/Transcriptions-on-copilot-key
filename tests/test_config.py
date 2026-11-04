"""Tests for configuration (TDD chain 1)."""
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
        assert cfg.live_typing_mode == "committed"

    def test_defaults_have_sane_audio_source(self) -> None:
        cfg = Config()
        assert "source" in cfg.audio_source or cfg.audio_source == "auto"

    def test_cpu_threads_positive(self) -> None:
        assert Config().cpu_threads >= 1

    def test_max_queue_depth_default(self) -> None:
        assert Config().max_queue_depth == 4

    def test_max_queue_depth_must_be_positive(self) -> None:
        with pytest.raises(ConfigError):
            Config(max_queue_depth=0).validate()

    def test_done_sound_mode_default_is_always(self) -> None:
        assert Config().done_sound_mode == "always"


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
    """Final delta paste: paste the final delta via the clipboard on completion."""

    def test_final_paste_defaults_and_toml(self, tmp_path: Path) -> None:
        """Defaults: final_paste off, ctrl+v, wl-copy; TOML accepts all three."""
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
        """Only "ctrl+v" and "ctrl+shift+v" are allowed paste chords."""
        p = write_config(tmp_path, 'paste_key = "ctrl+b"')
        with pytest.raises(ConfigError) as exc:
            load_config(p)
        assert "paste_key" in str(exc.value)
        # Validation (not just "unknown key"): list the allowed values.
        assert "ctrl+v" in str(exc.value)


class TestCapslockAutocorrect:
    """The app must NOT toggle CapsLock by default."""

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
        """The `capslock_led_inverted` key is recognized and accepted."""
        p = write_config(tmp_path, "capslock_led_inverted = true")
        cfg = load_config(p)
        assert cfg.capslock_led_inverted is True

    def test_capslock_led_inverted_key_accepted_false(self, tmp_path: Path) -> None:
        """`false` is also accepted (no ConfigError)."""
        p = write_config(tmp_path, "capslock_led_inverted = false")
        cfg = load_config(p)
        assert cfg.capslock_led_inverted is False

    def test_config_has_led_inverted_attribute_default_true(self) -> None:
        assert Config().capslock_led_inverted is True


class TestLatencyStage1Keys:
    """STAGE 1: finalize_mode / audio_dump_mode / latency_trace."""

    def test_defaults_are_previous_behaviour(self) -> None:
        """Without configuration, behavior is exactly as before."""
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
    """STAGE 2: live_typing_mode 'committed' (default) | 'full' (opt-in)."""

    def test_default_is_committed(self) -> None:
        """Without configuration, the production default applies (Kimi decision)."""
        cfg = Config()
        assert cfg.live_typing_mode == "committed"

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
    """STAGE 3: live_asr_until_s (default 15.0: int8 RTF ~0.05 makes the
    15s full-buffer tick ~0.75s < asr_interval_s 1.0s; 0.0 remains the
    explicit opt-in for 'always live')."""

    def test_default_is_off(self) -> None:
        assert Config().live_asr_until_s == 15.0

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
    """STAGE 3: streaming_mode='buffered' + buffered_* fields (default OFF)."""

    def test_buffered_is_valid_streaming_mode(self) -> None:
        cfg = Config(streaming_mode="buffered")
        cfg.validate()
        assert cfg.streaming_mode == "buffered"

    def test_default_streaming_mode_stays_whole_buffer(self) -> None:
        """Feature flag remains OFF by default - no production switch."""
        assert Config().streaming_mode == "whole_buffer"

    def test_buffered_defaults_match_transcriber_signature(self) -> None:
        """Config defaults == BufferedTranscriber defaults (Kimi plan)."""
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
        """Only positive values are required; size ratios are unrestricted."""
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


class TestAdaptiveIntervalConfig:
    def test_defaults_keep_behavior(self) -> None:
        cfg = Config()
        assert cfg.asr_interval_adaptive_k == 0.0
        assert cfg.asr_interval_max_s == 6.0

    def test_negative_k_rejected(self, tmp_path) -> None:
        p = write_config(tmp_path, "asr_interval_adaptive_k = -1.0\n")
        with pytest.raises(ConfigError):
            load_config(p)

    def test_max_below_interval_rejected(self, tmp_path) -> None:
        p = write_config(tmp_path, "asr_interval_adaptive_k = 2.0\n"
                                   "asr_interval_s = 2.0\nasr_interval_max_s = 1.0\n")
        with pytest.raises(ConfigError):
            load_config(p)

    def test_max_below_interval_ok_when_off(self, tmp_path) -> None:
        cfg = load_config(write_config(tmp_path, "asr_interval_s = 8.0\n"))
        assert cfg.asr_interval_s == 8.0


class TestSegmentConfig:
    """Phase 2 (C3): sentence-pause segmentation keys and validation."""

    def test_defaults_off(self) -> None:
        cfg = Config()
        assert (cfg.segment_pause_s, cfg.segment_min_s, cfg.segment_max_s) == (0.0, 3.0, 0.0)

    @pytest.mark.parametrize("body", [
        "segment_pause_s = -1.0\n",
        "segment_min_s = -1.0\n",
        "segment_max_s = -1.0\n",
        "segment_pause_s = 1.2\n",                                  # no cap
        "segment_pause_s = 1.2\nsegment_max_s = 20.0\n",            # cap > live limit 15
        "segment_pause_s = 1.2\nsegment_max_s = 15.0\nfinalize_mode = \"cached_mid_tail\"\n",
        "segment_pause_s = 1.2\nsegment_max_s = 15.0\nstreaming_mode = \"windowed\"\n",
    ])
    def test_invalid_combinations_rejected(self, tmp_path, body) -> None:
        with pytest.raises(ConfigError):
            load_config(write_config(tmp_path, body))

    def test_valid_segment_config(self, tmp_path) -> None:
        cfg = load_config(write_config(
            tmp_path, "segment_pause_s = 1.2\nsegment_max_s = 15.0\n"))
        assert cfg.segment_max_s == 15.0

    def test_cap_unbounded_when_live_limit_off(self, tmp_path) -> None:
        cfg = load_config(write_config(
            tmp_path, "segment_pause_s = 1.2\nsegment_max_s = 40.0\nlive_asr_until_s = 0.0\n"))
        assert cfg.segment_max_s == 40.0


class TestFocusUnknownFallbackKey:
    """F2: focus_unknown_fallback 'copy' (default) | 'type'."""

    def test_default_is_copy(self) -> None:
        assert Config().focus_unknown_fallback == "copy"

    def test_parse_type(self, tmp_path: Path) -> None:
        p = write_config(tmp_path, 'focus_unknown_fallback = "type"\n')
        assert load_config(p).focus_unknown_fallback == "type"

    def test_invalid_is_rejected(self, tmp_path: Path) -> None:
        p = write_config(tmp_path, 'focus_unknown_fallback = "paste"\n')
        with pytest.raises(ConfigError, match="focus_unknown_fallback"):
            load_config(p)


class TestFinalPasteMinChars:
    def test_default_disabled(self, tmp_path: Path) -> None:
        assert load_config(tmp_path / "nope.toml").final_paste_min_chars == 0

    def test_parsed(self, tmp_path: Path) -> None:
        p = tmp_path / "c.toml"
        p.write_text("final_paste_min_chars = 40\n")
        assert load_config(p).final_paste_min_chars == 40

    def test_negative_rejected(self, tmp_path: Path) -> None:
        p = tmp_path / "c.toml"
        p.write_text("final_paste_min_chars = -1\n")
        with pytest.raises(ConfigError):
            load_config(p)
