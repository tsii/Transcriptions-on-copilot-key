# EXECUTION PLAN v2 — Doppelklick-Zusammenfassung + Undo (Planer, 2026-11-05 21:4x)

Base: HEAD `c3cc170` (master), Arbeitsstand `M AGENTS.md` + untracked `.pi/*`,
`tools/model_fallback_probe.py`. Auftrag: `.pi/briefs/planner_task_summary_20261105.md`.
Vorgänger: `.pi/plans/plan_summary_mode.md` (Orchestrator-Entwurf) — **von mir geprüft und in den
Kernstücken D1/D2 ersetzt** (Gründe in §2 und §3, Abweichungsprotokoll in §9).
Stilmaßstab: `.pi/plans/plan_qwen_xpu.md` (VERDICT zuerst, Ground Truth mit Datei:Zeile,
TDD-Tabelle mit Rollback, Blockliste, Operator-TODO).

Konservativregel durchgehend: **was nicht belegt ist, wird nicht gelöscht.** Produktionspfad
(`backend`, `streaming_mode`, `finalize_mode`, `append_space`, `final_paste*`) bleibt bit-identisch;
die gesamte Funktion hängt an `summary_enabled=false` (Default aus).

## VERDICT: **GO — opt-in, zwei neue Module, drei additive Haken am Bestand. Größtes Risiko ist die Textgewinnung/-verortung, deshalb: Runde 1 mutiert nur, wenn der Cursor nachweislich am Ende des eigenen Blocks steht (`caret_at_block_end`); jede andere Lage bricht ab, ohne etwas zu löschen.**

## 1. GROUND TRUTH (alle Zeilen vom Planer am 2026-11-05 selbst nachgelesen)

| # | Fakt | Beleg |
|---|---|---|
| G1 | keyd bildet die Copilot-Taste auf F24 (Code 194) ab, Leser sieht nur das virtuelle keyd-Gerät | `src/stt_button/keys.py:9-17`, `:51` |
| G2 | Kanten kommen aus `KeyListener._handle`; Auto-Repeat (`value==2`) wird ignoriert, **bewusst kein Debounce** (Push-to-talk-Latenz) | `keys.py:273-307` (Kommentar `:283`) |
| G3 | Verkabelung: `app._on_press → controller.start_recording()`, `app._on_release → stop_recording()`; Listener im eigenen Daemon-Thread | `app.py:229-246`, `:254-260` |
| G4 | `start_recording()` startet auch im Zustand FINALIZING sofort neu; Queue voll → "press ignored" | `controller.py:149-166` |
| G5 | **Korrektur zum Brief:** `min_audio_s` (`asr.py:808`) gate **nur den Live-Tick**. `finalize()` transkribiert den Puffer **bedingungslos** — ein 0,3-s-Phantomlauf ruft also das ASR auf und landet beim Typer | `asr.py:806-809` (live), `asr.py:903` (`_transcribe()` ohne Gate), `controller.py:648-650` |
| G6 | `DeltaTyper.finalize()` hat eine Leer-Guard: `if not final_text.strip(): return` → ein Phantomlauf mit **leerem** ASR-Ergebnis schreibt nichts (auch kein Leerzeichen). Der reale Fremd-Leerzeichen-Fall ist ein **nicht leerer Finaltext mit leerem Delta** (bereits getippter Text wird erneut finalisiert): dann bleibt `space_in_paste` `False` und `key("space")` läuft. Live-Konfig: `append_space = true`, `min_audio_s = 1.0`. *(Zitatpruefungs-Korrektur des Orchestrators 22:0x, vom Planer nachgelesen und übernommen.)* | `typing.py:897-898` (Guard), `:1039-1042` (Space), `~/.config/stt-button/config.toml:29,34` |
| G7 | Es gibt bereits ein API für "Aufnahme verwerfen **ohne** jede Ausgabe": `Controller.abort()` stoppt Quelle, verwirft Transcriber, **kein** Finalize-Job, **kein** Tippen, **kein** Ton. Testabdeckung vorhanden | `controller.py:269-282`; `tests/test_controller.py:419,1574,1597,1611,1728,1953` |
| G8 | `DeltaTyper.last_text` (Property) liefert den zuletzt ausgegebenen Text; `reset()` leert ihn bei neuer Aufnahme (`_typing_epoch`-Wechsel) → Sitzungs-Puffer muss **im Finalisierer abgegriffen** werden, nicht lazy beim Geste | `typing.py:753-767`, `controller.py:612-614` |
| G9 | Muster für optionale Rückmeldung im Typer existiert: `on_focus_unknown`-Callback,try/except gekapselt | `typing.py:747-751`, `app.py:35-48,150` |
| G10 | Ton-Zweig: paplay, `Popen` fire-and-forget, `DEVNULL`, wirft nie; **absolute Pfade werden akzeptiert** (`name if isabs(name)`), `SOUND_DIR` ist nur Fallback fürs Namens-Schema | `controller.py:537-581`, `:35`, `:561-563` |
| G11 | Ton-Schlüssel vorhanden: `done_sound` ("" = aus), `done_sound_volume` (pp von 65536), `done_sound_mode` (`terminal`/`always`) | `config.py:193,200,205`, Validierung `:224-228` |
| G12 | Tippen: `DotoolTyper.key("ctrl+v")` zerlegt zu einem dotool-Chord (`key ctrl+v`); `backspace_and_type(n, text, trailing_key)` bündelt Backspaces + Text + Schluss-Taste in **einen** dotool-Prozess, weil dotool Leerzeichen in `type`-Zeilen verwirft | `typing.py:327-337`, `:300-325` |
| G13 | `ClipboardPaster`: `copy()` ohne Chord, `paste()` (wl-copy + `ctrl+v`), `paste_combined(text, backspaces=n)` = **Kopieren + (Backspaces & Chord) in einem dotool-Prozess**, Rückgabe `False` = nichts getan, Fallback möglich | `typing.py:505-583` |
| G14 | `focus.py` kann nur klassifizieren (`terminal`/`other`/`no_window`/`unreachable`) und `terminal_focused()`; **keine** Feldinhalte. Fenster-Identität (`app_id`, `title`) steckt nur im privaten `_call_focus()` | `focus.py:47`, `:79-99`, `:102-110` |
| G15 | **Nachgemessen (Planer, lesend, ohne Emission):** `wl-paste -p -t text/plain` funktioniert in dieser User-Umgebung ohne gesetzte `WAYLAND_DISPLAY` (Socket über `XDG_RUNTIME_DIR=/run/user/1000`), rc=0, Inhalt kam → Auslesen per Roundtrip ist machbar | Messung 21:4x, Primärauswahl gelesen, nichts geschrieben |
| G16 | `wl-paste` wird im Bestand **nirgends** verwendet (nur `wl-copy` als `paste_binary`-Default) → Lesen ist neuer Code mit eigenem Timeout | grep `src/` 21:4x, `config.py:149` |
| G17 | Konfig: frozen dataclass + `validate()`; unbekannte Schlüssel → `ConfigError` (`config_from_mapping`) → Schlüssel und Doku müssen **im selben Commit** landen | `config.py:214-230`, `:382-390` |
| G18 | CLI-Muster: `sub.add_parser(...)` mit `set_defaults(func=...)`, u. a. `bootstrap` | `__main__.py:393-444`, `:404-409` |
| G19 | Doku-Test-Muster existiert (liest Markdown ein und prüft Schlüsselwörter) | `tests/test_docs.py:1-30` |
| G20 | Name `session.py` ist belegt (`StreamingAsrSession`) → neuer Modulname muss abweichen; `sounds/` im Paket existiert **noch nicht** | `src/stt_button/session.py:1-4`, `ls src/stt_button/sounds` → fehlt |
| G21 | Gateway gemessen (Brief 20:04/20:09, vom Planer nicht wiederholt): `GET /v1/models` 200 / 56 Einträge; `my-qwen38-flash-next` 200 in 1,7–1,9 s; `qwen38-flash-ext` **400 "Invalid model name"** | Brief §4; `tools/model_fallback_probe.py` |
| G22 | Suite: 707 Tests, Voll-Lauf ≈ 150 s, zwei reine Reihenfolgen-Flakens (`test_ticks_stop_after_limit_final_text_complete`, `test_async_writes_wav_from_snapshot`) | Brief §4 (19:30-Lauf) |
| G23 | Nutzer ist in Gruppe `input` (evdev-Mitlesen prinzipiell möglich, aber nicht dieser Plan) | Brief §4 |
| G24 | Offener Fehler T1 (fehlendes Leerzeichen im Paste-Pfad) ist **derselbe Mechanismus** wie unsere Ersetzung: Leerzeichen im Paste-Payload kann das Zielfeld trimmen | `.pi/plans/TODO_next_iteration.md`, `typing.py:1013-1027`, `:1039` |
| G25 | **Schwellwerte aus echtem Nutzungsjournal (Planer, 2026-11-05, nur lesend):** 174 Kanten / 87 echte Haltedauern, **Minimum 0,81 s** (Median ≈ 3,5 s, Maximum 28 s); **kein** Release→Press-Abstand unter 2 s, d. h. doppeltes Kurzhub-Mustern kommt heute nie vor. Ein Druckfenster von 0,35 s hat 2,3× Abstand zur kürzesten echten Aufnahme und kann kein reales Diktat fressen | `journalctl --user -u stt-button --since 2026-11-05`, Auswertung 21:5x (n=87) |
| G26 | **Flake FL1, A/B-gemessen (Orchestrator 02:15, je 8 Einzel-Läufe, gleicher Lastpfad):** `test_ticks_stop_after_limit_final_text_complete` fails 2/8 auf HEAD mit T1 (`18944bf`) und 4/8 auf Pre-T1 (`c3cc170`, nur `typing.py` zurückgesetzt, Baum danach sauber) ⇒ **Defekt ist älter als T1 und durch T1 unverändert** (T1-Annahme erfüllt). Signatur: `typed` enthält ein literales `...` (`PAUSE_INDICATOR` via `show_paused`) und `('backspace','3')`, Assertion `typed.endswith("w0 … w11")`. Ursachenklasse: Tick-Anzahl hängt an **Realzeit** (`asr_interval_s=0.1`, `live_asr_until_s` nachträglich auf 1,5 gesetzt, 6× `run_once()`), der Test prüft eine Kontinuität, die nur bei bestimmter Tick-Zahl entsteht | `tests/test_controller.py:787-832` (Realzeit-Pfad `:810,:814,:816-828`), A/B-Messung 02:15 |

