"""Tests fuer das Trigger-Logging im KeyListener.

Hintergrund: Auf diesem ThinkPad tauchen im Journal Phantom-F24-Events auf,
wenn niemand die Copilot-Taste drueckt. Ohne Log bei Press/Release ist nicht
zuordenbar, ob das Diktat von einem Phantom-Event oder vom Nutzer stammt.

Es wird NUR geloggt - keine Verhaltensaenderung (kein Debounce), damit die
Push-to-Talk-Latenz unveraendert bleibt.
"""
from __future__ import annotations

import logging

from stt_button.keys import EV_KEY, InputDevice, KeyEvent, KeyListener


class FakeDevice:
    """Simuliert ein /dev/input/eventX (liefert nur das Gedrueckte)."""

    def __init__(self, path: str, name: str = "keyd virtual keyboard") -> None:
        self.path = path
        self.name = name
        self._queue: list[bytes] = []

    def push(self, type_: int, code: int, value: int) -> None:
        import struct

        self._queue.append(struct.pack("qqHHi", 0, 0, type_, code, value))

    def read(self, size: int = 4096) -> bytes:
        out = b"".join(self._queue)
        self._queue.clear()
        return out


def _listener(device: FakeDevice) -> tuple[KeyListener, list[KeyEvent], list[KeyEvent]]:
    presses: list[KeyEvent] = []
    releases: list[KeyEvent] = []
    listener = KeyListener(
        [InputDevice(device.path, device.name)],
        ["KEY_F24"],
        on_press=lambda e: presses.append(e),
        on_release=lambda e: releases.append(e),
        reader=device,
    )
    return listener, presses, releases


class TestTriggerLogging:
    def test_press_is_logged_with_code_and_device(self, caplog) -> None:
        dev = FakeDevice("/dev/input/event14", "keyd virtual keyboard")
        listener, presses, _ = _listener(dev)
        dev.push(EV_KEY, 194, 1)
        with caplog.at_level(logging.INFO, logger="stt_button.keys"):
            listener.process_pending()
        assert len(presses) == 1
        joined = " | ".join(r.getMessage() for r in caplog.records)
        assert "Trigger" in joined
        assert "194" in joined
        assert "keyd virtual keyboard" in joined

    def test_release_is_logged(self, caplog) -> None:
        dev = FakeDevice("/dev/input/event14", "keyd virtual keyboard")
        listener, _, releases = _listener(dev)
        dev.push(EV_KEY, 194, 0)
        with caplog.at_level(logging.INFO, logger="stt_button.keys"):
            listener.process_pending()
        assert len(releases) == 1
        joined = " | ".join(r.getMessage() for r in caplog.records)
        assert "Trigger" in joined
        assert "194" in joined

    def test_other_keys_are_not_logged_as_trigger(self, caplog) -> None:
        dev = FakeDevice("/dev/input/event14")
        listener, presses, _ = _listener(dev)
        dev.push(EV_KEY, 30, 1)  # KEY_A
        with caplog.at_level(logging.INFO, logger="stt_button.keys"):
            listener.process_pending()
        assert presses == []
        joined = " | ".join(r.getMessage() for r in caplog.records)
        assert "Trigger" not in joined

    def test_autorepeat_is_not_logged(self, caplog) -> None:
        dev = FakeDevice("/dev/input/event14")
        listener, presses, _ = _listener(dev)
        dev.push(EV_KEY, 194, 1)
        listener.process_pending()
        caplog.clear()
        dev.push(EV_KEY, 194, 2)
        with caplog.at_level(logging.INFO, logger="stt_button.keys"):
            listener.process_pending()
        joined = " | ".join(r.getMessage() for r in caplog.records)
        assert "Trigger" not in joined

    def test_logging_does_not_change_behaviour(self) -> None:
        """Kein Debounce: ein Press bleibt genau ein Press."""
        dev = FakeDevice("/dev/input/event14")
        listener, presses, releases = _listener(dev)
        dev.push(EV_KEY, 194, 1)
        device_events = listener.process_pending()
        assert device_events is None
        assert len(presses) == 1
        assert releases == []
