"""Tests for delta output (TDD chain 2).

DeltaTyper compares consecutive transcriptions and outputs only the difference.
Corrections use backspace.

Core rule: always use the *minimal* rollback—delete only characters that
changed. The committed (already final) prefix is never touched.
"""
from __future__ import annotations

import subprocess
import threading

import pytest

from stt_button.typing import (
    ClipboardPaster,
    ClipboardTyper,
    DeltaTyper,
    DotoolTyper,
    TyperError,
    WtypeTyper,
)


class FakeTyper:
    """Record all calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def type_text(self, text: str) -> None:
        self.calls.append(("type", text))

    def backspace(self, n: int) -> None:
        self.calls.append(("backspace", str(n)))

    def key(self, name: str) -> None:
        self.calls.append(("key", name))

    @property
    def typed(self) -> str:
        return "".join(c[1] for c in self.calls if c[0] == "type")


class TestFirstOutput:
    def test_first_update_types_whole_text(self) -> None:
        t = FakeTyper()
        d = DeltaTyper(t)
        d.update("Hallo")
        assert t.calls == [("type", "Hallo")]

    def test_empty_text_types_nothing(self) -> None:
        t = FakeTyper()
        d = DeltaTyper(t)
        d.update("")
        assert t.calls == []


class TestRevision:
    def test_changed_word_is_rewritten(self) -> None:
        t = FakeTyper()
        d = DeltaTyper(t)
        d.update("Hallo Weld")
        t.calls.clear()
        d.update("Hallo Welt")
        # Minimal rollback: common prefix is "Hallo Wel", so only "d" -> "t" changes.
        assert t.calls == [("backspace", "1"), ("type", "t")]

    def test_longer_text_only_appends(self) -> None:
        t = FakeTyper()
        d = DeltaTyper(t)
        d.update("Hallo")
        t.calls.clear()
        d.update("Hallo Welt")
        assert t.calls == [("type", " Welt")]

    def test_shorter_text_is_truncated(self) -> None:
        t = FakeTyper()
        d = DeltaTyper(t)
        d.update("Hallo Welt")
        t.calls.clear()
        d.update("Hallo")
        assert t.calls == [("backspace", "5")]

    def test_identical_text_does_nothing(self) -> None:
        t = FakeTyper()
        d = DeltaTyper(t)
        d.update("Hallo")
        t.calls.clear()
        d.update("Hallo")
        assert t.calls == []

    def test_empty_update_truncates_everything(self) -> None:
        t = FakeTyper()
        d = DeltaTyper(t)
        d.update("Hallo")
        t.calls.clear()
        d.update("")
        assert t.calls == [("backspace", "5")]

    def test_appended_word_after_change(self) -> None:
        t = FakeTyper()
        d = DeltaTyper(t)
        d.update("Hallo Weld")
        t.calls.clear()
        d.update("Hallo Welt und mehr")
        assert t.calls == [("backspace", "1"), ("type", "t und mehr")]


class TestCommittedPrefix:
    def test_committed_prefix_is_never_rewritten(self) -> None:
        t = FakeTyper()
        d = DeltaTyper(t)
        d.update("Hallo Welt", committed_len=6)
        t.calls.clear()
        # The model corrects the last word: "Welt" -> "Welz".
        # The committed prefix "Hallo " remains untouched.
        d.update("Hallo Welz", committed_len=6)
        assert t.calls == [("backspace", "1"), ("type", "z")]

    def test_committed_prefix_survives_contradiction(self) -> None:
        t = FakeTyper()
        d = DeltaTyper(t)
        d.update("Hallo Welt", committed_len=6)
        t.calls.clear()
        # The model loses the committed prefix ("Hall" instead of "Hallo ").
        # We accept none of that; the committed part remains in place.
        d.update("Hall", committed_len=6)
        assert all(c[0] != "type" for c in t.calls)

    def test_update_beyond_committed_prefix_appends(self) -> None:
        t = FakeTyper()
        d = DeltaTyper(t)
        d.update("Hallo", committed_len=0)
        t.calls.clear()
        d.update("Hallo Welt", committed_len=6)
        assert t.calls == [("type", " Welt")]

    def test_committed_prefix_is_never_deleted(self) -> None:
        t = FakeTyper()
        d = DeltaTyper(t)
        d.update("Hallo Welt", committed_len=11)
        t.calls.clear()
        # Complete contradiction: committed text must not be deleted.
        d.update("Ganz anders", committed_len=11)
        # The committed region "Hallo Welt" (11 characters) remains; only the
        # remainder is adjusted, but here the model text is shorter.
        assert not any(c == ("backspace", "11") for c in t.calls)


class TestRevisionLimit:
    def test_too_long_revision_is_avoided(self) -> None:
        t = FakeTyper()
        d = DeltaTyper(t, max_revision_chars=5)
        d.update("Ein sehr langer Text der sich komplett geaendert hat")
        t.calls.clear()
        d.update("voellig anderer Text")
        # Append instead of 40 backspaces (no destructive rollback)
        assert t.calls == [("type", "voellig anderer Text")]

    def test_short_revision_still_allowed(self) -> None:
        t = FakeTyper()
        d = DeltaTyper(t, max_revision_chars=60)
        d.update("Hallo Weld")
        t.calls.clear()
        d.update("Hallo Welt")
        assert t.calls[0] == ("backspace", "1")


class TestFinalize:
    def test_finalize_appends_space(self) -> None:
        t = FakeTyper()
        d = DeltaTyper(t, append_space=True)
        d.update("Hallo Welt")
        t.calls.clear()
        d.finalize("Hallo Welt")
        assert t.calls == [("key", "space")]

    def test_finalize_without_space(self) -> None:
        t = FakeTyper()
        d = DeltaTyper(t, append_space=False)
        d.update("Hallo Welt")
        t.calls.clear()
        d.finalize("Hallo Welt")
        assert t.calls == []

    def test_finalize_no_space_for_empty_text(self) -> None:
        t = FakeTyper()
        d = DeltaTyper(t, append_space=True)
        d.finalize("")
        assert t.calls == []

    def test_finalize_writes_final_text(self) -> None:
        t = FakeTyper()
        d = DeltaTyper(t, append_space=False)
        d.update("Hallo Weld")
        t.calls.clear()
        d.finalize("Hallo Welt")
        assert t.calls == [("backspace", "1"), ("type", "t")]

    def test_finalize_overrides_revision_limit(self) -> None:
        t = FakeTyper()
        d = DeltaTyper(t, append_space=False, max_revision_chars=5)
        d.update("Ein sehr langer Text der sich geaendert hat")
        t.calls.clear()
        # Final correction may retract >5 characters instead of duplicating
        d.finalize("Ein komplett anderer Text")
        # There must be a backspace (rollback) and NO duplicate:
        assert any(c[0] == "backspace" for c in t.calls)
        assert ("type", "Ein komplett anderer Text") not in t.calls

    def test_finalize_empty_does_not_erase(self) -> None:
        t = FakeTyper()
        d = DeltaTyper(t, append_space=False)
        d.update("Hallo Welt")
        t.calls.clear()
        d.finalize("")
        # Empty final output deletes nothing; an ASR error must not destroy text that
        # has already been typed.
        assert t.calls == []


class TestFinalPaste:
    """Final delta paste: finalize() pastes only the calculated delta."""

    def test_live_update_never_pastes(self) -> None:
        """Live updates use the typer (dotool), never paste during recording."""
        t = FakeTyper()
        pastes: list[str] = []

        class Recorder:
            def paste(self, text: str) -> None:
                pastes.append(text)

        d = DeltaTyper(t, final_paster=Recorder())
        d.update("Hallo Welt")
        d.update("Hallo Welt und")
        d.update("Hallo Welt und mehr")
        assert pastes == []
        assert t.typed == "Hallo Welt und mehr"

    def test_finalize_pastes_only_suffix_after_committed_prefix(self) -> None:
        """"Hallo Welt" -> "Hallo Welt und mehr": only " und mehr" is pasted."""
        t = FakeTyper()
        pastes: list[str] = []

        class Recorder:
            def paste(self, text: str) -> None:
                pastes.append(text)

        d = DeltaTyper(t, final_paster=Recorder())
        d.update("Hallo Welt", committed_len=0)
        d.finalize("Hallo Welt und mehr", committed_len=11)
        # append_space=True (default): the space is directly appended to the paste;
        # NO separate space key (space microfix).
        assert pastes == [" und mehr "]
        assert t.typed == "Hallo Welt"  # do not type the suffix twice

    def test_finalize_backspaces_before_pasting_revision_delta(self) -> None:
        """"Weld" -> "Welt": backspace 1, then paste "t"; do not retype the prefix."""
        t = FakeTyper()
        pastes: list[str] = []

        class Recorder:
            def paste(self, text: str) -> None:
                pastes.append(text)

        d = DeltaTyper(t, final_paster=Recorder())
        d.update("Hallo Weld")
        t.calls.clear()
        d.finalize("Hallo Welt")
        # append_space=True (default): backspace, then paste including the space;
        # NO separate space key (space microfix).
        assert t.calls == [("backspace", "1")]
        assert pastes == ["t "]

    def test_finalize_empty_delta_only_appends_space(self) -> None:
        """No delta (text unchanged) => no paste, only a space."""
        t = FakeTyper()
        pastes: list[str] = []

        class Recorder:
            def paste(self, text: str) -> None:
                pastes.append(text)

        d = DeltaTyper(t, final_paster=Recorder())
        d.update("Hallo Welt")
        t.calls.clear()
        d.finalize("Hallo Welt")
        assert pastes == []
        assert t.calls == [("key", "space")]

    def test_finalize_appends_space_after_paste(self) -> None:
        """The space comes after the paste; both calls in the correct order."""
        t = FakeTyper()
        events: list[tuple[str, str]] = []

        class Recorder:
            def paste(self, text: str) -> None:
                events.append(("paste", text))

        d = DeltaTyper(t, final_paster=Recorder(), append_space=True)
        d.update("Hallo")
        t.calls.clear()
        d.finalize("Hallo Welt")
        assert events == [("paste", " Welt ")]
        # The space is included IN the paste; NO separate space key (no double space).
        assert t.calls == [], t.calls

    def test_finalize_append_space_with_paster_empty_delta_only_key_space(
        self,
    ) -> None:
        """(BUG2/7) Paster + EMPTY to_add: still use key('space') for space."""
        t = FakeTyper()
        events: list[tuple[str, str]] = []

        class Recorder:
            def paste(self, text: str) -> None:
                events.append(("paste", text))

        d = DeltaTyper(t, final_paster=Recorder(), append_space=True)
        d.update("Hallo Welt")
        t.calls.clear()
        d.finalize("Hallo Welt")
        assert events == [], events
        assert t.calls == [("key", "space")], t.calls

    def test_finalize_sentence_final_space_not_suppressed_by_paste(self) -> None:
        """(T1) Sentence-final punctuation: the trailing space must be a real
        key('space') keystroke, not embedded in the paste (fields trim it)."""
        t = FakeTyper()
        events: list[tuple[str, str]] = []

        class Recorder:
            def paste(self, text: str) -> None:
                events.append(("paste", text))

        d = DeltaTyper(t, final_paster=Recorder(), append_space=True)
        d.update("Hallo Welt")
        t.calls.clear()
        d.finalize("Hallo Welt.")
        # Exactly ONE space, delivered as a real keystroke.
        assert t.calls.count(("key", "space")) == 1, t.calls
        # The paste must NOT carry the trailing space (many fields trim it);
        # the delta here is just the appended '.'.
        assert events == [("paste", ".")], events

    def test_finalize_append_space_without_paster_separate_space(self) -> None:
        """(BUG2/8) Without paster: keep the separate space (existing behavior)."""
        t = FakeTyper()
        d = DeltaTyper(t, append_space=True)
        d.update("Hallo")
        t.calls.clear()
        d.finalize("Hallo Welt")
        assert t.calls == [("type", " Welt"), ("key", "space")], t.calls


class TestClipboardPaster:
    """ClipboardPaster: wl-copy + paste chord with fallback to type_text."""

    def test_clipboard_paster_copies_delta_and_uses_configured_chord(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Copy via paste_binary, chord from paste_key (ctrl+shift+v), no restore."""
        calls = _capture(monkeypatch)
        t = FakeTyper()
        p = ClipboardPaster(t, copy_binary="wl-copy", paste_key="ctrl+shift+v")
        p.paste(" und mehr")
        assert calls == [
            ["wl-copy", " und mehr"],
            # Chord via the normal typer (dotool format: ctrl+shift+v)
        ]
        assert t.calls == [("key", "ctrl+shift+v")]
        # NO wl-paste, no restoration of the old clipboard content
        assert all("wl-paste" not in " ".join(c) for c in calls)

    def test_missing_or_failing_wl_copy_falls_back_without_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If wl-copy is missing or copy fails => type_text fallback, NO paste chord."""
        t = FakeTyper()
        monkeypatch.setattr("stt_button.typing.shutil.which", lambda b: None)
        p = ClipboardPaster(t, copy_binary="wl-copy")
        p.paste(" und mehr")
        assert t.calls == [("type", " und mehr")]  # Legacy path

        # Copy fails (rc != 0) => also type_text, no chord
        t2 = FakeTyper()
        monkeypatch.setattr("stt_button.typing.shutil.which", lambda b: "/usr/bin/wl-copy")

        def failing_run(cmd, *args, **kwargs):
            class R:
                returncode = 1
                stdout = ""
                stderr = "boom"

            return R()

        monkeypatch.setattr("stt_button.typing.subprocess.run", failing_run)
        p2 = ClipboardPaster(t2, copy_binary="wl-copy")
        p2.paste(" und mehr")
        assert t2.calls == [("type", " und mehr")]

    def test_paste_key_failure_is_not_retried(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Chord error after successful copy: do NOT repeat with type_text."""
        t = FakeTyper()
        monkeypatch.setattr("stt_button.typing.shutil.which", lambda b: "/usr/bin/wl-copy")
        monkeypatch.setattr(
            "stt_button.typing.subprocess.run",
            lambda cmd, *a, **k: subprocess_result(),
        )
        p = ClipboardPaster(t, copy_binary="wl-copy")

        def failing_key(name: str) -> None:
            raise TyperError("chord fehlgeschlagen")

        t.key = failing_key  # type: ignore[method-assign]
        p.paste(" und mehr")  # must NOT raise and MUST NOT type
        assert t.calls == []


