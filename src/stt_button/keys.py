"""Erkennung der Trigger-Taste.

Warum ein eigener Leser statt ``python-evdev``?
    ``python-evdev`` baut gegen die Kernel-Header und laesst sich ohne sudo
    nicht installieren. Der hier verwendete Leser kommt ohne Abhaengigkeiten
    aus: ``/dev/input/eventX`` liefert ``struct input_event``-Datensaetze,
    die direkt gelesen werden koennen.

Warum ein eigenes Device statt der physischen Tastatur?
    Auf diesem ThinkPad grabbt der Wayland-Compositor (mutter) die physische
    Tastatur, und ``keyd`` grabbt sie ebenfalls. Ein normaler User-Prozess
    sieht dort keine Tastendruecke. Die Copilot-Taste wird aber von ``keyd``
    auf F24 gemappt und auf dem virtuellen Device "keyd virtual keyboard"
    wieder ausgegeben - dieses Device ist als User lesbar.

Das Device wird deshalb **dynamisch aufgeloest** (Nummern von keyd wechseln
beim Neuladen) und ueber seinen Namen identifiziert.
"""
from __future__ import annotations

import glob
import logging
import os
import select
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Protocol

log = logging.getLogger(__name__)

# --- Kernel-Konstanten ---------------------------------------------------
EV_SYN = 0x00
EV_KEY = 0x01
EV_MSC = 0x04

#: Groesse von ``struct input_event`` bei 64-Bit-timeval.
EVENT_SIZE = 24
EVENT_FORMAT = "qqHHi"

#: Auswahl der Linux-Keycodes, die fuer die Trigger-Taste interessant sind.
#: Der vollstaendige Satz steht in ``/usr/include/linux/input-event-codes.h``;
#: hier die fuer Tastaturen relevanten Werte.
KEY_CODES: dict[str, int] = {
    # F-Tasten
    "KEY_F1": 59, "KEY_F2": 60, "KEY_F3": 61, "KEY_F4": 62, "KEY_F5": 63,
    "KEY_F6": 64, "KEY_F7": 65, "KEY_F8": 66, "KEY_F9": 67, "KEY_F10": 68,
    "KEY_F11": 87, "KEY_F12": 88, "KEY_F13": 183, "KEY_F14": 184,
    "KEY_F15": 185, "KEY_F16": 186, "KEY_F17": 187, "KEY_F18": 188,
    "KEY_F19": 189, "KEY_F20": 190, "KEY_F21": 191, "KEY_F22": 192,
    "KEY_F23": 193, "KEY_F24": 194,
    # Sondertasten, die manche Laptops belegen
    "KEY_MACRO_RECORD_START": 0x2B0,
    "KEY_MACRO_RECORD_STOP": 0x2B1,
    "KEY_MACRO_PRESET_CYCLE": 0x2B2,
    "KEY_MACRO_PRESET1": 0x2B3,
    "KEY_MACRO_PRESET2": 0x2B4,
    "KEY_MACRO_PRESET3": 0x2B5,
    "KEY_VOICECOMMAND": 582,
    "KEY_SEARCH": 217,
    "KEY_CHAT": 216,
    "KEY_CONNECT": 218,
    "KEY_CANCEL": 223,
    "KEY_CONFIG": 171,
    "KEY_PROG1": 148, "KEY_PROG2": 149, "KEY_PROG3": 202, "KEY_PROG4": 203,
    "KEY_UNKNOWN": 240,
    "KEY_VENDOR": 360,
    # Modifier
    "KEY_LEFTMETA": 125, "KEY_RIGHTMETA": 126,
    "KEY_LEFTALT": 56, "KEY_RIGHTALT": 100,
    "KEY_LEFTSHIFT": 42, "KEY_RIGHTSHIFT": 54,
    "KEY_LEFTCTRL": 29, "KEY_RIGHTCTRL": 97,
}

#: Umgekehrte Zuordnung (Wert -> Name) fuer die Kalibrierung.
KEY_CODES_BY_VALUE: dict[int, str] = {v: k for k, v in KEY_CODES.items()}


class KeyListenerError(RuntimeError):
    """Das Tasten-Device konnte nicht gelesen werden."""


@dataclass(frozen=True)
class InputDevice:
    """Ein input-Device."""

    path: Path
    name: str


@dataclass(frozen=True)
class KeyEvent:
    """Ein Tastenereignis."""

    code: int
    value: int

    @property
    def is_press(self) -> bool:
        """Wurde die Taste gerade gedrueckt?"""
        return self.value == 1

    @property
    def is_release(self) -> bool:
        """Wurde die Taste gerade losgelassen?"""
        return self.value == 0

    @property
    def is_repeat(self) -> bool:
        """Ist es ein Autorepeat-Ereignis?"""
        return self.value == 2


class EventReader(Protocol):
    """Liest rohe ``input_event``-Datensaetze."""

    def read(self, size: int = 4096) -> bytes: ...


