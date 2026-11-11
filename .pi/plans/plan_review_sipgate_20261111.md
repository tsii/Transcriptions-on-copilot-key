# Plan Review Sipgate 2026-11-11 — E1–E6
Laufmodell: cortecs/glm-5.3 (Fallback, Primärlauf tot)
## E1 Gate
Entscheidung: `pyaudio` aus `REQUIRED_MODULES` und `SHADOW_MODULES` in `deps_gate.py` streichen.
Begruendung: Code importiert es nie (Aufnahme via pw-record), es war nie in venv oder
runtime-requirements.txt — der Gate-Block ist ein Fake-Fehler, der Level 0 unmoeglich macht.
## E2 Launcher
Entscheidung: Zeile 16 auf `import numpy, stt_button` kuerzen; `sounddevice` weg.
Begruendung: `sounddevice` existiert nirgends, wuerde den Dienst nach Unit-Tausch mit
ModuleNotFoundError/exit 1 toeten. Neuer Test (ausfuehren, nicht lesen): der Launcher-Check
wird in einem subprocess mit PYTHONPATH=src ausgefuehrt und muss Exit-Code 0 liefern.
## E3 Pip-Hint
Neuer Wortlaut: `pip install --user --break-system-packages -r /opt/stt-button/packaging/runtime-requirements.txt`
## E4 Reihenfolge
Gate-Fix (E1) zuerst, dann R5 erneut — die `step_r5`-Gate-Wiederholung ist akzeptabel, weil
kein R4-Zustand getragen werden muss. Level 0 bleibt hart; `--permissive` (Commit ca872be)
nur Nothilfe bis der Gate-Fix steht, danach obsolet.
## E5 FH2
Kette behalten, Reihenfolge FH2-A…D bestaetigt; Zahlen-Vorgaben annehmbar.
Begruendung: FL1/FL4 gehaertet, FL2 statistisch wackelig (2/20 + 1/10 rot) —
`STT_TEST_READ_DELAY` bricht drei fremde Tests, daher nur gezielt, nie global.
## E6 Restrisiko
FL2 bleibt statistisch wackelig; FH2-A…D unumgesetzt; `--permissive` kann exakt den
einen Fehler tolerieren, verdeckt aber nicht den Gate-Fix-Bedarf. Runtime-Weg B
unbestritten. Votat je Unterlage:
- Befund A: pyaudio aus Gate streichen (fix).
- Befund B: Launcher-Check ohne sounddevice (fix + Test).
- Befund C: Pip-Hint auf runtime-requirements.txt umlenken (fix).
- Befund D: step_r5-Gate-Wiederholung ok, Level 0 hart, --permissive Nothilfe.
- Befund E: FH2-Kette behalten, Reihenfolge A…D, Zahlen ok.
## E2b Testform fuer den Launcher

Entscheidung: Weg (ii). Der Test parst die gepruefte Modulmenge aus dem
Launcher-Skript, verlangt exakt `{numpy, stt_button}` und fuehrt den
Launcher-Check gegen eine tmp-PYTHONPATH aus, die Stubs fuer ALLE geprueften
Module enthaelt. Damit besteht keine Abhaengigkeit zur Testumgebung (auch kein
echtes numpy); Weg (i) bliebe vom numpy-Vorhandensein der Testumgebung
abhaengig, Weg (iii) prueft nur Text statt Verhalten. Nur (ii) bleibt auf
frischer Maschine und im CI stabil gruen, weil der Launcher-Interpreter
in der tmp-Kopie neutralisiert wird und alle Importe aus Stubs stammen.
