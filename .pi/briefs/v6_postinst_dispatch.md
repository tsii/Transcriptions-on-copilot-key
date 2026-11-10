# Dispatch V6 — postinst-Hinweis auf Weg B (Kleinpaket: EINE Skriptdatei + BESTEHENDE Testdatei)

Repo `/home/salmon/pi-web-ui/STT-Button`. Plan `.pi/plans/plan_no_venv_B.md` — **nur die
V6-Zeile** lesen (Tabelle §3), sonst nichts.

## Vorgabe des Planers (V6-Zeile, inhaltlich woertlich)
Der postinst-Text in `tools/build-deb.sh` (`:93-109`) formuliert um auf Weg B:
1) `pip install --user --break-system-packages -r /opt/stt-button/packaging/runtime-requirements.txt`
   (plus die torch-Zeile vom CPU-Index),
2) `stt-button setup`,
3) Neustart des Dienstes.
**Kein venv-Satz mehr** im postinst-Hinweis.

Pruefe vor der Aenderung an einer Stelle, die der Plan nicht nennt, aber der Text referenziert:
existiert `packaging/runtime-requirements.txt` im Repo (falls nicht: melde das in EINER Zeile im
Ergebnis — **nicht** selbst anlegen, das entscheidet der Planer).

## Testpflicht — BESTEHENDE `tests/test_setup_script.py` erweitern
* `bash -n tools/build-deb.sh` bleibt gruen (Syntax),
* der postinst-Text enthaelt `--break-system-packages`,
* der postinst-Text enthaelt **kein** `venv` (auch kein `python3-venv`, kein `~/.local/stt-button/venv`),
* die bestehenden Tests dieser Datei bleiben gruen.
Tests lesen nur das Skript bzw. fuehren `bash -n` aus — **kein** Deb-Build, kein `dpkg-deb`,
kein Installieren, kein Netzwerk.

## TDD + Abschluss
Test ZUERST (ROT: postinst nennt noch venv), dann Skript (GRUEN). Blocktests dieser Datei
laufen lassen. **Keine VolSuite** (die ist wegen eines separaten, praedatierten Timing-Tests
vorgezogen in eigener Arbeit — der Orchestrator bucht das separat).
Commit: `fix(packaging): postinst guides the user-site install`

## Erlaubte Dateien
`tools/build-deb.sh`, `tests/test_setup_script.py`
Verboten: `src/**` (insb. `__main__.py` = V4, `bootstrap.py` = V7), `packaging/**` (V5, laeuft
parallel — Finger weg von `control`!), `systemd/**`, `README.md`/`CHANGELOG.md` (V7),
`pyproject.toml`, `tools/deps_gate.py`, `src/stt_button/deps_gate.py`; kein sudo/apt/dpkg,
kein Netzwerk, keine Tastatur-/Clipboard-/Ton-Emission, **kein git push**, kein
Dienst-Neustart, kein Schreiben unter `~/.config/`.

## Parallelitaet + Stall-Prophylaxe (verbindlich)
* Zwei andere Kinder laufen gerade: `tests/test_controller.py` (Langlauf) und
  `packaging/debian/control`. Du fasst keine davon an.
* postinst-Block nur in Stuecken <= 80 Z. lesen, aendern mit `edit`.
* KEINE `while pgrep`-Wartespruenge, KEINE 20x-Schleifen, KEIN conftest.py-Gesuche.
* Wenn dein letzter Schritt ein Werkzeug war und dir nichts mehr bleibt: melde das in EINER
  Zeile, statt schweigend zu enden.
* Schlussmeldung max 5 Zeilen (Sha, N passed, tatsaechlich gelaufenes Modell).