| G27 | **FL4 mit Zeithebel deterministisch bewiesen (Orchestrator, Selbstmessung):** `tests/test_controller.py:336-343` (`test_text_reaches_typer`) setzt voraus, dass zwei 1,0-s-Chunks in drei sofortigen `run_once()`-Aufrufen die Queue erreichen; der Capture-Thread notified unter `_lock` (`controller.py:461-462`) und verhungert gegen den Burst ⇒ Puffer bleibt bei 0,5 s, `asr.py:808` gibt None, `engine.calls = 0`. **Produkt korrekt** (bei 0,5 s darf nichts getippt werden) — die Timing-Annahme sitzt im Test. Hebel: `source.read` mit `sleep(0.05)` gebremst ⇒ **12/12 Fehler, `engine.calls` durchgehend 0**; ungebremst **0/12, `engine.calls` 1** | Befund `tests/test_controller.py:336-343`, `controller.py:461-462`, `asr.py:808`; A/B 12+12 Läufe |

## 2. UMFANG „diktiert **oder getippt**“ — Entscheidung M3a (Brief §3/§5.3, hier entschieden)

**Entscheidung: Anker-Block-Regel, Runde 1 ohne evdev.** Der Sitzungs-Block ist die *kleinste
Spanne, die alle unsere Diktat-Vorkommen in Reihenfolge enthält*. Manuell getippter Text **zwischen
und unmittelbar neben** unseren Diktaten liegt innerhalb dieser Spanne und wird deshalb **mit
zusammengefasst** — er ist durch unsereanker belegt, nicht geraten. Was außerhalb liegt (nur
getippte Passagen vor dem ersten Diktat, fremde Textblöcke im selben Feld), bleibt **unangetastet**;
das ist die ehrliche Restlücke (→ Frage **Q1** an den Betreiber, evdev im Backlog §8).

Warum nicht die Alternativen:
* *Nur eigener Puffer (Entwurf D2/D1 des Vorgängers):* unterschreitet den Auftrag unnötig, obwohl die
  Read-Back-Runde den getippten Text tatsächlich liefert (G15) — verschenkter Umfang.
* *Feldanfang→Cursor komplett zusammenfassen:* würde fremden Bestand (E-Mail-Verlauf, Code) mit
  Löschen umfassen — verstößt gegen die Konservativregel.
* *Passives evdev-Mitlesen:* eigener Ground-Truth-Lauf nötig (Geräteidentifikation neben keyd,
  Layout, Wiederanlauf), und es zählt nur Tasten, nicht das Ergebnis in Zielfeldern mit Autokorrektur.
  Zu groß für Runde 1.
* Schutz gegen „fremdes Riesenblock zwischen den Ankern“: `summary_max_gap_chars` (Default 2000).
  Größerer Innenbestand ⇒ Abbruch mit Grund `gap_too_large`, nichts wird gelöscht.

**Vorbedingung jeder Geste (gilt für D3 und D4, in B4 getestet):** `focus.focus_state()` (G14) muss
`"other"` liefern. Bei `"terminal"` **oder** `"unreachable"`/`"no_window"` bricht die Geste sofort ab
(Ton D7, Grund `focus_not_editable`) — in einem Terminal ist `ctrl+shift+Home` eine Terminal-Tastatur
(scroll to top / Tab-Aktion) und darf von uns nie emittiert werden. Bewusst strenger als der
Ton-Zweig, der bei Unknown trotzdem spielt (`controller.py:551-555`): hier wird eingegriffen, nicht
nur signalisiert.

## 3. PINNED DECISIONS

### D1 Doppelklick — **Erkennung auf der zweiten Loslass-Kante; Phantom-Aufnahmen werden mit dem bestehenden `abort()` verworfen**
* Neues Modul `src/stt_button/gestures.py`: `DoublePressDetector(press, release, on_gesture, ...)`
  sitzt **zwischen** Listener und Controller (`app.py:254-260` wird umgebaut, Controller bleibt reines Aufnahme-FSM).
* Kanten werden **nie unterdrückt**: `press` reicht immer an `start_recording()` weiter (Verlust-Regel
  wie im Vorgänger — bei Unterdrückung wäre ein echtes Diktat weg).
* **Abweichung vom Entwurf (neue Belege G5/G6/G7):** der Entwurf verließ sich auf `min_audio_s` als
  „Phantom-Verwerfer“ und wollte das in B1 erst messen. Das ist widerlegt: `finalize()` transkribiert
  bedingungslos (`asr.py:903`), ein Kurzhub läuft also wirklich durchs Modell. Was dabei ins Feld
  kommt, ist **nicht** deterministisch: leeres Ergebnis ⇒ dank Guard nichts (`typing.py:897`);
  nicht-leeres Ergebnis mit leerem Delta ⇒ ein echtes Fremd-Leerzeichen (`typing.py:1039`, G6);
  nicht-leeres Ergebnis mit Delta ⇒ Junk-Text aus 0,3 s Stille. Dazu zwei Finalisierer-Jobs, die
  neben dem echten Diktat in der Schlange stehen. Deshalb: **Release-Regel** — ist die Haltedauer
  `< summary_press_max_s`, ruft der Router
  `controller.abort()` statt `stop_recording()` (bestehende, getestete API G7: kein Job, kein Tippen,
  kein Ton) und meldet die Kante dem Detector.
* Gesture-Kriterium (alle drei): `hold_1 < press_max_s`, `hold_2 < press_max_s`,
  `press_2 - release_1 < gap_max_s`. Defaults `press_max_s=0.35`, `gap_max_s=0.35`.
  Der Abstand *Diktat-Release → erster Klick* ist bewusst **nicht** begrenzt (der Nutzer atmet erst durch).
* Wiederholung = Undo ist **kein Timing-Trick**: Zustand des `SummaryCoordinator`
  (`ACTIVE` mit `UndoRecord` → Geste = undo, sonst → zusammenfassen).
* Unangetastet: `max_record_s`-Auto-Finish, Phantom-Release-Schutz (`controller.py:238-256`),
  `live_typing_mode`, Auto-Repeat-Verhalten, Debounce-Freiheit (G2).
* Nebenwirkung (bewusst, nur bei `summary_enabled=true`): ein einzelner versehentlicher Kurzhub
  (< 0,35 s) erzeugt gar keine Ausgabe mehr statt — je nach ASR-Ergebnis — eines Fremd-Leerzeichens
  oder eines Junk-Texts (G6). Bei
  `summary_enabled=false` bleibt der Pfad bit-identisch. Begründung: unter `min_audio_s=1.0`
  (Live-Konfig) ist ein solcher Hub heute schon kein Diktat, sondern Müll.

### D2 Sitzungs-Puffer — **im Finalisierer abgegriffen, über einen additiven Output-Haken**
* `src/stt_button/summary.py` (Name frei, G20) mit `SummaryBuffer`: Liste von `Chunk {text, epoch, ts}`.
* Quelle: **ein neuer optionaler Callback** `PushToTalkController(..., on_output=None)`, aufgerufen
  nach erfolgreichem `self._delta_typer.finalize(...)` an der bestehenden Stelle
  (`controller.py:650`, direkt vor `_play_done_sound()`), Signatur `on_output(text: str, epoch: int)`.
  Default `None` ⇒ Verhalten unverändert; Kapselung try/except nach Muster `typing.py:747-751`.
  Warum hier und nicht `delta.last_text` beim Geste lesen: `reset()` löscht den Text bei der
  nächsten Aufnahme (G8) — beim Doppelklick wäre er schon weg.
* Append pro Diktat; **Reset** bei: Fensterwechsel (`focus.window_identity()`, additiv in D8),
  Idle > `summary_session_idle_s` (Default 600 s), erfolgreichem Zusammenfassen, Undo, Process-Neustart.
* Log: nur Zeichenanzahl + `sha1[:8]`, nie der Text (D5 Datenschutz, in B6 als Test).

### D3 Verortung — **Anker-Block; mutiert wird nur, wenn der Lese-Beleg „Block endet am Cursor“ liefert**
Grundlage: S0 = Puffer aus D2 (liefert die Anker), Beleg = ein Roundtrip:
`key ctrl+shift+Home` (Auswahl Cursor → Feldanfang) → **`wl-paste -p -t text/plain`** mit Timeout
(Planer-Entscheid 10:4x, Befund aus B9b `ac091cf`): eine Tastatur-Auswahl füllt die **Primär-**
Auswahl, nicht die Zwischenablage; ein Lesen ohne `-p` würde fremden oder stale Ablagen-Inhalt
liefern und damit auf **nicht-Feldtext** entschieden. **Kein Clipboard-Fallback, kein zusätzliches
`ctrl+c`** — ein Ablagen-Fallback könnte einen Text liefern, der unsere Anker enthält, ohne
Feldinhalt zu sein (Löschrisiko aus falscher Quelle); `-p` ist in dieser Umgebung gemessen (G15).
Gelesen wird damit der Feldtext **von Feldanfang bis Cursor**. Danach `key right`, damit die Auswahl kollabiert
und kein Markierungszustand im Feld bleibt.

| Fall im gelesenen Text | Aktion |
|---|---|
| alle Anker in Reihenfolge eindeutig, Innenabstand ≤ `summary_max_gap_chars`, **gelesener Text endet mit dem letzten Anker** | `caret_at_block_end` ✓ → Block = `[start(first), len(read))`, Mutation erlaubt |
| Anker eindeutig, aber der gelesene Text endet **nicht** mit dem letzten Anker (Nutzer hat danach weitergeschrieben, Cursor umgeparkt) | Abbruch `caret_not_at_end` — **nichts löschen** |
| ein Anker mehrfach im Gelesenen | Abbruch `ambiguous` |
| ein Anker fehlt (im Inneren umgebaut, extern gelöscht) | Abbruch `no_match` |
| Innenbestand > `summary_max_gap_chars` | Abbruch `gap_too_large` |
| Leselänge > `summary_max_read_chars` (Default 20000) oder Roundtrip leer/verweigert | Abbruch `too_long` / `empty_read` — **leere Roundtrip-Lesung ist immer `empty_read` (mit Ton), niemals `nothing_to_do`** (Planer 10:4x; `nothing_to_do` nur, wenn Puffer **und** Primärauswahl leer sind, D4) |