class TestFinalizeTrace:
    """latency_trace trace points in DeltaTyper.finalize() (debug level).

    Messages must appear at DEBUG and include the domino sizes (characters to
    delete/type) so perceived release latency can be analyzed without guessing.
    """

    def test_finalize_logs_rev_and_backspace(self, caplog) -> None:
        import logging as _l

        t = FakeTyper()
        d = DeltaTyper(t, append_space=False, latency_trace=True)
        d.update("Hallo Weld")
        t.calls.clear()
        with caplog.at_level(_l.DEBUG, logger="stt_button.typing"):
            d.finalize("Hallo Welt")
        messages = [r.getMessage() for r in caplog.records]
        assert any("latency[rev" in m for m in messages), messages
        assert any("latency[backspace" in m for m in messages), messages

    def test_finalize_logs_paste_and_space(self, caplog) -> None:
        import logging as _l

        class Recorder:
            def paste(self, text: str) -> None:
                pass

        t = FakeTyper()
        d = DeltaTyper(t, append_space=True, final_paster=Recorder(),
                       latency_trace=True)
        d.update("Hallo Welt", committed_len=0)
        t.calls.clear()
        with caplog.at_level(_l.DEBUG, logger="stt_button.typing"):
            d.finalize("Hallo Welt und mehr", committed_len=11)
        messages = [r.getMessage() for r in caplog.records]
        assert any("latency[paste" in m for m in messages), messages
        assert any("latency[space" in m for m in messages), messages

    def test_trace_disabled_by_default(self, caplog) -> None:
        import logging as _l

        t = FakeTyper()
        d = DeltaTyper(t, append_space=False)
        with caplog.at_level(_l.DEBUG, logger="stt_button.typing"):
            d.update("Hallo")
            d.finalize("Hallo Welt")
        assert not [r for r in caplog.records if "latency[" in r.getMessage()]


class TestCommittedLiveMode:
    """live_typing_mode='committed': type only confirmed growth live.

    The unstable partial is tracked internally but NEVER typed or retracted:
    no flicker, no live backspaces. finalize() computes against the effectively
    typed state—to_remove==0, and the suffix is pasted once (zero backspaces).
    """

    def _committed(self, typer, **kwargs):
        return DeltaTyper(typer, append_space=False, live_typing_mode="committed",
                          **kwargs)

    def test_partial_without_commit_types_nothing(self) -> None:
        """(1) Partial without commit -> no output."""
        t = FakeTyper()
        d = self._committed(t)
        d.update("Hallo Welt", committed_len=0)
        assert t.calls == []

    def test_correction_before_commit_is_silent(self) -> None:
        """(2) Correction before commit -> no output, no backspaces."""
        t = FakeTyper()
        d = self._committed(t)
        d.update("Hallo Weld", committed_len=0)
        d.update("Hallo Welt", committed_len=0)
        assert t.calls == []

    def test_commit_types_exactly_once(self) -> None:
        """(3) Commit -> type exactly once (up to the committed prefix)."""
        t = FakeTyper()
        d = self._committed(t)
        d.update("Hallo Welt und", committed_len=11)
        assert t.calls == [("type", "Hallo Welt ")]

    def test_next_commit_types_only_increment(self) -> None:
        """(4) Next commit -> type only the growth."""
        t = FakeTyper()
        d = self._committed(t)
        d.update("Hallo Welt und", committed_len=11)
        t.calls.clear()
        d.update("Hallo Welt und mehr", committed_len=15)
        assert t.calls == [("type", "und ")]

    def test_finalize_pastes_suffix_without_backspaces(self) -> None:
        """(5) Final text 'Hallo Welt und mehr' -> zero backspaces, paste(' und mehr')."""
        t = FakeTyper()
        pastes: list[str] = []

        class Recorder:
            def paste(self, text: str) -> None:
                pastes.append(text)

        d = self._committed(t, final_paster=Recorder())
        d.update("Hallo Welt und", committed_len=10)
        t.calls.clear()
        d.finalize("Hallo Welt und mehr")
        assert pastes == [" und mehr"]
        assert not any(c[0] == "backspace" for c in t.calls), t.calls

    def test_no_live_commits_pastes_full_final_once(self) -> None:
        """(6) Zero live commits -> paste the complete final text exactly once."""
        t = FakeTyper()
        pastes: list[str] = []

        class Recorder:
            def paste(self, text: str) -> None:
                pastes.append(text)

        d = self._committed(t, final_paster=Recorder())
        d.update("Hallo Welt", committed_len=0)   # never committed
        d.update("Hallo Welt und", committed_len=0)
        d.finalize("Hallo Welt und mehr")
        assert pastes == ["Hallo Welt und mehr"]
        assert not any(c[0] == "backspace" for c in t.calls), t.calls
        assert not any(c[0] == "type" for c in t.calls), t.calls

    def test_empty_final_and_committed_change_protections_stay(self) -> None:
        """(7) Guard: empty final text deletes nothing; if the model differs in
        the committed region, the old version wins (no retraction)."""
        t = FakeTyper()
        d = self._committed(t)
        d.update("Hallo Welt und", committed_len=10)
        t.calls.clear()
        d.finalize("")
        assert t.calls == []

        # Committed-prefix difference in final text: the old typed prefix wins; only
        # the suffix is pasted as a delta—zero backspaces.
        pastes: list[str] = []

        class Recorder:
            def paste(self, text: str) -> None:
                pastes.append(text)

        t2 = FakeTyper()
        d2 = self._committed(t2, final_paster=Recorder())
        d2.update("Hallo Welt und", committed_len=10)
        t2.calls.clear()
        d2.finalize("Hallo Weld und mehr")
        assert pastes == [" und mehr"]
        assert not any(c[0] == "backspace" for c in t2.calls), t2.calls

    def test_default_full_is_previous_behaviour(self) -> None:
        """(8) Default 'full' = exactly the previous behavior (type partial live)."""
        t = FakeTyper()
        d = DeltaTyper(t, append_space=False)  # no live_typing_mode -> full
        d.update("Hallo Weld")
        assert t.calls == [("type", "Hallo Weld")]
        t.calls.clear()
        d.update("Hallo Welt")
        assert t.calls == [("backspace", "1"), ("type", "t")]



