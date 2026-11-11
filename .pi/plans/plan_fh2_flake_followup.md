# FH2 — Flake-Härtung, vier Entscheidungen (FH1-Nachlese)

Planer: FH2 (P9-Planungsbesitz) · Datum: 2026-11-11 · Repo: STT-Button, `master`
Grundlagen (Autorität in dieser Reihenfolge): `.pi/reports/fh1_verification.md`
(Betreiber-Ground-Truth, B1–B3), `.pi/reports/flake_hardening.md` (FH1, durch B1–B3
teilweise entwertet), FH1-Zeile in `plan_summary_mode_v2.md` §5, gezielte Zeilen aus
`tests/test_controller.py` (:27 Hebel, :30 `_wait_delivered`, :434 FL3, :473/:609/:1468
Kollateralfaelle, :1193 FL2). Commit unter Pruefung: `f6971dd`.

Testkommando in allen Annahmen (P2, hier nicht ausgeführt):
`.venv/bin/python -m pytest -q -p no:randomly -o addopts=""` (+ gezielte Selektion).
Alle Bloecke sind **test-only** (`tests/test_controller.py`) oder Doku; kein Produktcode,
kein Eingriff in die laufende Produktion.

## Entscheidung

### 1. FL2 — eigener Block, nicht FH1-Nachbesserung (Blaue FH2-B, Zahl FL5, Alias FL2)
FL2 (`TestAudioDumpModes::test_async_writes_wav_from_snapshot`) hat eine andere
Wurzelklasse als FL1/FL3/FL4: Fehlerort ist der **async-Dump-Schreibpfad**
(Controller startet Schreib-Thread, Exceptions werden nur debug-logs,
`controller.py:709/:734`), nicht die Capture-Timing. Beleg Betreiber B1: FH1-naechster
Code, ungebremst **2/20 rot einzeln** und **1/10 rot Modul-Volllauf**; die
Report-Begruendung "4/4 gruen gebremst" gilt nicht, weil der Read-Hebel den
Schreibpfad per Annahme nicht trifft. Entscheidung: **herausloesen und als eigener
Block FH2-B** — mit eigenem Pro-Test-Hebel (`wave.open`-Injektion NUR in diesem Test,
kein Env) plus statistischem Re-Pro-Protokoll, und mit Hartung (caplog-Gate gegen
stilles Verslucken). Die FH1-Zusage "FL1–FL4 haerten" gilt damit als **nicht
abgeschlossen erst mit FH1, sondern mit FH2-B**; Nummer FL5 fuer den Monitor,
Aliastext "war FL2" in Report und Commit-Body, damit Querverweise gelten.

### 2. Hebel-Design — pro-Test-Wirkung per Marker+Fixture, als eigener erster Block (FH2-A)
Beleg Betreiber B3: globaler Env-Hebel auf den geteilten Fakes (`FakeAudioSource`/
`RestAudioSource`, `:27` import-Zeit-Konstante) macht im Modul-Volllauf **8/8 Laeufe
rot** an drei Tests, die FH1 nie angefasst hat — "jeder Test ungebremst UND gebremst
gruen" ist mit diesem Werkzeug fuer das Modul unerfuellbar. Entscheidung: **Ersetzen
durch pro-Test-Wirkung, in einer FH1-Nachbesserung als eigener Block FH2-A (erster
Commit der Serie)**, Design wie folgt, damit die Kette nicht beim Implementieren
entscheiden muss:
* Bremse wird **opt-in pro Test** per Marker `@pytest.mark.read_brake` + autouse-
  Fixture: die Fixture liest `os.environ["STT_TEST_READ_DELAY"]` **bei Setup** (nicht
  bei Import) und setzt den Modul-globalen Wert, `try/finally`-Reset im Teardown.
  Der Env-Wert bleibt Kommando-kompatibel erhalten; nur **markierte** Tests bremsen.
* **Kein `contextvars.ContextVar`**: `source.read()` laeuft im Capture-Thread, und
  jeder Thread startet mit eigenem, leerem Context — der Wert waere dort nicht Sicht-
  bar. Modul-Global + Teardown-Reset ist der korrekte Mechanismus; spaet lebende
  Daemon-Threads lesen nach Teardown nur `0.0` (= ungebremst, ungefaehrlich).