* **Warum diese Einschränkung (Kern-Korrektur am Entwurf):** der Entwurf wollte „genau dieses Span
  ersetzen“ (Vorspann + Ersatz + Nachspann). Es gibt **kein Tastatur-Primitiv**, den Cursor im Feld
  an eine Spanngrenze zu setzen — `ctrl+shift+Home` wählt bis Feldanfang, alles Weitere wäre Raten
  oder ein Refix von Feldanfang bis Cursor (Kosten: ganzes Präfix tippen; Risiko: ganzer Bestand).
  §5.4 des Briefs (Feldtyp-Unterschiede) trifft genau hier. Also: **nur Cursor-am-Block-Ende**,
  sonst Abbruch mit Ton. Das ist der einzige Fall, der mit `backspace_and_type` / `paste_combined`
  belegbar sicher ist (G12, G13), und es ist genau der Fall des Betriebstexts „nach dem Diktat“.
* **Mutation** (falls erlaubt), Länge `n = len(block_text)`:
  * `n < summary_paste_min_chars` (Default 40): `backspace_and_type(n, replacement, trailing_key="space")`
    — ein dotool-Prozess, echtes Schluss-Leerzeichen als Taste, **kein** eingebettetes Leerzeichen (T1-Falle G24).
  * sonst: `ClipboardPaster.paste_combined(replacement, backspaces=n)`; bei Rückgabe `False`
    Fallback auf `backspace_and_type`. Beides ersetzt exakt `n` Zeichen am Cursor.
  * Schluss-Leerzeichen: im Tipp-Pfad über `trailing_key="space"` (echter Tastendruck, G12); im
    Paste-Pfad über ein eigenes `typer.key("space")` **nach** `paste_combined` (ein dotool-Prozess mehr),
    weil das Leerzeichen **nicht** in den Clipboard-Text darf — genau dort trimmen Felder (T1, G24).
    Undo stellt den Block ohne angehängtes Leerzeichen wieder her (der Originalblock endete auch nicht damit).
* **Status-Semantik `ambiguous` (Planer-Entscheid 03:5x, aus B5 `2423115` gemeldet):** geprüft wird
  `count > 1` **pro Anker über den Gesamtext** — konservativ, **bleibt so in Runde 1**. Der Preis ist
  akzeptiert: identische Kurzdiktate (derselbe kurze Satz zweimal im Feld) brechen mit `ambiguous` +
  Ton, obwohl die Kette eindeutig wäre. Das ist ein **False-Positive mit Schutzcharakter**: es wird
  nichts gelöscht, der Nutzer sieht Ton und unveränderten Text. Die genauere Ketten-Regel ist als
  **B5b** im Backlog (§8, Punkt vi) mit Sicherheitsnachweis-Pflicht — **kein Wechsel der
  Semantik unter einem bereits dispatchten Nachfolger**: B6 (`apply_summary`/`undo_summary`)
  baut auf der B5-Version von `locate_block`, und die Handprüfung des Falls gehört in den
  Handzettel (§6.7 g).
* Alles andere (Teilersatz, Mehrfachtreffer-Raten, Löschen ohne Beleg) ist verboten.

### D4 Markierungs-Modus — **Primärauswahl als Auftrag, Ersetzen durch Einfüge-Chord, beide Auswahlen zurückholen**
* Vor jeder eigenen Clipboard-Aktion: `wl-paste -t text/plain` (Clipboard) und `wl-paste -p -t text/plain`
  (Primär) **sichern**; nach der Aktion wieder zurücksetzen (`wl-copy` / `wl-copy -p`), auch im
  Fehlerfall (`try/finally`). Ohne Restore wäre ein sichtbarer Nutzereffekt da (G13: `paste_combined`
  überschreibt die Ablage).
* Liefert die Primärauswahl nicht-leeren Text, ist eine lebende Markierung wahrscheinlich → **ihr
  Inhalt ist der Auftrag**. Ersetzen: `ClipboardPaster.paste_combined(summary)` — ein Einfügen mit
  lebender Auswahl ersetzt genau diese Auswahl (Feld-Standard, kein Locate, kein Cursor-Problem).
* `wl-paste -p` sagt **nicht**, ob die Auswahl noch lebt (Kopie bleibt, wenn die App sie verwaltet).
  Deshalb gilt zusätzlich die gleiche Fokus-Vorbedingung wie in D3 (nur `"other"`), und die
  Undo-Stufe (D5) ist Pflicht. Kein zweiter Lese-Roundtrip im Markierungsfall — der wäre ein
  zweiter Eingriff in ein Feld, dessen Auswahl wir gerade benutzt haben.
* Liefert die Primärauswahl nichts → Weg aus D3 (eigener Block). Beides leer → Abbruch `nothing_to_do` (stumm, Journal).
* Trennzeichen-Regel: die Replacement im Markierungsfall erhält **kein** Schluss-Leerzeichen
  (Mid-Satz-Ersetzung); in D3 (Block am Cursor) erhält sie eines per echter Taste.

### D5 Undo — **eine Stufe, wieder mit Locate-Beleg, kein blindes Backspace**
* **Rückgabe-Vertrag (Planer-Entscheid 04:1x — Planlücke; beide B6-Tests dürfen deshalb umgeschrieben
  werden, Commit-Text nennt die Planlücke):** `apply_summary()` und `undo_summary()` geben **beides**
  zurück, in **einem** gefrorenen Datensatz — Variante (c):
  ```python
  @dataclass(frozen=True)
  class ApplyResult:
      ok: bool                # True genau dann, wenn das Feld verändert wurde
      status: str             # geschlossene Menge, siehe unten
      mode: str               # "typed" | "pasted" | "" bei ok=False
      removed_text: str       # exakt das, was gelöscht wurde ("" bei ok=False)
      inserted_text: str      # exakt das, was eingefügt wurde ("" bei ok=False)
      reason: str | None      # Abbruchgrund bei ok=False, sonst None
  ```
  * `status` bei `ok=True`: `"typed"` (`backspace_and_type`), `"pasted"` (`paste_combined`),
    `"pasted_fallback"` (`paste_combined` lieferte `False`, danach getippt — `mode` ist dann `"typed"`),
    `"undone"` (Erfolg in `undo_summary`).
  * `status` bei `ok=False`: `"aborted"` mit `reason` aus dem Grundwortschatz von D3/D5/D6
    (`caret_not_at_end`, `ambiguous`, `no_match`, `gap_too_large`, `too_long`, `empty_read`,
    `focus_not_editable`, `busy`, `nothing_to_do`, `undo_not_found`) oder `"failed"` mit
    `reason="typer_error"`/`"paster_error"`, wenn der Typer/Paster warf. **Nie** eine Ausnahme nach
    außen und **nie** `ok=True` nach einem Fehler.
  * **Datenschutz im Datensatz:** `ApplyResult` überschreibt `__repr__` auf
    `(ok, status, mode, reason, len(removed_text), len(inserted_text), sha1[:8])` — ein unbeabsichtigtes
    `log.info("%s", result)` darf keinen Klartext ins Journal bringen (Test in B6).
* **Mapping auf den Coordinator:** `UndoRecord { original_text = result.removed_text,
  replacement_text = result.inserted_text, mode = result.mode, ts = now }`; `summary_undo_ttl_s`
  Default 300 s. Die Undo-Richtung kehrt die Felder um (entfernt `inserted_text`, setzt
  `original_text` ein).
* Zweite Geste innerhalb TTL: **zuerst derselbe Lese-Roundtrip aus D3**; nur wenn der gelesene Text
  **mit `replacement_text` endet** (Cursor noch am Ende, Inhalt seitdem unverändert), wird ersetzt:
  `backspace_and_type(len(replacement_text), original_text, trailing_key="space")` bzw.
  `paste_combined(original_text, backspaces=len(replacement_text))`. Danach Puffer auf den
  Wiederhergestellten zurückschreiben (sonst würde die nächste Geste die Zusammenfassung als
  Diktat-Anker suchen). Das Ergebnis ist wiederum ein `ApplyResult`, mit `status="undone"`,
  `mode` = benutztes Ursprungsmuster (`"typed"`/`"pasted"`), `removed_text=replacement_text` und
  `inserted_text=original_text`.
* **Abweichung vom Entwurf:** der Entwurf stellte „exakt `original_text`“ her — ohne Beleg, dass das
  Feld noch unsere Zusammenfassung enthält. Hat der Nutzer die Zusammenfassung editiert oder den
  Cursor bewegt, wäre ein `backspace len(replacement_text)` Löschen von fremdem Text. Also: kein Treffer
  ⇒ Abbruch `undo_not_found` + Ton, **nichts löschen**. (konservativregel, deckt mit: Nutzer hat
  die Zusammenfassung von Hand weiterbearbeitet und will sie nicht verlieren.)
* Verfall des Undo: neue erfolgreiche Zusammenfassung, neues Diktat, Fensterwechsel, TTL-Ablauf,
  Neustart. Bewusst **kein** Undo-Stapel (Rückmeldbarkeit, kein Stapel-Debugging).

### D6 Gehostetes Modell — **OpenAI-kompatibel mit `requests`, Default `my-qwen38-flash-next`, Picker = CLI (kein GUI-Dialog)**
* Neue Schlüssel (alle in einem Commit mit `config.example.toml`, G17):
  `summary_enabled=false`, `summary_base_url="http://172.28.8.10:9000/v1"`, `summary_token=""`
  (Gateway braucht keins — G21), `summary_model="my-qwen38-flash-next"` (Betreiberwahl O1, 20:10),
  `summary_timeout_s=12`, `summary_max_chars=4000`, `summary_max_read_chars=20000`,
  `summary_max_gap_chars=2000`, `summary_paste_min_chars=40`, `summary_session_idle_s=600`,
  `summary_undo_ttl_s=300`, `summary_press_max_s=0.35`, `summary_gap_max_s=0.35`,
  `summary_wait_idle_s=20` (**16. Schlüssel; Planlücken-Nachtrag zu B1, eingeführt im B9a-
  Schlüssel-Commit**, `<= 0` → `ConfigError`),
  `summary_error_sound="you_lose"`, `summary_error_sound_volume=24000`.
