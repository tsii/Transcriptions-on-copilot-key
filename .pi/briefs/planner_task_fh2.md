# Planer-Auftrag FH2 — vier Entscheidungen zur Flake-Haertung (FH1-Nachlese)

Du bist der Planungs-Experte fuer `/home/salmon/pi-web-ui/STT-Button`. Du triffst
**Entscheidungen und Blockzuschnitte**, keinen Code. Projektregeln: P1-P9 aus `AGENTS.md`
(besonders P2 Testkommando, P3 TDD, P9 Planungsbesitz).

## Lesestoff (in dieser Reihenfolge, sonst nichts)
1. `.pi/reports/fh1_verification.md` — **Ground Truth des Betreibers**, die Autoritaet.
2. `.pi/reports/flake_hardening.md` — der FH1-Report (durch 1 teilweise entwertet).
3. `.pi/plans/plan_summary_mode_v2.md` **nur §5, FH1-Zeile (ca. Zeile 423)** — der bisherige
   Block und seine Annahme.
4. bei Bedarf gezielt in `tests/test_controller.py` nachlesen (Zeilen, nicht die Datei ganz).

## Was geschehen ist
FH1 (`f6971dd`, test-only, `tests/test_controller.py` +76/−12) haertete FL1 und FL4
nachweislich (Hebel-Reproduktion wirkt, VolSuite 879 passed / 2 skipped, Kollektion 881).
Der Betreiber hat selbst nachgemessen und drei Luecken gefunden (B1-B3 in seinem Report).

## Deine vier Entscheidungen (je 1-3 Zeilen, konkret)
1. **FL2** (`TestAudioDumpModes::test_async_writes_wav_from_snapshot`, ungebremst 2/20
   einzeln, 1/10 Modul-Volllauf): eigener Hebel fuer den async-/Snapshot-Pfad + Haertung,
   oder aus dem FH1-Brief herausloesen und als **eigenen Block** (z. B. FL5) setzen?
2. **Hebel-Design**: `STT_TEST_READ_DELAY` wirkt global auf die geteilten Fakes
   (`FakeAudioSource`/`RestAudioSource`) und bricht 3 Tests, die FH1 nie angefasst hat
   (`TestCaptureThread::test_audio_not_lost_during_blocking_inference`,
   `TestLatencyLockDecoupling::test_release_during_live_asr_stops_audio_before_inference_returns`,
   `TestLiveLatencyTrace::test_live_tick_points_logged`; 8/8 Modul-Laeufe mit Bremse rot).
   Ersetze durch pro-Test-Wirkung (Kontextmanager/Klasse/Parameter)? In einer
   FH1-Nachbesserung oder als eigener Block?
3. **Akzeptanzregel** "je Flake Zeithebel-Reproduktion vor dem Fix": behalten, obwohl FL2
   (ungebremst flakend) und FL3 (einzeln per Hebel 8/8 gruen, faellt nur im Modul-Kontext)
   damit nicht reproduzierbar sind? Wenn behalten: **spezifiziere** Repro-Hebel + Signatur
   fuer FL2 und FL3. Wenn nicht: Report-Korrektur (B2) als Pflichtblock mit Formulier-
   vorgabe.
4. **Die 3 Kollateralfaelle** sind echte versteckte Zeitannahmen im Testcode: eigener
   Haertungsblock, oder erst Hebel-Umbau (Punkt 2) und danach neu messen?

## Form deiner Abgabe (verbindlich)
* Schreibe deine Entscheidung in **`/home/salmon/pi-web-ui/STT-Button/.pi/plans/plan_fh2_flake_followup.md`**.
* **In mehreren kleinen Stuecken** (4-6 `write`/`patch`-Aufrufe, je ein Abschnitt) — ein
  einziger großer `write` ist verboten (beruechteter Hangfall dieses Projekts).
* Struktur: `## Entscheidung` (je Punkt 1-4, mit Beleg aus dem Report), `## Blaue` (Tabelle
  wie in `plan_no_venv_B.md` §3: Block | Ziel | Dateien | Test zuerst ROT | Commit-Titel |
  Rueckholweg), `## Annahme` (was gruen sein muss, mit den **konkreten Laufzahlen und
  Selektionen**, z. B. "Modul-Volllauf 10x ungebremst 0 failed" statt pauschal "VolSuite gruen"),
  `## Was FH1 behalten darf` (was nicht zurueckgerollt wird).
* Jede Annahme muss **messbar** sein (Anzahl Laeufe, Selektion, Gebremst-/Ungebremst-Variante,
  erlaubte Fehlerquote 0). Keine Formulierung wie "Suite ist gruen" ohne Laufzahl.

## Grenzen
* Kein Code, kein Testlauf, kein git commit, kein sudo, kein Netzwerkzugriff noetig.
* Produktion des Betreibers laeuft (Diktat in Benutzung): **nichts** ausser der einen
  Plan-Datei schreiben.
* Wenn dir eine Information fehlt, die nur ein Experiment liefern kann: formuliere das
  Experiment im Plan als Teil eines Blocks, statt es selbst auszufuehren.
