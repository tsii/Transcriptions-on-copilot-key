# Dispatch FH1 — Flake-Haertung FL1-FL4 (test-only, EINE Datei)

Repo `/home/salmon/pi-web-ui/STT-Button`. Plan: `.pi/plans/plan_summary_mode_v2.md` **§5,
FH1-Zeile (Zeile 423)** — nur diese Zeile lesen. Kein Kind bekommt einen Plan ganz.

## Vorgeschichte (vom Orchestrator gemessen, Planer-Entscheid (1)=a)
Vier vorbestehende Timing-Tests in `tests/test_controller.py` fallen nicht nur unter Last:

| # | Test | Gemessene Rate |
|---|---|---|
| FL4 | `TestStreaming::test_text_reaches_typer` (`:336-343`) | **3/5 einzeln im Leerlauf** |
| FL1 | `TestLiveAsrUntilController::test_ticks_stop_after_limit_final_text_complete` (G26-Signatur: literales `'...'` im getippten Text + `('backspace','3')`) | faellt in VolLaeufen, einzeln meist gruen |
| FL2 | `TestAudioDumpModes::test_async_writes_wav_from_snapshot` | 3/3 einzeln gruen, in VolLaufen schon geplatzt |
| FL3 | `TestErrors::test_engine_error_aborts` | Leerlauf 5/5 gruen, unter Parallel-Last 2/5 rot |

**Wurzel (diagnostiziert + vom Orchestrator verifiziert):** der Capture-Thread notified unter
`self._lock` (`src/stt_button/controller.py:461-462`) und verhungert gegen einen sofortigen
`run_once()`-Burst des Hauptthreads. Der Puffer bleibt dann bei 0,5 s, `asr.py:808`
(`audio_seconds < min_audio_s`) gibt `None`, `engine.calls` bleibt 0 — **das Produkt
verhaelt sich korrekt**, die Tests setzen still voraus, dass genug Chunks angekommen sind.
Beweis: `source.read` mit `time.sleep(0.05)` gebremst => **12/12 Fehler, engine.calls=0**;
Kontrolle ohne Bremse => **0/12 Fehler, engine.calls=1**.

## Pflichtfolge je Flake (Planer, woertlich)
1. **Ohne Reproduktion kein Fix:** mit dem Zeithebel (`source.read` + `sleep(0.05)`)
   reproduzieren, Fehlsignatur im Report festhalten.
2. Test auf **Deadline-Warten auf die tatsaechliche Puffer-/Queue-Fuellung** umstellen statt
   blind auf eine Tick-Zahl. Danach muss der Test **ungebremst gruen UND gebremst gruen** sein
   (gebremst gruen = er wartet korrekt). Eine **ueberschrittene Deadline ist ein expliziter
   Fehler** mit klarer Meldung, kein Stillschweigen.
3. FL1, FL2, FL3 und den **Nachbartest `tests/test_controller.py:328-335`**
   (`test_run_once_feeds_and_transcribes`) mit demselben Hebel pruefen — dieselbe Wurzel ist
   die wahrscheinlichste Erklaerung. **Nicht reproduzierbare Flakes bleiben unveraendert** und
   werden als offen dokumentiert (im Report, nicht als stiller Wegfall).

## Annahme (Abnahme, Planer-Vorgabe)
* **10 Vol-Laeufe gruen, davon 5 unter CPU-Druck** (Druck erzeuge ich hier nicht selbst —
  nutze z. B. `nice -n 0`-Parallelkopien eines künstlichen Busy-Loops ODER gleichzeitige
  `pytest`-Teilplaene; Hauptsaache: die Last muss dokumentiert sein),
* plus **10 Einzel-Läufe je betroffenem Test**, beide Varianten (gebremst/gebrems-frei) wie
  oben. Kein Lauf darf durch Herausloeschen, Markieren (`skip`/`xfail`) oder Timeout-Erhohung
  "gruen" gemacht werden.

## Erlaubte / verbotene Dateien
Erlaubt: `tests/test_controller.py` (nur Testcode/Hilfsfunktionen dort), NEU
`.pi/reports/flake_hardening.md` (Report: Fehlsignaturen, Hebel-Laeufe, Vorher/Nachher-Raten,
als offen dokumentierte Flakes).
Verboten: jeder Produktcode — **insb. `src/stt_button/controller.py`, `asr.py`, `app.py`,
`summary.py`, `__main__.py`, `deps_gate.py`**; `systemd/**`, `packaging/**`, `tools/**`,
Doku (`README`/`CHANGELOG`), `pyproject.toml`, `conftest.py`; kein sudo, kein Netzwerk,
keine echte Tastatur/Clipboard/Ton, **kein git push**, kein Dienst-Neustart.

## TDD + Abschluss
ROT = Test mit Zeithebel gebremst fails (Reproduktion), GRUEN = derselbe Test wartet korrekt.
Commit: `test(controller): make capture-timing tests deterministic (FL1-FL4)`

## Parallelitaet + Stall-Prophylaxe (verbindlich)
* Ein anderes Kind laeuft parallel `packaging/debian/control` + `tests/test_deb_control.py`
  und fuehrt dabei Teilsuiten. Wenn einer DEINER Vol-Laeufe fehlschlaegt: pruefe zuerst, ob der
  Fehler in `tests/test_controller.py` ueberhaupt deine Handschrift traegt, und wiederhole den
  Lauf einmal, bevor du etwas buchst.
* **Kein `while pgrep`**, keine 20x-Schleifen ohne Auswertung, kein conftest.py-Gesuche.
* Produktcode ist tabu: wenn du meinst, ohne Produkt-Aenderung geht es nicht, **brich ab** und
  melde das in <=4 Zeilen — das entscheidet der Planer, nicht du.
* Schlussmeldung max 8 Zeilen (Sha, Vorher/Nachher-Raten, als offen dokumentierte Flakes,
  tatsaechlich gelaufenes Modell).
