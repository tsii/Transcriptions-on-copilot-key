"""Interaktives Kalibrierungstool fuer die Trigger-Taste.

Hilft dem Nutzer, Device und Keycode der Copilot-Taste zu ermitteln und in
die ``config.toml`` zu schreiben.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from .config import CONFIG_DIR, DEFAULT_CONFIG_PATH
from .keys import (
    KEY_CODES,
    KEY_CODES_BY_VALUE,
    KeyEvent,
    KeyListener,
    find_device_by_name,
    list_input_devices,
)

log = logging.getLogger(__name__)


def calibrate(args) -> int:
    """Interaktiv kalibrieren und die Config schreiben."""
    print("=== STT-Button Kalibrierung ===\n")

    devices = list_input_devices()
    print("Verfuegbare Tastatur-Devices:")
    for i, d in enumerate(devices):
        print(f"  [{i}] {d.path}  {d.name}")

    # Bevorzugt das keyd-Device, da die Copilot-Taste dort auf F24 liegt.
    default = find_device_by_name("keyd virtual keyboard", devices)
    if default is None:
        # Sonst irgendein Keyboard-artiges Device.
        keyboard_like = [d for d in devices
                         if "keyboard" in d.name.lower() or "kbd" in d.name.lower()]
        default = keyboard_like[0] if keyboard_like else (devices[0] if devices else None)

    if default is None:
        print("Kein input-Device gefunden. Bist du in Gruppe 'input'?")
        return 1

    print(f"\nNutze Device: {default.path} ({default.name})")
    print("Druecke jetzt die Copilot-Taste (mehrere Sekunden halten).\n")

    events: list[KeyEvent] = []

    def on_press(ev: KeyEvent) -> None:
        events.append(ev)
        name = KEY_CODES_BY_VALUE.get(ev.code, f"KEY_{ev.code}")
        print(f"  >>> Tastendruck: {name} (code={ev.code})")

    def on_release(ev: KeyEvent) -> None:
        print(f"  <<< Losgelassen: code={ev.code}")

    listener = KeyListener(
        [default], [name for name in KEY_CODES], on_press=on_press, on_release=on_release
    )
    listener.open()

    deadline = time.time() + 15.0
    try:
        while time.time() < deadline and not events:
            listener.process_pending()
            time.sleep(0.05)
    finally:
        listener.close()

    if not events:
        print("\nKein Tastendruck erkannt.")
        print("Hinweis: Auf ThinkPads wird die Copilot-Taste von keyd auf F24")
        print("gemappt und erscheint auf dem 'keyd virtual keyboard'.")
        print("Versuche die Taste erneut, oder waehle ein anderes Device.")
        return 1

    code = events[0].code
    key_name = KEY_CODES_BY_VALUE.get(code, f"KEY_{code}")
    print(f"\nErkannte Taste: {key_name} (code={code}) auf {default.name}")

    config_path = args.config if getattr(args, "config", None) else DEFAULT_CONFIG_PATH
    write_config(config_path, default.name, key_name)
    print(f"Config wurde nach {config_path} geschrieben.")
    print("Starte danach mit: stt-button run")
    return 0


def write_config(path, device_name: str, key_name: str) -> None:
    """Schreibt eine minimale Config."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    content = (
        f'# STT-Button Konfiguration (automatisch kalibriert)\n'
        f'input_device_name = "{device_name}"\n'
        f'key_codes = ["{key_name}"]\n'
    )
    path = Path(path)
    if path.exists():
        # Nur anhaengen ohne zu ueberschreiben - aber einfach fuer jetzt:
        existing = path.read_text(encoding="utf-8")
        if "input_device_name" in existing:
            print("(bestehende Konfiguration blieb unveraendert)")
            return
    path.write_text(content, encoding="utf-8")