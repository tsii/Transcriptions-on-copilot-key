# Mini-Auftrag — sechs Entscheidungen, max 60 Zeilen gesamt

Dein vorheriger Auftrag war zu gross: du hast vier Unterlagen gelesen und bist beim Schreiben
hngen geblieben (zwei stille Abbrche). Dieser Auftrag ist bewusst winzig. **Du liest keine
einzige Datei.** Alle ntigen Fakten stehen hier. Du triffst nur sechs Entscheidungen.

## Fakten (gemessen, akzeptiere sie als gegeben)
* Runtime-Weg B (Betreiber-Beschluss, unbestritten): kein venv, `/usr/bin/python3` 3.12.3,
  `pip install --user --break-system-packages` nach `~/.local/lib/python3.12/site-packages`.
* R1 (purge), R2 (python3-pip), R3 (torch 2.14.1+cpu + alle 60 Pins) sind erledigt.
* **Befund A**: `src/stt_button/deps_gate.py` Zeile 32/33 verlangt `pyaudio` in
  `REQUIRED_MODULES` und `SHADOW_MODULES`. Der Code importiert es nie (Aufnahme = externes
  Programm `pw-record`, `src/stt_button/audio.py:3,88`). Es steht nicht in
  `packaging/runtime-requirements.txt` und war nie im `.venv`. Gate meldet Level 1,
  Level 0 ist unerreichbar, damit blockiert die hartere Pruefung vor der Paket-Installation.
* **Befund B**: `systemd/stt-button-launcher.sh:16` prueft `import sounddevice, numpy,
  stt_button`. `sounddevice` existiert nirgends und wird nicht benutzt. Gemessen:
  `ModuleNotFoundError`. Nach dem Unit-Tausch wuerde der verpackte Dienst exit 1 liefern.
  Ohne `sounddevice` ist alles Importierbare: `numpy, faster_whisper, onnx_asr, requests`.
* **Befund C**: derselbe Launcher nennt im Pip-Hint `'/opt/stt-button[stt]'` und
  `$ROOT/requirements-stt.txt`; beides ist nicht im Paket (gemessen 0 Treffer). Vorhanden im
  Paket: `/opt/stt-button/packaging/runtime-requirements.txt`.
* **Befund D**: `--only`-Spruenge pruefen nur Spuren des vorigen Schritts. Der Betreiber hat
  R6 ohne R5 gestartet — korrekter Abbruch wegen fehlender Unit-Quelle. Der Skript-Experte hat
  nachgemessen: `step_r5` fuehrt das Gate selbst erneut aus, traegt keinen R4-Zustand.
  Er hat zusaetzlich `--permissive` eingebaut (opt-in; toleriert exakt `- not importable: pyaudio`,
  Level genau 1, keine Versionskonflikte). Commit `ca872be`, 9/9 Tests gruen, Dry-Run ohne sudo.
* **Befund E (aus der Flake-Kette, unveraendert gelten)**: Haertung `f6971dd` haellt fuer FL1/FL4
  (VolSuite 879 passed / 2 skipped / 0 failed, Kollektion 881). FL2 bleibt statistisch wackelig
  (2/20 + 1/10 rot). Der globale Test-Hebel `STT_TEST_READ_DELAY` bricht drei fremde Tests.
  Der Nachfolge-Plan `.pi/plans/plan_fh2_flake_followup.md` (FH2-A…D) ist unumgesetzt.

## Arbeitsweise (Pflicht, wegen deiner beiden Abbruche)
Datei: `.pi/plans/plan_review_sipgate_20261111.md`.
Sieben eigene, winzige Schreibschritte, je einer pro Punkt, und **der erste Schreibschritt ist
der allererste Aufruf ueberhaupt** (kein Lesekommando vorher):
1. `write`: Kopf, zwei Zeilen.
2. `patch` ans Ende: `E1 Gate` — Entscheidung + Begruendung, max 6 Zeilen.
3. `patch`: `E2 Launcher` — max 8 Zeilen, inklusive der einen Zeile, was der neue Test
   ausfhren (nicht lesen) muss.
4. `patch`: `E3 Pip-Hint` — max 4 Zeilen, wuertlicher neuer Wortlaut.
5. `patch`: `E4 Reihenfolge` — max 5 Zeilen: gengst du mit `step_r5`-Gate-Wiederholung,
   und bleibst du bei Level 0 hart (dann ist `--permissive` nur Noethilfe bis zum Gate-Fix)?
6. `patch`: `E5 FH2` — max 6 Zeilen: Kette behalten/aendern/verwerfen, Reihenfolge FH2-A…D
   bestaetigen oder aendern, Zahlen-Vorgaben annehmbar ja/nein.
7. `patch`: `E6 Restrisiko` + Votat je Unterlage in je einer Zeile — max 12 Zeilen.

Kein Code, kein git, kein sudo/apt/pip/systemctl/dpkg, kein Netzwerk, keine Schreibgriffe unter
`~/.config`, keine Tastatur-/Clipboard-/Ton-Aktionen. Nach Schritt 7 sofort Schlussmeldung:
max 8 Zeilen, sechs E-Antworten in je einem Halbsatz, dazu das tatsaechlich gelaufene Modell.
