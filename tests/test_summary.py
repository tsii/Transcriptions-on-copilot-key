"""Tests for the SummaryBuffer session buffer (D2, block B4).

Privacy rule D5: the buffer NEVER logs the text itself, only its character
length and a truncated sha1 hash. All tests use a fake clock; no network,
no focus/DBus access (that wiring arrives in a later block).
"""
from __future__ import annotations

import logging

import hashlib
import logging

from stt_button.summary import (
    UNDO_NOT_FOUND,
    ApplyResult,
    apply_summary,
    undo_summary,
)
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


# --- B6: apply_summary / undo_summary (Fake-Typer/-Paster, no real keys) ---

class FakeTyper:
    """Records every typing call; never touches the desktop."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def backspace_and_type(self, n, text, trailing_key=None):
        self.calls.append(("backspace_and_type", n, text, trailing_key))

    def backspace(self, n):
        self.calls.append(("backspace", n))

    def key(self, name):
        self.calls.append(("key", name))


class FakePaster:
    """Records paste calls; paste_combined result is scriptable."""

    def __init__(self, combined_result: bool = True) -> None:
        self.calls: list[tuple] = []
        self.combined_result = combined_result

    def paste(self, text):
        self.calls.append(("paste", text))

    def paste_combined(self, text, *, backspaces=0):
        self.calls.append(("paste_combined", text, backspaces))
        return self.combined_result


class RestoreRecorder:
    """Counts clipboard/primary restores with a shared order list."""

    def __init__(self, order: list[str]) -> None:
        self._order = order
        self.count = 0

    def __call__(self) -> None:
        self.count += 1
        self._order.append(self.name)  # set by the test

    name = ""


def make_restores():
    order: list[str] = []
    clip = RestoreRecorder(order)
    clip.name = "clipboard"
    prim = RestoreRecorder(order)
    prim.name = "primary"
    return clip, prim, order


LONG = "x" * 60   # >= paste_min_chars (40)
SHORT = "kurz"    # <  paste_min_chars


class TestApplySummaryShortBlock:
    def test_short_block_backspace_and_type_with_trailing_space(self):
        typer, paster = FakeTyper(), FakePaster()
        clip, prim, order = make_restores()
        repl = SHORT  # deliberately without trailing space
        result = apply_summary(
            repl, original_text="original block", n=13,
            typer=typer, paster=paster,
            restore_clipboard=clip, restore_primary=prim,
        )
        assert isinstance(result, ApplyResult)
        assert result.ok is True
        assert result.status == "typed" and result.mode == "typed"
        assert result.reason is None
        assert result.removed_text == "original block"
        assert result.inserted_text == "kurz"
        assert typer.calls == [
            ("backspace_and_type", 13, "kurz", "space"),
        ]
        assert paster.calls == []
        # restore in every branch: clipboard before primary, after typing
        assert clip.count == 1 and prim.count == 1
        assert order == ["clipboard", "primary"]

    def test_short_block_payload_has_no_trailing_space(self):
        typer, paster = FakeTyper(), FakePaster()
        clip, prim, _order = make_restores()
        repl = "  spacing  "
        result = apply_summary(
            repl, original_text="orig", n=4,
            typer=typer, paster=paster,
            restore_clipboard=clip, restore_primary=prim,
        )
        assert isinstance(result, ApplyResult)
        assert result.inserted_text == "  spacing  "
        # the type payload keeps the text as-is: NO synthetic trailing space
        assert typer.calls[0][2] == "  spacing  "
        assert typer.calls[0][3] == "space"


class TestApplySummaryLongBlock:
    def test_long_block_paste_combined(self):
        typer, paster = FakeTyper(), FakePaster(combined_result=True)
        clip, prim, order = make_restores()
        result = apply_summary(
            LONG, original_text="o" * 55, n=55,
            typer=typer, paster=paster,
            restore_clipboard=clip, restore_primary=prim,
        )
        assert isinstance(result, ApplyResult)
        assert result.ok is True
        assert result.status == "pasted" and result.mode == "pasted"
        assert result.inserted_text == LONG
        assert paster.calls == [("paste_combined", LONG, 55)]
        assert typer.calls == []
        assert clip.count == 1 and prim.count == 1
        assert order == ["clipboard", "primary"]

    def test_long_block_fallback_after_false(self):
        typer, paster = FakeTyper(), FakePaster(combined_result=False)
        clip, prim, _order = make_restores()
        result = apply_summary(
            LONG, original_text="o" * 55, n=55,
            typer=typer, paster=paster,
            restore_clipboard=clip, restore_primary=prim,
        )
        assert isinstance(result, ApplyResult)
        assert result.ok is True
        assert result.status == "pasted_fallback" and result.mode == "typed"
        # paste_combined returned False: the fallback is TYPING
        # (backspace_and_type), not backspace+paste
        assert typer.calls == [("backspace_and_type", 55, LONG, "space")]
        assert paster.calls == [("paste_combined", LONG, 55)]
        assert clip.count == 1 and prim.count == 1


class TestUndoSummary:
    def test_undo_endsuffix_retypes_original(self):
        typer, paster = FakeTyper(), FakePaster()
        clip, prim, order = make_restores()
        entry = ApplyResult(ok=True, status="typed", mode="typed",
                            removed_text="original block",
                            inserted_text="kurz", reason=None)
        read_text = "prefix text kurz"
        result = undo_summary(
            entry, read_text=read_text,
            typer=typer, paster=paster,
            restore_clipboard=clip, restore_primary=prim,
        )
        assert isinstance(result, ApplyResult)
        assert result.ok is True
        assert result.status == "undone"
        assert result.removed_text == "kurz"
        assert result.inserted_text == "original block"
        # delete exactly the replacement, retype the original
        assert typer.calls == [
            ("backspace_and_type", 4, "original block", "space"),
        ]
        assert clip.count == 1 and prim.count == 1
        assert order == ["clipboard", "primary"]

    def test_undo_without_endsuffix_no_typing(self):
        typer, paster = FakeTyper(), FakePaster()
        clip, prim, order = make_restores()
        entry = ApplyResult(ok=True, status="typed", mode="typed",
                            removed_text="original block",
                            inserted_text="kurz", reason=None)
        result = undo_summary(
            entry, read_text="kurz followed by more text",
            typer=typer, paster=paster,
            restore_clipboard=clip, restore_primary=prim,
        )
        assert isinstance(result, ApplyResult)
        assert result.ok is False
        assert result.status == "aborted"
        assert result.reason == UNDO_NOT_FOUND
        assert result.removed_text == "" and result.inserted_text == ""
        assert typer.calls == [] and paster.calls == []
        # restore still runs in the abort branch
        assert clip.count == 1 and prim.count == 1
        assert order == ["clipboard", "primary"]

    def test_undo_long_original_pastes(self):
        typer, paster = FakeTyper(), FakePaster()
        clip, prim, _order = make_restores()
        entry = ApplyResult(ok=True, status="pasted", mode="pasted",
                            removed_text="o" * 55, inserted_text=LONG,
                            reason=None)
        result = undo_summary(
            entry, read_text="lead " + LONG,
            typer=typer, paster=paster,
            restore_clipboard=clip, restore_primary=prim,
        )
        assert result.ok is True
        assert result.status == "undone"
        assert paster.calls == [("paste_combined", "o" * 55, 60)]
        assert typer.calls == []


class TestB6PrivacyLogging:
    def test_apply_log_contains_only_length_and_hash(self, caplog):
        typer, paster = FakeTyper(), FakePaster()
        clip, prim, _order = make_restores()
        with caplog.at_level(logging.INFO, logger="stt_button.summary"):
            apply_summary(
                "geheime zusammenfassung", original_text="orig", n=4,
                typer=typer, paster=paster,
                restore_clipboard=clip, restore_primary=prim,
            )
        assert "geheime zusammenfassung" not in caplog.text
        assert "geheim" not in caplog.text
        digest = hashlib.sha1("geheime zusammenfassung".encode()).hexdigest()
        assert digest[:8] in caplog.text
        assert "len=23" in caplog.text

    def test_repr_leaks_no_field_text(self):
        """repr(ApplyResult) shows metadata only - never the texts (D5)."""
        result = ApplyResult(ok=True, status="typed", mode="typed",
                             removed_text="geheimer alttext",
                             inserted_text="geheimer neutext",
                             reason=None)
        rendered = repr(result)
        assert "geheimer" not in rendered
        assert "alttext" not in rendered and "neutext" not in rendered
        assert "ok=True" in rendered
        assert "status='typed'" in rendered
        assert "mode='typed'" in rendered
        assert "reason=None" in rendered
        assert "removed_len=16" in rendered
        assert "inserted_len=16" in rendered
        digest = hashlib.sha1(
            ("geheimer alttext" + "\x00" + "geheimer neutext").encode()
        ).hexdigest()[:8]
        assert digest in rendered