* Die FH1-Markierungen (FL1, FL3, FL4 + Nachbar) werden im selben Commit gesetzt —
  ihre Gebremst-Nachweise bleiben identisch reproduzierbar (`STT_TEST_READ_DELAY=0.05`).

### 3. Akzeptanzregel — behalten, aber aufgeweicht auf "Hebel ODER statistisches Protokoll"; "nicht reproduzierbar" nur im Abnaehmekonfig-Raum
Regel behalten (sie hat FL1/FL4 echte Reproduktionen geliefert), aber umformulieren:
**"Je Flake muss vor dem Fix eine Reproduktion auf dem eigenen Fehlerpfad stehen —
entweder deterministischer Hebel auf genau diesem Pfad, oder ein statistisches
Protokoll mit festen Laufzahlen, Selektion und Signatur. Die Aussage 'nicht
reproduzierbar' gilt nur in derselben Konfiguration, in der auch die Abnahme gemessen
wird (Kontext + Gebremst-/Ungebremst-Variante)."** Konkretisiert (Belege B1, B2):
* **FL2/FL5** — Hebel: `wave.open`-Injektion per `monkeypatch` nur in diesem Test
  (Auswurf-Zweig: raise → Datei fehlt + Debug-Log => belegt das stille Verschlucken;
  Verzweigungs-Regel in FH2-B). Statistisches Protokoll natuerlicher Flake: einzeln,
  ungebremst, 20 Laeufe, Signatur `async-Dump fehlt`, Treffer >= 2 (Betreiber hat
  2/20 bereits gemessen; Block misst nach).
* **FL3** — Kontext-Protokoll: alter Testkoerper (`git show f6971dd^`), **Modul-
  Volllauf** mit Bremse 0.05, 10 Laeufe, Signatur `state == RECORDING != IDLE`
  (Assertion `tests/test_controller.py:421`); erwartet >= 1 Treffer (Betreiber: 1/6).
  0/10 Treffer => dokumentiert als "kontextabhaengig, nicht zuverlaessig reproduzierbar"
  — **und es wird keine Heilungs-Behauptung aus FL3 abgeleitet** (genau das war B2).
* Report-Korrektur (B1/B2/B3-Erratum) wird unabhaengig davon als Pflichtblock FH2-D
  gesetzt: der FH1-Report enthaelt nachweislich falsche Saetze, die als Beleg
  zirkulierten.

### 4. Die 3 Kollateralfaelle — erst Hebel-Umbau (FH2-A), dann neu messen; Hartungsblock FH2-C nur bei Treffer
Die drei Faelle (`test_audio_not_lost_during_blocking_inference` starr
`qsize()==50` nach `sleep(0.3)`; `test_release_during_live_asr_...` Budget
`elapsed < 0.3`; `test_live_tick_points_logged` 3 blinde Ticks) sind echte
Zeitannahmen, aber ihr einziger gemessener Ausfall kommt **vom globalen Hebel**, nicht
aus dem ungebremsten Betrieb. Entscheidung: **Reihenfolge Hebel-Umbau zuerst, dann
neu messen** — FH2-A enthaelt sie in beiden Abnahmevarianten (ungebremst 10x +
gebremst 10x, Bremse trifft sie nicht mehr). Nur wenn einer von ihnen in diesen 20
Abnahmelaeuften auftritt, wird FH2-C (bedingter Block) faellig: Umbau auf
Bedingung+Deadline (`_wait_delivered`-Muster / Ereignis-Reihenfolge statt starrer
Budgets). Kein vorschneller Umbau grüner Tests — jede Aenderung an ihnen braucht sonst
selbst wieder eine ROT-Reproduktion aus einem echten Vorfall.

## Blaue