* `SummaryClient.chat(text) -> str`: `POST {base}/chat/completions` mit `requests` (2.34.2 im `.venv`,
  **kein** `openai`-Paket nötig), System-Prompt Deutsch: „knapp, sachlich, sauber gegliedert; Stil der
  Vorlage erhalten (Du/Sie, lässig/formell); nichts hinzufügen, keine Zahlen erfinden; bei Zweifel
  Vorlage zurückgeben“, `temperature=0`. Eigenes `ModelUnavailable` für DNS/Verbindung, Timeout,
  4xx/5xx, leere Antwort, `answer == prompt` (Modell hat nicht gearbeitet).
* **Entscheidung zum Bestellnamen:** `my/qwen38-flash-ext` wird **nicht** als Picker-Eintrag
  angelegt (G21: HTTP 400 — ein toter Eintrag in einer Auswahl ist eine Falle). Er bleibt nur als
  dokumentierter Kommentar in `config.example.toml` mit Messergebnis. Provider-Präfix `my/`
  wird wie in `tools/model_fallback_probe.py` abgeschnitten, bevor die id ans Gateway geht.
  Hinweis zur Abgrenzung: `.pi/model-fallback.json` (AGENTS P8) betrifft nur Agent-Modelle, **nicht**
  `summary_model` (Brief §5.6).
* **Pull-Down:** `stt-button summary-models` listet live aus `GET {base}/models` (Timeout 3 s),
  markiert den aktuellen Wert, fällt bei Netzwerkfehler auf die gemessene Viererliste (G21) zurück;
  `summary-models --set <id>` schreibt die Konfig und liest sie mit `load_config()` zurück.
  **`summary_picker="zenity"` kommt nicht in Runde 1** (Entscheidung Brief §5.5): Der Dienst läuft
  headless als User-Service; ein `zenity`-Dialog aus dem Dienst heraus zieht Fokus auf sich, kann
  hinterm Terminal landen und ist exakt die Dialog-Emission in die lebende Session, die dieser Auftrag
  verbietet. Der Modellwechsel ist ein Seltener-Ereignis — CLI + `notify-send`-Zeile genügt.
  (Neuer Schlüssel `summary_picker` dadurch **nicht** nötig — weniger Fläche.)
* Ausführung in eigenem Worker-Thread (Schlange Länge 1, Überlauf ⇒ Auftragsverwurf + Ton);
  Key-Loop und Finalisierer werden nie blockiert.

### D7 Datenschutz und „you lose“-Ton
* `summary_enabled=false` als Default: erst mit `true` verlässt Text den Rechner. Journal nennt nur
  Zeichenanzahl und `sha1[:8]`, nie Text; `summary_token` erscheint in keiner Log-Zeile und keiner
  Exception (Test in B6).
* Asset `src/stt_button/sounds/you_lose.wav` (Ordner existiert noch nicht, G20): ≈ 0,45 s, zwei
  fallende Töne, leise, erzeugt von `tools/make_you_lose.py` (numpy, deterministisch → nachbaubar).
* Wiedergabe als **Modul-Funktion `play_error_sound()` in `src/stt_button/summary.py`** (Entscheid
  04:3x, Leseart (i); Signatur und Details in der B8-Zeile von §5), nach Muster `_play_done_sound`
  (G10): `shutil.which("paplay")`, `Popen` fire-and-forget,
  `DEVNULL`, fängt jede Ausnahme, `--volume` aus `summary_error_sound_volume`. Absoluter Pfad aus dem
  Paketverzeichnis (`os.path.isabs`-Ast ist vorgesehen, `controller.py:561`), **kein** `SOUND_DIR`,
  kein Systemverzeichnis (P5).
* **Auslösend:** alle `ModelUnavailable`-Fälle (D6), Abbrüche aus D3/D4/D5
  (`caret_not_at_end`, `ambiguous`, `no_match`, `gap_too_large`, `too_long`, `empty_read`,
  `focus_not_editable`, `undo_not_found`), Busy-Fall des Workers.
* **Stumm:** Funktion aus, `nothing_to_do` (Puffer **und** Primärauswahl leer — D4), Nutzerabbruch.
  Eine **leere Roundtrip-Lesung ist nicht stumm**: sie ist `empty_read` mit Ton (Planer 10:4x — ein
  Feld, das unsere Auswahl nicht als Primärverwaltung freigibt, ist ein echtes Problem, kein Leerlauf).
  Journal nennt den Grund immer. Der
  Enabled-Schalter liegt beim **Aufrufer** (`SummaryCoordinator`, B9) — `play_error_sound()` ist
  bewusst konfigurationsfrei (kein Parameter, kein globaler Zustand), damit B8 reine Mechanik prüft.

### D8 Verdrahtung, additive Bestand-Haken, Aufwand
* **Reihenfolge-Problem (vom Entwurf nicht gelöst):** die Geste kommt ≈ 0,5 s nach dem Diktat-Release,
  die Finalisierung läuft aber asynchron (CPU, bis mehrere Sekunden; `controller.py:518-535`). Eine
  Zusammenfassung, die währenddessen tippt/pastet, würde in eine laufende Diktat-Ausgabe pfuschen.
  `state` ist als Ruhe-Signal untauglich, weil `abort()` auf IDLE setzt, obwohl Jobs in der Schlange
  stehen (`controller.py:280`). Entscheidung: **eine additive Lesemethode**
  `Controller.has_pending_work() -> bool` (`under _lock`: `bool(self._finalize_queue) or
  self._finalizer_running or self.state is not IDLE`), vom Coordinator mit 0,1 s Polling und Timeout
  `summary_wait_idle_s=20` benutzt (Schlüssel wird in B9a eingeführt, siehe D6); Timeout ⇒ Abbruch `busy` + Ton. Kein Polling in der Key-Thread. Abschluss wird über die öffentliche Methode `wait_for_result(timeout=None) -> ApplyResult | None` beobachtet (B9a; B11 nutzt sie für synchrone Ausgabe).
* **Genau drei additive Haken am Bestand** (alle Default-aus, alle ohne Verhaltensänderung bei Default):
  1. `PushToTalkController(on_output=None)` — Callback nach erfolgreicher Ausgabe (`controller.py:650`).
  2. `Controller.has_pending_work()` — rein lesende Methode (5 Zeilen, kein Seiteneffekt).
  3. `focus.window_identity() -> tuple[str, str] | None)` — öffentlicher Dünnschalen-Wrapper über das
     bestehende `_call_focus()` (G14), nur für den Fensterwechsel-Reset in D2.
* **Neu:** `src/stt_button/gestures.py` (`DoublePressDetector`), `src/stt_button/summary.py`
  (`SummaryBuffer`, `locate_block()`, `SummaryClient`, `SummaryCoordinator`, `UndoRecord`),
  `src/stt_button/sounds/you_lose.wav`, `tools/make_you_lose.py`, `tools/summary_check.py`,
  `tests/test_gestures.py`, `tests/test_summary.py`, `tests/test_summary_check.py`,
  `tests/test_summary_wiring.py`.
* **Geändert:** `config.py`, `config.example.toml`, `app.py` (Router zwischen Listener und Controller,
  Coordinator-Aufbau in `start()`, `on_output`-Übergabe), `controller.py` (die zwei Haken 1+2),
  `focus.py` (Haken 3), `__main__.py` (`sub.add_parser("summary-models")`, `"summary-test"`, Muster
  `:393-444`), `README.md`, `CHANGELOG.md`, `tools/build-deb.sh` (Asset ins Paket).
* Coordinator-Arbeit läuft in **einem** eigenen Thread; er benutzt `DotoolTyper`/`ClipboardPaster`
  (G12/G13) über injizierte Fakes in den Tests und besitzt den **`SummaryClient`** (Injektion über
  `client=`, B10r, 11:1x) — `client.chat` + Ersetzung + Fehler/Ton-Pfad bilden **einen** queued Job,
  damit der Coordinator der einzige Schalternehmer bleibt (B9c). Kein Import von `requests` in
  `app`-Aufbau außer im Client-Modul (Test: Suite bleibt offline).
* Aufwand: ~1,5 Personentage. Block-Budget 5–15 min plus Voll-Suite (≈ 150 s, G22).

## 4. BLOCKLISTE (darf in keinem Block passieren)
* Keine Tasten-, Clipboard- oder Dialog-Emission in die laufende Desktop-Session des Betreibers.
  Tests arbeiten ausschließlich mit Fake-Typer/-Paster/-Client und fake `wl-paste`/`wl-copy`.
* **Kein Netzwerk in der Suite** — `requests` wird gemockt; Echt-Läufe nur hinter `STT_SUMMARY_REAL=1`.
* Kein sudo, keine Systemverzeichnisse, keine keyd-/systemd-/Unit-Änderungen (P5), kein
  Dienst-Start/-Stopp (P6), kein Push ohne ausdrückliche Freigabe (P7).
  **Einzige Ausnahme (Betreiberentscheid Q2=B, 21:52):** genau **ein** `systemctl --user restart
  stt-button` nach dem letzten Block B12, damit die neue Konfiguration wirksam wird. Kein
  `enable`/`disable`, kein zweiter Neustart, kein Neustart vor B12;
  `summary_enabled` setzt die Kette nicht (bleibt beim Betreiber). Wer nur diese Blockliste liest:
  dieser eine Neustart ist freigegeben — Abschnitt 7 und 10 bestätigen ihn wortgleich. Eine einzige
  Vorbedingung engt ihn ein: **T1-B3 erledigt** (Feldtyp-Matrix) oder ausdrückliche
  Betreiberfreigabe „Neustart ohne T1-B3“ — Begründung und Wortlaut in §5 und §7.
* Produktions-Defaults unverändert; `summary_enabled` bleibt `false`; keine Änderung an `backend`,
  `streaming_mode`, `finalize_mode`, `append_space`, `final_paste*`, `done_sound*`.
* T1 (`.pi/plans/TODO_next_iteration.md`) nicht verschlechtern, kein „Fix by the way": wir nutzen
  dieselbe Paste-Pfad-Falle (G24) und umgehen sie per `trailing_key`, reparieren sie aber nicht hier.
* **Nie** Feldtext löschen, dessen Umfang nicht durch den Lese-Beleg (D3) oder die eigene
  Undo-Aufzeichnung mit Locator (D5) belegt ist. Kein Raten bei Mehrdeutigkeit.
* Keine Emission von `ctrl+shift+Home` bei Terminal- oder Unknown-Fokus (D3-Vorbedingung).
* Kein Klartext im Journal — auch nicht über `repr()` eines Ergebnis-Objekts: `ApplyResult` setzt
  `__repr__` auf Länge + Hash (Vertrag D5).
