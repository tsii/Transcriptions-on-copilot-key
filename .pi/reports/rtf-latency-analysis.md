# RTF vs. Echtzeit: Warum STT-Button beim Mitschreiben nicht nachkommt (Analyse, nur gelesen)

Stand 2026-11-04. Grundlage: Code-Lektüre (STT-Button HEAD, TalkType `--depth 1` nach /tmp/talktype),
**1683 echte Diktate** aus dem Journal (`journalctl --user -u stt-button`, `latency[...]`-Trace,
28.10.–04.11.) und alte Messlogs. Keine Benchmarks gefahren, da die Maschine stark ausgelastet war.
Achtung: Im Journal-Zeitraum lagen verschiedene Konfigurationen (FP32/INT8, 8 s/15 s). Die Werte
zeigen also die Größenordnung, nicht genau eine Konfiguration.

## 1. Kurzfassung

- **Dein Gedanke stimmt nur zum Teil.** RTF < 1 reicht für Echtzeit nur, wenn jede Audiosekunde
  **einmal** gerechnet wird (inkrementell oder gestreamt). STT-Button rechnet bei jedem Tick den
  **ganzen bisherigen Puffer** neu (`asr.py:892-895`). Dann gilt nicht mehr „RTF < 1“, sondern
  **RTF × Pufferlänge < Intervall**. Mit FP32 (RTF ≈ 0,12) und 1,5 s Intervall reißt diese Grenze schon
  bei **≈ 12,5 s** Audio.
- Bis zum Live-Limit von 15 s ergibt das für 15 s Audio etwa **9,9 s CPU-Arbeit**. Die *effektive*
  RTF der Live-Pipeline liegt damit bei ≈ 0,66 und nicht bei 0,12.
- **Wo die Wartezeit nach dem Loslassen herkommt (gemessen, p50 / p90):**
  - Warten auf einen noch laufenden Live-Tick: **0,27 / 1,58 s** (max. 7,4 s; bei 39 % der Diktate > 0,5 s).
  - Finale Volltranskription: **0,95 / 2,52 s**.
  - Ausgabe (wl-copy + dotool-Prozess): **0,75 / 0,81 s**.
  - Summe: **2,33 / 4,55 s**.
- Ein Teil der Verzögerung ist **gewollt und algorithmisch, also keine CPU-Frage**. Im Modus
  `live_typing_mode="committed"` mit `commit_agreements=2` erscheint Text erst, wenn zwei Ticks
  übereinstimmen (≥ 1,5–3 s Rückstand). Nach 15 s wird live gar nichts mehr getippt.
- **TalkType rechnet ebenfalls den ganzen Puffer neu.** Der Vorsprung kommt nicht aus einer
  grundsätzlich anderen Inferenz. Er kommt aus drei Dingen:
  1. Segmentierung: Puffer-Reset bei einer Satzpause ≥ 1,2 s oder spätestens nach 35 s.
     Damit bleibt das Fenster klein, und das Finale rechnet nur das letzte Segment.
  2. Adaptive Taktung: Intervall = 2 × letzte Dekodierdauer.
  3. INT8 und Ausgabe direkt per `commitText` ohne Prozessstart.

## 2. Premise-Check: RTF < 1 heißt nicht automatisch Echtzeit-Mitschreiben

**Code-Beleg:**
- `maybe_transcribe()` (`src/stt_button/asr.py:641-697`) löst einen Tick aus, sobald seit dem letzten
  Lauf `interval_s` neues Audio dazugekommen ist (`:665-667`).
- `_transcribe()` übergibt **`bytes(self._buffer)`**, also den Gesamtpuffer, an die Engine
  (`asr.py:895`, `:901`).
- Das ONNX-Backend erkennt ebenfalls den Gesamtpuffer (`asr.py:279-288`). Ein Encoder-Cache oder ein
  Zustand zwischen Ticks existiert nicht.
- `finalize()` rechnet mit `finalize_mode="full"` den ganzen Puffer noch einmal
  (`asr.py:699ff`; Live-Config `~/.config/stt-button/config.toml:46`).