class TestPauseIndicator:
    """STAGE 4: pause indicator when live ASR stops due to the limit.

    ``show_paused()`` types the indicator (``PAUSE_INDICATOR``) EXACTLY ONCE
    as actual typed state (extends ``_last_text``), so the existing finalize()
    diff automatically removes it again.
    """

    def test_show_paused_types_indicator_exactly_once(self) -> None:
        """(1) Type the indicator exactly once; a repeated call is a no-op."""
        from stt_button.typing import PAUSE_INDICATOR

        t = FakeTyper()
        d = DeltaTyper(t, append_space=False)
        d.update("Hallo Welt")
        t.calls.clear()
        d.show_paused()
        d.show_paused()  # idempotent: no duplicate typing
        assert t.calls == [("type", PAUSE_INDICATOR)], t.calls
        # ASCII: three periods (dotool CANNOT type U+2026 in the de/nodeadkeys layout;
        # ground-truth experiment from the 11:36 real test).
        assert PAUSE_INDICATOR == "..."
        assert len(PAUSE_INDICATOR) == 3

    def test_finalize_removes_indicator_and_pastes_suffix(self) -> None:
        """(2) finalize: diff removes the indicator (3 backspaces), suffix without remainder."""
        from stt_button.typing import PAUSE_INDICATOR

        t = FakeTyper()
        pastes: list[str] = []

        class Recorder:
            def paste(self, text: str) -> None:
                pastes.append(text)

        d = DeltaTyper(t, append_space=False, final_paster=Recorder())
        d.update("Hallo Welt", committed_len=0)  # mode 'full'
        d.show_paused()
        t.calls.clear()
        d.finalize("Hallo Welt und mehr")
        # Only the indicator (3 characters, '...') is retracted:
        assert t.calls == [("backspace", str(len(PAUSE_INDICATOR)))], t.calls
        assert pastes == [" und mehr"]
        assert all(PAUSE_INDICATOR not in p for p in pastes)

    def test_finalize_without_paster_types_suffix_after_backspace(self) -> None:
        """(2b) Without final_paster: backspace(3), then type the suffix character by character."""
        from stt_button.typing import PAUSE_INDICATOR

        t = FakeTyper()
        d = DeltaTyper(t, append_space=False)
        d.update("Hallo Welt")
        d.show_paused()
        t.calls.clear()
        d.finalize("Hallo Welt und mehr")
        assert t.calls == [
            ("backspace", str(len(PAUSE_INDICATOR))),
            ("type", " und mehr"),
        ], t.calls

    def test_finalize_empty_keeps_indicator(self) -> None:
        """(3) Empty final_text: leave the indicator in place, no output."""
        from stt_button.typing import PAUSE_INDICATOR

        t = FakeTyper()
        d = DeltaTyper(t, append_space=False)
        d.update("Hallo Welt")
        d.show_paused()
        t.calls.clear()
        d.finalize("")
        assert t.calls == [], t.calls
        # Honest state: the indicator remains at the cursor (and in the state).
        assert d.last_text.endswith(PAUSE_INDICATOR)

    def test_reset_clears_pause_state(self) -> None:
        """(4) reset() clears the indicator state (clean next recording)."""
        from stt_button.typing import PAUSE_INDICATOR

        t = FakeTyper()
        d = DeltaTyper(t, append_space=False)
        d.show_paused()
        d.reset()
        t.calls.clear()
        d.show_paused()
        assert t.calls == [("type", PAUSE_INDICATOR)], t.calls

    def test_committed_mode_same_mechanics(self) -> None:
        """(6) live_typing_mode='committed': same mechanics, 3 backspaces."""
        from stt_button.typing import PAUSE_INDICATOR

        t = FakeTyper()
        pastes: list[str] = []

        class Recorder:
            def paste(self, text: str) -> None:
                pastes.append(text)

        d = DeltaTyper(
            t, append_space=False, final_paster=Recorder(),
            live_typing_mode="committed",
        )
        d.update("Hallo Welt und", committed_len=10)
        t.calls.clear()
        d.show_paused()
        t.calls.clear()
        d.finalize("Hallo Welt und mehr")
        assert t.calls == [("backspace", str(len(PAUSE_INDICATOR)))], t.calls
        # The indicator directly follows "Hallo Welt" (no space between) -> empty
        # common prefix, entire suffix pasted.
        assert pastes == [" und mehr"]

    def test_indicator_dots_collision_real_text(self) -> None:
        """(9) Indicator '...' vs. real periods: no swallowed text, no remainder.

        Ground-truth edge: typed state is 'Hallo' + indicator '...'; final text
        'HalloWelt.' ends with a REAL period. The prefix-based diff (common
        prefix of 'Hallo...' vs. 'HalloWelt.') keeps 'Hallo', removes the three
        indicator periods via backspace(3), and types/pastes 'Welt.' COMPLETELY,
        including the real final period. The common prefix ends BEFORE the
        periods (character 6 of 'HalloWelt.' is 'W', not '.'), so the diff
        cannot swallow real periods: to_remove counts ONLY the indicator tail.
        """
        from stt_button.typing import PAUSE_INDICATOR

        assert PAUSE_INDICATOR == "..."
        t = FakeTyper()
        pastes: list[str] = []

        class Recorder:
            def paste(self, text: str) -> None:
                pastes.append(text)

        d = DeltaTyper(t, append_space=False, final_paster=Recorder())
        d.update("Hallo", committed_len=0)  # mode 'full'
        d.show_paused()  # type '...' -> state 'Hallo...'
        t.calls.clear()
        d.finalize("HalloWelt.")
        # Exactly the indicator (3 periods) is retracted, NO more:
        assert t.calls == [("backspace", "3")], t.calls
        # 'Welt.' pasted as a WHOLE; the real period is preserved:
        assert pastes == ["Welt."], pastes
        assert d.last_text == "HalloWelt.", d.last_text

    def test_indicator_before_real_sentence_with_dots(self) -> None:
        """(10) Exact edge from the bugfix request: last typed text '... ',
        final '...und so weiter'. The common prefix '...' covers the periods;
        ONLY the difference (the space) is removed via backspace, and
        'und so weiter' is pasted. NO real period is swallowed: the diff is
        prefix-based and retracts exclusively old_tail[common:].
        """
        from stt_button.typing import PAUSE_INDICATOR

        t = FakeTyper()
        pastes: list[str] = []

        class Recorder:
            def paste(self, text: str) -> None:
                pastes.append(text)

        d = DeltaTyper(t, append_space=False, final_paster=Recorder())
        d.update("", committed_len=0)
        d.show_paused()  # type '...' -> state '...'
        # Last committed/live typed text was '... ' (indicator plus space); state via
        # the normal update() path:
        d.update("... ", committed_len=0)
        assert d.last_text == "... ", d.last_text
        t.calls.clear()
        d.finalize("...und so weiter")
        # Common prefix of '... ' vs. '...und so weiter' is '...' (3):
        # to_remove == 1 (only the space), remainder pasted.
        assert t.calls == [("backspace", "1")], t.calls
        # No real text swallowed: periods remain, suffix starts at 'und'.
        assert pastes == ["und so weiter"], pastes
        assert d.last_text == "...und so weiter", d.last_text

    def test_final_dots_equal_indicator_is_noop(self) -> None:
        """(11b) Most disruptive collision chain: the final text itself ends
        with '...', identical to the indicator. The diff is a no-op (common
        prefix == full length): no backspace, no paste, no real periods touched.
        """
        from stt_button.typing import PAUSE_INDICATOR

        t = FakeTyper()
        d = DeltaTyper(t, append_space=False)
        d.update("Hallo Welt", committed_len=0)
        d.show_paused()  # state 'Hallo Welt...'
        t.calls.clear()
        d.finalize("Hallo Welt...")  # real text happens to end with '...'
        assert t.calls == [], t.calls
        assert d.last_text == "Hallo Welt...", d.last_text

    def test_final_text_ending_with_dots_after_indicator(self) -> None:
        """(11) Collision edge: final text itself ENDS with '...'. The common
        prefix then extends BEYOND the indicator (up to min(len(old), len(new))),
        to_remove==0, nothing is retracted, and the remaining text is pasted
        unchanged.
        """
        from stt_button.typing import PAUSE_INDICATOR

        t = FakeTyper()
        pastes: list[str] = []

        class Recorder:
            def paste(self, text: str) -> None:
                pastes.append(text)

        d = DeltaTyper(t, append_space=False, final_paster=Recorder())
        d.update("Hallo Welt", committed_len=0)
        d.show_paused()  # state 'Hallo Welt...'
        t.calls.clear()
        d.finalize("Tschuess...")
        # Common prefix: 'Hallo Welt...' vs. 'Tschuess...' -> '' -> backspace(13)
        # retracts EVERYTHING (full final correction, force_revision semantics
        # in finalize), then paste everything anew.
        assert t.calls == [("backspace", "13")], t.calls
        assert pastes == ["Tschuess..."], pastes
        assert d.last_text == "Tschuess...", d.last_text


