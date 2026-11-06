"""B10 wiring tests: router, on_output hook and coordinator in app.py.

The core duty is the OFF-state regression: with ``summary_enabled=False``
the build must stay 1:1 with today's behavior - no coordinator object in
the lifecycle, ``on_output`` stays ``None`` and press/release behave
bit-identically (press -> start, release -> stop, never abort).

No real /dev/uinput, no wl-copy/wl-paste, no paplay, no network, no key
events: every collaborator is a fake or a pure state machine.
"""
from __future__ import annotations

import pytest

from stt_button.app import Application
from stt_button.config import Config
from stt_button.summary import SummaryBuffer


class TyperStub:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs


class PasterStub:
    def __init__(self, typer, **kwargs) -> None:
        self.typer = typer
        self.kwargs = kwargs


class EngineStub:
    def __init__(self) -> None:
        self.loaded = False

    def load(self) -> None:
        self.loaded = True


class ControllerStub:
    """Records constructor kwargs and every gesture-visible call."""

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.starts = 0
        self.stops = 0
        self.aborts = 0

    def start_recording(self) -> None:
        self.starts += 1

    def stop_recording(self) -> None:
        self.stops += 1

    def abort(self) -> None:
        self.aborts += 1

    def has_pending_work(self) -> bool:
        return False

    def run_once(self) -> None:
        pass

    def wait_for_work(self) -> None:
        pass

    def shutdown(self) -> None:
        pass


class ListenerStub:
    """Fake KeyListener: just captures the press/release callbacks."""

    def __init__(self, devices, key_codes, on_press=None, on_release=None):
        self.devices = devices
        self.key_codes = key_codes
        self.on_press = on_press
        self.on_release = on_release
        self.opened = False
        self.closed = False

    def open(self) -> None:
        self.opened = True

    def close(self) -> None:
        self.closed = True

    def run(self) -> None:
        pass


class CoordinatorStub:
    """Fake SummaryCoordinator: counts constructions and submissions."""

    instances: list["CoordinatorStub"] = []

    def __init__(self, controller, cfg, **kwargs) -> None:
        self.controller = controller
        self.cfg = cfg
        self.kwargs = kwargs
        self.submits: list = []
        self.shutdown_calls = 0
        CoordinatorStub.instances.append(self)

    def submit(self, op, undo: bool = False) -> bool:
        self.submits.append(op)
        return True

    def shutdown(self) -> None:
        self.shutdown_calls += 1


def _start_app(monkeypatch, cfg: Config) -> tuple[
        Application, ControllerStub, ListenerStub]:
    """Start an Application with all side-effecting parts faked."""
    CoordinatorStub.instances = []
    monkeypatch.setattr("stt_button.app.build_engine", lambda cfg: EngineStub())
    monkeypatch.setattr("stt_button.app.DotoolTyper", TyperStub)
    monkeypatch.setattr("stt_button.app.ClipboardPaster", PasterStub)
    monkeypatch.setattr("stt_button.app.PushToTalkController", ControllerStub)
    monkeypatch.setattr("stt_button.app.KeyListener", ListenerStub)
    monkeypatch.setattr("stt_button.app.SummaryCoordinator", CoordinatorStub)
    monkeypatch.setattr(
        "stt_button.app.resolve_device",
        lambda name: type("D", (), {"path": "/dev/null", "name": "fake"})())

    app = Application(cfg)
    app.start()
    controller = app._controller
    listener = app._listener
    assert isinstance(controller, ControllerStub)
    assert isinstance(listener, ListenerStub)
    return app, controller, listener