- Es gibt **einen** Ausführungs-Lock für Live-Tick und Finalize (`controller.py:295-302`, KONZEPT §10).

**Konsequenz:** Ein Tick bei Pufferlänge L kostet `c(L) ≈ RTF · L` (der Encoder dominiert; laut
TalkType-Messung ab ≈ 60 s sogar überlinear).

| Größe | FP32 (live, RTF 0,12) | INT8 (RTF 0,05) |
|---|---|---|
| Grenze `RTF·L < 1,5 s` | L ≈ **12,5 s** | L ≈ 30 s |
| Tick bei 15 s (Live-Limit) | ≈ **1,8 s > 1,5 s Intervall** | ≈ 0,75 s |
| CPU-Summe der Live-Ticks bis 15 s (10 Ticks) | ≈ 9,9 s | ≈ 4,1 s |
| dasselbe ohne Limit bis 30 s (20 Ticks) | ≈ 38 s (**> 30 s Audio ⇒ effektive RTF > 1**) | ≈ 16 s |

Die Gesamtarbeit wächst also mit Σk ∝ n², quadratisch. Selbst eine RTF von 0,12 wird durch das
ständige Neu-Rechnen auf eine effektive RTF > 1 aufgeblasen.

**⚠ Ein Anker-Fakt ist widerlegt bzw. veraltet:** Laut Kommentar ist `live_asr_until_s = 15` tragfähig,
weil der Tick bei 15 s nur 0,75 s kostet (`config.py:158-164`, `config.toml:52`). Das gilt aber nur für
**INT8**. Produktiv läuft seit 29.10. wieder **FP32** (`config.toml:22`). Dort kostet der Tick bei 15 s
≈ 1,8 s und liegt damit über dem Intervall von 1,5 s. Zwischen ≈ 12 und 15 s laufen die Ticks deshalb
lückenlos hintereinander, und der Live-Rückstand wächst.

Kleinere Abweichungen:
- Der Code-Default ist `asr_interval_s = 1.0` (`config.py:83`), die Live-Config setzt 1,5.
- KONZEPT §11.2 nennt 20 s als Live-Limit, Code und Config verwenden 15 s.

**Was an deiner Erwartung trotzdem stimmt:** Mit echtem Streaming sinkt die Arbeit pro Audiosekunde
auf eine Konstante, und RTF < 1 würde reichen. Das wäre ein Cache-aware-Encoder (eine *cached*
Encoder-Variante gibt es für Parakeet, TalkType lädt sie in LiteRT als Variante B) oder ein gefenstertes
Verfahren. **Der Grund ist die Architektur (whole-buffer), nicht die Rechenleistung.**

## 3. Zeit-Budget in STT-Button (gemessen, 1683 Diktate)

| Komponente | p50 | p90 | Max | Ursache (Code) |
|---|---|---|---|---|
| Stop → Capture/Source gestoppt | 0,04 s | – | – | `controller.py` `_finish`, unkritisch |
| **Warten auf laufenden Live-Tick** (`finalizer exec acquired`) | **0,27 s** | **1,58 s** | 7,39 s | Ein gemeinsamer `_exec_lock`; ein Live-Tick über den ganzen Puffer läuft nach dem Release zu Ende und wird anschließend **verworfen** (Post-Release-Discard, KONZEPT §11.3). Beispiel 04.11. 01:08:14: 3,75 s Wartezeit, Finalize danach 1,2 s. |
| **Finale Volltranskription** | **0,95 s** | 2,52 s | 15,1 s | `finalize_mode="full"`, Puffer wird zum zweiten bzw. n-ten Mal gerechnet |
| WAV-Dump | ≈ 0,003 s | – | – | vernachlässigbar |
| **Ausgabe** (wl-copy + dotool-Chord, ggf. Backspaces) | **0,75 s** | 0,81 s | 7,9 s | Prozessstarts: `typing.py:542ff` (OPT1 spart schon einen dotool-Start ≈ 0,7 s); dotool selbst kostet 20 ms Tastendruckpause je Zeichen (`typing.py:179,333`), relevant nur beim Live-Tippen |
| **Summe Release → Text** | **2,33 s** | **4,55 s** | 16,6 s | |
| Live-Ticks (nur echte Läufe > 50 ms) | 0,82 s | 2,08 s | 16,7 s | wachsen mit L |