* **Kein Zugriff auf private Methoden** (`_`-Präfix) aus einem anderen Modul heraus, kein
  `# noqa: SLF001` als Dauerlösung: ist die öffentliche Route zu eng, ist das eine **Planlücke**
  (Anhalten, melden, Planer entscheidet über neuen API-Punkt — Muster B10r, 11:1x).
* `summarizer`-Code importiert kein `torch`/ASR und kein `openai`-Paket; `.venv` wird nicht geändert.

## 5. TDD-BLÖCKE (erst der Test, dann GREEN, **ein Commit je Block**, P3)

Testkommando ausnahmslos: `nice -n 19 .venv/bin/python -m pytest -q -p no:randomly -o addopts=""` (P2).
Schwellwerte sind bereits gemessen (G25) — **kein Mess-Block nötig**.

**Dispatch-Hinweis (Betriebsanweisung 03:5x, P8-Modellage):** Blöcke **einzeln** zuweisen; der
Implementierer liest **nur** seine Tabellenzeile und die markierten Abschnitte (D3/D5, §4, §6.1) —
die ganze Plan-Datei (≈ 47 kB) hat bei `my/work-zai-org-glm-5.3-verda` bereits Stalls ausgelöst.
Ein Block = ein Commit; Statusmeldung nennt Commit-sha, Testzahl und welches Modell tatsächlich
lief (Fallback `cortecs/glm-5.3` nennt sich selbst, R2-Ehrlichkeit). **Planlücken-Regel (B6, 04:1x):
Bei einer Lücke im Vertrag (Rückgabetyp, Feldname, Statusmenge) hält der Block an und meldet an den
Planer — eigenmächtige Vertrags-Erfindung ist verboten; der Planer entscheidet und der Block läuft
weiter. Ein rot laufender Test, der auf einer erfundenen Erwartung beruht, wird nach der Entscheidung
umgeschrieben, nicht auf Grün verbogen.**

**Reihenfolge-Entscheidung des Betreibers (Q3=B, 21:52): die T1-Serie läuft ZUERST**, danach die
Zusammenfassung. Begründung des Betreibers: der gemeinsame Paste-Pfad (`typing.py`) soll sauber sein,
bevor unsere Ersetzung ihn benutzt. Die T1-Blöcke sind **unverändert** die in
`.pi/plans/TODO_next_iteration.md` ("Geplanter TDD-Zuschnitt") festgelegte Folge — hier nur als
T1-B1..T1-B4 referenziert, nicht neu erfunden.

**Duplikatsverbot:** weil T1 vor B1 landet, darf **B6 die `space_in_paste`-Logik in `typing.py` nicht
anfassen oder nachbauen**. B6 nutzt ausschließlich `trailing_key="space"` bzw. `typer.key("space")`
und behält seine Space-Tests reine Regression guards (Ist-Zustand nach T1, nicht der alte Fehler).

**Blockliste (Dispatch-Reihenfolge, eine Zeile je Block, strikt sequentiell). Zählbare Liste:
19 Punkte = T1-B1, T1-B2, T1-B3 (`pending-operator`, **kein** Commit), T1-B4, dann B1…B8 und
**B9a, B9b, B9c** (statt eines B9), dann B10, **B10r**, B11, B12 —
ein Punkt genau einmal, Idempotenzprüfung gegen diese Zeile, nicht gegen eigene Abschriften):**
* **T1-B1** Test: `finalize()` mit Endtext auf `.` und gesetztem `space_in_paste` → genau ein Space (HEUTE ROT, dokumentiert den Fehler)
* **T1-B2** GREEN in `typing.py` (`space_in_paste`-Logik)
* **T1-B3** Feldtyp-Matrix Editor/Browser/Terminal — **nur der Betreiber tippt**, die Kette bereitet Protokoll und Auswertung vor (Blockliste §4: keine Tasten-Emission in die lebende Session). Status: **`pending-operator`** — die Kette darf diesen Block **nicht** auf `done` setzen und **nicht** stillschweigend überspringen; er ist in jedem Statusbericht und jedem Abnahmebefund mitzuführen
* **T1-B4** Doku: Kommentar zu `append_space` in `config.example.toml` + CHANGELOG; No-Code-Hebel für den Rollback: `final_paste=false` bzw. `final_paste_min_chars=0`
* — **Portal (maschinell prüfbar): T1-B1, T1-B2 und T1-B4 grün, ehe B1 beginnt** —
* **T1-B3 ist kein Portal-Bestandteil** (Planer-Entscheid 03:0x; Betreiberentscheid „Nachtschicht: Ja,
  mit Sleep-Inhibit“, 02:50): der Block liegt physisch beim Betreiber, die Kette kann ihn nicht
  erfüllen. Wer B1..B12 parallel zu einem offenen T1-B3 laufen lässt, verletzt das Portal nicht.
  **Was T1-B3 stattdessen sperrt:** die Feldtyp-Matrix §6.5, den Handzettel §6.7 — und den
  **Dienst-Neustart nach B12**, weil genau dieser Neustart die T1-Änderung an `typing.py`
  (Produktions-Paste-Pfad) im lebenden Dienst scharf schaltet, bevor die Matrix sie bestätigt hat.
  Ausnahme nur mit ausdrücklichem „Neustart ohne T1-B3“ des Betreibers — im Report mit Zeitstempel.
* **Nachtschicht-Notiz (Quelle: Betreiber 02:50):** M2–M8 laufen in der Nacht 2026-11-05/06 mit
  Sleep-Inhibit (`pi-awake.service`, `systemd-inhibit --what=sleep --mode=block`), nachdem Suspend
  die Läufe 22:33–23:57 und 02:50–03:57 eingefroren hatte. Das Portal und die Blockliste §4 bleiben
  davon unberührt; insbesondere ändert Nachtschicht nichts am Verbot von Tasten-/Clipboard-Emission
  in die lebende Session (T1-B3 bleibt deshalb liegen, nicht weil gerade jemand schläft).
* **B1** Konfiguration: neue `summary_*`-Schlüssel + `validate()` + `config.example.toml` (ein Commit, G17)
* **B2** `gestures.py` `DoublePressDetector`: reine Zustandsmaschine für zwei Kurzhübe
* **B3** `gestures.py` `PressRouter`: Kurzhub ⇒ `controller.abort()` statt `stop_recording()`
* **B4** `summary.py` `SummaryBuffer` + additive Haken `on_output` / `has_pending_work` in `controller.py`
* **B5** `summary.py` `locate_block()`: Anker-Block, `caret_at_block_end`, alle Abbruchgründe
* **B6** `summary.py` `apply_summary()` / `undo_summary()`: Ersetzen, Schluss-Leerzeichen per Taste, Auswahl-Restore
* **B7** `summary.py` `SummaryClient`: `POST /chat/completions` mit `requests`, `ModelUnavailable`, Token-Logfreiheit
* **B8** Ton: `tools/make_you_lose.py` (deterministisch) + `sounds/you_lose.wav` + `play_error_sound()` in `src/stt_button/summary.py` (kein Eingriff in `controller.py`)
* **B9a–B9c** `summary.py` `SummaryCoordinator` in drei Commits: (a) Zustandsmaschine + Idle-Warten auf `has_pending_work()` (vorher Schlüssel `summary_wait_idle_s=20` als B1-Nachtrag), (b) Fokus-Gate + Markierungsweg + Roundtrip-Aufräumen, (c) Worker-Schlange 1 + Schalternehmer `summary_enabled=false` ⇒ null `Popen`
* **B10** `app.py`-Verdrahtung + `focus.window_identity()` + Regressionstest `summary_enabled=False`
* **B10r** Refactor `refactor(summary): public route for chat+apply`: Coordinator bekommt `client=` und `submit_summary(text, chunks)`; `app.py` ruft nur das — **Pflicht vor B11** (Planlücken-Entscheid 11:1x)
* **B11** CLI `summary-models` / `summary-test` (über `submit_summary` + `wait_for_result`) + `tools/summary_check.py` (Exitlevel 0/1/2)
* **B12** Doku (`README`, `CHANGELOG`) + `tools/build-deb.sh` (Asset), Doku-Test nach Muster G19

