"""Tests for the SummaryBuffer session buffer (D2, block B4).

Privacy rule D5: the buffer NEVER logs the text itself, only its character
length and a truncated sha1 hash. All tests use a fake clock; no network,
no focus/DBus access (that wiring arrives in a later block).
"""
from __future__ import annotations

import logging

from stt_button.summary import Chunk, SummaryBuffer, locate_block


class TestLocateBlock:
    """Seven cases for locate_block() (B5) - pure function, no I/O."""

    @staticmethod
    def _chunks(*texts: str):
        return [Chunk(text=t, epoch=1, ts=1.0) for t in texts]

    def test_a_match_at_text_end(self) -> None:
        """(a) read text ends with the block -> span + caret_at_block_end."""
        res = locate_block(self._chunks("diktat"), "vorne diktat")
        assert res.status == "caret_at_block_end"
        assert res.start == 6
        assert res.end == 12

    def test_b_caret_not_at_end(self) -> None:
        """(b) user typed after the block -> caret_not_at_end, no span."""
        res = locate_block(self._chunks("diktat"), "vorne diktat mehr")
        assert res.status == "caret_not_at_end"
        assert res.start is None
        assert res.end is None

    def test_c_ambiguous_anchor(self) -> None:
        """(c) anchor occurs twice in the read text -> ambiguous."""
        res = locate_block(self._chunks("diktat"), "diktat diktat")
        assert res.status == "ambiguous"
        assert res.start is None

    def test_d_no_match(self) -> None:
        """(d) anchor missing from the read text -> no_match."""
        res = locate_block(self._chunks("diktat"), "ganz anderer text")
        assert res.status == "no_match"
        assert res.start is None

    def test_e_gap_too_large(self) -> None:
        """(e) interior between anchors exceeds max gap -> gap_too_large."""
        res = locate_block(self._chunks("eins", "zwei"),
                           "eins " + "x" * 11 + " zwei",
                           summary_max_gap_chars=10)
        assert res.status == "gap_too_large"
        assert res.start is None

    def test_f_block_includes_typed_text(self) -> None:
        """(f) two anchors with typed text between -> block spans it (M3a)."""
        res = locate_block(self._chunks("eins", "zwei"), "eins getipptes zwei")
        assert res.status == "caret_at_block_end"
        assert res.start == 0
        assert res.end == 19

    def test_g_read_too_long(self) -> None:
        """(g) read text longer than summary_max_read_chars -> too_long."""
        res = locate_block(self._chunks("diktat"),
                           "a" * 21 + " diktat",
                           summary_max_read_chars=20)
        assert res.status == "too_long"
        assert res.start is None


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