Dazu kommen **Verzögerungen während des Sprechens**, die in obiger Messung gar nicht auftauchen:
1. **Algorithmische Commit-Latenz.** Mit `live_typing_mode="committed"` (`config.toml:51`) wird nur der
   bestätigte Präfix getippt. Bestätigt ist er erst nach `commit_agreements=2` übereinstimmenden Ticks
   (`asr.py:921-960`), und auch dann nur bis zur letzten Wortgrenze. Ein Wort erscheint also frühestens
   ≈ 2 Intervalle (3 s) plus Inferenzzeit nach dem Sprechen. Das ist eine Designentscheidung gegen
   Flackern.
2. **Erster Tick** frühestens bei `min_audio_s = 1.0` s.
3. **Live-Pause ab 15 s.** Danach wird live nichts mehr getippt (`asr.py:653-662`), der ganze Rest kommt
   erst nach dem Release.
4. **Rechenlast durch Fremdprozesse.** Load ≈ 14 (z. B. pytest-Läufe) verlängert alles. Die Ausreißer
   (max. 15 s Finalize) deuten darauf hin.

**Fazit Zeit-Budget:** Die Inferenz selbst ist in ≈ 1 s pro Puffer schnell. Verloren geht die Zeit
durch die **Wiederholung**: dieselben Sekunden werden n-mal gerechnet, und der letzte, verworfene
Live-Tick blockiert das Finalize. Dazu kommen ≈ 0,75 s Ausgabe-Overhead und eine bewusst konservative
Commit-Regel.

## 4. TalkType: Architektur und warum es schneller wirkt

Belege beziehen sich auf `/tmp/talktype/app/src/main/java/de/voicekeyboard/`.

| Merkmal | TalkType | Beleg |
|---|---|---|
| Inferenzmodus | **ebenfalls whole-buffer** pro Partial und Final | `dictation/DictationController.kt:409,843` (`window() = PcmWindow(pcm.copyOf(totalSamples))`), KDoc `DecodePace.kt` („decodes the COMPLETE buffer… total work quadratically“) |
| Modell / Quantisierung | Parakeet TDT 0.6B v3 **INT8** ONNX (istupakov), ORT **CPU-EP** (kein NNAPI/GPU/NPU); Variante B: Arm-LiteRT INT8 mit *cached* Encoder | `asr/OnnxSessions.kt:17,27-29`, `model/ModelManifest.kt:26-76` |
| Threads | intra = min(4, Kerne), inter = 1 | `asr/AsrThreadConfig.kt:94-103` |
| **Segmentierung** | Mini-Final und Puffer-Reset bei Satzpause ≥ **1,2 s** plus Satzzeichen, hart nach **35 s**; Mini-Final committet dauerhaft | `ime/DictationCoordinator.kt:124,635-660,1345-1370`, `VoiceKeyboardService.kt:1122`, `DictationController.kt:820-829` |
| **Adaptive Taktung** | nächstes Partial-Intervall = 2 × letzte Dekodierdauer, geklemmt auf 2–6 s; Basis-Chunk 2 s | `dictation/DecodePace.kt`, `AsrThreadConfig.kt:69-73`, `DictationCoordinator.kt:201` (`decodePacing = true`) |
| Commit-Regel | N = 3 Übereinstimmungen, 2 Wörter Zurückhaltung (konservativer als STT-Button) | `commit/CommitPolicy.kt` |
| Ausgabe | `InputConnection.commitText` / `deleteSurroundingText`, im Prozess, ohne Subprozess | `editor/InputConnectionSink.kt:155` |
| Gemessen (Telefon) | letzter Decode 583 ms bei 11,9 s ⇒ ≈ 49 ms/s (RTF ≈ 0,05); Kurve α = 38,5 ms/s | `docs/C2_EXPORT_AUSWERTUNG.md:8,26`, KDoc `segmentBoundary` |