| Block | Zuerst der Test (RED) | Implementierung (GREEN) | Dateien | Rollback |
|---|---|---|---|---|
| **B1** Konfiguration | `tests/test_config.py`: Defaults (`summary_enabled is False`, `summary_model == "my-qwen38-flash-next"`, `summary_token == ""`, `summary_press_max_s == 0.35`); `summary_error_sound_volume = 70000` → `ConfigError`; TOML-Roundtrip aller neuen Schlüssel; Produktions-defaults unverändert (`backend == "parakeet"`) | Felder + Validierung in `Config.validate()`; kommentierte Doku in `config.example.toml` inkl. Messvermerk zu `my/qwen38-flash-ext` (400) | `config.py`, `config.example.toml` | `git revert` (Schlüssel + Doku zusammen, sonst ConfigError, G17) |
| **B2** Gesterkennung | `tests/test_gestures.py` (Fake-Uhr): zwei Kurzhübe mit 0,12 s Gap → 1 Geste; Gap 1,2 s → keine; zweiter Hub 0,9 s → keine; langer Hub + Kurzhub → keine; jede Kante wird **genau einmal** weitergereicht | `gestures.py` `DoublePressDetector` (nur Zustandsmaschine, keine Threads) | neu | revert (neues Modul) |
| **B3** Phantom-Schutz | `tests/test_gestures.py`: `PressRouter` ruft bei Hold < `press_max_s` `controller.abort()` und **nie** `stop_recording()`; bei Hold ≥ `press_max_s` umgekehrt; Detector bekommt beide Kanten; bei `summary_enabled=False` verhält sich der Router exakt wie heute (1:1-Durchreichung). **Zusätzlich dokumentierend in `tests/test_typing.py` (die echte G6-Bedingung, nicht „leerer Text“):** `finalize("bereits getippter Text")` mit unverändertem Stand und `append_space=True` → genau **ein** `key space`, kein `type`/`backspace`; `finalize("   ")` → **kein** einziger Typer-Aufruf (Guard `typing.py:897`) | `gestures.py` `PressRouter` (Umgebung des Detektors, kennt nur ein Controller-Interface) | `gestures.py`, `tests/test_typing.py` | revert |
| **B4** Sitzungs-Puffer + Output-Haken | `tests/test_summary.py`: Append pro `on_output`, Reset nach Idle (Fake-Uhr), Reset bei `window_identity`-Wechsel; Log enthält nur Länge + Hash (caplog). `tests/test_controller.py`: `on_output` wird nach erfolgreicher Ausgabe mit (text, epoch) gerufen, Exception im Callback reißt den Finalisierer nicht, Default `None` = alter Pfad | `summary.py` `SummaryBuffer`; `controller.py` Haken 1 (`on_output`) + Haken 2 (`has_pending_work`) | `summary.py`, `controller.py` | revert beider Dateien; Default `None` ⇒ kein Verhaltensrisiko |
| **B5** Verortung | `tests/test_summary.py` gegen `locate_block(chunks, read_text, ...)`: (a) Treffer am Textende → Span + `caret_at_block_end`; (b) Treffer, aber Rest dahinter → `caret_not_at_end`; (c) Anker doppelt → `ambiguous`; (d) Anker fehlt → `no_match`; (e) Innenbestand > `summary_max_gap_chars` → `gap_too_large`; (f) zwei Anker mit dazwischenliegendem Tipp-Text → Block **umfasst** den Tipp-Text (M3a); (g) Lese-Length > `summary_max_read_chars` → `too_long` | `summary.py` `locate_block()` (reine Funktion, keine I/O) | `summary.py` | revert |
| **B6** Ersetzen + Undo | `tests/test_summary.py` mit Fake-Typer/-Paster: kurzer Block → `backspace_and_type(n, repl, trailing_key="space")`, Payload enthält **kein** Schluss-Leerzeichen im `type`-Anteil (T1-Klasse G24); langer Block → `paste_combined(repl, backspaces=n)`, bei `False` Fallback; Undo nur bei Endsuffiz `replacement_text`, sonst `undo_not_found` und **null** Tipp-Aufrufe; Clipboard- und Primär-Restore werden in jedem Zweig aufgerufen (Zähler + Reihenfolge). **Rückgabe nach Vertrag D5 (`ApplyResult`): die zwei rot laufenden Tests vergleichen `result.status` (`"pasted"` / `"pasted_fallback"`) statt roher Strings — die Aufruf Folgen-Assertions bleiben unangetastet; zusätzlich Test auf `__repr__`-Datenschutz (kein Klartext)** | `summary.py` `apply_summary()`, `undo_summary()` | `summary.py` | revert |
| **B7** HTTP-Client | `tests/test_summary.py` (monkeypatch auf `requests.post`, **kein** echtes Netzwerk): Erfolg → Text; 400 "Invalid model name", Timeout, Verbindungsfehler, 5xx, leer, Antwort == Vorlage → `ModelUnavailable`; Token nie in caplog; Prompt-Länge > `summary_max_chars` wird vorher gekürzt und protokolliert | `summary.py` `SummaryClient` | `summary.py` | revert |
| **B8** Ton | `tests/test_summary.py`: `ModelUnavailable` → genau ein `Popen` mit absolutem Pfad auf `you_lose.wav` + `--volume 24000`; `Popen` wirft → keine Exception nach außen; *(die Pflicht „`summary_enabled=false` ⇒ kein Ton“ liegt bei **B9**, Planer 05:0x: `play_error_sound()` kennt die Konfiguration nicht und trägt bewusst keinen Enabled-Parameter und keinen globalen Zustand)*; Asset > 0 Byte; `tools/make_you_lose.py` deterministisch (zweimal → gleiche Summe) | `tools/make_you_lose.py`, `src/stt_button/sounds/you_lose.wav`, **Modul-Funktion `play_error_sound()` in `src/stt_button/summary.py`** (Planer-Entscheid 04:3x, Leseart **(i)**): Muster G10 — `shutil.which("paplay")`, `Popen` fire-and-forget, `DEVNULL`, fängt jede Ausnahme, absoluter Pfad aus dem Paketverzeichnis, `--volume` aus `summary_error_sound_volume`. Signatur: `play_error_sound(name: str, volume: int, *, asset_dir: Path | None = None, popen=subprocess.Popen) -> bool` (`True` nur bei gestartem Prozess; `popen` injizierbar für Tests). **Kein** Eingriff in `controller.py`: `_play_done_sound` bleibt unberührt, die Produktion braucht die neue Funktion nicht (deshalb weder (ii) noch ein drittes Modul (iii)) | `src/stt_button/summary.py`, `tools/`, `src/stt_button/sounds/` | revert (ein Commit); `rm -rf src/stt_button/sounds` |
| **B9a–B9c** Coordinator, **drei Commits** (Planer-Entscheid 09:4x; Tests in neuer Datei `tests/test_summary_coordinator.py` — Stall-Prophylaxe) | `tests/test_summary_coordinator.py`: **B9a führt zuerst den fehlenden Schlüssel ein** (`summary_wait_idle_s`, Default 20, `<= 0` → `ConfigError`, `config.example.toml` dokumentiert, Test in `tests/test_config.py`) — Planlücken-Nachtrag zu B1 im **eigenen Commit davor**; dann: wartet auf `has_pending_work() is False` (Fake zählt Polls, Fake-Uhr) **bevor** er tippt; Timeout → `busy` + Ton, keine Tasten. **Abschluss-Beobachtung (Planlücke 10:1x, Leseart (a)):**
  öffentliche Methode `wait_for_result(timeout: float | None = None) -> ApplyResult | None` —
  blockiert den **Aufrufer**, bis der Worker ein Ergebnis hat oder die Zeit abläuft (`None` bei
  Timeout); führt **nichts** inline aus und wirft `RuntimeError`, wenn sie aus dem Worker-Thread
  selbst aufgerufen wird. **Jeder** Coordinator-Test ruft sie vor den Behauptungen auf — ein
  `polls`-Zähler allein beweist nichts (gemessen: `assert 0 >= 3`, weil der Worker beim Behaupten
  noch nicht gepollt hatte). Außerhalb der Tests nutzt **B11** die Methode, damit `summary-test`
  das Ergebnis synchron ausgeben kann. Kein `run_inline`-Modus, kein `_thread`-Zugriff aus Tests. **B9b (Commit 3):** Fokus `"terminal"`/`"unreachable"` → `focus_not_editable`, **keine** Taste, kein `ctrl+shift+Home`; Primärauswahl vorhanden → Markierungsweg ohne Roundtrip; Roundtrip-Aufräumregel (`key right` nach `wl-paste`) wird ausgeführt. **B9c (Commit 4):** Worker-Schlange 1 → zweiter Auftrag während Lauf ⇒ Ton, kein Parallel-Tippen. **Präzisierung (10:1x):**
  genau **ein** Ton pro verworfenem Auftrag; der später erfolgreiche laufende Auftrag löst **keinen**
  weiteren Ton aus — der Ton meldet die **Verwerfung des zweiten Auftrags**, nicht den Ausgang des
  ersten. **Ton nur bei aktivierter Funktion:** `summary_enabled=false` + erzwungenes `ModelUnavailable` ⇒ **null** `Popen`-Aufrufe (der Coordinator ist der einzige Schalternehmer; Planer 05:0x). **Der Coordinator liest für (i) ob ersetzt wurde, (ii) Modus für Undo und (iii) die Undo-Aufzeichnung ausschließlich `ok` / `status` / `mode` / `removed_text` / `inserted_text` aus `ApplyResult` (Vertrag D5) — es gibt keinen zweiten Kanal** | `summary.py` `SummaryCoordinator` (Thread, Zustandsmaschine `IDLE/WORKING/ACTIVE_UNDO`) | B9a: `config.py`, `config.example.toml` (Schlüssel-Commit) dann `summary.py`, `tests/test_summary_coordinator.py`; B9b/B9c: `summary.py` + dieselbe Testdatei | revert je Commit in umgekehrter Folge B9c→B9b→B9a→Schlüssel; der Schlüssel wird **nie einzeln** zurückgenommen, ohne B9a mitzunehmen |
| **B10** App-Verdrahtung | `tests/test_summary_wiring.py`: Router sitzt zwischen Listener und Controller; `on_output` an `SummaryBuffer` gebunden; `summary_enabled=False` ⇒ Aufbau identisch zu heute (Regression auf heutigen Stand, Router gereicht 1:1 durch, kein Coordinator-Objekt im Lebenszyklus) | `app.py` (`start()`-Aufbau, `_on_press`/`_on_release` umgebaut, `focus.window_identity()` Haken 3) | `app.py`, `focus.py` | revert |
| **B10r** Refactor: öffentliche Route für chat + apply | `tests/test_summary_wiring.py`: der Coordinator wird mit `client=` gebaut; `coord.submit_summary(text, chunks)` löst im **Worker-Thread** `client.chat(text)` + `_apply_flow` aus und liefert/protokolliert das `ApplyResult`; bei `ModelUnavailable` spielt **genau ein** Ton, und zwar **ausschließlich** über den Coordinator; bei `summary_enabled=false` ⇒ `submit_summary` ohne `Popen` und ohne `chat`. **`app.py` ruft keine `_`-Methode** (Test: `noqa: SLF001` im `app.py`-Diff verboten, `grep -c "coord\._" src/stt_button/app.py` == 0) | `SummaryCoordinator.__init__(..., client=None)`, neue öffentliche `submit_summary(text, chunks) -> bool`; `app.py` übergibt den Client beim Aufbau und ruft **nur** diese Methode; private `_apply_flow` bleibt privat | `src/stt_button/summary.py`, `src/stt_button/app.py`, `tests/test_summary_wiring.py` | `git revert` (ein Commit); `87bad6b` bleibt als Zwischenstand im Baum, die Route wird ersetzt, nicht zurückgedreht |
| **B11** CLI + Prüftool | **nutzt ausschließlich die öffentlichen Routen** `submit_summary(...)` + `wait_for_result()` (B10r) — keine privaten `_`-Methoden, kein eigener Verbund in `__main__.py`. `tests/test_cli.py`: `summary-models` listet aus gemocktem `GET /models`, markiert aktiv, zeigt Fallback-Viererliste bei Netzwerkfehler; `--set X` schreibt und liest die Konfig zurück; `summary-test "text"` druckt ohne Tasten, echt nur mit `STT_SUMMARY_REAL=1`. `tests/test_summary_check.py`: Gates Erreichbarkeit / Modell in Liste / Roundtrip-Zeit, Exitlevel 0/1/2 wie `tools/model_switch_check.py` | `__main__.py` (`sub.add_parser`, Muster G18), `tools/summary_check.py` | `__main__.py`, `tools/` | revert |
| **B12** Doku + Paket | `tests/test_docs.py` (Muster G19): README enthält Abschnitt "Zusammenfassen mit Doppelklick", `summary_enabled`, `my-qwen38-flash-next`, den Hinweis „Text verlässt den Rechner“, die Feldtyp-Grenze `caret_not_at_end` und den Restumfang aus §2; CHANGELOG-Eintrag; `tools/build-deb.sh` enthält `sounds/` | `README.md`, `CHANGELOG.md`, `tools/build-deb.sh` | Doku, Packaging | revert |