class TestReset:
    def test_reset_clears_state(self) -> None:
        t = FakeTyper()
        d = DeltaTyper(t)
        d.update("Hallo")
        d.reset()
        t.calls.clear()
        d.update("Neu")
        assert t.calls == [("type", "Neu")]


def _capture(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(cmd)
        return subprocess_result()

    monkeypatch.setattr("stt_button.typing.subprocess.run", fake_run)
    # CapsLock LED "off" = no diagnostic warning, no toggle.
    monkeypatch.setattr("stt_button.typing._capslock_state", lambda: "aus")
    return calls


def _capture_stdin(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record the stdin payload instead of the return code."""
    stdin: list[str] = []

    def fake_run(cmd, *args, **kwargs):
        stdin.append(kwargs.get("input", ""))
        return subprocess_result()

    monkeypatch.setattr("stt_button.typing.subprocess.run", fake_run)
    # CapsLock LED "off" = no diagnostic warning, no toggle.
    monkeypatch.setattr("stt_button.typing._capslock_state", lambda: "aus")
    return stdin


def subprocess_result():
    class R:
        returncode = 0
        stdout = ""
        stderr = ""

    return R()


class TestWtypeTyper:
    def test_type_text_builds_command(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = _capture(monkeypatch)
        WtypeTyper().type_text("Hallo Welt")
        assert calls == [["wtype", "-", "Hallo Welt"]]

    def test_backspace_uses_key_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = _capture(monkeypatch)
        WtypeTyper().backspace(3)
        assert calls == [["wtype", "-k", "BackSpace", "-k", "BackSpace",
                          "-k", "BackSpace"]]

    def test_backspace_is_batched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = _capture(monkeypatch)
        WtypeTyper().backspace(25)
        assert len(calls) == 2
        assert calls[0].count("BackSpace") == 20
        assert calls[1].count("BackSpace") == 5

    def test_backspace_zero_does_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = _capture(monkeypatch)
        WtypeTyper().backspace(0)
        assert calls == []

    def test_key_passes_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = _capture(monkeypatch)
        WtypeTyper().key("space")
        assert calls == [["wtype", "-k", "space"]]

    def test_failure_raises_typer_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(cmd, *args, **kwargs):
            raise FileNotFoundError("wtype fehlt")

        monkeypatch.setattr("stt_button.typing.subprocess.run", boom)
        with pytest.raises(TyperError):
            WtypeTyper().type_text("x")

    def test_nonzero_exit_raises_typer_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(cmd, *args, **kwargs):
            class R:
                returncode = 1
                stdout = ""
                stderr = "nope"

            return R()

        monkeypatch.setattr("stt_button.typing.subprocess.run", boom)
        with pytest.raises(TyperError):
            WtypeTyper().type_text("x")

    def test_newlines_become_return(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = _capture(monkeypatch)
        WtypeTyper().type_text("Zeile1\nZeile2")
        assert calls == [["wtype", "-", "Zeile1", "-k", "Return", "-", "Zeile2"]]


class TestClipboardTyper:
    def test_type_text_copies_and_pastes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = _capture(monkeypatch)
        ClipboardTyper().type_text("Hallo")
        assert calls == [
            ["wl-copy", "Hallo"],
            ["wtype", "-k", "ctrl", "-k", "v"],
        ]

    def test_backspace_is_not_supported(self) -> None:
        with pytest.raises(TyperError):
            ClipboardTyper().backspace(1)

    def test_key_uses_paste(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = _capture(monkeypatch)
        ClipboardTyper().key("ctrl+v")
        assert calls == [["wtype", "-k", "ctrl", "-k", "v"]]


class TestDotoolTyper:
    def test_type_text_sends_to_stdin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = _capture(monkeypatch)
        DotoolTyper().type_text("Hallo")
        assert calls == [["dotool"]]

    def test_backspace_repeats_key_commands(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stdin = _capture_stdin(monkeypatch)
        DotoolTyper().backspace(3)
        assert stdin[0] == "key backspace\nkey backspace\nkey backspace\n"

    def test_key_space(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stdin = _capture_stdin(monkeypatch)
        DotoolTyper().key("space")
        assert stdin[0] == "key space\n"

    def test_type_text_with_newline(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stdin = _capture_stdin(monkeypatch)
        DotoolTyper().type_text("Zeile1\nZeile2")
        assert "typedelay 20" in stdin[0]
        assert "type Zeile1\n" in stdin[0]
        assert "key enter\n" in stdin[0]
        assert "type Zeile2\n" in stdin[0]

    def test_missing_binary_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("stt_button.typing.shutil.which", lambda b: None)
        with pytest.raises(TyperError):
            DotoolTyper()

    def test_dotool_env_sets_de_nodeadkeys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """dotool receives DOTOOL_XKB_LAYOUT=de and VARIANT=nodeadkeys in its env."""
        captured: dict[str, object] = {}
        stdin: list[str] = []

        def fake_run(cmd, **kw):
            captured["env"] = kw.get("env")
            stdin.append(kw.get("input", ""))
            return subprocess_result()

        monkeypatch.setattr("stt_button.typing.subprocess.run", fake_run)
        monkeypatch.setattr("stt_button.typing._capslock_state", lambda: "aus")
        DotoolTyper().type_text("ä ö ü ß Ä Ö Ü ẞ")
        env = captured["env"]
        assert env["DOTOOL_XKB_LAYOUT"] == "de"
        assert env["DOTOOL_XKB_VARIANT"] == "nodeadkeys"
        assert any("ä ö ü ß Ä Ö Ü ẞ" in p for p in stdin), stdin


class TestAppendTrap:
    def test_large_revision_replaces_when_anchor_matches(self) -> None:
        """Fix D: large revision with matching committed anchor -> replace, not append."""
        t = FakeTyper()
        d = DeltaTyper(t, append_space=False, max_revision_chars=60)
        # Long text with committed prefix
        d.update("Das ist der Anfang eines sehr langen Textes mit vielen Woertern hier", committed_len=20)
        t.calls.clear()
        # New version: same committed prefix (20 characters), but long tail changed
        new = "Das ist der Anfang voellig anderer weiterer Text hier und noch mehr"
        d.update(new, committed_len=20)
        # It MUST be retracted (do not blindly append the new tail)
        assert any(c[0] == "backspace" for c in t.calls)
        # The new tail must match the revised remainder (from the common prefix)
        typed = "".join(c[1] for c in t.calls if c[0] == "type")
        assert typed != ""
        assert "anderer weiterer Text" in typed


class TestCapslockDiagnostics:
    def test_capslock_state_returns_valid_value(self) -> None:
        """Diagnostic function returns 'on', 'off', or '?' (never raises)."""
        from stt_button.typing import _capslock_state
        assert _capslock_state() in ("an", "aus", "?")

    def test_capslock_state_handles_missing_leds(self, monkeypatch) -> None:
        monkeypatch.setattr("stt_button.typing.glob.glob", lambda p: [])
        from stt_button.typing import _capslock_state
        assert _capslock_state() == "?"

    def test_capslock_leds_per_device(self, monkeypatch) -> None:
        """_capslock_leds() returns 'on'/'off'/'?' for each device name."""
        import builtins
        from stt_button.typing import _capslock_leds

        paths = [
            "/sys/class/leds/input3::capslock/brightness",
            "/sys/class/leds/input4::capslock/brightness",
            "/sys/class/leds/input5::capslock/brightness",
        ]
        monkeypatch.setattr("stt_button.typing.glob.glob", lambda p: paths)
        contents = {
            paths[0]: "1",
            paths[1]: "0",
            paths[2]: "\n",  # unreadable/unknown -> "?"
        }
        real_open = builtins.open

        def fake_open(path, *a, **k):
            if path in contents:
                import io
                return io.StringIO(contents[path])
            return real_open(path, *a, **k)

        monkeypatch.setattr("stt_button.typing.open", fake_open, raising=False)
        monkeypatch.setattr("builtins.open", fake_open, raising=False)
        result = _capslock_leds()
        assert result == {
            "input3::capslock": "an",
            "input4::capslock": "aus",
            "input5::capslock": "?",
        }

    def test_send_diagnose_mentions_unreliable_state(self, monkeypatch, caplog) -> None:
        """The diagnostic log contains the unreliable-LED-state warning."""
        import logging as _l
        monkeypatch.setattr("stt_button.typing._capslock_state", lambda: "aus")
        monkeypatch.setattr(
            "stt_button.typing.subprocess.run",
            lambda *a, **k: subprocess_result(),
        )
        with caplog.at_level(_l.WARNING, logger="stt_button.typing"):
            DotoolTyper().type_text("Test")
        messages = [r.message for r in caplog.records]
        assert any("LED is not a reliable state" in m for m in messages), messages

    def test_send_logs_capslock_when_active(self, monkeypatch, caplog) -> None:
        """When CapsLock is active (LED off, inverted), a warning is logged."""
        import logging as _l
        monkeypatch.setattr("stt_button.typing._capslock_state", lambda: "aus")
        calls = []
        monkeypatch.setattr(
            "stt_button.typing.subprocess.run",
            lambda *a, **k: calls.append(1) or subprocess_result(),
        )
        with caplog.at_level(_l.WARNING, logger="stt_button.typing"):
            DotoolTyper().type_text("Test")
        assert any("CAPSLOCK" in r.message for r in caplog.records)


class TestCapslockReset:
    """Default: NEVER toggle CapsLock, only warn.

    Empirically verified ground truth on this system: LED **off** = CapsLock
    active = inverted text; LED **on** = normal typing. LED semantics are
    therefore INVERTED relative to the usual convention.
    """

    def _capture_payloads(self, monkeypatch) -> list[str]:
        payloads: list[str] = []
        monkeypatch.setattr(
            "stt_button.typing.subprocess.run",
            lambda cmd, **kw: payloads.append(kw.get("input", "")) or subprocess_result(),
        )
        return payloads

    def _led_sequence(self, monkeypatch, states: list[str]) -> None:
        """Return states in sequence; thereafter, keep returning the last one."""
        seq = iter(states)
        last = states[-1]
        monkeypatch.setattr(
            "stt_button.typing._capslock_state", lambda: next(seq, last)
        )

    def test_default_never_toggles_even_when_led_off(self, monkeypatch) -> None:
        """LED off (= inverted, CapsLock active) -> NO toggle by default."""
        monkeypatch.setattr("stt_button.typing._capslock_state", lambda: "aus")
        payloads = self._capture_payloads(monkeypatch)
        DotoolTyper().type_text("Das")
        assert not any("key capslock" in p for p in payloads), payloads
        assert any("type Das" in p for p in payloads), payloads

    def test_default_warns_when_led_off(self, monkeypatch, caplog) -> None:
        """Warn when CapsLock is active (LED off, inverted)."""
        import logging as _l
        monkeypatch.setattr("stt_button.typing._capslock_state", lambda: "aus")
        self._capture_payloads(monkeypatch)
        with caplog.at_level(_l.WARNING, logger="stt_button.typing"):
            DotoolTyper().type_text("Das")
        messages = [r.message for r in caplog.records]
        assert any("CapsLock" in m for m in messages), messages

    def test_autocorrect_inverted_toggles_when_led_off(self, monkeypatch) -> None:
        """inverted=True: LED off means CapsLock active -> target 'on' -> toggle."""
        self._led_sequence(monkeypatch, ["aus", "an"])
        payloads = self._capture_payloads(monkeypatch)
        DotoolTyper(capslock_autocorrect=True, capslock_led_inverted=True).type_text("Das")
        assert any("key capslock" in p for p in payloads), payloads

    def test_autocorrect_conventional_toggles_when_led_on(self, monkeypatch) -> None:
        """inverted=False: LED on means CapsLock active -> target 'off' -> toggle."""
        self._led_sequence(monkeypatch, ["an", "aus"])
        payloads = self._capture_payloads(monkeypatch)
        DotoolTyper(capslock_autocorrect=True, capslock_led_inverted=False).type_text("Das")
        assert any("key capslock" in p for p in payloads), payloads

    def test_autocorrect_inverted_led_on_sends_no_toggle(self, monkeypatch) -> None:
        """inverted=True, LED on = already normal -> no toggle."""
        monkeypatch.setattr("stt_button.typing._capslock_state", lambda: "an")
        payloads = self._capture_payloads(monkeypatch)
        DotoolTyper(capslock_autocorrect=True, capslock_led_inverted=True).type_text("Das")
        assert not any("key capslock" in p for p in payloads)
        assert any("type Das" in p for p in payloads)

    def test_autocorrect_gives_up_after_two_attempts(self, monkeypatch) -> None:
        """Toggle cannot be verified -> give up after 2 attempts."""
        self._led_sequence(monkeypatch, ["aus", "aus", "aus"])
        payloads = self._capture_payloads(monkeypatch)
        DotoolTyper(capslock_autocorrect=True, capslock_led_inverted=True).type_text("Das")
        assert sum("key capslock" in p for p in payloads) == 2, payloads
        # The text is typed anyway (dictation must not be dropped).
        assert any("type Das" in p for p in payloads)

    def test_unknown_state_never_toggles(self, monkeypatch) -> None:
        monkeypatch.setattr("stt_button.typing._capslock_state", lambda: "?")
        payloads = self._capture_payloads(monkeypatch)
        DotoolTyper(capslock_autocorrect=True, capslock_led_inverted=True).type_text("Das")
        assert not any("key capslock" in p for p in payloads)



class TestSubprocessHaertung:
    """Hardening against the daemon pipe hang (final_paste freeze).

    Root cause: ``wl-copy`` forks a resident daemon process and inherits the
    stdout/stderr pipe FDs to GRANDCHILD processes. ``subprocess.run`` with
    ``capture_output=True`` (implicitly without a timeout) waits in
    ``communicate()`` for EOF on these pipes; it never arrives, so the
    finalizer thread hangs forever and the service freezes.

    Fix: ``_run()`` uses DEVNULL for all three FDs and a timeout;
    ``DotoolTyper._send_raw`` retains capture_output (dotool does not fork)
    but receives a payload-dependent timeout.
    """

    def _make_paster_with_blocking_copy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[ClipboardPaster, FakeTyper]:
        """Create a paster whose wl-copy hangs like a daemon.

        The fake simulates ``wl-copy`` BEFORE hardening: subprocess.run WITHOUT
        a ``timeout`` kwarg blocks forever (the event is never set). A call
        WITH ``timeout`` blocks only briefly and raises ``TimeoutExpired``;
        then the timeout fallback applies.
        """
        entered = threading.Event()
        never = threading.Event()  # never set -> blocks indefinitely

        def fake_run(cmd, *args, **kwargs):
            entered.set()
            if "timeout" in kwargs and kwargs["timeout"] is not None:
                # Hardening active: wait briefly, then raise like subprocess.
                raise subprocess.TimeoutExpired(cmd, timeout=kwargs["timeout"])
            # Before hardening: block forever (no timeout kwarg).
            never.wait(10.0)
            raise AssertionError("unreachable")  # just as a safeguard

        monkeypatch.setattr("stt_button.typing.subprocess.run", fake_run)
        monkeypatch.setattr(
            "stt_button.typing.shutil.which", lambda b: "/usr/bin/wl-copy"
        )
        t = FakeTyper()
        p = ClipboardPaster(t, copy_binary="wl-copy", paste_key="ctrl+v")
        p._test_copy_entered = entered  # type: ignore[attr-defined]
        return p, t

    def test_blocking_copy_does_not_freeze_paste(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Freeze reproduction: hanging wl-copy must not block paste().

        Reproduces the final_paste freeze: without a timeout in ``_run()``,
        the daemon pipe hang would block paste() forever. After hardening,
        the timeout raises TyperError and the fallback types the delta
        character by character via type_text.
        """
        p, t = self._make_paster_with_blocking_copy(monkeypatch)

        worker = threading.Thread(target=p.paste, args=("Delta",))
        worker.start()
        worker.join(timeout=0.15)
        # Core assertion: paste() must return (no daemon hang).
        assert not worker.is_alive(), (
            "paste() haengt am blockierenden wl-copy - "
            "_run() braucht DEVNULL+Timeout"
        )
        # Fallback: the delta was typed character by character.
        assert t.calls == [("type", "Delta")]

    def test_run_uses_devnull_and_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_run() uses DEVNULL FDs and a timeout, not capture_output.

        DEVNULL is the actual fix for the daemon pipe hang: without open pipe
        FDs, communicate() can never wait for an EOF that never arrives. The
        timeout is the second line of defense.
        """
        captured: dict[str, object] = {}

        def fake_run(cmd, *args, **kwargs):
            captured.update(kwargs)
            return subprocess_result()

        monkeypatch.setattr("stt_button.typing.subprocess.run", fake_run)
        from stt_button.typing import _run

        _run(["wtype", "-k", "space"])

        assert captured["stdin"] is subprocess.DEVNULL
        assert captured["stdout"] is subprocess.DEVNULL
        assert captured["stderr"] is subprocess.DEVNULL
        assert "capture_output" not in captured
        assert "timeout" in captured
        assert isinstance(captured["timeout"], (int, float))
        assert captured["timeout"] > 0
        assert captured.get("check") is False

    def test_run_timeout_raises_typer_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """subprocess.TimeoutExpired is translated into TyperError."""

        def fake_run(cmd, *args, **kwargs):
            raise subprocess.TimeoutExpired(cmd, timeout=kwargs.get("timeout"))

        monkeypatch.setattr("stt_button.typing.subprocess.run", fake_run)
        from stt_button.typing import _run

        with pytest.raises(TyperError, match="timeout"):
            _run(["wtype", "-k", "space"])

    def test_send_raw_timeout_scales_with_payload(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_send_raw: timeout grows with payload size (type lines).

        Budget: max(5.0, chars*(typedelay+typehold)/1000 + 5.0), so long
        dictations do not fail due to a fixed timeout.
        """
        timeouts: list[float | None] = []

        def fake_run(cmd, *args, **kwargs):
            timeouts.append(kwargs.get("timeout"))
            return subprocess_result()

        monkeypatch.setattr("stt_button.typing.subprocess.run", fake_run)
        monkeypatch.setattr("stt_button.typing._capslock_state", lambda: "aus")
        monkeypatch.setattr(
            "stt_button.typing.shutil.which", lambda b: "/usr/bin/dotool"
        )

        # short payload: small budget (5s + 0 + 5s buffer = 10s)
        DotoolTyper(typedelay_ms=20, typehold_ms=15).type_text("Hi")
        short = timeouts[-1]
        assert short is not None and short >= 5.0

        # long payload (500 characters): budget must be substantially larger.
        DotoolTyper(typedelay_ms=20, typehold_ms=15).type_text("x" * 500)
        long = timeouts[-1]
        assert long is not None
        assert long > short
        # expected: 500*(20+15)/1000 + 5.0 = 22.5s
        assert long == pytest.approx(22.5, rel=0.01)

    def test_send_raw_timeout_expired_raises_typer_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """dotool may also hang -> TimeoutExpired becomes TyperError."""

        def fake_run(cmd, *args, **kwargs):
            raise subprocess.TimeoutExpired(cmd, timeout=kwargs.get("timeout"))

        monkeypatch.setattr("stt_button.typing.subprocess.run", fake_run)
        monkeypatch.setattr("stt_button.typing._capslock_state", lambda: "aus")
        monkeypatch.setattr(
            "stt_button.typing.shutil.which", lambda b: "/usr/bin/dotool"
        )
        with pytest.raises(TyperError, match="timeout"):
            DotoolTyper().type_text("Hallo")


class TestPasteDebugLogs:
    """Debug logs in the paste path (final_paste freeze root-cause diagnosis).

    Logs must NOT contain dictated content (log data minimization), but must
    include length and phases (copy start/done, chord) to diagnose a freeze.
    """

    def test_paste_logs_phases_without_text(
        self, monkeypatch: pytest.MonkeyPatch, caplog
    ) -> None:
        import logging as _l

        t = FakeTyper()
        monkeypatch.setattr(
            "stt_button.typing.shutil.which", lambda b: "/usr/bin/wl-copy"
        )
        monkeypatch.setattr(
            "stt_button.typing.subprocess.run",
            lambda cmd, *a, **k: subprocess_result(),
        )
        p = ClipboardPaster(t, copy_binary="wl-copy", paste_key="ctrl+v")
        with caplog.at_level(_l.DEBUG, logger="stt_button.typing"):
            p.paste("Streng geheim")
        messages = [r.message for r in caplog.records]
        joined = "\n".join(messages)
        # phases visible:
        assert "copy start" in joined
        assert "copy done" in joined
        assert "chord" in joined
        # length is logged:
        assert "13 characters" in joined
        # dictated content must NOT appear in the log:
        assert "Streng geheim" not in joined




# ---------------------------------------------------------------------------
# OPT1 (operator, October 29): finalize backspace + paste chord in ONE dotool
# process—the ~0.7s cost is process startup, not the keys. The combined call
# saves an entire round trip.
# ---------------------------------------------------------------------------


class TestOpt1CombinedBackspacePaste:
    def test_dotool_backspace_and_key_single_process(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """backspace_and_key sends backspaces + chord in ONE _send."""
        stdin = _capture_stdin(monkeypatch)
        DotoolTyper().backspace_and_key(3, "ctrl+v")
        assert len(stdin) == 1, "genau ein dotool-Prozess erwartet"
        assert stdin[0] == (
            "key backspace\nkey backspace\nkey backspace\nkey ctrl+v\n"
        )

    def test_dotool_backspace_and_key_zero_is_plain_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """n=0 behaves exactly like key(name)—one process, chord only."""
        stdin = _capture_stdin(monkeypatch)
        DotoolTyper().backspace_and_key(0, "ctrl+v")
        assert stdin == ["key ctrl+v\n"]

    def test_paster_combined_copies_then_one_chord(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """paste_combined: first wl-copy, then ONE combined call (no second chord)."""
        calls: list[list[str]] = []
        monkeypatch.setattr("stt_button.typing.shutil.which",
                            lambda b: "/usr/bin/wl-copy")
        monkeypatch.setattr("stt_button.typing._run",
                            lambda cmd, *a, **k: calls.append(list(cmd)))

        class Recorder:
            def __init__(self):
                self.key_calls: list[str] = []
                self.combined: list[tuple[int, str]] = []

            def key(self, name):
                self.key_calls.append(name)

            def backspace_and_key(self, n, name):
                self.combined.append((n, name))

            def type_text(self, text):
                raise AssertionError("type_text unerwartet")

        rec = Recorder()
        p = ClipboardPaster(rec)  # type: ignore[arg-type]
        ok = p.paste_combined("Hallo", backspaces=3)
        assert ok is True
        assert calls == [["wl-copy", "Hallo"]]
        assert rec.combined == [(3, "ctrl+v")]
        assert rec.key_calls == [], "zweiter separater Chord waere Doppel-Timing"

    def test_paster_combined_never_double_inserts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Chord error AFTER successful copy: TyperError (state unknown)—NO
        silent False, no fallback to paste()/type_text (risk of duplicate
        insertion or deletion)."""
        monkeypatch.setattr("stt_button.typing.shutil.which",
                            lambda b: "/usr/bin/wl-copy")
        monkeypatch.setattr("stt_button.typing._run", lambda cmd, *a, **k: None)

        class Boom:
            def key(self, name):  # pragma: no cover
                raise AssertionError("key darf nicht separat laufen")

            def backspace_and_key(self, n, name):
                raise TyperError("dotool tot")

            def type_text(self, text):  # pragma: no cover
                raise AssertionError("type_text darf nicht nachchlegen")

        p = ClipboardPaster(Boom())  # type: ignore[arg-type]
        with pytest.raises(TyperError):
            p.paste_combined("Hallo", backspaces=3)

    def test_paster_combined_empty_text_no_chord(self) -> None:
        """Empty text: no copy, no chord, ok=False (caller uses legacy path)."""

        class Never:
            def key(self, name):  # pragma: no cover
                raise AssertionError("key bei leerem Text")

            def backspace_and_key(self, n, name):  # pragma: no cover
                raise AssertionError("combined bei leerem Text")

        p = ClipboardPaster(Never())  # type: ignore[arg-type]
        assert p.paste_combined("", backspaces=3) is False

    def test_finalize_uses_combined_path(self) -> None:
        """finalize() with to_remove>0 and a combined-capable paster calls
        combined EXACTLY ONCE, with no separate backspace()/paste()."""

        class TyperRec:
            def __init__(self):
                self.backspaces: list[int] = []

            def backspace(self, n):
                self.backspaces.append(n)

            def type_text(self, t):
                pass

            def key(self, name):
                pass

        typer = TyperRec()

        class CombinedPaster:
            def __init__(self):
                self.combined: list[tuple[str, int]] = []

            def paste(self, text):
                raise AssertionError("paste() darf im Combined-Pfad nicht rufen")

            def paste_combined(self, text, backspaces):
                self.combined.append((text, backspaces))
                return True

        paster = CombinedPaster()
        d = DeltaTyper(typer, append_space=True, final_paster=paster)  # type: ignore[arg-type]
        d.update("abcd efgh", committed_len=0, force_revision=True)
        typer.backspaces.clear()
        # Final version differs at position 0 -> to_remove=8, new to_add
        d.finalize("Xbcd efgh")
        # old contains the append_space space: 8+1=9 characters removed
        assert paster.combined == [("Xbcd efgh ", 9)]
        assert typer.backspaces == [], "separater backspace-Lauf waere ein zweiter Prozess"

    def test_finalize_plain_paster_path_unchanged(self) -> None:
        """Paster without paste_combined (e.g., fake/legacy paster): retain the
        previous backspace() + paste() path unchanged."""
        events: list = []

        class TyperRec:
            def backspace(self, n):
                events.append(("bs", n))

            def type_text(self, t):
                events.append(("type", t))

            def key(self, name):
                events.append(("key", name))

        class OldPaster:
            def paste(self, text):
                events.append(("paste", text))

        d = DeltaTyper(TyperRec(), append_space=True, final_paster=OldPaster())  # type: ignore[arg-type]
        d.update("abcd efgh", committed_len=0, force_revision=True)
        events.clear()
        d.finalize("Xbcd efgh")
        assert ("bs", 9) in events and ("paste", "Xbcd efgh ") in events


class TestFocusGatedFinalPaste:
    """Final delta: terminal focus -> clipboard only (no chord);
    browser focus -> typed via typer, clipboard untouched."""

    @staticmethod
    def _delta(monkeypatch, probe, **kw):
        calls = _capture(monkeypatch)
        monkeypatch.setattr("stt_button.typing.shutil.which", lambda b: "/usr/bin/" + b)
        t = FakeTyper()
        p = ClipboardPaster(t, copy_binary="wl-copy", paste_key="ctrl+v")
        d = DeltaTyper(t, final_paster=p, focus_probe=probe, **kw)
        return d, t, calls

    @staticmethod
    def _wl_copies(calls):
        return [c for c in calls if c and c[0] == "wl-copy"]

    def test_terminal_focus_copies_delta_with_space(self, monkeypatch) -> None:
        d, t, calls = self._delta(monkeypatch, lambda: True, append_space=True)
        d.update("Hallo", committed_len=5)
        t.calls.clear()
        d.finalize("Hallo Welt", committed_len=5)
        assert self._wl_copies(calls) == [["wl-copy", " Welt "]]
        assert ("key", "space") not in t.calls

    def test_terminal_focus_sends_no_chord(self, monkeypatch) -> None:
        d, t, calls = self._delta(monkeypatch, lambda: True)
        d.update("abcd", committed_len=0)
        t.calls.clear()
        d.finalize("abXY")
        assert not any(c[0] == "key" and "+" in c[1] for c in t.calls)
        assert ("backspace", "2") in t.calls
        assert t.typed == ""
        assert self._wl_copies(calls) == [["wl-copy", "XY "]]

    def test_unknown_focus_behaves_like_terminal(self, monkeypatch) -> None:
        d, t, calls = self._delta(monkeypatch, lambda: None)
        d.finalize("Hallo")
        assert self._wl_copies(calls) == [["wl-copy", "Hallo "]]
        assert ("key", "ctrl+v") not in t.calls

    def test_probe_exception_behaves_like_terminal(self, monkeypatch) -> None:
        def boom():
            raise RuntimeError("dbus down")
        d, t, calls = self._delta(monkeypatch, boom)
        d.finalize("Hallo")
        assert self._wl_copies(calls) == [["wl-copy", "Hallo "]]
        assert ("key", "ctrl+v") not in t.calls

    def test_browser_focus_types_and_never_touches_clipboard(self, monkeypatch) -> None:
        d, t, calls = self._delta(monkeypatch, lambda: False, append_space=True)
        d.update("abcd", committed_len=0)
        t.calls.clear()
        d.finalize("abXY")
        assert self._wl_copies(calls) == []
        assert t.calls == [("backspace", "2"), ("type", "XY"), ("key", "space")]

    def test_browser_focus_without_space(self, monkeypatch) -> None:
        d, t, calls = self._delta(monkeypatch, lambda: False, append_space=False)
        d.finalize("Hallo")
        assert self._wl_copies(calls) == []
        assert t.calls == [("type", "Hallo")]

    def test_no_probe_keeps_legacy_chord(self, monkeypatch) -> None:
        d, t, calls = self._delta(monkeypatch, None, append_space=True)
        d.finalize("Hallo")
        assert self._wl_copies(calls) == [["wl-copy", "Hallo "]]
        assert ("key", "ctrl+v") in t.calls
        assert ("key", "space") not in t.calls

    def test_no_paster_ignores_probe_and_types(self, monkeypatch) -> None:
        """final_paste=false: typer path unchanged, probe never consulted."""
        calls = _capture(monkeypatch)
        t = FakeTyper()
        asked: list[int] = []
        d = DeltaTyper(t, focus_probe=lambda: asked.append(1) or True)
        d.finalize("Hallo")
        assert t.calls == [("type", "Hallo"), ("key", "space")]
        assert self._wl_copies(calls) == []
        assert asked == []

    def test_probe_called_once_per_finalize(self, monkeypatch) -> None:
        asked: list[int] = []
        d, t, calls = self._delta(monkeypatch, lambda: asked.append(1) or False)
        d.update("abcd", committed_len=0)
        assert asked == []
        d.finalize("abXY")
        assert asked == [1]


class TestClipboardPasterCopy:
    def test_copy_runs_wl_copy_without_chord(self, monkeypatch) -> None:
        calls = _capture(monkeypatch)
        monkeypatch.setattr("stt_button.typing.shutil.which", lambda b: "/usr/bin/" + b)
        t = FakeTyper()
        p = ClipboardPaster(t, copy_binary="wl-copy")
        assert p.copy("abc") is True
        assert calls == [["wl-copy", "abc"]]
        assert t.calls == []

    def test_copy_missing_binary_falls_back_to_typing(self, monkeypatch) -> None:
        _capture(monkeypatch)
        monkeypatch.setattr("stt_button.typing.shutil.which", lambda b: None)
        t = FakeTyper()
        assert ClipboardPaster(t, copy_binary="wl-copy").copy("abc") is False
        assert t.calls == [("type", "abc")]

    def test_copy_empty_is_noop(self, monkeypatch) -> None:
        calls = _capture(monkeypatch)
        t = FakeTyper()
        assert ClipboardPaster(t).copy("") is False
        assert calls == [] and t.calls == []


class TestFocusUnknownFallback:
    """F2: configurable fallback for unknown focus + loud first-None warning."""

    @staticmethod
    def _typer(monkeypatch, probe, **kw):
        monkeypatch.setattr("stt_button.typing.shutil.which", lambda b: "/usr/bin/" + b)
        t = FakeTyper()
        p = ClipboardPaster(t, copy_binary="wl-copy", paste_key="ctrl+v")
        return DeltaTyper(t, final_paster=p, focus_probe=probe, **kw)

    def test_unknown_focus_default_copies(self, monkeypatch) -> None:
        assert self._typer(monkeypatch, lambda: None)._final_mode() == "copy"

    def test_unknown_focus_type_fallback(self, monkeypatch) -> None:
        d = self._typer(monkeypatch, lambda: None, focus_unknown_fallback="type")
        assert d._final_mode() == "type"

    def test_type_fallback_keeps_terminal_copy(self, monkeypatch) -> None:
        d = self._typer(monkeypatch, lambda: True, focus_unknown_fallback="type")
        assert d._final_mode() == "copy"

    def test_unknown_focus_warns_once(self, monkeypatch, caplog) -> None:
        d = self._typer(monkeypatch, lambda: None)
        with caplog.at_level("WARNING", logger="stt_button.typing"):
            d._final_mode(); d._final_mode()
        assert sum("focus extension unreachable" in r.message
                   for r in caplog.records) == 1

    def test_warning_rearms_after_recovery(self, monkeypatch, caplog) -> None:
        states = iter([None, False, None])
        d = self._typer(monkeypatch, lambda: next(states))
        with caplog.at_level("WARNING", logger="stt_button.typing"):
            d._final_mode(); d._final_mode(); d._final_mode()
        assert sum("focus extension unreachable" in r.message
                   for r in caplog.records) == 2

    def test_on_focus_unknown_called_once_per_outage(self, monkeypatch) -> None:
        hits = []
        d = self._typer(monkeypatch, lambda: None,
                        on_focus_unknown=lambda: hits.append(1))
        d._final_mode(); d._final_mode()
        assert hits == [1]

    def test_on_focus_unknown_exception_is_swallowed(self, monkeypatch) -> None:
        def boom():
            raise RuntimeError("no notify")
        d = self._typer(monkeypatch, lambda: None, on_focus_unknown=boom)
        assert d._final_mode() == "copy"

    def test_invalid_fallback_rejected(self, monkeypatch) -> None:
        with pytest.raises(ValueError):
            self._typer(monkeypatch, lambda: None, focus_unknown_fallback="paste")


class TestFocusStateProbe:
    """F2b: probe may return focus_state() strings; only 'unreachable' warns."""

    @staticmethod
    def _typer(monkeypatch, probe, **kw):
        monkeypatch.setattr("stt_button.typing.shutil.which", lambda b: "/usr/bin/" + b)
        t = FakeTyper()
        p = ClipboardPaster(t, copy_binary="wl-copy", paste_key="ctrl+v")
        return DeltaTyper(t, final_paster=p, focus_probe=probe, **kw)

    @pytest.mark.parametrize("state,mode", [
        ("terminal", "copy"), ("other", "type"),
        ("no_window", "copy"), ("unreachable", "copy"),
    ])
    def test_state_modes(self, monkeypatch, state, mode) -> None:
        assert self._typer(monkeypatch, lambda: state)._final_mode() == mode

    def test_type_fallback_applies_to_both_unknowns(self, monkeypatch) -> None:
        for s in ("no_window", "unreachable"):
            d = self._typer(monkeypatch, lambda: s, focus_unknown_fallback="type")
            assert d._final_mode() == "type"

    def test_no_window_does_not_warn(self, monkeypatch, caplog) -> None:
        hits = []
        d = self._typer(monkeypatch, lambda: "no_window",
                        on_focus_unknown=lambda: hits.append(1))
        with caplog.at_level("WARNING", logger="stt_button.typing"):
            d._final_mode()
        assert not any("unreachable" in r.message for r in caplog.records)
        assert hits == []

    def test_unreachable_warns(self, monkeypatch, caplog) -> None:
        d = self._typer(monkeypatch, lambda: "unreachable")
        with caplog.at_level("WARNING", logger="stt_button.typing"):
            d._final_mode()
        assert any("focus extension unreachable" in r.message
                   for r in caplog.records)

    def test_trace_names_reason(self, monkeypatch, caplog) -> None:
        d = self._typer(monkeypatch, lambda: "unreachable", latency_trace=True)
        with caplog.at_level("DEBUG", logger="stt_button.typing"):
            d._final_mode()
        assert any("focus unreachable -> copy" in r.getMessage()
                   for r in caplog.records)


class MergingFakeTyper(FakeTyper):
    """FakeTyper that also offers the merged dotool path (M2a/M2b)."""

    def backspace_and_type(self, n: int, text: str, trailing_key: str | None = None) -> None:
        self.calls.append(("bs_type", str(n), text, str(trailing_key)))


_PRE = "typedelay 20\ntypehold 15\n"


class TestM2aSingleDotoolPayload:
    def test_dotool_backspace_and_type_single_payload(self, monkeypatch) -> None:
        stdin = _capture_stdin(monkeypatch)
        DotoolTyper().backspace_and_type(2, "Welt", trailing_key="space")
        assert stdin == [
            "key backspace\nkey backspace\n" + _PRE + "type Welt\nkey space\n"
        ]

    def test_dotool_backspace_and_type_without_backspace_or_key(self, monkeypatch) -> None:
        stdin = _capture_stdin(monkeypatch)
        DotoolTyper().backspace_and_type(0, "a\nb")
        assert stdin == [_PRE + "type a\nkey enter\ntype b\n"]

    def test_finalize_merges_backspace_text_and_space(self) -> None:
        t = MergingFakeTyper()
        d = DeltaTyper(t)
        d.update("abcd")
        t.calls.clear()
        d.finalize("abXY")
        assert t.calls == [("bs_type", "2", "XY", "space")]

    def test_finalize_merges_without_backspace(self) -> None:
        t = MergingFakeTyper()
        d = DeltaTyper(t)
        d.finalize("Hallo")
        assert t.calls == [("bs_type", "0", "Hallo", "space")]

    def test_finalize_no_append_space_no_trailing_key(self) -> None:
        t = MergingFakeTyper()
        d = DeltaTyper(t, append_space=False)
        d.finalize("Hallo")
        assert t.calls == [("bs_type", "0", "Hallo", "None")]

    def test_finalize_empty_to_add_keeps_separate_space(self) -> None:
        t = MergingFakeTyper()
        d = DeltaTyper(t)
        d.update("Hallo")
        t.calls.clear()
        d.finalize("Hallo")
        assert t.calls == [("key", "space")]

    def test_finalize_typer_without_method_falls_back(self) -> None:
        t = FakeTyper()
        d = DeltaTyper(t)
        d.update("abcd")
        t.calls.clear()
        d.finalize("abXY")
        assert t.calls == [("backspace", "2"), ("type", "XY"), ("key", "space")]

    def test_finalize_dotool_end_to_end_one_process(self, monkeypatch) -> None:
        stdin = _capture_stdin(monkeypatch)
        d = DeltaTyper(DotoolTyper())
        d.finalize("Hallo")
        assert stdin == [_PRE + "type Hallo\nkey space\n"]

    def test_space_trace_only_when_separate_process(self, caplog) -> None:
        d = DeltaTyper(MergingFakeTyper(), latency_trace=True)
        with caplog.at_level("DEBUG", logger="stt_button.typing"):
            d.finalize("Hallo")
        msgs = [r.getMessage() for r in caplog.records]
        assert not any(m.startswith("latency[space ") and "in type" not in m for m in msgs)


class TestM2bLiveCorrectionSinglePayload:
    def test_correction_tick_is_one_merged_call(self) -> None:
        t = MergingFakeTyper()
        d = DeltaTyper(t)
        d.update("abcd")
        t.calls.clear()
        d.update("abXY")
        assert t.calls == [("bs_type", "2", "XY", "None")]

    def test_append_only_tick_single_call(self) -> None:
        t = MergingFakeTyper()
        d = DeltaTyper(t)
        d.update("ab")
        t.calls.clear()
        d.update("abcd")
        assert len(t.calls) == 1
        assert t.calls[0][2] == "cd"

    def test_backspace_only_tick_keeps_plain_backspace(self) -> None:
        t = MergingFakeTyper()
        d = DeltaTyper(t)
        d.update("abcd")
        t.calls.clear()
        d.update("ab")
        assert t.calls == [("backspace", "2")]

    def test_dotool_correction_one_process(self, monkeypatch) -> None:
        stdin = _capture_stdin(monkeypatch)
        d = DeltaTyper(DotoolTyper())
        d.update("abcd")
        stdin.clear()
        d.update("abXY")
        assert stdin == ["key backspace\nkey backspace\n" + _PRE + "type XY\n"]

    def test_committed_mode_unchanged(self) -> None:
        t = MergingFakeTyper()
        d = DeltaTyper(t, live_typing_mode="committed")
        d.update("Hallo Welt", committed_len=6)
        assert t.calls == [("type", "Hallo ")]


class FakePaster:
    def __init__(self) -> None:
        self.pasted: list[str] = []
        self.copied: list[str] = []

    def paste(self, text: str) -> None:
        self.pasted.append(text)

    def copy(self, text: str) -> bool:
        self.copied.append(text)
        return True


class TestM1FinalPasteMinChars:
    @staticmethod
    def _d(probe, threshold, **kw):
        t = MergingFakeTyper()
        p = FakePaster()
        d = DeltaTyper(t, final_paster=p, focus_probe=probe,
                       final_paste_min_chars=threshold, **kw)
        return d, t, p

    def test_default_zero_types_as_before(self) -> None:
        t = MergingFakeTyper()
        p = FakePaster()
        d = DeltaTyper(t, final_paster=p, focus_probe=lambda: "other")
        d.finalize("x" * 100)
        assert p.pasted == [] and p.copied == []
        assert t.calls == [("bs_type", "0", "x" * 100, "space")]

    def test_threshold_hit_pastes_once_with_space(self) -> None:
        d, t, p = self._d(lambda: "other", 10)
        d.finalize("Hallo schöne Welt")
        assert p.pasted == ["Hallo schöne Welt "]
        assert t.calls == []

    def test_threshold_no_append_space(self) -> None:
        d, t, p = self._d(lambda: "other", 3, append_space=False)
        d.finalize("Hallo")
        assert p.pasted == ["Hallo"]
        assert t.calls == []

    def test_below_threshold_types(self) -> None:
        d, t, p = self._d(lambda: "other", 10)
        d.finalize("Hallo")
        assert p.pasted == []
        assert t.calls == [("bs_type", "0", "Hallo", "space")]

    def test_backspace_before_paste_kept(self) -> None:
        d, t, p = self._d(lambda: False, 3)
        d.update("abcd")
        t.calls.clear()
        d.finalize("abWXYZ")
        assert t.calls == [("backspace", "2")]
        assert p.pasted == ["WXYZ "]

    def test_terminal_never_pastes(self) -> None:
        d, t, p = self._d(lambda: "terminal", 1)
        d.finalize("Hallo Welt")
        assert p.pasted == []
        assert p.copied == ["Hallo Welt "]

    @pytest.mark.parametrize("state", [None, "unreachable", "no_window"])
    def test_unknown_never_pastes_even_with_type_fallback(self, state) -> None:
        d, t, p = self._d(lambda: state, 1, focus_unknown_fallback="type")
        d.finalize("Hallo Welt")
        assert p.pasted == []
        assert t.calls == [("bs_type", "0", "Hallo Welt", "space")]

    def test_markers_stripped_before_threshold(self) -> None:
        d, t, p = self._d(lambda: "other", 6)
        d.finalize("^[[200~Hallo~")
        assert p.pasted == []
        assert t.calls == [("bs_type", "0", "Hallo", "space")]


class TestFinalizeDocumentingG6:
    """Documenting G6 tests (plan block B3): finalize() guarantees for the
    summary-mode short-press path. These tests only pin existing behavior."""

    def test_finalize_unchanged_text_only_space_key(self) -> None:
        """Unchanged state + append_space=True: exactly one key('space'),
        no type, no backspace (nothing to correct)."""
        t = FakeTyper()
        d = DeltaTyper(t, append_space=True)
        d.update("bereits getippter Text")
        t.calls.clear()
        d.finalize("bereits getippter Text")
        assert t.calls == [("key", "space")], t.calls

    def test_finalize_whitespace_only_no_typer_calls(self) -> None:
        """Whitespace-only final output: guard (typing.py:897) returns early,
        no typer call at all."""
        t = FakeTyper()
        d = DeltaTyper(t, append_space=True)
        d.update("Vorher")
        t.calls.clear()
        d.finalize("   ")
        assert t.calls == [], t.calls
