"""Tests fuer die Delta-Ausgabe (TDD-Kette 2).

Der DeltaTyper vergleicht aufeinanderfolgende Transkriptionen und gibt nur
die Differenz aus. Korrigiert wird per Backspace.

Grundregel: Es wird immer der *minimale* Rueckbau verwendet - es werden nur
die Zeichen geloescht, die sich geaendert haben. Der committete (bereits
endgueltige) Praefix wird nie angefasst.
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


class TestFinalPaste:
    """Final-Delta-Paste: finalize() pastet nur das berechnete Delta."""

    def test_live_update_never_pastes(self) -> None:
        """Live-Updates bleiben beim Typer (dotool), niemals Paste waehrend der Aufnahme."""
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
        """"Hallo Welt" -> "Hallo Welt und mehr": nur " und mehr" wird gepastet."""
        t = FakeTyper()
        pastes: list[str] = []

        class Recorder:
            def paste(self, text: str) -> None:
                pastes.append(text)

        d = DeltaTyper(t, final_paster=Recorder())
        d.update("Hallo Welt", committed_len=0)
        d.finalize("Hallo Welt und mehr", committed_len=11)
        # append_space=True (Default): Das Leerzeichen haengt direkt am
        # Paste - KEIN separater Space-Key (Space-Microfix).
        assert pastes == [" und mehr "]
        assert t.typed == "Hallo Welt"  # kein doppeltes Tippen des Suffixes

    def test_finalize_backspaces_before_pasting_revision_delta(self) -> None:
        """"Weld" -> "Welt": backspace 1, dann paste "t" - keine Neu-Eingabe des Praefixes."""
        t = FakeTyper()
        pastes: list[str] = []

        class Recorder:
            def paste(self, text: str) -> None:
                pastes.append(text)

        d = DeltaTyper(t, final_paster=Recorder())
        d.update("Hallo Weld")
        t.calls.clear()
        d.finalize("Hallo Welt")
        # append_space=True (Default): Backspace, dann Paste inklusive
        # Leerzeichen - KEIN separater Space-Key (Space-Microfix).
        assert t.calls == [("backspace", "1")]
        assert pastes == ["t "]

    def test_finalize_empty_delta_only_appends_space(self) -> None:
        """Kein Delta (Text unveraendert) => kein Paste, nur Leerzeichen."""
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
        """Das Leerzeichen kommt nach dem Paste - beide Aufrufe in richtiger Reihenfolge."""
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
        # Das Leerzeichen ist IM Paste enthalten - KEIN separater Space-Key
        # (kein doppeltes Leerzeichen).
        assert t.calls == [], t.calls

    def test_finalize_append_space_with_paster_empty_delta_only_key_space(
        self,
    ) -> None:
        """(BUG2/7) Paster + to_add LEER: Space weiterhin via key('space')."""
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

    def test_finalize_append_space_without_paster_separate_space(self) -> None:
        """(BUG2/8) Ohne Paster: getrennter Space bleibt (bestehendes Verhalten)."""
        t = FakeTyper()
        d = DeltaTyper(t, append_space=True)
        d.update("Hallo")
        t.calls.clear()
        d.finalize("Hallo Welt")
        assert t.calls == [("type", " Welt"), ("key", "space")], t.calls


class TestClipboardPaster:
    """ClipboardPaster: wl-copy + Paste-Chord mit Fallback auf type_text."""

    def test_clipboard_paster_copies_delta_and_uses_configured_chord(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Copy via paste_binary, Chord aus paste_key (ctrl+shift+v), kein Restore."""
        calls = _capture(monkeypatch)
        t = FakeTyper()
        p = ClipboardPaster(t, copy_binary="wl-copy", paste_key="ctrl+shift+v")
        p.paste(" und mehr")
        assert calls == [
            ["wl-copy", " und mehr"],
            # Chord via den normalen Typer (dotool-Format: ctrl+shift+v)
        ]
        assert t.calls == [("key", "ctrl+shift+v")]
        # KEIN wl-paste, kein Zurueckspielen des alten Clipboard-Inhalts
        assert all("wl-paste" not in " ".join(c) for c in calls)

    def test_missing_or_failing_wl_copy_falls_back_without_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fehlt wl-copy oder schlaegt der Copy fehl => type_text-Fallback, KEIN Paste-Chord."""
        t = FakeTyper()
        monkeypatch.setattr("stt_button.typing.shutil.which", lambda b: None)
        p = ClipboardPaster(t, copy_binary="wl-copy")
        p.paste(" und mehr")
        assert t.calls == [("type", " und mehr")]  # Alter Pfad

        # Copy schlaegt fehl (rc != 0) => ebenfalls type_text, kein Chord
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
        """Chord-Fehler nach erfolgreichem Copy: NICHT mit type_text wiederholen."""
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
        p.paste(" und mehr")  # darf NICHT werfen und NICHT tippen
        assert t.calls == []


class TestFinalizeTrace:
    """latency_trace-Tracepunkte in DeltaTyper.finalize() (Debug-Stufe).

    Die Meldungen muessen auf DEBUG erscheinen und die Domino-Groessen
    (zu loeschende/zu tippende Zeichen) nennen, damit die gefuehlte
    Release-Latenz ohne Raten zerlegbar ist.
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
    """live_typing_mode='committed': Live nur den gesicherten Zuwachs tippen.

    Der unstabile Partial wird intern weitergefuehrt, aber NIE getippt oder
    zurueckgetippt: kein Flackern, keine Live-Backspaces. finalize() rechnet
    gegen den effektiv getippten Stand - to_remove==0, das Suffix kommt als
    ein Paste (null Backspaces).
    """

    def _committed(self, typer, **kwargs):
        return DeltaTyper(typer, append_space=False, live_typing_mode="committed",
                          **kwargs)

    def test_partial_without_commit_types_nothing(self) -> None:
        """(1) Partial ohne Commit -> keine Ausgabe."""
        t = FakeTyper()
        d = self._committed(t)
        d.update("Hallo Welt", committed_len=0)
        assert t.calls == []

    def test_correction_before_commit_is_silent(self) -> None:
        """(2) Korrektur vor Commit -> keine Ausgabe, keine Backspaces."""
        t = FakeTyper()
        d = self._committed(t)
        d.update("Hallo Weld", committed_len=0)
        d.update("Hallo Welt", committed_len=0)
        assert t.calls == []

    def test_commit_types_exactly_once(self) -> None:
        """(3) Commit -> genau einmal tippen (bis zum committeten Praefix)."""
        t = FakeTyper()
        d = self._committed(t)
        d.update("Hallo Welt und", committed_len=11)
        assert t.calls == [("type", "Hallo Welt ")]

    def test_next_commit_types_only_increment(self) -> None:
        """(4) Naechster Commit -> nur der Zuwachs wird getippt."""
        t = FakeTyper()
        d = self._committed(t)
        d.update("Hallo Welt und", committed_len=11)
        t.calls.clear()
        d.update("Hallo Welt und mehr", committed_len=15)
        assert t.calls == [("type", "und ")]

    def test_finalize_pastes_suffix_without_backspaces(self) -> None:
        """(5) Finaltext 'Hallo Welt und mehr' -> null Backspaces, paste(' und mehr')."""
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
        """(6) Null Live-Commits -> kompletter Finaltext genau einmal gepastet."""
        t = FakeTyper()
        pastes: list[str] = []

        class Recorder:
            def paste(self, text: str) -> None:
                pastes.append(text)

        d = self._committed(t, final_paster=Recorder())
        d.update("Hallo Welt", committed_len=0)   # nie committet
        d.update("Hallo Welt und", committed_len=0)
        d.finalize("Hallo Welt und mehr")
        assert pastes == ["Hallo Welt und mehr"]
        assert not any(c[0] == "backspace" for c in t.calls), t.calls
        assert not any(c[0] == "type" for c in t.calls), t.calls

    def test_empty_final_and_committed_change_protections_stay(self) -> None:
        """(7) Schutz: leerer Finaltext loescht nichts; weicht das Modell im
        committeten Bereich ab, gewinnt die alte Fassung (kein Zuruecktippen)."""
        t = FakeTyper()
        d = self._committed(t)
        d.update("Hallo Welt und", committed_len=10)
        t.calls.clear()
        d.finalize("")
        assert t.calls == []

        # committed-Praefix-Abweichung im Finaltext: alter getippter Praefix
        # gewinnt, nur das Suffix wird als Delta gepastet - null Backspaces.
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
        """(8) Default 'full' = exakt bisheriges Verhalten (Partial live tippen)."""
        t = FakeTyper()
        d = DeltaTyper(t, append_space=False)  # kein live_typing_mode -> full
        d.update("Hallo Weld")
        assert t.calls == [("type", "Hallo Weld")]
        t.calls.clear()
        d.update("Hallo Welt")
        assert t.calls == [("backspace", "1"), ("type", "t")]