| **FH1** Flake-Härtung FL1–FL4 | **eigener Block, test-only, sofort dispatchbar** (eine Datei `tests/test_controller.py`, kein Produktcode). Pflichtfolge je Flake: (1) mit dem **Zeithebel** (`source.read` + `sleep(0.05)`) reproduzieren, Fehlsignatur im Report festhalten — **ohne Reproduktion kein Fix**; (2) Test auf **Deadline-Warten auf die tatsächliche Puffer-/Queue-Füllung** umstellen (wartet auf Bedingung mit Deadline) statt blind auf eine Tick-Zahl, dann muss der Test **ungebremst grün und gebremst grün** sein (gebremst grün = er wartet korrekt; überschrittene Deadline ist ein expliziter Fehler, kein Stillschweigen); (3) FL1 (`test_ticks_stop_after_limit_final_text_complete`, G26), FL2 (`test_async_writes_wav_from_snapshot`), FL3 und den Nachbartest `tests/test_controller.py:328-335` (`test_run_once_feeds_and_transcribes`) mit demselben Hebel prüfen — dieselbe Wurzel ist die wahrscheinlichste Erklärung; **nicht** reproduzierbare Flakes bleiben unverändert und werden als offen dokumentiert. **Annahme:** 10 Voll-Läufe grün, davon 5 unter CPU-Druck, plus 10 Einzel-Läufe je betroffenem Test | `tests/test_controller.py` (nur Tests) | `test(controller): make capture-timing tests deterministic (FL1-FL4)` | `git revert` (eine Testdatei, Produkt unberührt) |

