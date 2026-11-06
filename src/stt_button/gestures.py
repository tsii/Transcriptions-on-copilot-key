"""Gesture detection for summary mode (plan blocks B2/B3).

Pure state machines: no threads, no evdev, no ASR, no torch, no network.
Edges (press/release with timestamps) are fed in from the outside so the
classes stay testable with a fake clock.

B2 ``DoublePressDetector``: recognizes a double press (two short holds with
a small gap) and forwards every edge exactly once to a callback.
"""
from __future__ import annotations

import time
from typing import Callable


class DoublePressDetector:
    """Detect two short presses with a small gap (plan block B2).

    Criterion (all three must hold):
    ``hold_1 < press_max_s`` AND ``hold_2 < press_max_s`` AND
    ``press_2 - release_1 < gap_max_s``.

    The distance dictation-release -> first click is deliberately NOT
    limited (plan line 93): a long dictation hold simply invalidates the
    current candidate instead of blocking a later double press.
    """

    def __init__(
        self,
        press_max_s: float = 0.35,
        gap_max_s: float = 0.35,
        on_edge: Callable[[str, float], None] | None = None,
        on_double_press: Callable[[], None] | None = None,
    ) -> None:
        self.press_max_s = press_max_s
        self.gap_max_s = gap_max_s
        self._on_edge = on_edge
        self._on_double_press = on_double_press
        self._press_t: float | None = None
        # (release time of a short first hold) or None
        self._candidate_release: float | None = None

    def _forward(self, kind: str, t: float) -> None:
        if self._on_edge is not None:
            self._on_edge(kind, t)

    def press(self, t: float) -> None:
        """Feed a press edge at time ``t`` (seconds, monotonic clock)."""
        self._forward("press", t)
        self._press_t = t

    def release(self, t: float) -> None:
        """Feed a release edge at time ``t`` and evaluate the criterion."""
        self._forward("release", t)
        press_t = self._press_t
        self._press_t = None
        if press_t is None:
            # Release without press: cannot measure a hold; no candidate.
            self._candidate_release = None
            return
        hold = t - press_t
        if hold >= self.press_max_s:
            # Long hold (dictation): invalidates any pending candidate but
            # does not limit the distance to a future double press.
            self._candidate_release = None
            return
        candidate = self._candidate_release
        self._candidate_release = t
        if candidate is not None and press_t - candidate < self.gap_max_s:
            # Second short hold with a small gap: double press detected.
            self._candidate_release = None
            if self._on_double_press is not None:
                self._on_double_press()