class TestPauseIndicator:
    """STUFE 4: Pausen-Indikator, wenn die Live-ASR wegen des Limits abschaltet.

    ``show_paused()`` tippt den Indikator (``PAUSE_INDICATOR``) GENAU EINMAL
    als echten getippten Stand (``_last_text`` wird verlaengert), sodass
    die bestehende finalize()-Differenz ihn automatisch wieder entfernt.
    """

    def test_show_paused_types_indicator_exactly_once(self) -> None:
        """(1) Indikator genau einmal tippen, erneuter Aufruf ist No-Op."""
        from stt_button.typing import PAUSE_INDICATOR

        t = FakeTyper()
        d = DeltaTyper(t, append_space=False)
        d.update("Hallo Welt")
        t.calls.clear()
        d.show_paused()
        d.show_paused()  # idempotent: kein Doppeltippen
        assert t.calls == [("type", PAUSE_INDICATOR)], t.calls
        # ASCII: drei Punkte (dotool kann U+2026 im de/nodeadkeys-Layout
        # NICHT tippen - Ground-Truth-Experiment vom 11:36-Realtest).
        assert PAUSE_INDICATOR == "..."
        assert len(PAUSE_INDICATOR) == 3

    def test_finalize_removes_indicator_and_pastes_suffix(self) -> None:
        """(2) finalize: Diff entfernt den Indikator (3 Backspaces), Suffix ohne Rest."""
        from stt_button.typing import PAUSE_INDICATOR

        t = FakeTyper()
        pastes: list[str] = []

        class Recorder:
            def paste(self, text: str) -> None:
                pastes.append(text)

        d = DeltaTyper(t, append_space=False, final_paster=Recorder())
        d.update("Hallo Welt", committed_len=0)  # Modus 'full'
        d.show_paused()
        t.calls.clear()
        d.finalize("Hallo Welt und mehr")
        # Nur der Indikator (3 Zeichen '...') wird zurueckgenommen:
        assert t.calls == [("backspace", str(len(PAUSE_INDICATOR)))], t.calls
        assert pastes == [" und mehr"]
        assert all(PAUSE_INDICATOR not in p for p in pastes)

    def test_finalize_without_paster_types_suffix_after_backspace(self) -> None:
        """(2b) Ohne final_paster: Backspace(3), dann Suffix zeichenweise tippen."""
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
        """(3) Leerer final_text: Indikator bleibt stehen, keine Ausgabe."""
        from stt_button.typing import PAUSE_INDICATOR

        t = FakeTyper()
        d = DeltaTyper(t, append_space=False)
        d.update("Hallo Welt")
        d.show_paused()
        t.calls.clear()
        d.finalize("")
        assert t.calls == [], t.calls
        # Ehrlicher Zustand: Indikator bleibt am Cursor (und im Stand).
        assert d.last_text.endswith(PAUSE_INDICATOR)

    def test_reset_clears_pause_state(self) -> None:
        """(4) reset() loescht die Indikator-Markierung (naechste Aufnahme sauber)."""
        from stt_button.typing import PAUSE_INDICATOR

        t = FakeTyper()
        d = DeltaTyper(t, append_space=False)
        d.show_paused()
        d.reset()
        t.calls.clear()
        d.show_paused()
        assert t.calls == [("type", PAUSE_INDICATOR)], t.calls

    def test_committed_mode_same_mechanics(self) -> None:
        """(6) live_typing_mode='committed': gleiche Mechanik, 3 Backspaces."""
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
        # Der Indikator haengt direkt an "Hallo Welt" (kein Leerzeichen
        # dazwischen) -> gemeinsamer Praefix leer, ganzes Suffix gepastet.
        assert pastes == [" und mehr"]

    def test_indicator_dots_collision_real_text(self) -> None:
        """(9) Indikator '...' vs. echte Punkte: kein Text-Fressen, kein Rest.

        Grund-Truth-Kante: Getippter Stand ist 'Hallo' + Indikator '...',
        finaler Text 'HalloWelt.' endet auf ECHTEM Punkt. Die Differenz
        rechnet praefixbasiert (common prefix von 'Hallo...' vs.
        'HalloWelt.'): 'Hallo' bleibt stehen, die drei Indikator-Punkte
        werden per Backspace(3) entfernt, 'Welt.' WIRD KOMPLETT neu
        getippt/gepastet - inklusive des echten Punkts am Ende. Der
        gemeinsame Praefix endet VOR den Punkten (punkt 6 von 'HalloWelt.'
        ist 'W', nicht '.'), also kann die Diff keine echten Punkte
        'fressen': to_remove zaehlt NUR den Indikator-Rest.
        """
        from stt_button.typing import PAUSE_INDICATOR

        assert PAUSE_INDICATOR == "..."
        t = FakeTyper()
        pastes: list[str] = []

        class Recorder:
            def paste(self, text: str) -> None:
                pastes.append(text)

        d = DeltaTyper(t, append_space=False, final_paster=Recorder())
        d.update("Hallo", committed_len=0)  # Modus 'full'
        d.show_paused()  # tippt '...' -> Stand 'Hallo...'
        t.calls.clear()
        d.finalize("HalloWelt.")
        # Exakt der Indikator (3 Punkte) wird zurueckgenommen, NICHT mehr:
        assert t.calls == [("backspace", "3")], t.calls
        # 'Welt.' als GANZES gepastet - der echte Punkt bleibt erhalten:
        assert pastes == ["Welt."], pastes
        assert d.last_text == "HalloWelt.", d.last_text

    def test_indicator_before_real_sentence_with_dots(self) -> None:
        """(10) Exakte Kante aus dem Bugfix-Auftrag: letzter getippter Text
        '... ', final '...und so weiter' - der gemeinsame Praefix '...'
        deckt die Punkte ab, NUR der Unterschied (das Leerzeichen) wird
        per Backspace entfernt und 'und so weiter' gepastet. KEIN echter
        Punkt wird gefressen: die Diff ist praefixbasiert, zurueckgenommen
        wird ausschliesslich old_tail[common:].
        """
        from stt_button.typing import PAUSE_INDICATOR

        t = FakeTyper()
        pastes: list[str] = []

        class Recorder:
            def paste(self, text: str) -> None:
                pastes.append(text)

        d = DeltaTyper(t, append_space=False, final_paster=Recorder())
        d.update("", committed_len=0)
        d.show_paused()  # tippt '...' -> Stand '...'
        # Letzter committeter/live getippter Text war '... ' (Indikator
        # plus Leerzeichen): Stand per normalem update()-Pfaf:
        d.update("... ", committed_len=0)
        assert d.last_text == "... ", d.last_text
        t.calls.clear()
        d.finalize("...und so weiter")
        # Common prefix von '... ' vs. '...und so weiter' ist '...' (3):
        # to_remove == 1 (nur das Leerzeichen), Rest als Paste.
        assert t.calls == [("backspace", "1")], t.calls
        # Kein echter Text gefressen: die Punkte bleiben, Suffix ab 'und'.
        assert pastes == ["und so weiter"], pastes
        assert d.last_text == "...und so weiter", d.last_text

    def test_final_dots_equal_indicator_is_noop(self) -> None:
        """(11b) Stoerste Kollisionskette ueberhaupt: finaler Text endet
        SELBST auf '...' - deckungsgleich mit dem Indikator. Die Diff ist
        ein No-Op (common prefix == ganze Laenge): kein Backspace, kein
        Paste, keine echten Punkte angefasst.
        """
        from stt_button.typing import PAUSE_INDICATOR

        t = FakeTyper()
        d = DeltaTyper(t, append_space=False)
        d.update("Hallo Welt", committed_len=0)
        d.show_paused()  # Stand 'Hallo Welt...'
        t.calls.clear()
        d.finalize("Hallo Welt...")  # echter Text endet zufaellig auf '...'
        assert t.calls == [], t.calls
        assert d.last_text == "Hallo Welt...", d.last_text

    def test_final_text_ending_with_dots_after_indicator(self) -> None:
        """(11) Kollisionskante: finaler Text ENDET selbst auf '...' -
        der gemeinsame Praefix verlaengert sich dann UEBER den Indikator
        hinaus (bis min(len(old), len(new))), to_remove==0, nichts wird
        zurueckgenommen und der Resttext unveraendert gepastet.
        """
        from stt_button.typing import PAUSE_INDICATOR

        t = FakeTyper()
        pastes: list[str] = []

        class Recorder:
            def paste(self, text: str) -> None:
                pastes.append(text)

        d = DeltaTyper(t, append_space=False, final_paster=Recorder())
        d.update("Hallo Welt", committed_len=0)
        d.show_paused()  # Stand 'Hallo Welt...'
        t.calls.clear()
        d.finalize("Tschuess...")
        # Common prefix: 'Hallo Welt...' vs. 'Tschuess...' -> '' ->
        # Backspace(13) nimmt ALLES zurueck (volle finale Korrektur,
        # force_revision-Semantik in finalize), dann alles neu pasten.
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
    # CapsLock-LED "aus" = keine Diagnose-Warnung, kein Toggle.
    monkeypatch.setattr("stt_button.typing._capslock_state", lambda: "aus")
    return calls


