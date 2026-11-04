"""Configuration for STT-Button.

The configuration lives at ``~/.config/stt-button/config.toml`` by
default and is loaded via :func:`load_config`. Missing values fall back
to defaults; invalid values are rejected with a clear message.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

CONFIG_DIR = Path(
    os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
) / "stt-button"
DEFAULT_CONFIG_PATH = CONFIG_DIR / "config.toml"

VALID_LANGUAGES = ("de", "en", "auto")
VALID_TYPERS = ("wtype", "clipboard", "dotool")
VALID_BACKENDS = ("whisper", "parakeet", "qwen")
VALID_STREAMING_MODES = ("windowed", "whole_buffer", "buffered")
VALID_PASTE_KEYS = ("ctrl+v", "ctrl+shift+v")
#: Finalization mode: "full" = re-transcribe the buffer (proven),
#: "cached" = carry over the unchanged buffer from the last successful
#: tick (saves a complete engine pass),
#: "cached_mid_tail" = OPT-IN: after the live limit (live_asr_until_s),
#: every mid_tail_step_s seconds a background engine call on
#: [checkpoint end - context | buffer end], stitched onto the working
#: hypothesis; finalize() then returns only the small final tail
#: instead of the whole buffer when the chain is valid.
VALID_FINALIZE_MODES = ("full", "cached", "cached_mid_tail")
#: WAV dump of the last recording: "sync" = in the finalizer thread
#: (proven), "async" = own thread with a byte snapshot, "off" = no dump.
VALID_AUDIO_DUMP_MODES = ("sync", "async", "off")
#: Live preview mode: "full" (default) types unstable partials live
#: (with backspace corrections); "committed" types only the secured
#: increment (STAGE 2, latency optimization without quality change).
VALID_LIVE_TYPING_MODES = ("full", "committed")


class ConfigError(ValueError):
    """Invalid or unusable configuration."""


@dataclass(frozen=True)
class Config:
    """All application settings."""

    # --- Trigger ---------------------------------------------------------
    #: Name of the input device the trigger key appears on.
    #: "keyd virtual keyboard" is the default for ThinkPads where the
    #: Copilot key is mapped to F24 via keyd.
    input_device_name: str = "keyd virtual keyboard"
    #: Linux key codes that trigger push-to-talk (all must be pressed).
    key_codes: list[str] = field(default_factory=lambda: ["KEY_F24"])

    # --- Audio -----------------------------------------------------------
    #: PipeWire source name. "auto" picks the first sensible source.
    audio_source: str = "auto"
    sample_rate: int = 16000

    # --- ASR -------------------------------------------------------------
    #: Engine backend: "whisper" (faster-whisper) or "parakeet" (NVIDIA TDT).
    backend: str = "parakeet"
    model: str = "nvidia/parakeet-tdt-0.6b-v3"
    language: str = "auto"
    compute_type: str = "int8"
    cpu_threads: int = field(default_factory=lambda: os.cpu_count() or 4)
    # --- Streaming ---------------------------------------------------------
    #: Streaming mode: "windowed" (windows + timestamp commit),
    #: "whole_buffer" (old approach, whole buffer re-transcribed) or
    #: "buffered" (STAGE 3: hybrid whole-buffer -> bounded window).
    #: Default stays "whole_buffer" - the new mode is a feature flag.
    streaming_mode: str = "whole_buffer"
    #: Minimum audio length (seconds) before anything is transcribed.
    #: Very short chunks make the transducer capitalize sentence starts
    #: wrongly and swallow words (hallucination from isolated 1s
    #: fragments). 3s gives the model enough context for the first pass.
    min_audio_s: float = 3.0
    #: Interval (seconds of audio) at which re-transcription happens.
    asr_interval_s: float = 1.0
    #: Phase 2 (B): adaptive live interval factor. 0.0 = off (default,
    #: previous behavior); 2.0 = TalkType pacing (interval = 2x last tick).
    asr_interval_adaptive_k: float = 0.0
    #: Phase 2 (B): upper clamp of the adaptive interval (seconds).
    asr_interval_max_s: float = 6.0
    #: Maximum recording duration (seconds) -> automatic finalization.
    max_record_s: float = 300.0
    #: Number of consecutive identical prefixes until a prefix is
    #: considered "committed" and never rolled back again.
    commit_agreements: int = 2

    # --- Buffered mode (STAGE 3, only with streaming_mode="buffered") ----
    #: Until this buffer length the whole buffer is transcribed.
    buffered_whole_buffer_until_s: float = 8.0
    #: Left context of the window in seconds (quality anchor).
    buffered_left_context_s: float = 8.0
    #: New stable segment per tick in seconds.
    buffered_chunk_s: float = 2.0
    #: Right context (lookahead) after the commit horizon in seconds.
    buffered_right_context_s: float = 2.0
    #: Hard budget for finalization/catch-up windows in seconds.
    buffered_max_final_window_s: float = 18.0

    # --- Output ----------------------------------------------------------
    #: Typing adapter: "dotool" (default, works under GNOME/Wayland),
    #: "wtype" or "clipboard" (fallback via the clipboard).
    typer: str = "dotool"
    #: XKB keyboard layout for dotool ("de" for a German keyboard).
    xkb_layout: str = "de"
    xkb_variant: str = "nodeadkeys"
    #: Dotool typing timing. Too small -> under CPU load key events can
    #: be lost and modifiers (Shift/CapsLock) can "stick" -> the text
    #: then appears inverted/capitalized. Raise on problems (e.g. 30/25).
    typedelay_ms: int = 20
    typehold_ms: int = 15
    #: Should the app force CapsLock on/off before typing?
    #: Default is **False**: the lock state is not reliably readable
    #: (LED != state, xset != state), so only a warning is emitted.
    #: Whoever wants the intervention sets the value explicitly to True.
    capslock_autocorrect: bool = False
    #: Semantics of this system's CapsLock LED. Ground truth (empirically
    #: established, ThinkPad + keyd + GNOME/Wayland): LED **off** =
    #: CapsLock active = inverted text; LED **on** = normal typing. This
    #: is INVERTED from the usual convention, hence True as default. With
    #: capslock_autocorrect=True the LED target state is "on" (normal).
    capslock_led_inverted: bool = True
    append_space: bool = True
    #: Should the final delta at finalization be pasted via the clipboard
    #: instead of typed character by character? This speeds up long
    #: dictations massively (300 chars * ~35 ms typing time go away).
    #: Default False = previous behavior (type everything via dotool).
    final_paste: bool = False
    #: Insert chord for final_paste. "ctrl+shift+v" for terminals where
    #: ctrl+v triggers a different signal.
    paste_key: str = "ctrl+v"
    #: Binary that puts the delta into the clipboard (wl-clipboard).
    paste_binary: str = "wl-copy"
    #: Maximum length (chars) of a revision. Longer corrections are
    #: avoided to not destroy user text.
    max_revision_chars: int = 60

    # --- Latency (STAGE 1) ------------------------------------------------
    #: "full" (default): finalize re-transcribes the whole buffer.
    #: "cached": audio unchanged since the last successful tick is taken
    #: from the cache without an engine call (only makes sense with
    #: deterministic engines like Parakeet).
    finalize_mode: str = "full"
    #: WAV dump of the recording to /tmp/stt_last.wav: "sync" (default,
    #: previous behavior), "async" (own thread, no finalizer blocking)
    #: or "off".
    audio_dump_mode: str = "sync"
    #: Log debug timings at the latency measurement points (release -> output).
    latency_trace: bool = False
    #: Live preview mode (STAGE 2): "full" (default) = previous behavior -
    #: unstable partials are typed live and corrected via backspace;
    #: "committed" = only the secured prefix increment is typed live, the
    #: final suffix comes as one paste on release.
    live_typing_mode: str = "committed"
    #: Stop live ASR after this buffer length (seconds) (STAGE 3):
    #: the whole-buffer approach gets more expensive per tick; beyond
    #: this only finalize() produces text. Default 15.0 (OPT 29.10.):
    #: with the int8 engine (RTF ~0.05) a 15s whole-buffer tick costs
    #: ~0.75s and stays under asr_interval_s (1.0s) - live preview up to
    #: 15s is viable, longer dictations pause with an indicator. 0.0 is
    #: the explicit opt-in "live ticks always active" (previous behavior).
    live_asr_until_s: float = 15.0
    #: Phase 2 (C): sentence-pause segmentation. >0 = VAD pause (seconds)
    #: after a hypothesis ending in .?! closes the segment (mini-final,
    #: permanent commit, buffer reset). 0.0 = off (default). Requires
    #: segment_max_s > 0; live_asr_until_s then applies PER SEGMENT.
    segment_pause_s: float = 0.0
    #: Phase 2 (C): minimum segment length (seconds) before a pause cut.
    segment_min_s: float = 3.0
    #: Phase 2 (C): hard cap per segment (seconds); 0.0 = none. Must be
    #: <= live_asr_until_s so the live limit never pauses a segment.
    segment_max_s: float = 0.0
    #: Done signal (operator, 31.10.): freedesktop sound name (e.g.
    #: "complete" or "bell") that plays quietly once via paplay after a
    #: SUCCESSFUL finalization - hearing instead of guessing when the
    #: text is in the clipboard. '' = off.
    done_sound: str = ""
    #: Volume of the done signal as paplay permille (0..65536);
    #: 65536 = 'as the desktop volume dictates' (default).
    #: Note, ground truth 31.10.: paplay --volume scales the SINK volume
    #: - at 24% sink, 35000 was practically inaudible. Want it quieter:
    #: control it via the desktop volume or set e.g. 32768 here
    #: (~50% of the sink level).
    done_sound_volume: int = 65536
    #: When the done signal sounds: 'terminal' (default) = only when a
    #: terminal is focused (GNOME Shell extension GetFocus; None/unknown
    #: -> it still plays, feedback over silence); 'always' (default) =
    #: always.
    done_sound_mode: str = "always"
    max_queue_depth: int = 4

    def validate(self) -> None:
        if self.max_queue_depth < 1:
            raise ConfigError("max_queue_depth must be >= 1")
        if self.done_sound_mode not in ("terminal", "always"):
            raise ConfigError(
                "done_sound_mode must be 'terminal' or 'always', "
                f"is {self.done_sound_mode!r}"
            )

        """Raises :class:`ConfigError` on invalid values."""
        if self.language not in VALID_LANGUAGES:
            raise ConfigError(
                f"language must be one of {VALID_LANGUAGES}, "
                f"is {self.language!r}"
            )
        if self.typer not in VALID_TYPERS:
            raise ConfigError(
                f"typer must be one of {VALID_TYPERS}, is {self.typer!r}"
            )
        if self.backend not in VALID_BACKENDS:
            raise ConfigError(
                f"backend must be one of {VALID_BACKENDS}, is {self.backend!r}"
            )
        if self.streaming_mode not in VALID_STREAMING_MODES:
            raise ConfigError(
                f"streaming_mode must be one of {VALID_STREAMING_MODES}, "
                f"is {self.streaming_mode!r}"
            )
        if self.finalize_mode not in VALID_FINALIZE_MODES:
            raise ConfigError(
                f"finalize_mode must be one of {VALID_FINALIZE_MODES}, "
                f"is {self.finalize_mode!r}"
            )
        if self.audio_dump_mode not in VALID_AUDIO_DUMP_MODES:
            raise ConfigError(
                f"audio_dump_mode must be one of {VALID_AUDIO_DUMP_MODES}, "
                f"is {self.audio_dump_mode!r}"
            )
        if self.live_typing_mode not in VALID_LIVE_TYPING_MODES:
            raise ConfigError(
                f"live_typing_mode must be one of "
                f"{VALID_LIVE_TYPING_MODES}, "
                f"is {self.live_typing_mode!r}"
            )
        if self.live_asr_until_s < 0:
            raise ConfigError("live_asr_until_s must not be negative")
        for key in ("segment_pause_s", "segment_min_s", "segment_max_s"):
            if getattr(self, key) < 0:
                raise ConfigError(f"{key} must not be negative")
        if self.segment_pause_s > 0:
            if self.streaming_mode != "whole_buffer":
                raise ConfigError("segment_pause_s requires streaming_mode='whole_buffer'")
            if self.finalize_mode == "cached_mid_tail":
                raise ConfigError("segment_pause_s cannot be combined with cached_mid_tail")
            if self.segment_max_s <= 0:
                raise ConfigError("segment_pause_s requires segment_max_s > 0")
            if 0 < self.live_asr_until_s < self.segment_max_s:
                raise ConfigError("segment_max_s must be <= live_asr_until_s")
        if self.asr_interval_s <= 0:
            raise ConfigError("asr_interval_s must be greater than 0")
        if self.asr_interval_adaptive_k < 0:
            raise ConfigError("asr_interval_adaptive_k must not be negative")
        if self.asr_interval_adaptive_k > 0 and self.asr_interval_max_s < self.asr_interval_s:
            raise ConfigError("asr_interval_max_s must be >= asr_interval_s")
        if self.min_audio_s <= 0:
            raise ConfigError("min_audio_s must be greater than 0")
        if self.max_record_s <= 0:
            raise ConfigError("max_record_s must be greater than 0")
        if self.cpu_threads < 1:
            raise ConfigError("cpu_threads must be at least 1")
        if self.max_revision_chars < 0:
            raise ConfigError("max_revision_chars must not be negative")
        if self.typedelay_ms < 0:
            raise ConfigError("typedelay_ms must not be negative")
        if self.typehold_ms < 0:
            raise ConfigError("typehold_ms must not be negative")
        if self.commit_agreements < 1:
            raise ConfigError("commit_agreements must be at least 1")
        for name in ("buffered_whole_buffer_until_s", "buffered_left_context_s",
                     "buffered_chunk_s", "buffered_right_context_s",
                     "buffered_max_final_window_s"):
            if getattr(self, name) <= 0:
                raise ConfigError(f"{name} must be greater than 0")
        if self.sample_rate < 8000:
            raise ConfigError("sample_rate must be at least 8000")
        if not self.key_codes:
            raise ConfigError("key_codes must not be empty")
        if not self.input_device_name.strip():
            raise ConfigError("input_device_name must not be empty")
        if self.paste_key not in VALID_PASTE_KEYS:
            raise ConfigError(
                f"paste_key must be one of {VALID_PASTE_KEYS}, "
                f"is {self.paste_key!r}"
            )
        if not self.paste_binary.strip():
            raise ConfigError("paste_binary must not be empty")


def _coerce(name: str, value: Any, target_type: Any) -> Any:
    """Converts a TOML value to the expected type."""
    try:
        if target_type is bool:
            if not isinstance(value, bool):
                raise TypeError(value)
            return value
        if target_type is int:
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(value)
            return value
        if target_type is float:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(value)
            return float(value)
        if target_type is str:
            if not isinstance(value, str):
                raise TypeError(value)
            return value
        if target_type is list:
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise TypeError(value)
            return list(value)
    except TypeError as exc:
        raise ConfigError(
            f"{name}: wrong type, expected {getattr(target_type, '__name__', target_type)}"
        ) from exc
    return value


def config_from_mapping(data: dict[str, Any]) -> Config:
    """Builds a :class:`Config` from a mapping and validates it."""
    known = {f.name: f for f in fields(Config)}
    unknown = set(data) - set(known)
    if unknown:
        raise ConfigError(
            "unknown configuration keys: " + ", ".join(sorted(unknown))
        )

    kwargs: dict[str, Any] = {}
    for name, value in data.items():
        target = known[name].type
        if isinstance(target, str):  # from __future__ import annotations -> "str"
            target = {"str": str, "int": int, "float": float, "bool": bool,
                      "list[str]": list}[target]
        kwargs[name] = _coerce(name, value, target)

    cfg = Config(**kwargs)
    cfg.validate()
    return cfg


def load_config(
    path: Path | str | None = None,
    *,
    override: dict[str, Any] | None = None,
) -> Config:
    """Loads the configuration.

    Args:
        path: Path to the TOML file. If absent, ``DEFAULT_CONFIG_PATH``
            is used; if that is also absent, the defaults apply.
        override: Values that override the file (e.g. CLI arguments).
    """
    target = Path(path) if path is not None else DEFAULT_CONFIG_PATH

    data: dict[str, Any] = {}
    if target.is_file():
        try:
            with target.open("rb") as fh:
                data = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{target}: invalid TOML file ({exc})") from exc
        except OSError as exc:
            raise ConfigError(f"{target}: not readable ({exc})") from exc

    if override:
        data = {**data, **override}

    return config_from_mapping(data)
