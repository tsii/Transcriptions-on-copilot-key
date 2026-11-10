# Dispatch V5 — `control`-Abhaengigkeiten ohne `python3-venv` (Kleinpaket: EINE Datei + EINE neue Testdatei)

Repo `/home/salmon/pi-web-ui/STT-Button`. Plan `.pi/plans/plan_no_venv_B.md` — **nur die
V5-Zeile** lesen (Tabelle §3), sonst nichts.

## Vorgabe des Planers (V5-Zeile, inhaltlich woertlich)
`packaging/debian/control`:
* `Depends: python3, python3-pip, pipewire-bin, pulseaudio-utils, wtype` — **`python3-venv` raus.**
  Ist schon gemessen: Zeile 7 enthaelt heute `python3, python3-venv, python3-pip, ...` — es fehlt
  also nur das `python3-venv`, `python3-pip` ist bereits drin.
* Beschreibungstext auf Weg B umformulieren: `pip install --user --break-system-packages`
  (Drittbibliotheken ins User-Site, **kein** venv-Bau).

## Testpflicht — NEUE Datei `tests/test_deb_control.py`
* `python3-venv` steht **nicht** in `Depends`,
* `python3-pip` steht in `Depends`,
* die Beschreibung enthaelt `--user`,
* in der **Laufzeitbeschreibung** kommt das Wort `venv` nirgends vor (auch nicht in
  Auskommentierung oder `python3-venv`-Rest).
Tests lesen nur die `control`-Datei (parsen der `Depends:`-Zeile, Textsuche im Description-Block).
Kein `dpkg-deb`, kein `lintian`, kein Netzwerk, kein Build.

## TDD + Abschluss
Testdatei ZUERST (ROT: `python3-venv` ist noch in `Depends`), dann `control` (GRUEN).
Blocktests laufen lassen. **Keine VolSuite** — die ist wegen eines separate, praeda
tierten Timing-Tests (`tests/test_controller.py::TestStreaming::test_text_reaches_typer`)
aktuell kein Abnahmeregister; das entscheidet der Orchestrator separat.
Commit: `fix(packaging): drop python3-venv, require python3-pip`

## Erlaubte Dateien
`packaging/debian/control`, NEU `tests/test_deb_control.py`
Verboten: `src/**` (insb. `__main__.py` = V4), `systemd/**`, `tools/build-deb.sh` (V6),
Doku (V7), `pyproject.toml`, `packaging/debian/changelog` (Version bleibt, aendert der
Orchestrator); kein sudo/apt/dpkg, kein Netzwerk, keine Tastatur-/Clipboard-/Ton-Emission,
**kein git push**, kein Dienst-Neustart, kein Schreiben unter `~/.config/`.

## Stall-Prophylaxe (verbindlich)
* `control` ist kurz — komplett lesen ist ok; aendern mit `edit`.
* KEINE `while pgrep`-Wartespruenge, KEINE 20x-Testschleifen, KEIN conftest.py-Gesuche.
* Wenn dein letzter Schritt ein Werkzeug war und dir nichts mehr bleibt: melde das in EINER
  Zeile, statt schweigend zu enden.
* Schlussmeldung max 5 Zeilen (Sha, N passed, tatsaechlich gelaufenes Modell).
