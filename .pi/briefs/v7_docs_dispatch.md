# Dispatch V7 — Doku fuer den venv-freien Weg (Kleinpaket: 4 Textdateien + BESTEHENDE Testdatei)

Repo `/home/salmon/pi-web-ui/STT-Button`. Plan `.pi/plans/plan_no_venv_B.md` — **nur die
V7-Zeile** lesen (Tabelle §3), sonst nichts.

## Vorgabe des Planers (V7-Zeile, inhaltlich woertlich)
1. **README**: neuer Abschnitt „Installation ohne venv (Weg B)" mit
   * den Root-Zeilen (`sudo dpkg --purge stt-button`, `sudo apt install -y python3-pip`,
     `sudo apt install ./dist/stt-button_<Version>_amd64.deb`),
   * `pip install --user --break-system-packages -r /opt/stt-button/packaging/runtime-requirements.txt`
     (Drittbibliotheken ins User-Site `~/.local/lib/python3.12/site-packages`),
   * **und dem Isolations-Rezept fuer den Shadow-Fall**: `Environment=PYTHONUSERBASE=%h/.local/stt`
     in der Unit **plus identische Variable bei der Installation** — fuer den Fall, dass ein
     anderweitig installiertes numpy/scipy/soundfile/pyaudio ins User-Site-shadowed.
   Ist alles noch nicht da (gemessen: README kennt weder „Weg B", `--break-system-packages`
   noch `PYTHONUSERBASE`).
2. **CHANGELOG**: Eintrag `[0.2.1]` mit den Weg-B-Aenderungen (Format an die bestehenden
   Eintraege anpassen, Datum heute 2026-11-10).
3. **`src/stt_button/bootstrap.py`**: **nur das Modul-Docstring** auf „dev-only, kein
   Laufzeitpfad mehr" umstellen. Kein Code, keine Signatur, keine Funktionstexte.
4. **`tools/build-deb.sh`**: **nur der Kopfkommentar Zeile 6** — er behauptet weiterhin, die
   “Python runtime (venv + ML stack, per user) is built on the first run”, was nach Weg B
   falsch ist. Zeilen 2 und 40 („no .venv") sind bereits richtig und **muessen bleiben**.

## Testpflicht — BESTEHENDE `tests/test_docs.py` erweitern
* README enthaelt „Weg B", `--break-system-packages`, `PYTHONUSERBASE`,
* CHANGELOG enthaelt `0.2.1`,
* `tools/build-deb.sh` enthaelt im Kopf **kein** „built on the" mehr, aber weiter „no .venv",
* `bootstrap.py`-Docstring nennt „dev" und **keinen** Laufzeit-Behauptungssatz (Form:
  Muster auf `dev`),
* die bestehenden 19 Tests dieser Datei bleiben gruen.
Tests lesen nur Textdateien — kein Build, kein Deb, kein Netzwerk.

## TDD + Abschluss
Tests ZUERST (ROT), dann Doku (GRUEN). **Blocktests dieser Datei** laufen lassen —
**keine VolSuite** (laeuft gerade in anderer Arbeit).
Commit: `docs: venv-free installation path (way B)`

## Erlaubte Dateien
`README.md`, `CHANGELOG.md`, `src/stt_button/bootstrap.py` (**nur Docstring**),
`tools/build-deb.sh` (**nur Kopfkommentar**), `tests/test_docs.py`
Verboten: `pyproject.toml` und `packaging/debian/control` (Versionshebung mache ICH nach
deinem Commit), `src/stt_button/__main__.py` (V4), `systemd/**`, `tests/test_controller.py`
(laeuft gerade), `tests/test_setup_script.py`, `tests/test_deb_control.py`, beide `deps_gate.py`,
`packaging/**`; kein sudo/apt/pip, kein Netzwerk, keine Tastatur-/Clipboard-/Ton-Emission,
**kein git push**, kein Dienst-Neustart, kein Schreiben unter `~/.config/`.

## Parallelitaet + Stall-Prophylaxe (verbindlich)
* Ein anderes Kind schreibt gerade an `tests/test_controller.py` — nicht anfassen, nicht
  als "Test lauf" in die VolSuite ziehen.
* README/CHANGELOG nur in Stuecken <= 80 Z. lesen; Aenderungen mit `edit`, nicht mit `write`
  (kein Overschreiben fremder Abschnitte).
* KEINE `while pgrep`-Wartespruenge, KEINE 20x-Schleifen, KEIN conftest.py-Gesuche.
* Wenn dein letzter Schritt ein Werkzeug war und dir nichts mehr bleibt: melde das in EINER
  Zeile, statt schweigend zu enden.
* Schlussmeldung max 5 Zeilen (Sha, N passed, tatsaechlich gelaufenes Modell).
