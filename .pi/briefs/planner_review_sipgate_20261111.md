# Planungsauftrag (Neubewertung) — sipgate Coding Ultra als Planungsinstanz

## Lage in zwei Saetzen
Die gesamte bisherige Planung dieses Projekts stammt nachweislich aus Sitzungen des Modells
`my-qwen38-flash-next` (Sitzungsdateien: 204 bzw. 40 Modell-Eintraege, ausschliesslich dieses
Modell — unabhaengig davon, welche Modellnamen die Aufrufmeldungen nannten). Der Betreiber hat
entschieden: **nichts verwerfen**, sondern du als Planungsinstanz **bewertest die vorhandene
Planung neu**, und was du aendern willst, setzt der Orchestrator um.

## Was bereits gilt (unbestritten, von dir nicht neu aufzurollen)
* Produktentscheidung: Backend bleibt `parakeet`; XPU/Qwen ist Zusatzpfad.
* Runtime-Weg B ist Betreiber-Beschluss: **kein venv**, `/usr/bin/python3` (3.12.3) +
  `pip install --user --break-system-packages` nach `~/.local/lib/python3.12/site-packages`.
* TDD-Pflicht (erst rot, dann gruen), Testkommando:
  `nice -n 19 .venv/bin/python -m pytest -q -p no:randomly -o addopts=""` (venv nur fuer Tests).
* Python 3.12, src-Layout, Dokus/Docstrings englisch, Repo-Kommentare deutschen Stil beibehalten.
* Suite-Baseline nach der Haertung: **879 passed, 2 skipped, 0 failed**, Kollektion 881.
* Produktion laeuft live aus dem Repo (`.venv`, `ExecStart=.../.venv/bin/stt-button -v run`) und
  wird gerade zum Diktieren benutzt.

## Deine Aufgabe
Bewerte die folgenden Unterlagen und entscheide, was zu aendern ist. **Du aenderst keinen Code.**

1. `.pi/plans/plan_no_venv_B.md` — Arbeitsgrundlage der V-Serie (V1–V8, umgesetzt, Commits
   `44a4a31`…`0fd73ac`, Release 0.2.1 `da717a6`).
2. `.pi/plans/plan_fh2_flake_followup.md` — Flake-Nachfolge (FH2-A…D, **nichts umgesetzt**).
3. `.pi/reports/fh1_verification.md` — Betreiber-Ground-Truth zur Haerung `f6971dd` (massgeblich).
4. `.pi/reports/flake_hardening.md` — Haerungs-Bericht (in Teilen durch 3 widerlegt).
5. `.pi/reports/dep_review_0.2.0.md` — Abhaengigkeitspruefung.
6. `.pi/briefs/install_script_findings.md` — **vier heute gemessene Befunde A–D** (mit
   Kommandos/Ausgaben). Dazu musst du dich ausdruecklich verhalten, siehe Punkt "Pflicht-Entscheide".

## Pflicht-Entscheide (bitte numeriert beantworten)
* **E1 (`deps_gate.py`)**: `REQUIRED_MODULES` enthaelt `pyaudio`, `SHADOW_MODULES` auch. Der Code
  importiert es nie (Aufnahme laeuft ueber das externe Programm `pw-record`, `src/stt_button/audio.py:3,88`),
  es steht nicht in `packaging/runtime-requirements.txt`, war nie im `.venv`. Folge: Gate-Level 0
  — und damit die hartere Bedingung vor der Paket-Installation — ist **unerfuellbar**.
  Entscheide: Streichen, oder durch die tatsächlich importierten Module ersetzen
  (`numpy`, `faster_whisper`, `onnx_asr`, `requests`)? Was folgt fuer `SHADOW_MODULES`
  (Level-3-Erkennung apt-installierter Bibliotheken)? Welche Tests muessen neu rot werden?
* **E2 (Launcher)**: `systemd/stt-button-launcher.sh:16` prueft `import sounddevice, numpy,
  stt_button`; `sounddevice` existiert nirgends und wird nicht benutzt — gemessen:
  `ModuleNotFoundError`. Nach dem Unit-Tausch wuerde der verpackte Dienst exit 1 liefern.
  Entscheide die exakte Pruefmenge und einen Test, der die Pruefung **ausfuehrt** (nicht
  Text liest), ohne das echte System zu veraendern.
* **E3 (Pip-Hint)**: Hint nennt `'/opt/stt-button[stt]'` und `$ROOT/requirements-stt.txt`;
  beides existiert im Paket nicht (gemessen 0 Treffer im Deb). Entscheide den korrekten Wortlaut.
* **E4 (Reihenfolge)**: `--only`-Spruenge pruefen nur Spuren des vorigen Schritts. Betreiber hat
  R6 ohne R5 gestartet — korrekter Abbruch wegen fehlender Unit-Quelle. Entscheide, ob R5 das
  Gate selbst erneut ausfuehren muss statt auf R4 zu vertrauen.
* **E5 (FH2-Kette)**: Halten, aendern oder verwerfen? Falls halten: Reihenfolge FH2-A…D bestaetigen
  oder aendern, und akzeptierst du die Zahlen-Vorgaben des Plans als Abnahme?
* **E6 (Restrisiko)**: Was bleibt aus deiner Sicht nach A–D offen (z. B. das im Skript bekannte
  Loch, dass nach Rueckspielern der Sicherung die Gesundheitspruefung nicht wiederholt wird)?

## Ergebnisformat
Eine Datei `.pi/plans/plan_review_sipgate_20261111.md`, in **mehreren kleinen Teilschritten**
geschrieben (kein einzelner Gross-Write), mit:
* Je Unterlage: `behalten` / `aendern` / `verwerfen` + Ein-Satz-Begruendung.
* Danach: **Blockliste zum Umsetzen** — je Block: ID, Zweck, betroffene Dateien, Testverpflichtung
  (was muss zuerst rot sein), Abnahmekriterium mit messbarer Zahl. Reihenfolge so, dass die
  Installation des Pakets 0.2.1 so frueh wie moeglich wieder moeglich wird.
* Danach: die numerierten Antworten E1–E6.
Kein Code, kein `git`, kein sudo/apt/pip/systemctl/dpkg, kein Netzwerk, keine Schreibgriffe unter
`~/.config`, keine Tastatur-/Clipboard-/Ton-Aktionen. Schlussmeldung max 12 Zeilen und nennt das
**tatsaechlich gelaufene** Modell.