def _capture_stdin(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Zeichnet den stdin-Payload statt des Returncodes auf."""
    stdin: list[str] = []

    def fake_run(cmd, *args, **kwargs):
        stdin.append(kwargs.get("input", ""))
        return subprocess_result()

    monkeypatch.setattr("stt_button.typing.subprocess.run", fake_run)
    # CapsLock-LED "aus" = keine Diagnose-Warnung, kein Toggle.
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
        """dotool bekommt DOTOOL_XKB_LAYOUT=de und VARIANT=nodeadkeys in der Env."""
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

    def test_capslock_leds_per_device(self, monkeypatch) -> None:
        """_capslock_leds() liefert pro Device-Name den Wert 'an'/'aus'/'?'."""
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
            paths[2]: "\n",  # unlesbar/unbekannt -> "?"
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
        """Der Diagnose-Log enthaelt den Hinweis 'LED ist kein verlaesslicher State'."""
        import logging as _l
        monkeypatch.setattr("stt_button.typing._capslock_state", lambda: "aus")
        monkeypatch.setattr(
            "stt_button.typing.subprocess.run",
            lambda *a, **k: subprocess_result(),
        )
        with caplog.at_level(_l.WARNING, logger="stt_button.typing"):
            DotoolTyper().type_text("Test")
        messages = [r.message for r in caplog.records]
        assert any("kein verlaesslicher State" in m or "LED != State" in m for m in messages), messages

    def test_send_logs_capslock_when_active(self, monkeypatch, caplog) -> None:
        """Wenn CapsLock aktiv ist (LED aus, invertiert), wird eine Warnung geloggt."""
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
    """Standard: CapsLock wird NIE getoggelt, nur gewarnt.

    Ground Truth (empirisch belegt auf diesem System): LED **aus** =
    CapsLock aktiv = invertierter Text; LED **an** = normale Schreibung.
    Die LED-Semantik ist also INVERTIERT zur ueblichen Konvention.
    """

    def _capture_payloads(self, monkeypatch) -> list[str]:
        payloads: list[str] = []
        monkeypatch.setattr(
            "stt_button.typing.subprocess.run",
            lambda cmd, **kw: payloads.append(kw.get("input", "")) or subprocess_result(),
        )
        return payloads

    def _led_sequence(self, monkeypatch, states: list[str]) -> None:
        """Liefert die Zustaende der Reihe nach; danach bleibt der letzte."""
        seq = iter(states)
        last = states[-1]
        monkeypatch.setattr(
            "stt_button.typing._capslock_state", lambda: next(seq, last)
        )

    def test_default_never_toggles_even_when_led_off(self, monkeypatch) -> None:
        """LED aus (= invertiert, CapsLock aktiv) -> im Default KEIN Toggle."""
        monkeypatch.setattr("stt_button.typing._capslock_state", lambda: "aus")
        payloads = self._capture_payloads(monkeypatch)
        DotoolTyper().type_text("Das")
        assert not any("key capslock" in p for p in payloads), payloads
        assert any("type Das" in p for p in payloads), payloads

    def test_default_warns_when_led_off(self, monkeypatch, caplog) -> None:
        """Bei aktivem CapsLock (LED aus, invertiert) wird gewarnt."""
        import logging as _l
        monkeypatch.setattr("stt_button.typing._capslock_state", lambda: "aus")
        self._capture_payloads(monkeypatch)
        with caplog.at_level(_l.WARNING, logger="stt_button.typing"):
            DotoolTyper().type_text("Das")
        messages = [r.message for r in caplog.records]
        assert any("CapsLock" in m for m in messages), messages

    def test_autocorrect_inverted_toggles_when_led_off(self, monkeypatch) -> None:
        """inverted=True: LED aus ist CapsLock aktiv -> Ziel 'an' -> toggle."""
        self._led_sequence(monkeypatch, ["aus", "an"])
        payloads = self._capture_payloads(monkeypatch)
        DotoolTyper(capslock_autocorrect=True, capslock_led_inverted=True).type_text("Das")
        assert any("key capslock" in p for p in payloads), payloads

    def test_autocorrect_conventional_toggles_when_led_on(self, monkeypatch) -> None:
        """inverted=False: LED an ist CapsLock aktiv -> Ziel 'aus' -> toggle."""
        self._led_sequence(monkeypatch, ["an", "aus"])
        payloads = self._capture_payloads(monkeypatch)
        DotoolTyper(capslock_autocorrect=True, capslock_led_inverted=False).type_text("Das")
        assert any("key capslock" in p for p in payloads), payloads

    def test_autocorrect_inverted_led_on_sends_no_toggle(self, monkeypatch) -> None:
        """inverted=True, LED an = bereits normal -> kein Toggle."""
        monkeypatch.setattr("stt_button.typing._capslock_state", lambda: "an")
        payloads = self._capture_payloads(monkeypatch)
        DotoolTyper(capslock_autocorrect=True, capslock_led_inverted=True).type_text("Das")
        assert not any("key capslock" in p for p in payloads)
        assert any("type Das" in p for p in payloads)

    def test_autocorrect_gives_up_after_two_attempts(self, monkeypatch) -> None:
        """Toggeln ist nicht verifizierbar -> nach 2 Versuchen aufgeben."""
        self._led_sequence(monkeypatch, ["aus", "aus", "aus"])
        payloads = self._capture_payloads(monkeypatch)
        DotoolTyper(capslock_autocorrect=True, capslock_led_inverted=True).type_text("Das")
        assert sum("key capslock" in p for p in payloads) == 2, payloads
        # Der Text wird trotzdem getippt (Diktat darf nicht ausfallen).
        assert any("type Das" in p for p in payloads)

    def test_unknown_state_never_toggles(self, monkeypatch) -> None:
        monkeypatch.setattr("stt_button.typing._capslock_state", lambda: "?")
        payloads = self._capture_payloads(monkeypatch)
        DotoolTyper(capslock_autocorrect=True, capslock_led_inverted=True).type_text("Das")
        assert not any("key capslock" in p for p in payloads)



class TestSubprocessHaertung:
    """Haertung gegen den Daemon-Pipe-Hang (final_paste-Freeze).

    Root Cause: ``wl-copy`` forked einen residenten Daemon-Prozess und
    vererbt diestdout/stderr-Pipe-FDs an ENKEL-Prozesse. ``subprocess.run``
    mit ``capture_output=True`` (implizit ohne Timeout) wartet in
    ``communicate()`` auf EOF dieser Pipes - das kommt nie, also haengt
    der Finalizer-Thread fuer immer und der Dienst friert ein.

    Fix: ``_run()`` nutzt DEVNULL fuer alle drei FDs und ein Timeout;
    ``DotoolTyper._send_raw`` behaelt capture_output (dotool forked
    nicht), bekommt aber ein payload-abhaengiges Timeout.
    """

    def _make_paster_with_blocking_copy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[ClipboardPaster, FakeTyper]:
        """Erzeugt einen Paster, dessen wl-copy wie ein Daemon haengt.

        Der Fake simuliert ``wl-copy`` VOR der Haertung: ein subprocess.run
        OHNE ``timeout``-Kwarg blockiert fuer immer (Event wird nie gesetzt).
        Ein Aufruf MIT ``timeout`` blockiert nur kurz und wirft
        ``TimeoutExpired`` - dann greift der Timeout-Fallback.
        """
        entered = threading.Event()
        never = threading.Event()  # wird nie gesetzt -> unendliches Blocken

        def fake_run(cmd, *args, **kwargs):
            entered.set()
            if "timeout" in kwargs and kwargs["timeout"] is not None:
                # Haertung aktiv: kurz warten, dann wie subprocess werfen.
                raise subprocess.TimeoutExpired(cmd, timeout=kwargs["timeout"])
            # Vor der Haertung: fuer immer blocken (kein Timeout-Kwarg).
            never.wait(10.0)
            raise AssertionError("unreachable")  # nur zur Sicherheit

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
        """Freeze-Repro: haengendes wl-copy darf paste() nicht blockieren.

        Reproduziert den final_paste-Freeze: Ohne Timeout in ``_run()``
        wuerde der Daemon-Pipe-Hang paste() fuer immer blockieren. Nach der
        Haertung wirft das Timeout einen TyperError und der Fallback tippt
        das Delta zeichenweise via type_text.
        """
        p, t = self._make_paster_with_blocking_copy(monkeypatch)

        worker = threading.Thread(target=p.paste, args=("Delta",))
        worker.start()
        worker.join(timeout=0.15)
        # Kern-Assertion: paste() muss zurueckkehren (kein Daemon-Hang).
        assert not worker.is_alive(), (
            "paste() haengt am blockierenden wl-copy - "
            "_run() braucht DEVNULL+Timeout"
        )
        # Fallback: das Delta wurde zeichenweise getippt.
        assert t.calls == [("type", "Delta")]

    def test_run_uses_devnull_and_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_run() nutzt DEVNULL-FDs und ein Timeout, kein capture_output.

        DEVNULL ist der eigentliche Fix gegen den Daemon-Pipe-Hang: ohne
        geoeffnete Pipe-FDs kann communicate() nie auf ein nie kommendes
        EOF warten. Das Timeout ist die zweite Verteidigungslinie.
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
        """subprocess.TimeoutExpired wird in TyperError uebersetzt."""

        def fake_run(cmd, *args, **kwargs):
            raise subprocess.TimeoutExpired(cmd, timeout=kwargs.get("timeout"))

        monkeypatch.setattr("stt_button.typing.subprocess.run", fake_run)
        from stt_button.typing import _run

        with pytest.raises(TyperError, match="Timeout"):
            _run(["wtype", "-k", "space"])

    def test_send_raw_timeout_scales_with_payload(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_send_raw: Timeout wächst mit der Payload-Groesse (type-Zeilen).

        Budget: max(5.0, chars*(typedelay+typehold)/1000 + 5.0) - damit
        lange Diktate nicht am Fixed-Timeout scheitern.
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

        # kurzer Payload: kleinines Budget (5 s + 0 + 5 s Puffer = 10 s)
        DotoolTyper(typedelay_ms=20, typehold_ms=15).type_text("Hi")
        short = timeouts[-1]
        assert short is not None and short >= 5.0

        # langer Payload (500 Zeichen): Budget muss deutlich groesser sein.
        DotoolTyper(typedelay_ms=20, typehold_ms=15).type_text("x" * 500)
        long = timeouts[-1]
        assert long is not None
        assert long > short
        # Erwartung: 500*(20+15)/1000 + 5.0 = 22.5 s
        assert long == pytest.approx(22.5, rel=0.01)

    def test_send_raw_timeout_expired_raises_typer_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Auch dotool haengt ggf. -> TimeoutExpired wird zu TyperError."""

        def fake_run(cmd, *args, **kwargs):
            raise subprocess.TimeoutExpired(cmd, timeout=kwargs.get("timeout"))

        monkeypatch.setattr("stt_button.typing.subprocess.run", fake_run)
        monkeypatch.setattr("stt_button.typing._capslock_state", lambda: "aus")
        monkeypatch.setattr(
            "stt_button.typing.shutil.which", lambda b: "/usr/bin/dotool"
        )
        with pytest.raises(TyperError, match="Timeout"):
            DotoolTyper().type_text("Hallo")


class TestPasteDebugLogs:
    """Debug-Logs im Paste-Pfad (Root-Cause-Diagnose final_paste-Freeze).

    Die Logs duerfen den Diktatinhalt NICHT enthalten (Dataminimierung im
    Log), aber Laenge und Phasen (copy start/done, chord) fuer die
    Nachvollziehbarkeit eines Freezes.
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
        # Phasen sichtbar:
        assert "copy start" in joined
        assert "copy done" in joined
        assert "chord" in joined
        # Laenge wird geloggt:
        assert "13 Zeichen" in joined
        # Diktatinhalt darf NICHT im Log landen:
        assert "Streng geheim" not in joined


