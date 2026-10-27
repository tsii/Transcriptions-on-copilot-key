"""Tests fuer die Delta-Ausgabe (TDD-Kette 2).

Der DeltaTyper vergleicht aufeinanderfolgende Transkriptionen und gibt nur
die Differenz aus. Korrigiert wird per Backspace.

Grundregel: Es wird immer der *minimale* Rueckbau verwendet - es werden nur
die Zeichen geloescht, die sich geaendert haben. Der committete (bereits
endgueltige) Praefix wird nie angefasst.
"""
from __future__ import annotations

import pytest

from stt_button.typing import (
    ClipboardTyper,
    DeltaTyper,
    DotoolTyper,
    TyperError,
    WtypeTyper,
)


class FakeTyper:
    """Zeichnet alle Aufrufe auf."""

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
        # Minimaler Rueckbau: gemeinsamer Praefix ist "Hallo Wel",
        # also wird nur "d" -> "t" getauscht.
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
        # Das Modell korrigiert das letzte Wort: "Welt" -> "Welz".
        # Der committete Praefix "Hallo " bleibt unangetastet.
        d.update("Hallo Welz", committed_len=6)
        assert t.calls == [("backspace", "1"), ("type", "z")]

    def test_committed_prefix_survives_contradiction(self) -> None:
        t = FakeTyper()
        d = DeltaTyper(t)
        d.update("Hallo Welt", committed_len=6)
        t.calls.clear()
        # Das Modell verliert den committeten Praefix ("Hall" statt "Hallo ").
        # Wir uebernehmen nichts davon, der committete Teil bleibt stehen.
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
        # Kompletter Widerspruch: committeter Text darf nicht geloescht werden.
        d.update("Ganz anders", committed_len=11)
        # Der committete Bereich "Hallo Welt" (11 Zeichen) bleibt, nur der
        # Rest wird angepasst - aber hier ist der Modelltext kuerzer.
        assert not any(c == ("backspace", "11") for c in t.calls)


class TestRevisionLimit:
    def test_too_long_revision_is_avoided(self) -> None:
        t = FakeTyper()
        d = DeltaTyper(t, max_revision_chars=5)
        d.update("Ein sehr langer Text der sich komplett geaendert hat")
        t.calls.clear()
        d.update("voellig anderer Text")
        # Statt 40 Backspaces nur anhaengen (kein zerstoerender Rueckbau)
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
        # Finale Korrektur darf auch >5 Zeichen zuruecknehmen, statt zu duplizieren
        d.finalize("Ein komplett anderer Text")
        # Es muss ein Backspace geben (Rueckbau) und KEIN Duplikat:
        assert any(c[0] == "backspace" for c in t.calls)
        assert ("type", "Ein komplett anderer Text") not in t.calls

    def test_finalize_empty_does_not_erase(self) -> None:
        t = FakeTyper()
        d = DeltaTyper(t, append_space=False)
        d.update("Hallo Welt")
        t.calls.clear()
        d.finalize("")
        # Eine leere Endausgabe loescht nichts - ein ASR-Fehler darf den
        # bereits getippten Text nicht vernichten.
        assert t.calls == []


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
    # CapsLock deterministisch "aus" (sonst Reset-Payload je nach Systemzustand).
    monkeypatch.setattr("stt_button.typing._capslock_state", lambda: "aus")
    return calls


def _capture_stdin(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Zeichnet den stdin-Payload statt des Returncodes auf."""
    stdin: list[str] = []

    def fake_run(cmd, *args, **kwargs):
        stdin.append(kwargs.get("input", ""))
        return subprocess_result()

    monkeypatch.setattr("stt_button.typing.subprocess.run", fake_run)
    # CapsLock deterministisch "aus", damit der Test nicht vom echten
    # Systemzustand abhaengt (sonst wird ein Reset-Payload eingefuegt).
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


class TestAppendTrap:
    def test_large_revision_replaces_when_anchor_matches(self) -> None:
        """Fix D: Grosse Revision mit passendem committed-Anker -> ersetzen, nicht anhaengen."""
        t = FakeTyper()
        d = DeltaTyper(t, append_space=False, max_revision_chars=60)
        # Langer Text mit committed-Prefix
        d.update("Das ist der Anfang eines sehr langen Textes mit vielen Woertern hier", committed_len=20)
        t.calls.clear()
        # Neue Fassung: gleicher committed-Prefix (20 Zeichen), aber langer Tail geaendert
        new = "Das ist der Anfang voellig anderer weiterer Text hier und noch mehr"
        d.update(new, committed_len=20)
        # Es MUSS zurueckgenommen werden (kein blindes Anhaengen des neuen Tails)
        assert any(c[0] == "backspace" for c in t.calls)
        # Und der neue Tail muss dem revidierten Rest entsprechen (ab common prefix)
        typed = "".join(c[1] for c in t.calls if c[0] == "type")
        assert typed != ""
        assert "anderer weiterer Text" in typed


class TestCapslockDiagnostics:
    def test_capslock_state_returns_valid_value(self) -> None:
        """Diagnose-Funktion liefert 'an', 'aus' oder '?' (nie Exception)."""
        from stt_button.typing import _capslock_state
        assert _capslock_state() in ("an", "aus", "?")

    def test_capslock_state_handles_missing_leds(self, monkeypatch) -> None:
        monkeypatch.setattr("stt_button.typing.glob.glob", lambda p: [])
        from stt_button.typing import _capslock_state
        assert _capslock_state() == "?"

    def test_send_logs_capslock_when_active(self, monkeypatch, caplog) -> None:
        """Wenn CapsLock an ist, wird eine Warnung geloggt."""
        import logging as _l
        monkeypatch.setattr("stt_button.typing._capslock_state", lambda: "an")
        calls = []
        monkeypatch.setattr(
            "stt_button.typing.subprocess.run",
            lambda *a, **k: calls.append(1) or subprocess_result(),
        )
        with caplog.at_level(_l.WARNING, logger="stt_button.typing"):
            DotoolTyper().type_text("Test")
        assert any("CAPSLOCK" in r.message for r in caplog.records)


class TestCapslockReset:
    def test_capslock_active_triggers_reset(self, monkeypatch) -> None:
        """Bei aktivem CapsLock wird 'key capslock' gesendet."""
        states = iter(["an", "aus"])          # vor Reset: an; nach Reset: aus
        monkeypatch.setattr(
            "stt_button.typing._capslock_state", lambda: next(states, "aus")
        )
        payloads: list[str] = []
        monkeypatch.setattr(
            "stt_button.typing.subprocess.run",
            lambda cmd, **kw: payloads.append(kw.get("input", "")) or subprocess_result(),
        )
        DotoolTyper().type_text("Das")
        assert any("key capslock" in p for p in payloads), payloads
        # Der eigentliche Text muss danach auch getippt werden.
        assert any("type Das" in p for p in payloads), payloads

    def test_capslock_off_sends_no_reset(self, monkeypatch) -> None:
        """Bei ausgeschaltetem CapsLock wird kein Reset gesendet."""
        monkeypatch.setattr("stt_button.typing._capslock_state", lambda: "aus")
        payloads: list[str] = []
        monkeypatch.setattr(
            "stt_button.typing.subprocess.run",
            lambda cmd, **kw: payloads.append(kw.get("input", "")) or subprocess_result(),
        )
        DotoolTyper().type_text("Das")
        assert not any("key capslock" in p for p in payloads)
        assert any("type Das" in p for p in payloads)
