"""Tests fuer die Tastenerkennung (TDD-Kette 4).

Der KeyListener liest /dev/input/event* direkt (reines Python, kein evdev-Paket
noetig). Getestet wird gegen eine gefaelschte Event-Quelle.
"""
from __future__ import annotations

import pytest

from stt_button.keys import (
    EV_KEY,
    EV_SYN,
    InputDevice,
    KeyEvent,
    KeyListener,
    KeyListenerError,
    device_name,
    find_device_by_name,
    list_input_devices,
)


class FakeDevice:
    """Simuliert ein /dev/input/eventX."""

    def __init__(self, path: str, name: str = "keyd virtual keyboard") -> None:
        self.path = path
        self.name = name
        self.closed = False
        self.events: list[tuple[int, int, int]] = []
        self._queue: list[tuple[int, int, int]] = []

    def push(self, type_: int, code: int, value: int) -> None:
        self._queue.append((type_, code, value))

    def read(self, size: int = 4096) -> bytes:
        if self.closed:
            raise OSError("closed")
        out = b""
        import struct

        for type_, code, value in self._queue:
            out += struct.pack("qqHHi", 0, 0, type_, code, value)
        self._queue.clear()
        return out


class TestDeviceDiscovery:
    def test_find_device_by_name(self, tmp_path) -> None:
        dev = tmp_path / "event14"
        dev.write_text("")
        found = find_device_by_name(
            "keyd virtual keyboard", [InputDevice(dev, "keyd virtual keyboard")]
        )
        assert found is not None
        assert found.path == dev

    def test_find_device_returns_none_when_absent(self, tmp_path) -> None:
        assert find_device_by_name("gibt es nicht", []) is None

    def test_list_input_devices_reads_sysfs(self, tmp_path) -> None:
        assert list_input_devices() is not None

    def test_device_name_handles_missing_sysfs(self) -> None:
        assert device_name("/dev/input/event99999") == "?"


class TestKeyEvent:
    def test_press_and_release(self) -> None:
        ev = KeyEvent(code=194, value=1)
        assert ev.is_press
        assert not ev.is_release

    def test_release(self) -> None:
        ev = KeyEvent(code=194, value=0)
        assert ev.is_release
        assert not ev.is_press

    def test_repeat_is_neither_press_nor_release(self) -> None:
        ev = KeyEvent(code=194, value=2)
        assert not ev.is_press
        assert not ev.is_release


class TestKeyListener:
    def _listener(self, device: FakeDevice, **kwargs) -> KeyListener:
        presses: list[KeyEvent] = []
        releases: list[KeyEvent] = []
        listener = KeyListener(
            [InputDevice(device.path, device.name)],
            ["KEY_F24"],
            on_press=lambda e: presses.append(e),
            on_release=lambda e: releases.append(e),
            reader=device,
            **kwargs,
        )
        return listener, presses, releases

    def test_press_triggers_callback(self) -> None:
        dev = FakeDevice("/dev/input/event14")
        listener, presses, releases = self._listener(dev)
        dev.push(EV_KEY, 194, 1)
        listener.process_pending()
        assert len(presses) == 1
        assert presses[0].code == 194

    def test_release_triggers_callback(self) -> None:
        dev = FakeDevice("/dev/input/event14")
        listener, presses, releases = self._listener(dev)
        dev.push(EV_KEY, 194, 0)
        listener.process_pending()
        assert len(releases) == 1

    def test_autorepeat_is_ignored(self) -> None:
        dev = FakeDevice("/dev/input/event14")
        listener, presses, releases = self._listener(dev)
        dev.push(EV_KEY, 194, 1)
        dev.push(EV_KEY, 194, 2)
        dev.push(EV_KEY, 194, 2)
        listener.process_pending()
        assert len(presses) == 1
        assert len(releases) == 0

    def test_other_key_is_ignored(self) -> None:
        dev = FakeDevice("/dev/input/event14")
        listener, presses, releases = self._listener(dev)
        dev.push(EV_KEY, 30, 1)  # KEY_A
        listener.process_pending()
        assert presses == []
        assert releases == []

    def test_syn_events_are_ignored(self) -> None:
        dev = FakeDevice("/dev/input/event14")
        listener, presses, releases = self._listener(dev)
        dev.push(EV_SYN, 0, 0)
        listener.process_pending()
        assert presses == []

    def test_press_then_release(self) -> None:
        dev = FakeDevice("/dev/input/event14")
        listener, presses, releases = self._listener(dev)
        dev.push(EV_KEY, 194, 1)
        dev.push(EV_KEY, 194, 0)
        listener.process_pending()
        assert len(presses) == 1
        assert len(releases) == 1

    def test_multiple_codes_require_all(self) -> None:
        dev = FakeDevice("/dev/input/event14")
        presses: list[KeyEvent] = []
        releases: list[KeyEvent] = []
        listener = KeyListener(
            [InputDevice(dev.path, dev.name)],
            ["KEY_F24", "KEY_F23"],
            on_press=lambda e: presses.append(e),
            on_release=lambda e: releases.append(e),
            reader=dev,
        )
        dev.push(EV_KEY, 194, 1)
        listener.process_pending()
        # Nur F24 gedrueckt -> noch kein Trigger
        assert presses == []
        dev.push(EV_KEY, 193, 1)
        listener.process_pending()
        assert len(presses) == 1

    def test_unknown_code_is_rejected(self) -> None:
        dev = FakeDevice("/dev/input/event14")
        with pytest.raises(KeyListenerError):
            KeyListener(
                [InputDevice(dev.path, dev.name)],
                ["KEY_TOTALLY_UNKNOWN"],
                on_press=lambda e: None,
                on_release=lambda e: None,
                reader=dev,
            )

    def test_reader_error_raises(self) -> None:
        dev = FakeDevice("/dev/input/event14")
        dev.closed = True
        listener, presses, releases = self._listener(dev)
        with pytest.raises(KeyListenerError):
            listener.process_pending()

    def test_release_without_press_does_not_crash(self) -> None:
        dev = FakeDevice("/dev/input/event14")
        listener, presses, releases = self._listener(dev)
        dev.push(EV_KEY, 194, 0)
        listener.process_pending()
        assert len(releases) == 1