## 6. VERIFIKATION (nach B12, von der Implementierungskette auszuführen)
1. **Suite offline:** `nice -n 19 .venv/bin/python -m pytest -q -p no:randomly -o addopts=""`.
   Grün — **mit Ausnahme der beiden bekannten Realzeit-/Thread-Flakes FL1 und FL2** (G22, G26; die
   Suite läuft mit `-p no:randomly`, „Reihenfolge-Flake" ist die ältere Bezeichnung). Ein Ausfall
   dieser beiden darf nicht ungeprüft als Flake gelten; **Nachweisprotokoll (einmal je Block, der
   die Suite anstößt):** (a) den Test **dreimal einzeln** laufen lassen — 3/3 grün ⇒ als bekannter
   Flake verbucht; (b) Fehlsignatur gegen G26 abgleichen (FL1: literales `...` im `typed` + 
   `('backspace','3')` + `endswith`-Assertion); (c) ist (a) oder (b) nicht erfüllt, ist es **kein**
   Flake, sondern ein Regressionsverdacht → an den Planer eskalieren, nicht „reparieren by the way“.
   Ein neuer Flake (ein anderer Test) ist sofort zu melden. Neues Gate: Suite läuft ohne Netzwerk
   (Test blockt `socket.connect`) und in < 3 min. **Eigenmächtige Änderungen an FL1/FL2 oder an den
   Zeit-Schwellen der Tests sind verboten** (kein `sleep` hochdrehen, kein Timeout-Strecken).
2. **Diff-Gegenprüfung Produktionspfad:** `git diff c3cc170 -- src/stt_button/config.py` zeigt **keine**
   Änderung an `backend`, `streaming_mode`, `finalize_mode`, `append_space`, `final_paste*`,
   `done_sound*`, `min_audio_s`; der `app.py`-Diff enthält bei `summary_enabled=False` keinen neuen
   Aufruf im Kantenpfad (B10-Regression als Beleg).
3. **Offline-Funktionsnachweis** (keine echte Taste, kein echtes Clipboard): protokollierter
   Fake-Durchlauf über drei Diktate mit (a) sauberem Block am Ende, (b) dazwischen getipptem Text
   (M3a-Fall, wird mitgefasst), (c) Cursor nicht am Ende → Abbruch `caret_not_at_end` mit **null**
   Tastenereignissen. Journal-Auszug nach `.pi/reports/summary_verify.md`.
4. **Echt-Lauf gegen das Gateway** (nur mit `STT_SUMMARY_REAL=1`, reiner HTTP-Test ohne Tastatur):
   `tools/summary_check.py --list-models` (erwartet ~56 Einträge, `my-qwen38-flash-next` markiert) und
   ein deutscher Testtext durch `summary-test`; Gates: HTTP 200, Antwort ≠ Vorlage, Laufzeit <
   `summary_timeout_s`. Beweise nach `.pi/reports/summary_verify.md`.
5. **Feldtyp-Matrix (nur Betreiber; Agenten dürften hier nicht tippen):** je ein Durchlauf in GNOME
   Text Editor, Browser-Textfeld (Firefox), LibreOffice Writer. Erwartung je Feld: Ersetzung an Ort
   und Stelle, genau ein Leerzeichen am Ende, Vorspann unverändert, Ablage danach wieder wie vorher.
   **Zusätzlich je Feld zu prüfen:** der Roundtrip (`wl-paste -p`) liefert nach `ctrl+shift+Home`
   tatsächlich Text — also Tastatur-Auswahl besetzt die Primärauswahl (D3, Planer 10:4x). Ein Feld,
   das das nicht tut, bricht mit `empty_read` + Ton ab und wird als **bekannte Grenze** protokolliert
   (ein `ctrl+c`-Variante wäre ein neuer Eingriff und braucht eine eigene Entscheidung).
   Eine Zeile je Feld im Report; Abbrüche mit `caret_not_at_end` sind **bestätigtes Verhalten**, kein Fehler.
6. **Gesten-Trefferquote (nur Betreiber):** fünf echte Doppelklicks nach Diktaten. Journal zählt
   `gesture accepted` / `phantom press suppressed`. Akzeptanz ≥ 4 von 5; sonst
   `summary_press_max_s`/`summary_gap_max_s` nachjustieren (Anker G25: echte Mindest-Haltedauer 0,81 s).
7. **Handzettel (nur der Betreiber, live in seiner Session):** a) Doppelklick nach Diktat →
   strukturierte Fassung an selber Stelle, Du/Sie und Ton bleiben; b) zweiter Doppelklick → exakt der
   Ausgangstext; c) markierter Absatz → nur dieser umgearbeitet; d) `summary_model` auf
   `my/qwen38-flash-ext` → dezenter Ton, **Text unangetastet**; e) Terminal-Fokus → nichts passiert,
   nur Ton; f) Zwischenablage enthält nach dem Eingriff wieder den alten Inhalt; g) **derselbe kurze
   Satz zweimal im Feld** (z. B. „Danke." zweimal) → erwarteter Abbruch `ambiguous` mit Ton, Text
   unangetastet: das ist **vorgesehenes Verhalten** (D3, Status-Semantik), kein Fehler.
8. **Packung (nur Prüfung, keine Installation):** der `tools/build-deb.sh`-Pfad nimmt
   `src/stt_button/sounds/you_lose.wav` mit (P5: keine Systempfade, kein Installationslauf).

## 7. OPERATOR-TODO und Freigabelage (Betreiber, 21:52)
* `summary_enabled = true` in `~/.config/stt-button/config.toml` (Datenschutz-Schalter, D7) —
  **bleibt beim Betreiber; die Kette setzt ihn nicht** (Q2=B).
* **Dienst-Neustart: von der Kette auszuführen, nach dem letzten Block (B12)** — der Betreiber hat
  `systemctl --user restart stt-button` am 21:52 ausdrücklich freigegeben (P6-Freigabe, Q2=B).
  Messvermerk: `stt-button.service` läuft bereits (pid 2073, seit 07:52) und die Konfiguration wird
  nur beim Start gelesen → ohne Neustart wirkt nichts. Ohne diese Freigabe: kein Neustart.
  Die Kette protokolliert vor dem Neustart `systemctl --user show stt-button -p ExecMainStartTimestamp`
  (Beleg, dass der Dienst danach neu läuft) und meldet die Journal-Zeilen der ersten Geste danach.
  **Vorbedingung (Portal-Ableitung §5, Planer 03:0x):** Neustart erst nach erledigtem **T1-B3**
  (Feldtyp-Matrix), weil er die T1-Änderung an `typing.py` im lebenden Dienst scharf schaltet.
  Ohne T1-B3 nur mit ausdrücklicher Betreiberfreigabe „Neustart ohne T1-B3“ (im Report mit Zeitstempel).
* Handzettel (6.7) und Feldtyp-Matrix (6.5) selbst ausführen und Ergebnisse abzeichnen — Agenten
  dürfen in diese Session nicht tippen (§4).
* Falls das Debian-Paket die Funktion enthalten soll: `build-deb.sh`-Lauf und Systempfade (P5).

## 8. ROLLBACK und BACKLOG
* Ein Commit je Block → `git revert` in umgekehrter Reihenfolge **B12→B10, dann B9c→B9b→B9a→
  Schlüssel-Commit (`summary_wait_idle_s`), dann B8→B1, dann T1-B4→T1-B1**
  (T1 ist eine eigene, vorher commitete Serie und wird bei einem reinen Rückzug der Zusammenfassung
  **nicht** mit zurückgenommen — der Leerzeichen-Fix bleibt wertvoll). Neue Dateien sind damit
  spurlos entfernt; `.venv`, Systempfade und der Produktionspfad wurden nie angefasst.
* **Gesamt-Rückzug ohne Code:** `summary_enabled = false` + Dienst-Neustart (P6) — der Kantenpfad ist
  dann bit-identisch zu heute (B10-Regressionstest garantiert genau das).
* **Backlog (nicht dieser Plan):** (i) evdev-Mitlesen manueller Tastatur für die wörtlich
  vollständige Betriebsdefinition (G23; eigener Ground-Truth-Lauf nötig); (ii) T1
  (`TODO_next_iteration.md`) — unsere Pfade umgehen die T1-Falle per echter Space-Taste, reparieren
  sie aber nicht; (iii) Undo-Stapel; (iv) GUI-Modellwahl (zenity/yad) — bewusst abgelehnt (D6), bei
  Bedarf als Terminal-Menü im CLI statt als Dialog; **(v) FL1/FL2 deterministisch machen** → **vorgezogen als Block FH1 in §5** (Beweis über FL4, G26:
  der Zeithebel macht die Timing-Annahme messbar; G27). Die ursprünglich vorgeschlagene injizierte
  `monotonic`-Zeitquelle bleibt als Alternative bestehen, falls der Deadline-Ansatz in FH1 nicht für
  alle vier Flakes reicht.
  (`tests/test_controller.py:787-832`, G26): Zeitquelle injizierbar statt Realzeit — additiver
  Parameter `monotonic: Callable[[], float] = time.monotonic` am Controller und am Transcriber-
  Intervall-Gate, Test fährt eine Fake-Uhr und macht die Tick-Folge zählbar (Alternative: Tick-Zählung
  statt Zeitschwelle). **Absichtliche Auslagerung aus dieser Kette (Planer-Entscheid 02:2x):**
  (1) Produktverhalten ist nicht betroffen, nur die Testarchitektur; (2) die Nahtstelle liegt in
  `controller.py`/`streaming_types.py` — genau dort setzt B4 `on_output`/`has_pending_work`, zwei
  Ketten im selben heißen Pfad ist vermeidbares Rebase-Risiko ohne Nutzengewinn; (3) ein Flake-Fix
  braucht erst ein verlässliches RED unter Last, das ist ein eigenes Experiment (Lastprotokoll wie in
  G26), kein 5-Minuten-Block. **Annahme für diesen Plan:** Akzeptanz nach §6.1 mit
  Nachweisprotokoll. **Annahme für den Backlog-Block:** 10 aufeinanderfolgende Voll-Läufe grün,
  davon 5 unter CPU-Druck (z. B. parallel 8× `yes > /dev/null`), plus 10 Einzel-Läufe von FL1/FL2
  grün. Rollback: `git revert` (der Parameter defaultet auf `time.monotonic`, Verhalten undifferiert).
* **(vi) B5b — `ambiguous` über die Chunk-Kette statt Gesamtextzählung** (Beobachtung aus B5
  `2423115`; Planer-Entscheid 03:5x: zurückgestellt, weil B6 bereits auf der B5-Semantik läuft und
  B5 fail-safe ist). **Regel:** der linke Blocksrand ist die Position des **ersten** Ankers, das
  rechte Ende ist durch `caret_at_block_end` ohnehin ans Lesende gebunden. Mehrdeutig ist der Block nur,
  wenn **zwei mit allen übrigen Ankern in Reihenfolge verträgliche Ketten zu unterschiedlichen
  Spannen** führen. Duplikate **mittlerer** Anker dürften dann nicht mehr abbrechen, Duplikate des
  **ersten** Ankers mit zwei gültigen Ketten weiterhin. **Akzeptanz (Tests zuerst):** (a) zwei
  identische Kurzdiktate, nur eine passende Kette → Block gefunden, `caret_at_block_end` ✓;
  (b) zwei identische Anker, zwei passende Ketten, verschiedene Linksränder → weiterhin `ambiguous`;
  (c) identischer Anker als **mittlerer** Chunk → kein Abbruch; (d) alle sieben B5-Fälle bleiben
  grün; (e) 10 Voll-Läufe ohne neue Flakes. Rollback: `git revert` (B5-Regel ist die konservative,
  kein Datenrisiko beim Rückzug).

## 9. ABWEICHUNGSPROTOKOLL gegen den Orchestrator-Entwurf (`plan_summary_mode.md`)
Der Entwurf bleibt als Quelle liegen; ersetzt wird, was einer Nachprüfung nicht standhält.

| Entwurfs-punkt | Prüfung durch den Planer | Behandlung |
|---|---|---|
| D1: „kein Unterdrücken; die kurze Aufnahme läuft mit und wird von `min_audio_s` (`asr.py:808`) verworfen; in B1 erst zu messen“ | **widerlegt**: `asr.py:808` gate nur den Live-Tick, `finalize()` transkribiert bedingungslos (`asr.py:904`); das Ergebnis eines Kurzhubs landet beim Typer und ist je nach ASR-Ausgabe Leerzeichen (Delta leer, `typing.py:1039`) oder Junk-Text — nur der Fall „leeres Ergebnis“ bleibt dank Guard folgenlos (`typing.py:897`) (G5, G6) | **VERWORFEN und ersetzt** durch D1: Release-Regel + bestehendes `controller.abort()` (G7); Schwellwerte nicht mehr „später gemessen“, sondern aus dem echten Journal belegt (G25) |
| D2: „Ersatz = Vorspann + zusammengefasst + Nachspann, Ersetzen **genau dieses Spans**“ | **nicht ausführbar**: es gibt kein Tastatur-Primitiv, den Cursor an eine Spanngrenze zu setzen; `ctrl+shift+Home` wählt bis Feldanfang, der Rest wäre Raten oder Refix des ganzen Präfix (Feldtyp-Streuung laut Brief §5.4) | **ERSETZT** durch D3: Mutation nur bei Lese-Beleg `caret_at_block_end`; sonst Abbruch ohne Löschung |
| D2: Grundregel „eindeutiger Teilstring-Fund, Mehrdeutigkeit ⇒ Abbruch“ | trägt, aber deckt „oder getippt“ nicht ab, obwohl der Roundtrip den Tipp-Text liefert (G15) | **BEHALTEN und erweitert**: Anker-Block-Regel mit `summary_max_gap_chars` (§2, M3a-Entscheidung) |
| D2: Schluss-Leerzeichen immer per `trailing_key` | für den Tipp-Pfad richtig; `paste_combined` (langer Block) hat **keinen** `trailing_key` (G13) | **ERWEITERT** (D3): im Paste-Pfad eigenes `typer.key("space")` nach dem Chord; Leerzeichen nie im Clipboard-Payload (T1, G24) |
| D3 Markierung inkl. Clipboard-/Primär-Restore | trägt; Restore ist zwingend, weil `paste_combined` die Ablage überschreibt | **BEHALTEN**, plus Fokus-Vorbedingung `"other"` (kein `ctrl+shift+Home` in Terminals) und explizite Trennzeichen-Regel |
| D4 Undo „stellt exakt `original_text` wieder her“ | **zu riskant**: ohne Beleg, dass das Feld noch unsere Zusammenfassung enthält, löscht ein `backspace len(replacement)` fremden Text | **VERSCHÄRFT** (D5): Locator-Beleg nötig, sonst `undo_not_found` und null Löschung |
| D5 Default `my-qwen38-flash-next`, Timeout 12 s, Prompt-Regeln, Temperatur 0 | bestätigt (O1; Gateway-Messung G21) | **BEHALTEN** |
| D5: toter Picker-Eintrag `my/qwen38-flash-ext` + `summary_picker="zenity"` | 400-modell in einer Auswahl ist eine Falle; Zenity-Dialog aus dem Headless-Dienst = verbotene Dialog-Emission in die lebende Session | **VERWORFEN**: Doku-Kommentar statt Eintrag; kein `summary_picker`-Schlüssel, CLI bleibt der Picker |
| D6 Datenschutz (Default aus, nur Länge + Hash im Log) | trägt | **BEHALTEN**, als Test in B4/B7 verankert |
| D7 Ton (Asset, numpy-Generator, fire-and-forget) | trägt; `os.path.isabs`-Ast vorhanden (G10), `sounds/` fehlt noch (G20) | **BEHALTEN**, Auslöserliste um alle Abbruchgründe aus D3/D4/D5 erweitert |
| D8 Dateien/Aufwand, B0-Messblock | B0 ist überflüssig (G25 bereits gemessen); dafür fehlen dem Entwurf der Phantom-Router und die Reihenfolge-Frage mit dem Finalisierer | **ERWEITERT**: drei additive Haken explizit listed (D8), Blöcke B3 (Router) und B9 (Coordinator/Ordnen) neu, B0 gestrichen |

## 10. BETREIBER-ENTSCHEIDE (Q1–Q3, eingegangen 21:52 — nichts offen)
* **Q1 = A** — Anker-Block wie geplant: manuell getippter Text innerhalb unserer Anker wird
  mitgefasst, rein getippte Passagen außerhalb bleiben unangetastet; evdev bleibt Backlog (§8).
  Quelle: Betreiber, 21:52. **Folge:** §2 und D3 bleiben unverändert bestehen.
* **Q2 = B** — expressliche P6-Freigabe: die Kette führt nach dem letzten Block (B12) genau **ein**
  `systemctl --user restart stt-button` aus; der Datenschutz-Schalter `summary_enabled` bleibt beim
  Betreiber und wird von der Kette nicht gesetzt. Quelle: Betreiber, 21:52.
  **Folge:** §4 (Blockliste, einzige Ausnahme) und §7 sind darauf abgestimmt. Engende
  Vorbedingung aus §5/§7: der Neustart setzt **T1-B3 erledigt** voraus (oder eine ausdrückliche
  Betreiberfreigabe „Neustart ohne T1-B3“), weil er die T1-Änderung an `typing.py` scharf schaltet.
* **Q3 = B** — **T1 zuerst** (Abweichung von meiner Empfehlung A): die Kette implementiert zuerst die
  Leerzeichen-Blockfolge T1-B1..T1-B4 aus `.pi/plans/TODO_next_iteration.md`, danach erst B1..B12.
  Begründung des Betreibers: der gemeinsame Paste-Pfad soll vorher sauber sein.
  Quelle: Betreiber, 21:52. **Folge:** §5 um die T1-Serie und das Duplikatsverbot für B6 ergänzt.

**Noch offene Betreiberfragen: null.** Alle Punkte aus Brief §5 sind entschieden: 5.1 → Plan
schreibt in Abschnitten (dieser Plan), 5.2 → §9, 5.3 → Q1=A, 5.4 → `caret_at_block_end` (D3) +
Feldtyp-Matrix (6.5), 5.5 → CLI-Picker, zenity verworfen (D6), 5.6 → keine Verwechslung (D6).

