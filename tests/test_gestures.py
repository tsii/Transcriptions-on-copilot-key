"""Tests for gesture detection (summary mode, plan block B2).

``DoublePressDetector`` is a pure state machine — no threads, no evdev;
edges are fed in from outside with explicit timestamps (fake clock).
"""
from __future__ import annotations

from stt_button.gestures import DoublePressDetector, PressRouter


class EdgeRecorder:
    """Collects forwarded edges and detected gestures."""

    def __init__(self) -> None:
        self.edges: list[tuple[str, float]] = []
        self.gestures = 0

    def on_edge(self, kind: str, t: float) -> None:
        self.edges.append((kind, t))

    def on_double_press(self) -> None:
        self.gestures += 1


class TestDoublePressDetector:
    def test_two_short_presses_small_gap_one_gesture(self) -> None:
        rec = EdgeRecorder()
        d = DoublePressDetector(on_edge=rec.on_edge, on_double_press=rec.on_double_press)
        d.press(0.0)
        d.release(0.10)
        d.press(0.22)  # gap 0.12 s after release
        d.release(0.32)
        assert rec.gestures == 1

    def test_gap_too_large_no_gesture(self) -> None:
        rec = EdgeRecorder()
        d = DoublePressDetector(on_edge=rec.on_edge, on_double_press=rec.on_double_press)
        d.press(0.0)
        d.release(0.10)
        d.press(1.30)  # gap 1.2 s
        d.release(1.40)
        assert rec.gestures == 0

    def test_second_hold_too_long_no_gesture(self) -> None:
        rec = EdgeRecorder()
        d = DoublePressDetector(on_edge=rec.on_edge, on_double_press=rec.on_double_press)
        d.press(0.0)
        d.release(0.10)
        d.press(0.22)
        d.release(1.12)  # second hold 0.9 s >= press_max_s
        assert rec.gestures == 0

    def test_long_then_short_hold_no_gesture(self) -> None:
        rec = EdgeRecorder()
        d = DoublePressDetector(on_edge=rec.on_edge, on_double_press=rec.on_double_press)
        d.press(0.0)
        d.release(0.90)  # first hold 0.9 s >= press_max_s
        d.press(1.00)
        d.release(1.10)
        assert rec.gestures == 0

    def test_every_edge_forwarded_exactly_once(self) -> None:
        rec = EdgeRecorder()
        d = DoublePressDetector(on_edge=rec.on_edge, on_double_press=rec.on_double_press)
        d.press(0.0)
        d.release(0.10)
        d.press(0.22)
        d.release(0.32)
        d.press(5.00)
        d.release(5.10)
        assert rec.edges == [
            ("press", 0.0),
            ("release", 0.10),
            ("press", 0.22),
            ("release", 0.32),
            ("press", 5.00),
            ("release", 5.10),
        ]

    def test_defaults_are_constructor_arguments(self) -> None:
        d = DoublePressDetector()
        assert d.press_max_s == 0.35
        assert d.gap_max_s == 0.35

    def test_gap_after_dictation_release_not_limited(self) -> None:
        # Distance dictation-release -> first click is deliberately
        # unbounded (plan line 93): a long first hold that was NOT short
        # does not poison a later double press, and a large gap between a
        # dictation release and a fresh double press is fine.
        rec = EdgeRecorder()
        d = DoublePressDetector(on_edge=rec.on_edge, on_double_press=rec.on_double_press)
        d.press(0.0)
        d.release(5.00)  # long dictation hold
        # much later: a proper double press
        d.press(600.0)
        d.release(600.10)
        d.press(600.22)
        d.release(600.32)
        assert rec.gestures == 1


class FakeController:
    """Records controller calls; the only interface PressRouter knows."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def start_recording(self) -> None:
        self.calls.append("start")

    def stop_recording(self) -> None:
        self.calls.append("stop")

    def abort(self) -> None:
        self.calls.append("abort")




class FakeClock:
    """Monotonic fake clock: only advances when told."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class TestPressRouterShortHoldAborts:
    def test_short_hold_aborts_never_stops(self) -> None:
        c = FakeController()
        clock = FakeClock()
        r = PressRouter(c, press_max_s=0.35, summary_enabled=True, now=clock)
        r.on_press()
        clock.advance(0.20)
        r.on_release()
        assert c.calls == ["start", "abort"]
        assert "stop" not in c.calls

    def test_hold_exactly_at_limit_stops(self) -> None:
        c = FakeController()
        clock = FakeClock()
        r = PressRouter(c, press_max_s=0.35, summary_enabled=True, now=clock)
        r.on_press()
        clock.advance(0.35)
        r.on_release()
        assert c.calls == ["start", "stop"]
        assert "abort" not in c.calls

    def test_long_hold_stops_never_aborts(self) -> None:
        c = FakeController()
        clock = FakeClock()
        r = PressRouter(c, press_max_s=0.35, summary_enabled=True, now=clock)
        r.on_press()
        clock.advance(2.0)
        r.on_release()
        assert c.calls == ["start", "stop"]
        assert "abort" not in c.calls


class TestPressRouterDetectorEdges:
    def test_detector_receives_both_edges_with_times(self) -> None:
        c = FakeController()
        clock = FakeClock()
        rec = EdgeRecorder()
        d = DoublePressDetector(on_edge=rec.on_edge, on_double_press=rec.on_double_press)
        r = PressRouter(c, press_max_s=0.35, summary_enabled=True, now=clock, detector=d)
        r.on_press()
        clock.advance(0.10)
        r.on_release()
        kinds = [e[0] for e in rec.edges]
        assert kinds == ["press", "release"]
        assert abs((rec.edges[1][1] - rec.edges[0][1]) - 0.10) < 1e-9


class TestPressRouterSummaryDisabled:
    def test_disabled_is_exact_passthrough(self) -> None:
        c = FakeController()
        clock = FakeClock()
        r = PressRouter(c, press_max_s=0.35, summary_enabled=False, now=clock)
        r.on_press()
        clock.advance(0.10)  # short hold: today still stop_recording()
        r.on_release()
        assert c.calls == ["start", "stop"]
        assert "abort" not in c.calls

    def test_disabled_long_hold_passthrough(self) -> None:
        c = FakeController()
        clock = FakeClock()
        r = PressRouter(c, press_max_s=0.35, summary_enabled=False, now=clock)
        r.on_press()
        clock.advance(2.0)
        r.on_release()
        assert c.calls == ["start", "stop"]