| Block | Ziel | Dateien | Test (zuerst ROT) | Commit-Titel | Rückholweg |
|---|---|---|---|---|---|
| **FH2-A** Hebel pro Test | Bremse nur noch per `@pytest.mark.read_brake` + Fixture (Env-Leseart bei Setup, try/finally-Reset, kein ContextVar); FH1-Tests markiert; neue Guard-Tests `TestReadBrakeScoping`: (a) markierter Fake-Lauf ist gebremst (Wandeil >= n*Bremse), (b) unmarkierter laeuft ungebremst, wahrend Env gesetzt ist — (b) ist mit `STT_TEST_READ_DELAY=0.05 pytest tests/test_controller.py -k TestReadBrakeScoping` heute ROT (globaler Hebel bremst alles), nach Fix GRÜN; ohne Env beide GRÜN | `tests/test_controller.py` | Guard (b) wie beschrieben | `test(controller): scope read-delay brake to marked tests only` | `git revert` (eine Testdatei; Env bleibt Rueckwaerts-kompatibel) |
| **FH2-B** FL2→FL5 Hartung | Schritt 1 Diagnose-Lauf (ROT-Pflicht nach Akzeptanzregel): caplog-Gate im Test (keine Record `"Audio dump failed"`) + 20x einzeln ungebremst, Signatur `async-Dump fehlt` >= 2 Treffer; Verzweigung: (a) Gate feuert -> verschluckte Exception ist Wurzel, Test hardet auf die Ursache; (b) Gate feuert nie bei fehlender Datei -> Tear-down-/Thread-Rennlause des Dump-Threads, Test wartet auf beobachtbares Ende (`"Recording saved"`-Log statt blinder Datei-Existenz). Schritt 2 Fix + Guard: bei injizierter Write-Exception (`monkeypatch` auf `wave.open`, nur in diesem Test) muss die Fail-Signatur die injizierte Ursache nennen statt nur `async-Dump fehlt` — heute nennt sie sie nicht; ROT-Signatur (blindes `async-Dump fehlt`) im Block-Report dokumentieren | `tests/test_controller.py` | Hazard-Guard-Test (Injektion) + caplog-Gate; ROT dokumentieren | `test(controller): harden async audio-dump test (FL5, formerly FL2)` | `git revert`; FH1-Commit bleibt unberührt |
| **FH2-C** (bedingts Kollateral-Hartung) | NUR faellig bei >= 1 Treffer in FH2-Abnahme (10x ungebremst + 10x gebremst): drei Tests auf Bedingung+Deadline umstellen (`qsize`-Starrwert -> `_wait_delivered`-Muster; `elapsed<0.3` -> Ereignis-Reihenfolge; Live-Tick-Test wartet auf geloggte Messpunkte statt 3 blinder Ticks) | `tests/test_controller.py` | je Test: alte Behauptung bleibt, neue Deadline-Form; ROT-Beleg = reproduzierter Treffer aus der Abnahme (sonst Block nicht ausführen) | `test(controller): replace fixed-time assumptions in 3 timing tests` | `git revert` (je Test ein Commit moeglich) |
| **FH2-D** Erratum + Regeltext | `.pi/reports/flake_hardening.md` erhaelt Erratum-Abschnitt mit Formuliervorgabe: (1) FL2 "nicht reproduzierbar (4/4)" -> "ungebremst 2/20 reproduziert (Betreiber B1), haertung in FH2-B"; (2) FL3-Signatur-Nachweis -> "einzeln 8/8 gruen; nur Kontextstichprobe 1/6, Protokoll FH2"; (3) "gebremst 9/9" -> "gezielte Auswahl, Volllauf gebremst 8/8 rot (B3), behoben durch FH2-A"; aufgeweichter Akzeptanzregel-Text (Entscheidung 3) in `.pi/plans/plan_summary_mode_v2.md` FH1-Zeile ergaenzen (Fußnote, Zeile nicht umschreiben) | `.pi/reports/flake_hardening.md`, `.pi/plans/plan_summary_mode_v2.md` | kein Code-Test (Doku); Abnahme = Voll-Zitat der drei korrigierten Stellen im FH2-Report | `docs(reports): FH1 erratum B1-B3 + amended flake repro rule` | `git revert` (nur Doku) |

Reihenfolge: **FH2-A -> FH2-B -> (FH2-C nur bei Treffer) -> FH2-D** (Erratum zuletzt,
damit es die echten Abschlusszahlen zitieren kann). Ein Commit je Block (P3: je Schritt
ROT dokumentiert, dann GRÜN). Kein Block darf Produktcode, Skip/Xfail, oder die
Zeit-Schwellen fremder Tests anfassen; kein Block braucht sudo/Netzwerk/Dienst.

