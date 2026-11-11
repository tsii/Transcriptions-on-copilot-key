# FH1 — Flake-Härtung FL1–FL4 (test-only)

Datum: 2026-11-11 · Repo: STT-Button, `master` · Zuständig: FH1-Kind (TDD-Kette).
Betroffene Datei: `tests/test_controller.py` (nur Testcode + Hilfsfunktionen).
Kein Produktcode verändert.

## Hebel (Reproduktionswerkzeug, test-only)

`STT_TEST_READ_DELAY` (Env) drosselt `source.read()` in `FakeAudioSource` und
`RestAudioSource` um den gesetzten Wert (z. B. `0.05`). Zusätzlich zählt jedes
Fake `delivered` (an den Capture-Thread übergebene Chunks). Neue Helfer
`_wait_delivered(source, n, timeout=5.0, what="")` wartet mit Deadline auf die
tatsächliche Queue-Füllung; überschrittene Deadline = expliziter AssertionError
mit klarer Meldung (kein Stillschweigen).

## Reproduktion (ROT, Hebel `STT_TEST_READ_DELAY=0.05`, alte Testkörper)

| Flake | Test | ROT-Signatur (gebremst) |
|---|---|---|
| FL4 | `TestStreaming::test_text_reaches_typer` | `any(type)` false — `engine.calls=0`, Queue bei `run_once`-Burst leer |
| FL4-Nachbar | `TestStreaming::test_run_once_feeds_and_transcribes` | `engine.calls >= 1` verletzt, `calls=0` |
| FL3 | `TestErrors::test_engine_error_aborts` | `state == RECORDING != IDLE` — nie genug Audio für Transkription/Abort |
| FL1 | `TestLiveAsrUntilController::test_ticks_stop_after_limit_final_text_complete` | G26-Signatur: `('type','...')` Pause-Indikator + naive `type`-Verkettung ohne Backspace-Abzug → `endswith("w0 … w11")` false. Trefferquote ungebremst ca. 1/3 (Wiederholungsläufe), gebremst erstmals im Volllauf-Kontext gesehen |
| FL2 | `TestAudioDumpModes::test_async_writes_wav_from_snapshot` | **nicht reproduzierbar** (4/4 grün gebremst; async-Deadline 5 s existiert bereits) |

## Fixes (GRÜN)

* FL4 + Nachbar: `_wait_delivered(source, 2)` (1.0 s ≥ `min_audio_s` 0.8 s),
  dann EIN `run_once()` statt 2–3 blinde Ticks.
* FL3: `_wait_delivered(source, 2)` vor den Ticks; Abort feuert deterministisch.
* FL1: Phasengliederung — (1) unter dem Limit `delivered>=2` + 1 Tick ⇒
  `engine.calls>=1` (nicht-vakuant), (2) Limit auf 1.5 s ziehen,
  (3) `delivered>=12` + `sleep(0.12)` (Interval-Gate kann Limit nicht maskieren),
  3 weitere Ticks ⇒ keine weiteren Engine-Calls; Auswertung über
  `typer.typed_text_final` (Backspace-bewusst) statt naiver Verkettung;
  explizite IDLE-Deadline-Assertion ergänzt.
* FL2: unverändert (nicht reproduzierbar, s. o.) — als offen dokumentiert.

GRÜN-Nachweis beide Varianten (5 Tests, `TestStreaming`/`TestErrors`/FL1):
ungebremst 9/9, gebremst (`0.05`) 9/9.