**Warum TalkType schneller wirkt, nach Wirkung geordnet:**
1. **Segmentierung an Satzpausen.** Im Alltag (Sätze mit Pausen) bleibt der Puffer meist bei wenigen
   Sekunden. Beim Stop wird nur das **letzte Segment** dekodiert; frühere Sätze sind schon final im
   Editor. So bleibt die Final-Latenz auch nach 2 Minuten Diktat ≈ konstant. STT-Button rechnet beim
   Release immer alles. Das ist der größte Unterschied.
2. **Adaptive Taktung.** Die CPU wird nie zu mehr als ≈ 50 % von Partials belegt, Ticks laufen nicht
   hintereinander auf. Dadurch ist beim Stop seltener ein langer Decode „in flight“. STT-Button nutzt
   ein festes Intervall von 1,5 s, auch wenn der Tick 1,8 s braucht.
3. **INT8 statt FP32.** Faktor ≈ 2–2,4 je Lauf (ABSCHLUSSBERICHT: 0,050 gegenüber 0,12).
4. **Ausgabe ohne Prozessstart** (≈ 0 ms gegenüber ≈ 0,75 s).

Hardware ist **nicht** der Grund. TalkType läuft ebenfalls rein auf der CPU (NNAPI fällt aus, weil nicht
alle Parakeet-Ops unterstützt werden). Das Telefon ist pro Lauf eher langsamer als der Lunar Lake.

## 5. Die Differenz: die vier wichtigsten Ursachen

1. **Unbegrenztes Fenster.** STT-Button hat keine Segmentierung, TalkType ein Fenster von höchstens
   35 s, meist pro Satz. Die Kosten für Final und Tick wachsen bei STT-Button mit der ganzen
   Diktatlänge, bei TalkType nur mit der Satzlänge.
2. **Festes Intervall bei FP32.** Ab ≈ 12,5 s laufen die Ticks lückenlos hintereinander. In 39 % der
   Diktate blockiert ein verworfener Tick das Finalize um > 0,5 s (p90 1,58 s).
3. **FP32 statt INT8.** Das ist eine Qualitätsentscheidung des Betreibers vom 29.10. und kostet Faktor
   ≈ 2,4 auf jeden Lauf.
4. **Ausgabe über Subprozesse** (wl-copy + dotool, ≈ 0,75 s fix). TalkType schreibt direkt in den Editor.

## 6. Ideen zur Beschleunigung (Diskussion, kein Arbeitsplan)

