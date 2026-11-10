# Planer-Auftrag: Runtime ohne venv (Weg B) — TDD-Blockplan

## Beschluss des Betreibers (2026-11-10 22:44, wörtlich)
> "Wir setzen auf Weg B: Ubuntu-System-python3"

Kein venv mehr, kein Self-Healing-Bootstrap. Basis = `/usr/bin/python3` aus dem
Ubuntu-Paket; Drittbibliotheken per `pip install --user --break-system-packages`
nach `~/.local/lib/python3.12/site-packages`.

## Inputs (bitte NUR diese lesen — nicht den Summary-Plan, nicht src/ ganz)
1. `.pi/briefs/decision_no_venv_B.md` — Entscheidung + eigene Messwerte (PEP-668-Marker,
   pip fehlt komplett, user-site aktiv, `python3-torch` ohne Kandidat, numpy 1.26 vs Lock 2.5.4)
2. `.pi/reports/dep_review_0.2.0.md` (45 Z.) — Gutachten des Oracle-Experten, von mir
   stichprobenartig verifiziert: `bootstrap.py:41` `TORCH_SPECS = ("torch==2.14.1+cpu", ...)`,
   `packaging/runtime-requirements.txt:34` `numpy==2.5.4`, `systemd/stt-button.service:15`
   `TimeoutStartSec=1800`, `systemd/stt-button-launcher.sh:23` `PYTHONPATH="$ROOT/src"`,
   `__main__.py:388-389` (setup biegt Unit auf `/usr/bin/stt-button run` um, sobald
   `/opt/stt-button` existiert), `intel-oneapi-pytorch` nur 2.0.1.0
3. Zu prüfen bereit: `src/stt_button/bootstrap.py`, `systemd/stt-button-launcher.sh`,
   `systemd/stt-button.service`, `packaging/debian/control`, `tools/build-deb.sh`,
   `_cmd_setup` in `src/stt_button/__main__.py` (Zeile 330-405)

## Harte Randbedingungen (gemessen, nicht verhandelbar)
- **Kein Root durch Agenten.** `sudo apt install -y python3-pip` und
  `sudo dpkg --purge stt-button` (Paket steht auf `0.2.0 state=unpacked`) macht der Betreiber.
- **Produktion läuft weiter aus dem Repo** (`ExecStart=.../STT-Button/.venv/bin/stt-button -v run`,
  Dienst gesund). Jeder Plan-Schritt muss rückholbar sein, und **nichts** darf die
  Repo-Produktion beim nächsten Neustart brechen — insbesondere darf `stt-button setup`
  nicht laufen, bevor Launcher + Unit tragfähig sind.
- Suite nur mit `nice -n 19 .venv/bin/python -m pytest -q -p no:randomly -o addopts=""`
  (Baseline **821 passed / 2 skipped**, FL1/FL2 = praedatierte Flakes).
- Produktion bleibt `backend = "parakeet"`, `summary_enabled` bleibt false.
- Kleinpaket: ein Block = eine Funktion ODER eine Testdatei; Ziel >300 Z. => neue Testdatei.

## Was der Plan entscheiden muss (Punkte 1-2 sind die eigentlichen Designfragen)
1. **numpy-Haerte — die Kernfrage von Weg B:** Der Lock pinnt `numpy==2.5.4`, Ubuntu liefert
   1.26.4, und `asr.py` importiert numpy an >=4 Stellen. `--user`-Installation von numpy 2
   **überdeckt** user-seitig die Distro-Version und kann gegen 1.26 gebaute Distro-Erweiterungen
   (`python3-soundfile`, `python3-scipy`, `python3-pyaudio`) brechen — für den ganzen Nutzer.
   Entscheide mit Begründung:
   (a) numpy 2 mit-installieren und Risiko akzeptieren (welche Tests belegen, dass unsere
   Audio-Pfade nicht leiden?),
   (b) `numpy<2` erzwingen — hält dann `torch==2.14.1+cpu` / `ctranslate2==4.8.4` /
   `transformers==5.20.2` (ABI-Frage), und
   (c) System-numpy ganz meiden, indem `PYTHONNOUSERSITE`/eigener Site-Pfad pro Dienst gesetzt wird.
   Wenn Du es nicht messen kannst (PyPI-Zugriff im Agenten unklar), definiere einen
   **Prüfblock**, der die Entscheidung auf dem Rechner messtechnisch fällt, bevor umgestellt wird.
2. **Schicksal von `src/stt_button/bootstrap.py`:** behalten für Repo-Entwicklung (dann aber
   aus dem Pfad von Launcher/`setup`/Unit heraus) oder zurückbauen? Begründe inkl. der Frage,
   was `stt-button setup` ohne venv-Bau noch leisten soll (Prüfen? Anleiten? Nichts?).
3. **Konkret umzustellende Stellen** (Block für Block, mit Testpflicht):
   - `systemd/stt-button-launcher.sh`: Bootstrap-Block (:23-31) raus, Import-Check + klarer
     pip-Hint, `exec python3 -m stt_button "$@"`; **kein** `PYTHONPATH`-Export ins Kind
     (sonst `~/.local`-Shadowing unkontrolliert) — prüfe, ob das mit `/opt/stt-button/src`
     anders gelöst werden muss (`PYTHONPATH` gezielt nur für das Paket).
   - `systemd/stt-button.service`: `ExecStart`-Ziel, `TimeoutStartSec=1800` + Kommentar weg.
   - `packaging/debian/control:7`: `python3-venv` raus; `python3-pip` rein oder raus?
   - `tools/build-deb.sh` `postinst`-Text (build-deb.sh:97-109) auf Weg B.
   - `_cmd_setup` (`__main__.py:330-405`): kein venv-Bau; Unit-Schreibschutz, solange der
     Launcher nicht tragfähig ist (Gefahr: Repo-Produktion).
   - Doku: README-Abschnitt "Installation ohne venv", CHANGELOG 0.2.1, `config.example.toml`?
4. **Reihenfolge + root-Zeilen für den Betreiber** als eigene Sektion: was muss in welcher
   Reihenfolge passieren (purge → apt python3-pip → pip --user libs → neue Unit → Neustart),
   damit es zu keinem Zeitpunkt einen toten Dienst gibt. Vor jedem Schritt die Rückhol-Anweisung.
5. **Verifikation ohne Root:** welche Belege liefern Agenten (Test-Suite, `python3 -c
   "import stt_button"` mit gesetztem PYTHONPATH gegen `/opt/stt-button/src`,
   `pip list --user`-Vergleich gegen den Lock, `systemd-analyze verify` der Unit), und welcher
   Beweis bleibt beim Betreiber.

## Lieferform (Pflicht, zwei Plan-Laeufe sind an großen Einzel-Schreiben gestorben)
- Datei `.pi/plans/plan_no_venv_B.md`, **in mindestens 6 kleinen Schreibschritten**
  (write für Kopf, dann edit/patch je <=25 Zeilen), Umfang **max 200 Zeilen**.
- Blöcke nummeriert `V1..Vn`, je Block: Ziel, Dateien, Testdatei (ROT→GRÜN), Commit-Titel,
  Rückholweg. Kein Block darf zwei große Dateien anfassen.
- Am Ende: `VERDICT: GO/NO-GO` + die root-Zeilen in Ausführungsreihenfolge.
- Schlussmeldung max 8 Zeilen (Blöcke, numpy-Entscheidung, VERDICT, Modell).