## Annahme

Alle Laeufe mit `.venv/bin/python -m pytest -q -p no:randomly -o addopts=""`
(Auswahl wie angegeben), Umgebung sonst unveraendert, Fehlerquote erlaubt: **0**.

* **FH2-A GRÜN wenn:** (1) Guard-Tests `TestReadBrakeScoping` 5x einzeln mit UND
  ohne `STT_TEST_READ_DELAY=0.05` je 5/5 gruen; (2) Modul-Volllauf **ungebremst 10x:
  0 failed**; (3) Modul-Volllauf **mit Env 0.05 10x: 0 failed** (heute: 8/8 rot —
  B3 gilt als behoben erst hier); (4) FH1-markierte Selektion
  `-k "test_ticks_stop_after_limit_final_text_complete or test_engine_error_aborts or test_text_reaches_typer or test_run_once_feeds_and_transcribes"`
  gebremst 5x 0 failed und ungebremst 5x 0 failed.
* **FH2-B ROT wenn:** Diagnose-Lauf 20x einzeln ungebremst zeigt >= 2x Signatur
  `async-Dump fehlt` (Untergrenze Betreiber 2/20) — ohne diesen ROT-Wurf kein Fix.
  **GRÜN wenn:** fixer Test 20x einzeln ungebremst 0 failed **und**
  `-k TestAudioDumpModes` 10x 0 failed **und** Modul-Volllauf ungebremst 10x
  0 failed; der caplog-Gate bleibt als Dauerwacht im Test.
* **FH2-C (nur bei Treffer) GRÜN wenn:** die drei betroffenen Tests je 10x einzeln
  ungebremst + 10x einzeln gebremst (Env 0.05) 0 failed und im Modul-Volllauf
  beider Varianten 0 failed.
* **Serien-Abnahme nach FH2-D:** VolSuite einmal ungebrems t: `nice -n 19` —
  Kollektion **881**, **879 passed / 2 skipped / 0 failed** (Betreiber-Baseline
  2026-11-11 13:20); danach Modul `tests/test_controller.py` Volllauf ungebremst
  **10x 0 failed** (FH1 schaffte 9/10) und gebremst **10x 0 failed** (FH1: 0/10).
* **FL3-Abschluss:** Kontext-Protokoll (Entscheidung 3) ergibt >= 1 Treffer in 10
  Laeufen ⇒ Signatur im Erratum belegt; 0 Treffer ⇒ "nicht zuverlaessig
  reproduzierbar" im Erratum — in beiden Faellen ohne Heilungs-Behauptung fuer FH1.

## Was FH1 behalten darf

* Commit `f6971dd` wird **nicht zurückgedreht**: FL1-Phasengliederung +
  `typed_text_final`, FL3/FL4 `_wait_delivered`-Vorwarten, Nachbar-Fix, Helper
  `_wait_delivered` inkl. Deadline-Assertion, `delivered`- Zaehler an beiden Fakes —
  alles durch Betreiber-Abnahme verifiziert (FL1/FL4-Hebel-Repro, ungebremst gruen).
* Der Read-Brems-Hebel bleibt **als Mechanik** erhalten (Env-Wert + sleep in
  `read()`), nur seine **Wirkung wird pro Test gerichtet** (FH2-A). FH1s
  Gebremst-Nachweise bleiben mit Marker-Setzung identisch nachfahrbar.
* Der als "offen dokumentiert" ehrliche Teil des FH1-Reports (FL2 offen, Commit-
  Titeltreue) bleibt stehen; nur die durch B1–B3 entwerteten Saetze erhalten ihr
  Erratum in FH2-D.

Grenzen dieser Kette: test-only + Doku, P2-Kommando, P3 ROT→GRÜN je Schritt, P5/P6
unberührt (kein sudo, kein Dienst), Push nur auf `origin/master` (P7). Bei
Ueberraschungen (z. B. FH2-B Verzweigung (b) bestaetigt einen echten Produktbug im
Async-Dump): **an den Planer eskalieren**, nicht im Block entscheiden.