| Idee | Nutzen | Aufwand | Risiko / Anmerkung |
|---|---|---|---|
| **A. Laufenden Live-Tick beim Release nicht abwarten.** Finalize auf separater Engine-Session oder sofort starten und Tick-Ergebnis verwerfen; ORT erlaubt parallele `run()`-Aufrufe | spart p50 0,27 s / p90 1,58 s / max 7 s | klein–mittel | doppelte CPU-Last während der Überlappung; mit FP32/HF-Pfad ist ein zweites Modell nötig (RAM) |
| **B. Adaptive Taktung wie TalkType** (Intervall = max(1,5 s, 2 × letzte Tickdauer)) | keine lückenlos laufenden Ticks, Wartezeit A kleiner, CPU-Luft | klein | Live-Text etwas träger; Variante: am Tick-Start prüfen, wie viel Budget bleibt |
| **C. Segmentierung an Satzpausen** (VAD-Pause ≥ 1–1,2 s plus Satzzeichen ⇒ Mini-Final, Text endgültig, Puffer-Reset) | **größter Hebel**: Final-Latenz ≈ Satzlänge statt Diktatlänge, Live-Limit 15 s weitgehend überflüssig | mittel | Satz-Nahtstellen (Großschreibung, Komma), siehe OPT2-Stitch-Drift im ABSCHLUSSBERICHT; mit **harten** Pausen-Schnitten statt Overlap-Stitching ist das Risiko kleiner als bei `cached_mid_tail` |
| **D. INT8 (wieder) aktivieren**, eventuell nur für Live-Ticks, FP32 nur fürs Finale | Faktor ≈ 2,4 bei den Ticks; Grenze 12,5 → 30 s | Config-Zeile bzw. klein | Qualität: Live-Text nur Vorschau, Finale bleibt FP32; RAM für zwei Modelle (≈ 0,9 GB + FP32) |
| **E. Finalize aus dem letzten Tick wiederverwenden** (`finalize_mode="cached"` existiert) | spart das Finale, wenn nach dem letzten Tick kein neues Audio kam | Config | in der Praxis kommt fast immer ≥ 0,1 s Rest; Nutzen gering, außer man wartet bewusst auf den „Ende“-Tick |
| **F. Ausgabe beschleunigen**: dotool als Daemon (`dotoold`/persistente Pipe), wl-copy vorwärmen | ≈ 0,5–0,7 s | klein–mittel | Wayland/Fokus-Eigenheiten, Paste-Doppeleinfügung (Kommentare in `typing.py`) |
| **G. Commit-Regel lockern** (z. B. `live_typing_mode="full"` oder N = 1 mit Zurückhaltung) | Live-Text früher sichtbar | Config | Flackern und Backspaces; die Kosten sind bekannt (KONZEPT §11.1) |
| **H. Echtes Streaming**: Parakeet mit cache-aware Encoder (Arm-LiteRT „encoder_cached“ wie TalkType Variante B, NeMo-Streaming-Export) oder ein fertig gestreamtes Modell | konstante Arbeit pro Audiosekunde, **dann erst gilt deine RTF-<1-Intuition** | **groß (Rewrite der Engine-Schicht)** | anderes Modell bzw. anderer Export, eventuell schlechtere WER; die `windowed`-Variante ist im Repo bereits gescheitert (Deadlocks, 137 statt 256 Zeichen, `docs/HANDOFF.md:90-95`) |
| **I. Encoder-Ausgaben cachen** (nur neue Frames kodieren) | theoretisch groß | groß | Der Standard-Conformer hat volle Self-Attention, daher hängen alte Frames vom neuen Audio ab. Ohne Streaming-Export **nicht exakt** möglich, nur näherungsweise mit Fenster (≈ H) |

Ehrliche Einordnung: **A + B + C** zusammen würden den Großteil des Abstands zu TalkType schließen,
ohne Modellwechsel. **D** ist der billigste Einzelhebel, wurde aber aus Qualitätsgründen abgelehnt. Ein
Rewrite (H) lohnt sich nur, wenn echte Wort-für-Wort-Echtzeit verlangt ist. Für „wenig Warten nach dem
Loslassen“ reicht C.

## 7. Empfohlene nächste Messungen

1. Die Journal-Auswertung **nach Konfiguration trennen** (FP32 gegen INT8, Zeitraum ab 29.10.) und die
   Tickdauer als Funktion von L plotten, um c(L) = RTF·L direkt zu bestätigen.
2. Die Wartezeit auf den laufenden Tick (`finalizer exec acquired`) gegen die Pufferlänge beim Release
   auftragen. Erwartung: Sprung ab ≈ 12 s bei FP32.
3. Ein ruhiges System sicherstellen (Load < 2) und eine Referenzreihe aufnehmen (10 Diktate à 10/20/30 s);
   bisher kommen Ausreißer von bis zu 15 s vermutlich aus Fremdlast.
4. Den Ausgabepfad isoliert messen: Kosten des wl-copy-Starts gegenüber dem dotool-Start (der Trace hat
   `output 0.757s`).
5. Die Pausenstatistik echter Diktate bestimmen: Wie oft gibt es VAD-Pausen ≥ 1,2 s? Das entscheidet,
   wie viel Idee C bringt.
