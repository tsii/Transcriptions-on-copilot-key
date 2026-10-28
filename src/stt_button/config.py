"""Konfiguration fuer STT-Button.

Die Konfiguration liegt standardmaessig in ``~/.config/stt-button/config.toml``
und wird ueber :func:`load_config` geladen. Fehlende Werte fallen auf Defaults
zurueck; ungueltige Werte werden mit einer klaren Meldung abgelehnt.
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
#: Finalisierungs-Modus: "full" = Puffer neu transkribieren (bewährt),
#: "cached" = unveraenderten Puffer aus dem letzten erfolgreichen Tick
#: uebernehmen (spart einen kompletten Engine-Durchlauf).
VALID_FINALIZE_MODES = ("full", "cached")
#: WAV-Dump der letzten Aufnahme: "sync" = im Finalizer-Thread (bewährt),
#: "async" = eigener Thread mit Bytes-Snapshot, "off" = kein Dump.
VALID_AUDIO_DUMP_MODES = ("sync", "async", "off")


class ConfigError(ValueError):
    """Fehlerhafte oder unbrauchbare Konfiguration."""


@dataclass(frozen=True)
class Config:
    """Alle Einstellungen der Anwendung."""

    # --- Ausloeser -------------------------------------------------------
    #: Name des input-Devices, auf dem die Trigger-Taste erscheint.
    #: "keyd virtual keyboard" ist der Default fuer ThinkPads, auf denen der
    #: Copilot-Key per keyd auf F24 gemappt wird.
    input_device_name: str = "keyd virtual keyboard"
    #: Linux-Keycodes, die den Push-to-Talk ausloesen (alle muessen gedrueckt sein).
    key_codes: list[str] = field(default_factory=lambda: ["KEY_F24"])

    # --- Audio -----------------------------------------------------------
    #: PipeWire-Quellenname. "auto" waehlt die erste sinnvolle Quelle.
    audio_source: str = "auto"
    sample_rate: int = 16000

    # --- ASR -------------------------------------------------------------
    #: Engine-Backend: "whisper" (faster-whisper) oder "parakeet" (NVIDIA TDT).
    backend: str = "parakeet"
    model: str = "nvidia/parakeet-tdt-0.6b-v3"
    language: str = "auto"
    compute_type: str = "int8"
    cpu_threads: int = field(default_factory=lambda: os.cpu_count() or 4)
    # --- Streaming ---------------------------------------------------------
    #: Streaming-Modus: "windowed" (Fenster + Zeitstempel-Commit),
    #: "whole_buffer" (alter Ansatz, ganzer Puffer re-transkribiert) oder
    #: "buffered" (STUFE 3: Hybrid Volllpuffer -> bounded Fenster).
    #: Default bleibt "whole_buffer" - der neue Modus ist ein Feature-Flag.
    streaming_mode: str = "whole_buffer"
    #: Mindest-Audiolaenge (Sekunden), bevor ueberhaupt transkribiert wird.
    #: Zu kurze Chunks lassen den Transducer Satzanfaenge falsch gross
    #: schreiben und Woerter verschlucken (Halluzination aus isolierten
    #: 1s-Fetzen). 3s gibt dem Modell genug Kontext fuer den ersten Lauf.
    min_audio_s: float = 3.0
    #: Intervall (Sekunden Audio), in dem neu transkribiert wird.
    asr_interval_s: float = 1.0
    #: Maximale Aufnahmedauer (Sekunden) -> automatische Finalisierung.
    max_record_s: float = 300.0
    #: Anzahl aufeinanderfolgender identischer Praefixe, bis ein Praefix
    #: als "committed" gilt und nie mehr zurueckgenommen wird.
    commit_agreements: int = 2

    # --- Buffered-Modus (STUFE 3, nur bei streaming_mode="buffered") ----
    #: Bis zu dieser Pufferlaenge wird der ganze Puffer transkribiert.
    buffered_whole_buffer_until_s: float = 8.0
    #: Links-Kontext des Fensters in Sekunden (Qualitaetsanker).
    buffered_left_context_s: float = 8.0
    #: Neues stabiles Segment pro Tick in Sekunden.
    buffered_chunk_s: float = 2.0
    #: Rechts-Kontext (Lookahead) nach dem Commit-Horizont in Sekunden.
    buffered_right_context_s: float = 2.0
    #: Hartes Budget fuer Finalisierungs-/Catch-up-Fenster in Sekunden.
    buffered_max_final_window_s: float = 18.0

    # --- Ausgabe ---------------------------------------------------------
    #: Typ-Ausgabe-Adapter: "dotool" (Default, funktioniert unter GNOME/Wayland),
    #: "wtype" oder "clipboard" (Fallback via Zwischenablage).
    typer: str = "dotool"
    #: XKB-Tastaturlayout fuer dotool (de fuer deutsche Tastatur).
    xkb_layout: str = "de"
    xkb_variant: str = "nodeadkeys"
    #: Dotool-Tipptiming. Zu klein -> unter CPU-Last koennen Key-Events
    #: verloren gehen und Modifier (Shift/CapsLock) "haengen" -> der Text
    #: erscheint dann invertiert/gross. Bei Problemen erhoehen (z.B. 30/25).
    typedelay_ms: int = 20
    typehold_ms: int = 15
    #: Soll die App CapsLock vor dem Tippen zwangsweise einschalten/ausschalten?
    #: Standard ist **False**: der Lock-State ist nicht zuverlaessig lesbar
    #: (LED != State, xset != State), deshalb wird nur gewarnt. Wer den
    #: Eingriff trotzdem will, setzt den Wert explizit auf True.
    capslock_autocorrect: bool = False
    #: Semantik der CapsLock-LED dieses Systems. Ground Truth (empirisch
    #: belegt, ThinkPad + keyd + GNOME/Wayland): LED **aus** = CapsLock
    #: aktiv = invertierter Text; LED **an** = normale Schreibung. Das ist
    #: INVERTIERT zur ueblichen Konvention, daher True als Default. Bei
    #: capslock_autocorrect=True ist das Ziel der LED-Zustand "an" (normal).
    capslock_led_inverted: bool = True
    append_space: bool = True
    #: Soll das finale Delta beim Abschluss (finalize) per Zwischenablage
    #: eingefuegt werden statt zeichenweise getippt? Das beschleunigt lange
    #: Diktate massiv (300 Zeichen * ~35 ms Tippzeit entfallen). Default
    #: False = bisheriges Verhalten (alles per dotool tippen).
    final_paste: bool = False
    #: Einfuege-Chord fuer final_paste. "ctrl+shift+v" fuer Terminals, in
    #: denen ctrl+v ein anderes Signal ausloest.
    paste_key: str = "ctrl+v"
    #: Binary, die das Delta in die Zwischenablage legt (wl-clipboard).
    paste_binary: str = "wl-copy"
    #: Maximale Laenge (Zeichen) einer Ruecknahme. Laengere Korrekturen
    #: werden vermieden, um Nutzertext nicht zu zerstoeren.
    max_revision_chars: int = 60

    # --- Latenz (STUFE 1) --------------------------------------------------
    #: "full" (Default): Finalize transkribiert den ganzen Puffer neu.
    #: "cached": unveraendertes Audio seit dem letzten erfolgreichen Tick
    #: wird ohne Engine-Call aus dem Cache uebernommen (nur bei
    #: deterministischen Engines wie Parakeet sinnvoll).
    finalize_mode: str = "full"
    #: WAV-Dump der Aufnahme nach /tmp/stt_last.wav: "sync" (Default,
    #: bisheriges Verhalten), "async" (eigener Thread, kein Blockieren des
    #: Finalizers) oder "off".
    audio_dump_mode: str = "sync"
    #: Loggt Debug-Zeiten an den Latenz-Messpunkten (Release -> Ausgabe).
    latency_trace: bool = False

    def validate(self) -> None:
        """Wirft :class:`ConfigError` bei unbrauchbaren Werten."""
        if self.language not in VALID_LANGUAGES:
            raise ConfigError(
                f"language muss eines von {VALID_LANGUAGES} sein, "
                f"nicht {self.language!r}"
            )
        if self.typer not in VALID_TYPERS:
            raise ConfigError(
                f"typer muss eines von {VALID_TYPERS} sein, nicht {self.typer!r}"
            )
        if self.backend not in VALID_BACKENDS:
            raise ConfigError(
                f"backend muss eines von {VALID_BACKENDS} sein, nicht {self.backend!r}"
            )
        if self.streaming_mode not in VALID_STREAMING_MODES:
            raise ConfigError(
                f"streaming_mode muss eines von {VALID_STREAMING_MODES} sein, "
                f"nicht {self.streaming_mode!r}"
            )
        if self.finalize_mode not in VALID_FINALIZE_MODES:
            raise ConfigError(
                f"finalize_mode muss eines von {VALID_FINALIZE_MODES} sein, "
                f"nicht {self.finalize_mode!r}"
            )
        if self.audio_dump_mode not in VALID_AUDIO_DUMP_MODES:
            raise ConfigError(
                f"audio_dump_mode muss eines von {VALID_AUDIO_DUMP_MODES} sein, "
                f"nicht {self.audio_dump_mode!r}"
            )
        if self.asr_interval_s <= 0:
            raise ConfigError("asr_interval_s muss groesser als 0 sein")
        if self.min_audio_s <= 0:
            raise ConfigError("min_audio_s muss groesser als 0 sein")
        if self.max_record_s <= 0:
            raise ConfigError("max_record_s muss groesser als 0 sein")
        if self.cpu_threads < 1:
            raise ConfigError("cpu_threads muss mindestens 1 sein")
        if self.max_revision_chars < 0:
            raise ConfigError("max_revision_chars darf nicht negativ sein")
        if self.typedelay_ms < 0:
            raise ConfigError("typedelay_ms darf nicht negativ sein")
        if self.typehold_ms < 0:
            raise ConfigError("typehold_ms darf nicht negativ sein")
        if self.commit_agreements < 1:
            raise ConfigError("commit_agreements muss mindestens 1 sein")
        for name in ("buffered_whole_buffer_until_s", "buffered_left_context_s",
                     "buffered_chunk_s", "buffered_right_context_s",
                     "buffered_max_final_window_s"):
            if getattr(self, name) <= 0:
                raise ConfigError(f"{name} muss groesser als 0 sein")
        if self.sample_rate < 8000:
            raise ConfigError("sample_rate muss mindestens 8000 sein")
        if not self.key_codes:
            raise ConfigError("key_codes darf nicht leer sein")
        if not self.input_device_name.strip():
            raise ConfigError("input_device_name darf nicht leer sein")
        if self.paste_key not in VALID_PASTE_KEYS:
            raise ConfigError(
                f"paste_key muss eines von {VALID_PASTE_KEYS} sein, "
                f"nicht {self.paste_key!r}"
            )
        if not self.paste_binary.strip():
            raise ConfigError("paste_binary darf nicht leer sein")


def _coerce(name: str, value: Any, target_type: Any) -> Any:
    """Wandelt einen TOML-Wert in den erwarteten Typ um."""
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
            f"{name}: falscher Typ, erwartet {getattr(target_type, '__name__', target_type)}"
        ) from exc
    return value


def config_from_mapping(data: dict[str, Any]) -> Config:
    """Baut eine :class:`Config` aus einem Mapping und validiert sie."""
    known = {f.name: f for f in fields(Config)}
    unknown = set(data) - set(known)
    if unknown:
        raise ConfigError(
            "unbekannte Konfigurationsschluessel: " + ", ".join(sorted(unknown))
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
    """Laedt die Konfiguration.

    Args:
        path: Pfad zur TOML-Datei. Fehlt er, wird ``DEFAULT_CONFIG_PATH``
            verwendet; existiert auch der nicht, gelten die Defaults.
        override: Werte, die die Datei ueberschreiben (z.B. CLI-Argumente).
    """
    target = Path(path) if path is not None else DEFAULT_CONFIG_PATH

    data: dict[str, Any] = {}
    if target.is_file():
        try:
            with target.open("rb") as fh:
                data = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{target}: ungueltige TOML-Datei ({exc})") from exc
        except OSError as exc:
            raise ConfigError(f"{target}: nicht lesbar ({exc})") from exc

    if override:
        data = {**data, **override}

    return config_from_mapping(data)
