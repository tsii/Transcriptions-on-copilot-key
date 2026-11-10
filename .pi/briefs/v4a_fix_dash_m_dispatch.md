# Dispatch V4a-fix — deps_gate per `-m` lauffaehig + Stille-Regel (Kleinpaket)

Repo `/home/salmon/pi-web-ui/STT-Button`. Plan `.pi/plans/plan_no_venv_B.md` — **nur die
V4a-Zeile (Zeile 69)** lesen; sie enthaelt jetzt die Nachbesserung. Kein Kind bekommt den Plan
ganz.

## Befund (vom Orchestrator gemessen, Planer-Entscheidung (A))
`src/stt_button/deps_gate.py` (V4a, `1cc10d2`) hat `def main()` **ohne**
`if __name__ == "__main__": sys.exit(main())`. Folge:
`python -m stt_button.deps_gate --requirements /nonexistent` -> **exit 0, keine Ausgabe**,
waehrend `python tools/deps_gate.py --requirements /nonexistent` korrekt **exit 2** liefert.
Ein Gate, das bei Nicht-Aufruf Erfolg meldet, ist eine False-Green-Falle.

## Vorgabe des Planers (woertlich)
* `if __name__ == "__main__": sys.exit(main())` in `src/stt_button/deps_gate.py`.
* **Subprozess-Tests ueber BEIDE Eingaenge** (`-m stt_button.deps_gate` UND `tools/deps_gate.py`)
  mit **identischen** Levels: fehlende Requirements -> **2**, heutiger Stand -> **1**.
* **Stille-Regel** als Test: ein Lauf ohne Report/Ausgabe darf **nie** Level 0 liefern
  (`exit != 0` ODER Report existiert).

## Testpflicht — BESTEHENDE `tests/test_deps_gate.py` erweitern
* die **bestehenden 10 Tests bleiben gruen**,
* NEU: `-m`-Pfad fehlt-Requirements => 2; `-m`-Pfad echter Stand => 1 (falls dein Rechner
  erfuellt ist, was hier NICHT der Fall ist: dann ueber `--python`-Zeiger auf ein Interpreter-
  Snap-in, das pip vortaeuscht — Hauptsache, der Test haelt ohne Netz/pip),
* NEU: Stille-Regel (kein Report geschrieben UND exit 0 => Test失败).
Kein echtes pip, kein Netzwerk, keine Schreibgriffel ausser tmp_path.

## TDD + Abschluss
ZUERST die neuen Tests (ROT: `-m` liefert 0), dann Guard (GRUEN).
VolSuite **hoechstens einmal** im LEERLAUF:
`nice -n 19 .venv/bin/python -m pytest -q -p no:randomly -o addopts=""`.
Kollektion vorher **841** (838 + 3 aus V4a); gruen = 0 failed ODER genau EIN bekannter
Timing-Flake: FL1 `TestLiveAsrUntilController::test_ticks_stop_after_limit_final_text_complete`
(G26-Signatur: literales `'...'` + `('backspace','3')`), FL2
`TestAudioDumpModes::test_async_writes_wav_from_snapshot`, FL3 `TestErrors::test_engine_error_aborts`
(im Leerlauf 5/5 gruen, unter Parallel-Last faellt er). Nie ungeprüft buchen — dreimal einzeln.
Commit: `refactor(tools): make deps_gate runnable via -m`

## Erlaubte Dateien
`src/stt_button/deps_gate.py`, `tests/test_deps_gate.py`
Verboten: `tools/deps_gate.py` (ist fertige Huelle), `src/stt_button/__main__.py` (V4),
`bootstrap.py`, `systemd/**`, `packaging/**`, `tools/build-deb.sh`, Doku, `pyproject.toml`;
kein sudo/apt/pip, kein Netzwerk, keine Tastatur-/Clipboard-/Ton-Emission, **kein git push**,
kein Dienst-Neustart, kein Schreiben unter `~/.config/`.

## Stall-Prophylaxe (verbindlich)
* Nur die letzten 20 Zeilen des Moduls aendern (`edit`), nicht umschreiben.
* KEINE `while pgrep`-Wartespruenge, KEINE 20x-Schleifen, KEIN conftest.py-Gesuche.
* Wenn dein letzter Schritt ein Werkzeug war und dir nichts mehr bleibt: melde das in EINER
  Zeile, statt schweigend zu enden.
* Schlussmeldung max 5 Zeilen (Sha, N passed, tatsaechlich gelaufenes Modell).
