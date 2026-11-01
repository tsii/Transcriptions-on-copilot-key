"""Detection of the trigger key.

Why a custom reader instead of ``python-evdev``?
    ``python-evdev`` builds against the kernel headers and cannot be
    installed without sudo. The reader used here has no dependencies:
    ``/dev/input/eventX`` provides ``struct input_event`` records that can
    be read directly.

Why a separate device instead of the physical keyboard?
    On this ThinkPad, the Wayland compositor (mutter) grabs the physical
    keyboard, and ``keyd`` grabs it as well. A normal user process sees no
    key presses there. However, ``keyd`` maps the Copilot key to F24 and
    emits it again on the virtual device "keyd virtual keyboard"; this
    device is readable by the user.

The device is therefore **resolved dynamically** (keyd device numbers change
when it reloads) and identified by its name.
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

# --- Kernel constants ----------------------------------------------------
EV_SYN = 0x00
EV_KEY = 0x01
EV_MSC = 0x04

#: Size of ``struct input_event`` with 64-bit timeval.
EVENT_SIZE = 24
EVENT_FORMAT = "qqHHi"

#: Selection of Linux key codes relevant to the trigger key.
#: The complete set is in ``/usr/include/linux/input-event-codes.h``;
#: these are the values relevant to keyboards.
KEY_CODES: dict[str, int] = {
    # Function keys
    "KEY_F1": 59, "KEY_F2": 60, "KEY_F3": 61, "KEY_F4": 62, "KEY_F5": 63,
    "KEY_F6": 64, "KEY_F7": 65, "KEY_F8": 66, "KEY_F9": 67, "KEY_F10": 68,
    "KEY_F11": 87, "KEY_F12": 88, "KEY_F13": 183, "KEY_F14": 184,
    "KEY_F15": 185, "KEY_F16": 186, "KEY_F17": 187, "KEY_F18": 188,
    "KEY_F19": 189, "KEY_F20": 190, "KEY_F21": 191, "KEY_F22": 192,
    "KEY_F23": 193, "KEY_F24": 194,
    # Special keys present on some laptops
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

#: Reverse mapping (value -> name) for calibration.
KEY_CODES_BY_VALUE: dict[int, str] = {v: k for k, v in KEY_CODES.items()}


class KeyListenerError(RuntimeError):
    """The key device could not be read."""


@dataclass(frozen=True)
class InputDevice:
    """An input device."""

    path: Path
    name: str


@dataclass(frozen=True)
class KeyEvent:
    """A key event."""

    code: int
    value: int

    @property
    def is_press(self) -> bool:
        """Was the key just pressed?"""
        return self.value == 1

    @property
    def is_release(self) -> bool:
        """Was the key just released?"""
        return self.value == 0

    @property
    def is_repeat(self) -> bool:
        """Is this an auto-repeat event?"""
        return self.value == 2


class EventReader(Protocol):
    """Reads raw ``input_event`` records."""

    def read(self, size: int = 4096) -> bytes: ...


# ---------------------------------------------------------------------------
# Device resolution
# ---------------------------------------------------------------------------
def device_name(path: str | Path) -> str:
    """Read the name of an input device from sysfs."""
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
    """List all readable input devices."""
    devices: list[InputDevice] = []
    for path in sorted(glob.glob("/dev/input/event*")):
        devices.append(InputDevice(Path(path), device_name(path)))
    return devices


def find_device_by_name(
    name: str, devices: Iterable[InputDevice] | None = None
) -> InputDevice | None:
    """Find a device by its name.

    ``devices`` supports testing; if omitted, live devices are searched.
    """
    if devices is None:
        devices = list_input_devices()
    for device in devices:
        if device.name == name:
            return device
    return None


def resolve_device(name: str) -> InputDevice:
    """Find a device or raise a clear error message."""
    device = find_device_by_name(name)
    if device is None:
        available = ", ".join(
            f"{d.path} ({d.name})" for d in list_input_devices()
        )
        raise KeyListenerError(
            f"No input device named {name!r} found. "
            f"Available: {available}"
        )
    return device


# ---------------------------------------------------------------------------
# Listener
# ---------------------------------------------------------------------------
class KeyListener:
    """Detect presses and releases of the trigger key.

    ``key_codes`` is a list of Linux key codes. If multiple codes are given,
    the key triggers only when **all** are pressed simultaneously.
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
                f"Unknown key codes: {', '.join(unknown)}. "
                f"Examples of allowed values: {', '.join(sorted(KEY_CODES)[:5])} ..."
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

    # -- Setup ----------------------------------------------------------
    def open(self) -> None:
        """Open the devices for reading (idempotent)."""
        if self._reader is not None:
            return
        if self._fds:
            return
        for device in self.devices:
            try:
                fd = os.open(device.path, os.O_RDONLY | os.O_NONBLOCK)
            except OSError as exc:
                raise KeyListenerError(
                    f"{device.path} ({device.name}) is unreadable: {exc}"
                ) from exc
            self._fds[fd] = device

    def close(self) -> None:
        """Close all devices."""
        self._closed = True
        for fd in self._fds:
            try:
                os.close(fd)
            except OSError:
                pass
        self._fds.clear()

    # -- Processing -----------------------------------------------------
    def process_pending(self) -> None:
        """Read and process all pending events."""
        if self._reader is not None:
            try:
                data = self._reader.read()
            except OSError as exc:
                raise KeyListenerError(f"Read error: {exc}") from exc
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
                        f"{device.path} ({device.name}): read error ({exc})"
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

    def _device_names(self) -> str:
        """Device names for logging (multiple devices are possible)."""
        names = [d.name for d in self.devices]
        return ", ".join(names) if names else "?"

    def _handle(self, code: int, value: int) -> None:
        if code not in self._wanted:
            return

        if value == 1:
            if code in self._pressed:
                return
            self._pressed.add(code)
            # Phantom events (e.g., F24 without a key press) can only be
            # correlated in the journal if every trigger is logged.
            # Deliberately NO debounce: it would worsen push-to-talk latency.
            log.info(
                "Trigger press: code=%d device=%s", code, self._device_names()
            )
            if self._all_pressed():
                self._on_press(KeyEvent(code=code, value=value))
            return

        if value == 0:
            was_pressed = code in self._pressed
            self._pressed.discard(code)
            # A release is reported even if the press was not observed
            # (e.g., a key pressed before the service started). For
            # combinations, it is reported only once no key remains pressed.
            if not self._pressed:
                log.info(
                    "Trigger release: code=%d device=%s", code, self._device_names()
                )
                self._on_release(KeyEvent(code=code, value=value))
            elif was_pressed:
                # Partial release of a combination: no trigger yet.
                pass
            return

        # value == 2 -> auto-repeat; ignore.

    def _all_pressed(self) -> bool:
        return self._wanted.issubset(self._pressed)

    # -- Operation ------------------------------------------------------
    def run(self) -> None:
        """Block and process input until ``close()``."""
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
