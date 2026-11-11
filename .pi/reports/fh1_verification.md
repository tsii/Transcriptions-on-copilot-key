# FH1 — eigene Abnahme des Orchestrators (Puls 2026-11-11 13:10–13:20 UTC)

Repo: STT-Button, `master`. Commit unter Pruefung: `f6971dd`
`test(controller): make capture-timing tests deterministic (FL1-FL4)`
(gepusht, `origin/master` = `f6971dd`; Inhalt = exakt derFH1-Arbeitsstand, den ich
vor dem Commit im Working-Tree getestet habe — `git diff HEAD` nach dem Commit leer).
Dateien im Commit: nur `tests/test_controller.py` (+76/−12) und
`.pi/reports/flake_hardening.md` — keine fremden Dateien, keine skip/xfail-Devices.

Getestet ausschliesslich mit: `.venv/bin/python -m pytest -q -p no:randomly -o addopts=""` (P2).

## 1. Was haelt (VERIFIZIERT)

* **FL4** (`TestStreaming::test_text_reaches_typer`) und **FL1**
  (`TestLiveAsrUntilController::test_ticks_stop_after_limit_final_text_complete`):
  Hebel reproduziert die alte Testkoerper wirklich. Alte Version (per
  `git stash`) + `STT_TEST_READ_DELAY=0.05`, gezielte Auswahl:
  `2 failed, 5 passed` — FL4-Signatur und FL1-Signatur
  (`('type','...')` + naive Verkettung → `endswith` false) wie im Report beschrieben.
* **Fix wirkt**: neue Version ungebremst 5/5 und 7/7 gruen; Modul Volllauf
  ungebremst **9/10 grün** (1× FL2, s. u.).
* **VolSuite im Leerlauf** (nach `f6971dd`, `nice -n 19`):
  **879 passed, 2 skipped, 0 failed** in 108,81 s. Kollektion: **881**
  (Hausnummer des Monitors "843 vor FH1" ist ueberholt).
* Commit-Botschaft und Report nennen FL2 offen — das ist ehrlich dokumentiert.

## 2. Befunde, die den Report entwaerten (PLANLUECKE — nicht selbst entschieden)

### B1  FL2 ist NICHT geheilt und flakt weiter (P1)
Report: "FL2 nicht reproduzierbar (4/4 gruen gebremst; async-Deadline existiert
bereits)" → unveraendert gelassen. Meine Messung:

| Lauf (NEUER Code, ungebremst) | Ergebnis |
|---|---|
| `TestAudioDumpModes::test_async_writes_wav_from_snapshot` 20× einzeln | **2/20 rot** |
| Modul `tests/test_controller.py` 10× Volllauf | **1/10 rot** (Run 9: genau FL2) |

FL2 flakt also auch nach FH1 in der Hauptkonfiguration (ungebremst). Die
Begruendung "4/4 gruen mit Bremse" beweist nichts, weil der Hebel FL2 per
Annahme nicht trifft — Repro-Attempt und Abnaehmekonfiguration sind nicht
derselbe Raum. Brief verlangte HAERTUNG von FL1-FL4, nicht 3 von 4.

Zusaetzlich: alte Version (Periode mit Bremse, Modul Volllauf, 6 Laeufe) zeigt
FL2 **1× rot** (Run 6) — die Report-Aussage "nicht reproduzierbar" haelt also
auch in dem Kontext nicht, in dem FL3 ueberhaupt fiel.

### B2  FL3-Reproduktion ist nicht reproduzierbar (P2)
Report signiert FL3 mit `state == RECORDING != IDLE`. Meine Messung: alte Version
+ Bremse, einzeln **8/8 gruen** (sollte nach Report rot sein). FL3 faellt nur in
der Modul-Volllauf-Kontextstichprobe (1/6, Run 2, Assertion
`tests/test_controller.py:421`). Der dokumentierte Signatur-Nachweis ist damit
nicht die Behauptung, die er zu sein vorgibt.

### B3  Der Hebel bricht 3 weitere Tests — "gebremst gruen" ist unerreichbar (P1)
NEUER Code, Modul Volllauf **mit** Bremse 0.05: **8/8 Laeufe rot**, konstant
dieselben 3, die FH1 nicht angefasst hat:

* `TestCaptureThread::test_audio_not_lost_during_blocking_inference`
  (`time.sleep(0.3)` dann starr `qsize() == 50`; mit Bremse schaffen 0.3 s nur
  ~6 Chunks)
* `TestLatencyLockDecoupling::test_release_during_live_asr_stops_audio_before_inference_returns`
  (`elapsed < 0.3`-Budget)
* `TestLiveLatencyTrace::test_live_tick_points_logged`

Ursache: `STT_TEST_READ_DELAY` ist global und drosselt die geteilten Fakes
(`FakeAudioSource`/`RestAudioSource`), haelt aber alle anderen Tests mit
fester Zeitanahme mit. Konsequenz: die Brief-Forderung "jeder Test ungebremst
UND gebremst gruen" ist mit diesem Hebel-Werkzeug fuer das ganze Modul
unerfuellbar — der Report-Beleg "gebremst 9/9" stammt aus einer gezielten
Auswahl, nicht aus einem Volllauf.

## 3. Offene Entscheidungen (gehoeren zum Planer, nicht zum Orchestrator)

1. FL2: eigener Hebel (async-/Snapshot-Pfad) + Haertung, oder bewusst aus dem
   FH1-Brief herausloesen und als eigenen Block FL5 setzen?
2. Hebel-Design: globaler Env-Hebel auf geteilten Fakes ersetzt — Alternative:
   pro Test per Kontextmanager/Klasse, damit Nicht-Zeit-Tests nicht mitgezogen
   werden. Betrifft auch die 3 Kollateralfaelle.
3. Akzeptanzregel "je Flake Zeithebel-Reproduktion": gilt sie weiter, obwohl
   FL2/FL3 mit diesem Hebel nicht reproduzierbar sind? Wenn ja: FL2/FL3
   Repro-Wege spezifizieren. Wenn nein: Report-Korrekturen (B2) nachziehen.
4. Die 3 Kollateralfaelle sind echte versteckte Zeitannahmen im Testcode —
   eigenener Haertungsblock oder Hebel-Umbau zuerst?

## 4. Randnotiz (keine FH1-Wirkung, Betreiber-Info)

Service wurde 13:06:42 `Stopping` → 13:07:53 `Started` (NRestarts=0, also
manueller Restart, Grund aus Journal nicht ersichtlich), 2 min vor dem FH1-
Commit. Produktion laeuft unveraendert aus dem Repo
(`.venv/bin/stt-button -v run`), active. 13:11 war ein Live-Diktat
(`wl-copy` im CGroup) — Betreiber nutzt das System gerade.