# ---------------------------------------------------------------------------
# Device-Aufloesung
# ---------------------------------------------------------------------------
def device_name(path: str | Path) -> str:
    """Liest den Namen eines input-Devices aus sysfs."""
    base = os.path.basename(str(path))
    uevent = Path(f"/sys/class/input/{base}/device/uevent")
    try:
        for line in uevent.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("NAME="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return "?"


def list_input_devices() -> list[InputDevice]:
    """Listet alle lesbaren input-Devices auf."""
    devices: list[InputDevice] = []
    for path in sorted(glob.glob("/dev/input/event*")):
        devices.append(InputDevice(Path(path), device_name(path)))
    return devices


def find_device_by_name(
    name: str, devices: Iterable[InputDevice] | None = None
) -> InputDevice | None:
    """Findet ein Device anhand seines Namens.

    ``devices`` dient der Testbarkeit; ohne Angabe wird live gesucht.
    """
    if devices is None:
        devices = list_input_devices()
    for device in devices:
        if device.name == name:
            return device
    return None


def resolve_device(name: str) -> InputDevice:
    """Findet ein Device oder wirft eine klare Fehlermeldung."""
    device = find_device_by_name(name)
    if device is None:
        available = ", ".join(
            f"{d.path} ({d.name})" for d in list_input_devices()
        )
        raise KeyListenerError(
            f"Kein input-Device namens {name!r} gefunden. "
            f"Verfuegbar: {available}"
        )
    return device


# ---------------------------------------------------------------------------
# Listener
# ---------------------------------------------------------------------------
class KeyListener:
    """Erkennt Druck und Loslassen der Trigger-Taste.

    ``key_codes`` ist eine Liste von Linux-Keycodes. Sind mehrere angegeben,
    loest die Taste erst aus, wenn **alle** gleichzeitig gedrueckt sind.
    """

    def __init__(
        self,
        devices: list[InputDevice],
        key_codes: list[str],
        *,
        on_press: Callable[[KeyEvent], None],
        on_release: Callable[[KeyEvent], None],
        reader: EventReader | None = None,
    ) -> None:
        unknown = [c for c in key_codes if c not in KEY_CODES]
        if unknown:
            raise KeyListenerError(
                f"Unbekannte Keycodes: {', '.join(unknown)}. "
                f"Erlaubt sind z.B. {', '.join(sorted(KEY_CODES)[:5])} ..."
            )
        self.devices = devices
        self.key_codes = list(key_codes)
        self._wanted = {KEY_CODES[c] for c in key_codes}
        self._on_press = on_press
        self._on_release = on_release
        self._reader = reader
        self._pressed: set[int] = set()
        self._fds: dict[int, InputDevice] = {}
        self._closed = False

    # -- Aufbau ---------------------------------------------------------
    def open(self) -> None:
        """Oeffnet die Devices fuer das Lesen (idempotent)."""
        if self._reader is not None:
            return
        if self._fds:
            return
        for device in self.devices:
            try:
                fd = os.open(device.path, os.O_RDONLY | os.O_NONBLOCK)
            except OSError as exc:
                raise KeyListenerError(
                    f"{device.path} ({device.name}) nicht lesbar: {exc}"
                ) from exc
            self._fds[fd] = device

    def close(self) -> None:
        """Schliesst alle Devices."""
        self._closed = True
        for fd in self._fds:
            try:
                os.close(fd)
            except OSError:
                pass
        self._fds.clear()

    # -- Auswertung ----------------------------------------------------
    def process_pending(self) -> None:
        """Liest alle anliegenden Events aus und wertet sie aus."""
        if self._reader is not None:
            try:
                data = self._reader.read()
            except OSError as exc:
                raise KeyListenerError(f"Lesefehler: {exc}") from exc
            self._process(data)
            return
        for fd, device in list(self._fds.items()):
            while True:
                try:
                    data = os.read(fd, 4096)
                except BlockingIOError:
                    break
                except OSError as exc:
                    raise KeyListenerError(
                        f"{device.path} ({device.name}): Lesefehler ({exc})"
                    ) from exc
                if not data:
                    break
                self._process(data)

    def _process(self, data: bytes) -> None:
        usable = len(data) - (len(data) % EVENT_SIZE)
        for offset in range(0, usable, EVENT_SIZE):
            _t, _u, type_, code, value = struct.unpack(
                EVENT_FORMAT, data[offset:offset + EVENT_SIZE]
            )
            if type_ != EV_KEY:
                continue
            self._handle(code, value)

    def _handle(self, code: int, value: int) -> None:
        if code not in self._wanted:
            return

        if value == 1:
            if code in self._pressed:
                return
            self._pressed.add(code)
            if self._all_pressed():
                self._on_press(KeyEvent(code=code, value=value))
            return

        if value == 0:
            was_pressed = code in self._pressed
            self._pressed.discard(code)
            # Ein Loslassen wird auch gemeldet, wenn der Druck nicht
            # mitbekommen wurde (z.B. Tastendruck vor dem Start des
            # Dienstes). Bei Kombinationen wird erst gemeldet, wenn
            # keine Taste mehr gedrueckt ist.
            if not self._pressed:
                self._on_release(KeyEvent(code=code, value=value))
            elif was_pressed:
                # Teilstelle einer Kombination: noch kein Trigger.
                pass
            return

        # value == 2 -> Autorepeat, ignorieren.

    def _all_pressed(self) -> bool:
        return self._wanted.issubset(self._pressed)

    # -- Betrieb --------------------------------------------------------
    def run(self) -> None:
        """Blockiert und verarbeitet Eingaben bis ``close()``."""
        self.open()
        fds = list(self._fds)
        try:
            while not self._closed:
                if not fds:
                    break
                select.select(fds, [], [], 0.5)
                self.process_pending()
        except KeyboardInterrupt:
            pass
        finally:
            self.close()
