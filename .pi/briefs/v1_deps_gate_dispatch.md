# Dispatch V1 — Mess-Gate `tools/deps_gate.py` (Kleinpaket: EIN Tool + EINE neue Testdatei)

Repo `/home/salmon/pi-web-ui/STT-Button`. Plan: `.pi/plans/plan_no_venv_B.md` — **nur die
V1-Zeile in Zeile 62** lesen (plus §1 GROUND TRUTH, Zeilen 9-20). NIEMALS den ganzen Plan oder
`src/` komplett (damit sind Laeufe gestorben).

## Vorgabe des Planers (Zeile 62, inhaltlich woertlich)
`tools/deps_gate.py` (NEU) prueft:
* pip-Vorhandensein unter `/usr/bin/python3`,
* Import der Pflicht-Module unter `/usr/bin/python3` mit `--src`-Pfad
  (Default `/opt/stt-button/src`; **vor der Paket-Installation ist die Repo-Kopie der
  vorgesehene Weg**, also `--src <repo>/src`),
* Versionsvergleich gegen `--requirements` (Default
  `/opt/stt-button/packaging/runtime-requirements.txt`; **dieser Pfad existiert bis R5 nicht**),
* ob `dist-packages` `numpy`/`scipy`/`soundfile`/`pyaudio` enthaelt (Shadow-Risiko).

**Geschlossene Exitlevel-Menge** (exakt so, keine eigenen Level erfinden):
| Level | Grund |
|---|---|
| 0 | alles gut |
| 1 | `pip_missing` oder `deps_missing` |
| 2 | `requirements_missing` (Datei nicht gefunden — genau die R1/R3-Verwechslung) |
| 3 | `shadow_risk` |

Schreibt additionally `.pi/reports/deps_gate.md` (Kurzwiedergabe der Befunde).

## Testpflicht — NEUE Datei `tests/test_deps_gate.py`
* heute (Ist-Zustand dieses Rechners): `pip_missing` => Exitlevel **1**
* `--requirements /nonexistent` => **`requirements_missing`/Level 2** und ausdriuecklich
  **nicht** `deps_missing`
* Fake-Umgebung mit allem vorhanden => 0
* Fall mit `numpy` in `dist-packages` => `shadow_risk`/3
Die Tests faehren das Gate **in-process oder per Subprocess gegen `/usr/bin/python3` mit
Temp-Verzeichnissen** — kein echtes Netzwerk, kein `sudo`, kein apt, kein pip, kein Schreiben
außer in `tmp_path` (`.pi/reports/deps_gate.md` im Test auf einen tmp-Pfad umlenken).

## TDD + Abschluss
Testdatei ZUERST (ROT: Tool fehlt), dann Tool (GRUEN). VolSuite **hoechstens einmal**:
`nice -n 19 .venv/bin/python -m pytest -q -p no:randomly -o addopts=""` (Baseline
**821 passed / 2 skipped**; FL1 `test_ticks_stop_after_limit_final_text_complete` +
FL2 `test_async_writes_wav_from_snapshot` = bekannte praedatierte Flakes, nur melden).
Commit: `feat(tools): dependency gate for venv-free runtime`

## Erlaubte Dateien
NEU `tools/deps_gate.py`, NEU `tests/test_deps_gate.py` (Report `.pi/reports/deps_gate.md`
entsteht zur Laufzeit, nicht committen — .gitignore-prüfen, sonst Report weglassen).
Verboten: `src/**`, `systemd/**`, `packaging/**`, `__main__.py`, `tools/` sonst, `pyproject.toml`,
`CHANGELOG.md`, `README.md`; kein sudo/apt/dpkg/pip-Install, kein Netzwerk, keine
Tastatur-/Clipboard-/Ton-Emission; **kein** `git push`.

## Stall-Prophylaxe (verbindlich)
* Dateien nur in Stuecken <= 80 Z. lesen; KEIN Voll-`write` einer bestehenden Datei.
* KEINE `while pgrep`-Wartesprleifen, KEINE 20x-Testschleifen, KEIN conftest.py-Gesuche.
* Wenn dein letzter Schritt ein Werkzeug war und dir nichts mehr bleibt: melde das in EINER
  Zeile, statt schweigend zu enden (drei Laeufe sind daran gestorben).
* Nach zwei erfolglosen Schleifen: abbrechen, Fehlerausgabe in <=4 Zeilen zitieren, Baum liegen
  lassen. Schlussmeldung max 5 Zeilen (Sha, N passed, tatsaechlich gelaufenes Modell).