class TestOffStateRegression:
    """summary_enabled=False: build and behavior identical to today."""

    def test_no_coordinator_no_buffer_on_output_none(self, monkeypatch):
        app, controller, _l = _start_app(
            monkeypatch, Config(summary_enabled=False))
        # No coordinator object anywhere in the lifecycle.
        assert CoordinatorStub.instances == []
        # on_output stays None (today's default wiring).
        assert controller.kwargs["on_output"] is None
        # No summary collaborators were built.
        assert app._summary_buffer is None
        assert app._summary_client is None
        assert app._coordinator is None
        app.shutdown()

    def test_press_release_bit_identical_short_and_long(self, monkeypatch):
        app, controller, listener = _start_app(
            monkeypatch, Config(summary_enabled=False))

        # Short hold: press -> start, release -> stop (never abort).
        listener.on_press(None)
        assert controller.starts == 1
        listener.on_release(None)
        assert controller.stops == 1
        assert controller.aborts == 0

        # Long hold: same pass-through, still no abort.
        listener.on_press(None)
        listener.on_release(None)
        assert controller.starts == 2
        assert controller.stops == 2
        assert controller.aborts == 0

        # Rapid double press: still pure pass-through, no coordinator.
        listener.on_press(None)
        listener.on_release(None)
        listener.on_press(None)
        listener.on_release(None)
        assert (controller.starts, controller.stops, controller.aborts) \
            == (4, 4, 0)
        assert CoordinatorStub.instances == []
        app.shutdown()

    def test_shutdown_is_clean_and_idempotent(self, monkeypatch):
        app, _c, listener = _start_app(
            monkeypatch, Config(summary_enabled=False))
        app.shutdown()
        assert listener.closed is True
        assert app._controller is None
        app.shutdown()  # must not raise


class TestOnStateWiring:
    """summary_enabled=True: coordinator, buffer hook and gesture routing."""

    def _start_enabled(self, monkeypatch, identities):
        import stt_button.focus as focus_mod
        seq = list(identities)

        def fake_identity():
            return seq.pop(0) if seq else seq

        monkeypatch.setattr(focus_mod, "window_identity", fake_identity)
        return _start_app(
            monkeypatch, Config(summary_enabled=True))

    def test_coordinator_built_once_in_start(self, monkeypatch):
        app, controller, _l = self._start_enabled(monkeypatch, [])
        coords = CoordinatorStub.instances
        assert len(coords) == 1
        coord = coords[0]
        # Coordinator is wired to the controller and the config.
        assert coord.controller is controller
        assert coord.cfg is app.config
        # Typer and paster collaborators were passed.
        assert isinstance(coord.kwargs["typer"], TyperStub)
        assert isinstance(coord.kwargs["paster"], PasterStub)
        app.shutdown()
        assert coord.shutdown_calls == 1

    def test_on_output_binds_to_summary_buffer(self, monkeypatch):
        app, controller, _l = self._start_enabled(
            monkeypatch, [("app", "t1")])
        on_output = controller.kwargs["on_output"]
        assert callable(on_output)
        assert isinstance(app._summary_buffer, SummaryBuffer)
        on_output("eins", epoch=1)
        chunks = app._summary_buffer.chunks
        assert [c.text for c in chunks] == ["eins"]
        assert chunks[0].epoch == 1
        app.shutdown()

    def test_window_identity_change_resets_buffer(self, monkeypatch):
        # First output in window A, second in window B -> session reset.
        app, controller, _l = self._start_enabled(
            monkeypatch, [("appA", "t1"), ("appB", "t2")])
        on_output = controller.kwargs["on_output"]
        on_output("first", epoch=1)
        on_output("second", epoch=2)
        chunks = app._summary_buffer.chunks
        # Only the chunk of the NEW window survives the reset.
        assert [c.text for c in chunks] == ["second"]
        app.shutdown()

    def test_double_press_hits_coordinator_no_phantom_recording(
            self, monkeypatch):
        app, controller, listener = self._start_enabled(
            monkeypatch, [("app", "t1")])
        # Seed the session buffer with one dictation output.
        controller.kwargs["on_output"]("diktat text", epoch=1)

        # Double press: two short holds with a tiny gap (real clock,
        # holds of ~0 s are far below summary_press_max_s=0.35).
        listener.on_press(None)
        listener.on_release(None)
        listener.on_press(None)
        listener.on_release(None)

        coord = CoordinatorStub.instances[-1]
        # The gesture reached the coordinator exactly once ...
        assert len(coord.submits) == 1
        # ... and both presses still reached the controller (loss rule).
        assert controller.starts == 2
        # No phantom recording: short releases aborted, never stopped.
        assert controller.aborts == 2
        assert controller.stops == 0
        # The consumed session was reset after the successful submit.
        assert app._summary_buffer.chunks == []
        app.shutdown()

    def test_double_press_without_chunks_submits_nothing(self, monkeypatch):
        app, _controller, listener = self._start_enabled(monkeypatch, [])
        listener.on_press(None)
        listener.on_release(None)
        listener.on_press(None)
        listener.on_release(None)
        coord = CoordinatorStub.instances[-1]
        assert coord.submits == []
        app.shutdown()
