"""Tests for the SummaryBuffer session buffer (D2, block B4).

Privacy rule D5: the buffer NEVER logs the text itself, only its character
length and a truncated sha1 hash. All tests use a fake clock; no network,
no focus/DBus access (that wiring arrives in a later block).
"""
from __future__ import annotations

import logging

from stt_button.summary import SummaryBuffer


class FakeClock:
    """Deterministic monotonic clock."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TestAppend:
    def test_append_per_on_output(self) -> None:
        """Every on_output call appends exactly one chunk (text, epoch, ts)."""
        clock = FakeClock()
        buf = SummaryBuffer(idle_s=300.0, clock=clock)

        buf.append("hallo welt", epoch=1, window_identity=("kitty", "term"))
        clock.advance(5.0)
        buf.append("zweiter satz", epoch=1, window_identity=("kitty", "term"))

        assert len(buf.chunks) == 2
        assert buf.chunks[0].text == "hallo welt"
        assert buf.chunks[0].epoch == 1
        assert buf.chunks[0].ts == 1000.0
        assert buf.chunks[1].text == "zweiter satz"
        assert buf.chunks[1].ts == 1005.0

    def test_reset_clears_chunks(self) -> None:
        buf = SummaryBuffer(idle_s=300.0, clock=FakeClock())
        buf.append("alt", epoch=1, window_identity=None)
        buf.reset()
        assert buf.chunks == []


class TestIdleReset:
    def test_idle_gap_resets_before_append(self) -> None:
        """A gap > idle_s between outputs starts a fresh session."""
        clock = FakeClock()
        buf = SummaryBuffer(idle_s=300.0, clock=clock)

        buf.append("erster diktatblock", epoch=1, window_identity=("a", "b"))
        clock.advance(301.0)  # > summary_session_idle_s
        buf.append("neue sitzung", epoch=2, window_identity=("a", "b"))

        assert len(buf.chunks) == 1
        assert buf.chunks[0].text == "neue sitzung"
        assert buf.chunks[0].epoch == 2

    def test_gap_below_idle_keeps_chunks(self) -> None:
        clock = FakeClock()
        buf = SummaryBuffer(idle_s=300.0, clock=clock)

        buf.append("eins", epoch=1, window_identity=("a", "b"))
        clock.advance(299.0)
        buf.append("zwei", epoch=1, window_identity=("a", "b"))

        assert len(buf.chunks) == 2


class TestWindowIdentityReset:
    def test_window_identity_change_resets(self) -> None:
        clock = FakeClock()
        buf = SummaryBuffer(idle_s=300.0, clock=clock)

        buf.append("im terminal", epoch=1, window_identity=("kitty", "term"))
        buf.append("immer noch terminal", epoch=1,
                   window_identity=("kitty", "term"))
        buf.append("im browser", epoch=2, window_identity=("firefox", "web"))

        assert len(buf.chunks) == 1
        assert buf.chunks[0].text == "im browser"
        assert buf.chunks[0].epoch == 2

    def test_window_identity_none_never_resets(self) -> None:
        """Unknown identity (None) must not wipe the session."""
        clock = FakeClock()
        buf = SummaryBuffer(idle_s=300.0, clock=clock)

        buf.append("eins", epoch=1, window_identity=("kitty", "term"))
        buf.append("zwei", epoch=1, window_identity=None)

        assert len(buf.chunks) == 2


class TestPrivacyLogging:
    def test_log_contains_only_length_and_hash(self, caplog) -> None:
        buf = SummaryBuffer(idle_s=300.0, clock=FakeClock())
        with caplog.at_level(logging.INFO, logger="stt_button.summary"):
            buf.append("streng geheimer diktattext", epoch=7,
                       window_identity=("a", "b"))

        assert "streng geheimer diktattext" not in caplog.text
        assert "geheim" not in caplog.text
        assert "len=26" in caplog.text
        # truncated sha1 of the text (8 hex chars)
        import hashlib
        digest = hashlib.sha1("streng geheimer diktattext".encode()).hexdigest()
        assert digest[:8] in caplog.text

    def test_reset_log_contains_no_text(self, caplog) -> None:
        buf = SummaryBuffer(idle_s=300.0, clock=FakeClock())
        buf.append("geheimer inhalt", epoch=1, window_identity=("a", "b"))
        with caplog.at_level(logging.INFO, logger="stt_button.summary"):
            buf.reset()
        assert "geheimer inhalt" not in caplog.text
