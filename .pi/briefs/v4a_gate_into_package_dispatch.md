# Dispatch V4a — Gate ins Paket verlegen (Kleinpaket: EIN Modul + HUELLE + Testdatei erweitern)

Repo `/home/salmon/pi-web-ui/STT-Button`. Plan `.pi/plans/plan_no_venv_B.md` — **nur die
V4a-Zeile (Zeile 69)** lesen, sonst nichts. NIEMALS den ganzen Plan.

## Warum dieser Block ueberhaupt existiert (Planer-Entscheidung (1)=(i))
`tools/build-deb.sh` kopiert **kein** `tools/` ins Deb. Wuerte `setup` (V4) die Messroutine aus
`tools/deps_gate.py` importieren, greift sie im installierten Paket ins Leere. Also: Messroutine
gehoert ins Paket. Option (ii) `tools/` ins Deb = abgelehnt (Packaging-Flaeche), Option (iii)
doppelte Routine = abgelehnt (Drift).

## Vorgabe des Planers (V4a-Zeile, inhaltlich woertlich)
* Messroutine wandert nach **`src/stt_button/deps_gate.py`** (neu) — **Exitlevel-Vokabular aus V1
  unveraendert**: `0` ok / `1` `pip_missing`|`deps_missing` / `2` `requirements_missing` / `3` `shadow_risk`.
* `tools/deps_gate.py` wird zur **duennen Huelle**: `from stt_button.deps_gate import main; sys.exit(main())`
  — **keine** Logik mehr im `tools/`-Pfad.
* Reines Verschieben: Verhalten, Reportdatei, Argumente, Ausgabe bleiben wie in V1 (`44a4a31`).

## Testpflicht — BESTEHENDE `tests/test_deps_gate.py` erweitern (nicht neu anlegen)
* die **bestehenden 6 Tests bleiben gruen** (ihr Verhalten aendert sich nicht),
* NEU: Import + Aufruf ueber `stt_button.deps_gate` funktioniert,
* NEU: CLI-Huelle liefert **dieselben** Exitlevels wie der Modulaufruf — insbesondere
  `requirements_missing` bleibt **Level 2** (fehlende Requirements-Datei) und `pip_missing` Level 1,
* NEU: der `tools/`-Pfad enthaelt keine Messlogik mehr (z. B. `grep`-Test: keine `subprocess`/
  `_parse_requirements`-Aufrufe in `tools/deps_gate.py`).
Tests arbeiten mit tmp_path/`--requirements`-Zeigern — **kein** pip, **kein** Netzwerk, **kein**
installiertes `/opt` erforderlich.

## TDD + Abschluss
ZUERST die neuen Tests (ROT: `stt_button.deps_gate` existiert noch nicht), dann verschieben (GRUEN).
VolSuite **hoechstens einmal**: `nice -n 19 .venv/bin/python -m pytest -q -p no:randomly -o addopts=""`
im LEERLAUF (keine Parallel-Suite!). Kollektion vorher **838**; gruen = 835 passed / 2 skipped /
0 failed ODER genau EIN bekannter Timing-Flake: FL1
`TestLiveAsrUntilController::test_ticks_stop_after_limit_final_text_complete` (Signatur G26:
literales `'...'` + `('backspace','3')`), FL2 `TestAudioDumpModes::test_async_writes_wav_from_snapshot`,
FL3 `TestErrors::test_engine_error_aborts` (im Leerlauf 5/5 gruen, unter Last faellt er).
Nie ungeprüft buchen — im Zweifel dreimal einzeln.
Commit: `refactor(tools): move dependency gate into the package`

## Erlaubte Dateien
`src/stt_button/deps_gate.py` (neu), `tools/deps_gate.py` (nur Huelle), `tests/test_deps_gate.py`
Verboten: `src/stt_button/__main__.py` (das ist V4!), `src/stt_button/bootstrap.py`,
`systemd/**`, `packaging/**`, `tools/build-deb.sh`, Doku, `pyproject.toml`; kein sudo/apt/pip,
kein Netzwerk, keine Tastatur-/Clipboard-/Ton-Emission, **kein git push**, kein Dienst-Neustart,
kein Schreiben unter `~/.config/`.

## Stall-Prophylaxe (verbindlich)
* `tools/deps_gate.py` (204 Z.) nur in Stuecken <= 80 Z. lesen; Modulrumpf per `edit`/`patch`
  schreiben, NICHT mit einem einzigen großen `write`.
* KEINE `while pgrep`-Wartespruenge, KEINE 20x-Testschleifen, KEIN conftest.py-Gesuche.
* Wenn dein letzter Schritt ein Werkzeug war und dir nichts mehr bleibt: melde das in EINER
  Zeile, statt schweigend zu enden.
* Schlussmeldung max 5 Zeilen (Sha, N passed, tatsaechlich gelaufenes Modell).
