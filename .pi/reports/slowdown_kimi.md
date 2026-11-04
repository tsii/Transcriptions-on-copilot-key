# Diagnose: wahrgenommene Verlangsamung nach A/B (2026-11-04)

**Modus:** ausschließlich Read-only-Analyse; keine Tests, kein Service-Eingriff, keine Codeänderung.
**Quellen:** HEAD `fad84f5`, Live-Config, `journalctl --user -u stt-button` (08:00–11:00 und
12:36–12:55), dotool-Quelle und die korrigierte Android-Referenz
`/home/salmon/Downloads/voice-keyboard-0.1.1.apk` (statisch, keine Ausführung).

## Kurzurteil

1. **A funktioniert messbar:** Die Wartezeit bis zum `_exec_lock` sank im Post-Sample von p50
   **0,194 s auf 0,032 s**; auch Release→Finalize-Ende verbesserte sich im Gesamtvergleich.
2. **Release→Chime verschlechterte sich trotzdem:** p50 **2,950 s → 4,413 s**. Der dominante
   Post-12:36-Kostenblock ist nicht mehr die ASR, sondern das abschließende dotool-Tippen plus
   separate Leertaste. Im Post-Sample lagen alle 16 Finalisierungen im `type`-Pfad.
3. **Das ist kein sauberer A/B-Wirkungsnachweis gegen A oder B.** Die Post-Diktate sind länger,
   ihr finales Delta ist rund doppelt so lang, und der Ausgabe-/Fokus-Mix unterscheidet sich von
   den beiden Vorher-Teilstichproben. A/B ändern das Zeichen-für-Zeichen-Tippen nicht.
4. **Der Live-Eindruck wird durch drei bereits vorher vorhandene Mechanismen geprägt:**
   `committed` zeigt nur stabile Präfixe, Präfixkonflikte verwerfen weitere Live-Updates, und ab
   15 s stoppt Live-ASR vollständig. B veränderte die mediane Tickkadenz in dieser Stichprobe
   praktisch nicht.
5. **Voice Keyboard 0.1.1 ist kein Gegenbeweis zugunsten eines Encoder-Caches:** Die APK nutzt
   nachweislich Parakeet **INT8**, aber ebenfalls **Whole-Buffer-Re-Transkription**. Der größte
   belegte Unterschied zum gefühlten Tempo ist die direkte Editor-Ausgabe inklusive instabiler
   Partials ohne 15-s-Totzone; INT8 liefert zusätzlich Rechenreserve.

---

## 1. Journalmessung: Was wurde besser, was schlechter?

### 1.1 Methode und Abgrenzung

Gezählt wurden echte `Transcription of ...`-Aufrufe, nicht die zahlreichen leeren
`live asr start/end`-Polls. Zeiten stammen aus den Millisekunden-Zeitstempeln in den Logmeldungen.
Als Chime-Näherung dient `latency[paste]`: Dieser Trace liegt unmittelbar vor
`_play_done_sound()` (`controller.py:650-657`). p50 ist das gewöhnliche Median, p90 der nächstgelegene
Rangwert.

Wichtige Einschränkungen:

- Post umfasst nur **16 Releases**, davon vier Diktate über 15 s; kleine Stichprobe, keine
  kontrollierte Last.
- Die Dauer Recording-Start→Release betrug im Mittel **5,275 s vorher** gegenüber **8,395 s
  nachher**. Längere Diktate erklären mehr Ticks und längere Textdeltas.
- Bei schnell aufeinanderfolgenden Diktaten kann die globale `_stop_entered_at`-Basis durch den
  nächsten Release überschrieben werden. Beispiel 12:47:50: Der alte Job meldet `paste +2.641s`,
  der folgende Job zunächst `finalizer exec acquired +2.642s`. Deshalb werden unten Verteilungen
  gleichnamiger Tracepunkte verwendet, nicht jede einzelne Differenz als unabhängige Jobkette
  interpretiert.
- `type` und `copy` sind nicht dieselbe User Experience: Bei `copy` landet der Rest nur in der
  Zwischenablage, ohne automatischen Chord. Deshalb ist der Gesamtvergleich allein nicht
  ausreichend.

### 1.2 Release- und Ausgabelatenz

| Trace / Stichprobe | PRE gesamt 08–11 | PRE `type` 08–09 | PRE `copy` 09–11 | POST 12:36–12:55 |
|---|---:|---:|---:|---:|
| Releases / Traceketten | 114 | 61 | 53 | 16 |
| `finalizer exec acquired`, p50 | 0,194 s | 0,080 s | 0,384 s | **0,032 s** |
| dto., Mittel / p90 | 0,548 / 1,761 s | 0,280 / 1,383 s | 0,857 / 1,761 s | **0,352 / 1,422 s** |
| Release→`finalize end`, p50 | 0,890 s | 0,462 s | 2,301 s | **0,585 s** |
| dto., Mittel / p90 | 1,514 / 3,667 s | 0,635 / 1,383 s | 2,525 / 4,734 s | **1,042 / 2,011 s** |
| finales `output`, Mittel / p50 | 1,175 / 1,132 s | 2,170 / 2,084 s | 0,047 / 0,035 s | **3,773 / 2,949 s** |
| separate `space`, Mittel / p50 | 0,691 / 0,691 s | 0,691 / 0,691 s | entfällt | **0,697 / 0,696 s** |
| Release→`paste`/Chime, Mittel | 3,072 s | 3,465 s | 2,619 s | **5,590 s** |
| Release→`paste`/Chime, p50 / p90 | 2,950 / 4,987 s | 3,259 / 4,987 s | 2,323 / 4,871 s | **4,413 / 10,003 s** |

**Befund:** A verbessert die vorgesehene Wartekomponente. Der End-to-End-Chime ist im Post-Sample
dennoch langsamer. Selbst gegen den vergleichbareren frühen `type`-Pfad stieg der Median von
3,259 auf 4,413 s; gegen das gemischte Gesamtfenster von 2,950 auf 4,413 s.

Der Grund steht unmittelbar im Beispiel aus der Frage:

```text
12:52:16,909 Live tick cancelled by release
12:52:16,912 latency[finalizer exec acquired] +0.152s
12:52:17,336 latency[finalize end] +0.576s
12:52:17,340 latency[rev 0/34]
12:52:19,226 latency[output 1.886s]
12:52:19,915 latency[space 0.690s]
12:52:19,916 latency[paste] +3.156s
```

Von 3,156 s bis zum Chime entfallen nur 0,576 s auf Release→Finalize-Ende, aber **2,576 s auf
Finales Tippen und Leertaste**. Zudem belegt `rev 0/34`, dass entgegen des visuellen Eindrucks noch
34 Zeichen des finalen Textes fehlten.

Beim langen Diktat ist der Effekt extremer:

```text
12:50:39,676 latency[live paused] +15.608s
12:50:49,223 latency[finalize end] +1.349s
12:50:49,227 latency[rev 3/296]
... latency[output 11.193s]
... latency[paste] +13.967s
```

### 1.3 Live-Ticks und Live-Typing

| Kennzahl | PRE 08–11 | POST 12:36–12:55 |
|---|---:|---:|
| echte Live-Ticks / Diktat, Mittel / p50 | 2,044 / 1 | 3,250 / 2 |
| Tickdauer, Mittel / p50 / p90 | 0,596 / 0,445 / 1,120 s | 0,555 / 0,478 / 0,884 s |
| Abstand aufeinanderfolgender Tick-Enden, Mittel / p50 | 2,011 / 1,755 s | 1,933 / 1,746 s |
| nichtleeres Live-Typing, Mittel / p50 / p90 | 1,321 / 1,233 / 1,880 s | 2,039 / 1,781 / 2,825 s |
| Live-Cancel durch Release | 0 | 3 |
| `live paused` | 4 | 4 |

Die höhere Tickzahl nachher folgt vor allem aus längeren Diktaten. **B erzeugt in dieser Stichprobe
keine spürbar geringere mediane Kadenz:** Der p50-Abstand der Tick-Enden ist mit 1,746 statt
1,755 s praktisch gleich. Bei einzelnen Ticks über 0,75 s greift `2 × Tickdauer` über die
1,5-s-Untergrenze hinaus; das erklärt einzelne größere Lücken, aber nicht die gemessene allgemeine
Chime-Verschlechterung.

Die Codeformel steht in `asr.py:653-658`:

```python
paced = self.asr_interval_adaptive_k * self._last_tick_s
return max(self.interval_s, min(paced, self.asr_interval_max_s))
```

Die Bedingung nutzt neu aufgenommenes Audio seit dem Tick-Start (`asr.py:763-766`), nicht eine feste
Wanduhr. Deshalb setzt sich ein Abstand typischerweise aus Intervall plus Inferenz-/Typing-Zeit
zusammen.

### 1.4 Erklärung der drei Beschwerden nach geschätztem Beitrag

1. **Chime nach finalem Tippen und Leertaste — Hauptursache der späten Fertigmeldung.**
   `controller.py:650-657` ruft `_play_done_sound()` erst nach
   `DeltaTyper.finalize()` auf. `typing.py:943,950-953` tippt das Delta und startet danach für die
   Leertaste erneut dotool. Diese Semantik ist nicht neu, wird aber durch lange Post-Deltas teuer.
2. **Committed-Backlog plus Präfixkonflikte — Hauptursache für spätes/fehlendes Live-Wachstum.**
   `typing.py:778` verwendet nur `text[:committed_len]`; zwei Agreements benötigen mindestens zwei
   ASR-Ergebnisse, und die Commit-Logik schneidet zusätzlich an einer Wortgrenze
   (`asr.py:1124-1159`). Bei einem Konflikt verwirft `typing.py:786-791` das Update komplett:
   `committed live: prefix conflict - update discarded`. Das Journal zeigt dies 12:49:45 einmal und
   12:50:34–12:50:39 viermal in Folge. Dadurch wächst der finale Rest zusätzlich.
3. **15-s-FP32-Totzone — Hauptursache bei langen Diktaten.** `asr.py:749-762` liefert oberhalb des
   Limits kein Live-Ergebnis mehr. Belege: `live paused` 12:48:44 nach 15,728 s, 12:49:54 nach
   17,730 s und 12:50:39 nach 15,608 s. Der Kommentar in der Config begründet 15 s mit einem
   **INT8**-Budget von 0,75 s, obwohl produktiv FP32 läuft (`config.toml:22,52`). Das Limit war
   bereits vor A/B aktiv.
4. **Adaptive Ticks — kleiner, situativer Beitrag zum Live-Eindruck, nicht zur Chime-Regression.**
   Die mediane Kadenz blieb gleich; lange Ticks können dennoch den nächsten sichtbaren Commit
   weiter hinausschieben. A wiederum reduziert nachweislich die Release-Wartezeit.
5. **Restarts — nur Startup-/Abbruchbeitrag, keine Erklärung für 12:52.** Erster Reload:
   12:35:57,265 `Loading model warm ...` bis 12:36:01,377 `Parakeet model loaded.` = **4,11 s**.
   Zweiter: 12:46:10,392 bis 12:46:29,168 = **18,78 s**, einschließlich DNS-Retrys; vom eigentlichen
   `Loading Parakeet ...` 12:46:14,090 bis loaded = **15,08 s**. Die späteren Diktate nutzten das
   bereits warme Modell.

**Restart-Ursache 12:46:** kein OOM und kein eigenständiger Servicecrash. Um 12:45:34 stoppte eine
vollständige GNOME-Session unter anderem `gnome-shell`, `pipewire`, `wireplumber`, `dbus` und
`stt-button`. Der `ENODEV` auf `/dev/input/event9 (keyd virtual keyboard)` ist Folge dieses
Session-Teardowns. Der neue User-Manager `systemd[1874]` startete die Unit 12:46:10 mit PID 1960.
Die Unit hat `Restart=on-failure`; hier greift aber der normale Start in der neuen Session.

---

## 2. Warum dotool für 34 Zeichen 1,9 s braucht

### 2.1 Belegter Mechanismus

`DotoolTyper.type_text()` erzeugt einen Payload (`typing.py:259-276`):

```text
typedelay 20
typehold 15
type Gib mir bitte milestones und EtAs.
```

`_send_raw()` startet dafür jedes Mal einen neuen `dotool`-Prozess und berechnet sein Timeout mit
derselben Kostenformel (`typing.py:312-334`):

```python
type_s = typed_chars * (self._typedelay_ms + self._typehold_ms) / 1000
```

Die installierte Binary ist identisch mit `/home/salmon/projects/Whisper-local/dotool/dotool`
(SHA-256 gleich, Version `1.6-2-g4df1f7f0bcb5`). In `dotool.go:356-376` läuft pro Unicode-Rune:

1. `Utf32ToKeysym`, dann `getChord(keymap, sym)`; bei Großbuchstaben enthält der Chord Shift.
2. `Chord.KeyDown`: Modifier und Key über uinput drücken.
3. `time.Sleep(typehold)` = **15 ms**.
4. `Chord.KeyUp`.
5. `time.Sleep(typedelay)` = **20 ms**.

Falls kein direkter Chord existiert, versucht dotool zwei Dead-Key-Chords; diese kosten zwei
Hold-/Delay-Phasen. Unbekannte Zeichen werden mit Warnung verworfen. Die Python-Seite selbst tippt
also keine Chords, sondern dotool übersetzt jedes Zeichen über XKB in Tastendrücke.

Damit:

```text
34 × (20 + 15) ms = 1,190 s
gemessener Rest     = 1,886 - 1,190 = 0,696 s (Prozessstart/XKB/uinput)
```

Über alle 16 Post-Ausgaben beträgt `output - 0,035 × Zeichen` im Mittel **0,726 s**. Beim frühen
`type`-Fenster waren es **0,701 s**. Die Formel erklärt die Messwerte fast exakt. Zusätzlich kostet
`key space` als zweiter Prozess nochmals rund 0,69 s.

Das längere Post-Sample hat im Mittel **87,1 finale Zeichen** (`rev .../N`), das frühe
`type`-Sample **42,0**. Allein diese Verdopplung erklärt rund 1,58 s zusätzliche Tippzeit.

### 2.2 Was eine Halbierung ermöglichen würde, ohne Terminal-Guards zu brechen

- **Direkte Halbierung der variablen Tippzeit:** `typedelay/typehold` von 20/15 auf etwa **10/7 ms**
  senken: 35 → 17 ms pro Zeichen. 34 Zeichen kosten dann 0,578 statt 1,190 s; mit Start etwa
  1,27 statt 1,89 s. `_final_mode()`, `strip_paste_markers()` und die Terminalregel bleiben
  unverändert. Risiko: genau die Shift-/Dropped-Key-Probleme, für deren Vermeidung die Werte laut
  Kommentar erhöht wurden; daher nur mit Zeichenfehler-Gate.
- **Größerer Hebel für lange Deltas:** Im Nicht-Terminal einen langen finalen Rest per vorhandenem
  `wl-copy` + Paste-Chord ausgeben, statt alle Zeichen zu simulieren. Der Terminalpfad muss weiterhin
  `copy` ohne Chord verwenden; die Bracketed-Paste-Bereinigung bleibt am selben Eingang. Risiko ist
  damit nicht der Terminalguard, sondern das Überschreiben der Zwischenablage und die zuverlässige
  Chord-Wahl in Nicht-Terminals.
- **Fixkosten:** Leertaste in denselben Type-Payload aufnehmen spielt weitere rund **0,69 s** pro
  Diktat ein. Ein persistenter `dotoold` könnte den Prozessstart sparen, braucht aber Lifecycle-,
  FIFO-Serialisierungs- und Layout-Regeln; das ist riskanter als das Zusammenfassen eines Payloads.

---

## 3. Voice Keyboard: Was die APK tatsächlich belegt

Die frühere Bezeichnung „Voice Translayer“ war falsch; gemeint ist **Voice Keyboard**. Die APK wurde
nur statisch gelesen. Temporäre Analyseergebnisse liegen ausschließlich unter `/tmp`.

### 3.1 APK-Evidence (keine Inferenz)

- **Identität:** Paket `de.voicekeyboard`, Version `0.1.1` (`versionCode=2`), Main-Activity
  `de.voicekeyboard.ime.SetupActivity`. Eingebettete Git-Revision:
  `e6c4ab676419b61d2c65b69907c04d5c76ad37fb`. Eine Projekt-URL ist in den geprüften Metadaten
  nicht enthalten.
- **Engine:** native `libonnxruntime.so` für arm64-v8a (**34.208.672 Bytes**) und x86_64
  (**40.827.936 Bytes**) plus JNI. DEX-Klassen: `ParakeetAsrEngine`, `OnnxSessions`, `TdtGreedy`.
- **Modell:** `ModelManifest.BASE_URL` ist exakt
  `https://huggingface.co/istupakov/parakeet-tdt-0.6b-v3-onnx/resolve/main/`.
  Es werden vier Dateien nachgeladen, nicht als Modell-Assets mitgeliefert:
  - `encoder-model.int8.onnx`: **652.183.999 Bytes**
  - `decoder_joint-model.int8.onnx`: **18.202.004 Bytes**
  - `nemo128.onnx`: **139.764 Bytes**
  - `vocab.txt`: **93.939 Bytes**

  Die APK selbst ist 81.148.470 Bytes groß und enthält nur Layouts/Emoji, keine großen Modell-Assets.
  **INT8 ist damit sicher; ein kleineres Whisper-/Vosk-/Sherpa-Modell ist nicht im Spiel.**
- **Whole-Buffer statt Encoder-Streaming:** `DictationController.window()` erzeugt
  `Arrays.copyOf(this.pcm, this.totalSamples)`. `feedAudio()` hängt neue Samples an und ruft für das
  Partial `transcribe(window())` auf; `stop()` ruft erneut `transcribe(window())` auf. Der PCM-Puffer
  wird bei `start()` geleert, nicht an Satzpausen. Der Encoder-Vertrag besitzt nur `audio_signal`
  und `length` als Eingänge und `outputs`/`encoded_lengths` als Ausgänge — keine Cache-Tensors.
- **Taktung:** `DictationCoordinator.TICK_SAMPLES_1S = 16000`; der Default ist `tickSamples=16000`.
  Der Controller-Default ist 2,0 s Mindestaudio für Partials (die Decompiler-Anzeige des Double-Bits
  ist irreführend; der Konstruktor enthält `4611686018427387904`). Damit: erstes Partial frühestens
  nach 2 s, danach grundsätzlich ein Whole-Buffer-Decode pro 1-s-Audio-Tick.
- **Commit und sichtbare Ausgabe:** `COMMIT_AGREEMENTS=3`, `COMMIT_HOLDBACK_WORDS=2` — konservativer
  als STT-Button. Entscheidend: `writeHypothesis()` ruft trotzdem `typer.revise(p7.getText())` auf.
  `InputConnectionSink.replaceBeforeCursorSync()` verwendet `deleteSurroundingText(...)` und
  `commitText(...)` direkt im IME-Prozess. Auch instabile Partials werden daher sichtbar und revidiert.
- **Keine APK-Grundlage für spätere TalkType-Features:** In der installierten 0.1.1 ist im
  untersuchten Dictation-Controller weder ein Encoder-Cache noch ein Satzpausen-Reset zu finden;
  `DecodePace`/`segmentBoundary` sind im DEX nicht vorhanden. Der ältere Bericht
  `.pi/reports/rtf-latency-analysis.md` beschreibt einen anderen/späteren Source-Stand unter
  `/tmp/talktype` und darf nicht ungeprüft auf diese APK übertragen werden.

### 3.2 Architekturvergleich und dominanter Unterschied

| Dimension | STT-Button jetzt | Voice Keyboard 0.1.1 (APK) |
|---|---|---|
| Modell | Parakeet 0.6B v3 FP32/HF | Parakeet 0.6B v3 **INT8/ORT** |
| Inferenz | ganzer Puffer je Tick und Final | ebenfalls ganzer Puffer je Tick und Final |
| Partial-Takt | 1,5 s; B bis 2× Tickdauer, max. 6 s | 1-s-Ticks nach initialen 2 s |
| sichtbarer Text | nur committed Präfixe | komplette Hypothese, revidierbar |
| Commit | 2 Agreements, letzte Wortgrenze | 3 Agreements, 2 Wörter Holdback |
| lange Diktate | kein Live-ASR mehr ab 15 s | kein 15-s-Stopp im Controller |
| Ausgabe | externe dotool-Prozesse, 35 ms/Zeichen + Start | direkte IME-`commitText`-Revision |
| Final | nochmals ganzer Puffer | nochmals ganzer Puffer |

**Klar als Inferenz markiert:** Der gefühlte Vorsprung wird am plausibelsten zuerst von der
**Ausgabearchitektur** erklärt: kontinuierlich sichtbare/revidierbare Partials und direkte Editor-
Commits vermeiden sowohl den committed-only-Backlog als auch die 35-ms/Zeichen- und
Prozessstartkosten. Danach kommt **INT8** (mehr Live-Reserve, kein hartes 15-s-Budget-Problem) und
der kürzere 1-s-Takt. Ein Streaming-Encoder-Cache kann den Vorsprung dieser APK nicht erklären,
weil er nachweislich fehlt. Auch die konservativere Android-Commit-Regel kann nicht der Grund sein;
sichtbar wird dort eben nicht nur der committed Anteil.

Die vom Betreiber empfundene „gleiche Qualität“ ist ein relevanter Praxisdatenpunkt, aber kein WER-
Nachweis. Sie ist besonders deshalb interessant, weil STT-Button und APK dasselbe INT8-Parakeet-
Modellreferenz verwenden.

---

## 4. Priorisierte Vorschläge (nur Analyse, keine Implementierung)

### 1) Finalen Nicht-Terminal-Rest nicht mehr zeichenweise tippen

**Skizze:** In `DeltaTyper.finalize()` für `focus=other` oberhalb einer kleinen Delta-Schwelle den
vorhandenen Clipboard-Paster mit korrektem GUI-Paste-Chord nutzen; kurze Deltas dürfen weiter
getippt werden. Terminal/unknown unverändert `copy` ohne Chord; Bracketed-Paste-Filter unverändert.
Trailing Space in denselben Paste-/Type-Vorgang legen.

**Erwarteter gefühlter Gewinn:** größter und sofortiger Chime-Gewinn: beim 296-Zeichen-Fall etwa
**10–11 s**, bei 34 Zeichen einschließlich separater Leertaste grob **1 s**. Unabhängig von ASR-
Qualität und A/B.

**Risiko:** Clipboard-Verlust, falsche/ignorierte Chords in einzelnen GUI-Apps, kein Retry nach
unbekanntem Teil erfolg (bestehender Schutz muss bleiben). Terminal darf niemals automatisch
Ctrl+V erhalten.

**Messplan:** Gleiche Fokusart und gleiche Textlängen; `output`, `space`, Release→`paste`, p50/p90/
p95; zusätzlich Fehl-/Doppeleinfügungen und Clipboard-Fehler. Vorher/nachher mindestens 30 Diktate
in Längen bins <40, 40–100, >100 Zeichen.

### 2) Verbleibendes dotool-Tippen und seine Fixkosten halbieren

**Skizze:** Zuerst Trailing Space in den Type-Payload kombinieren. Danach gestuft 20/15 → 15/10 →
10/7 ms testen; ein persistentes dotool nur erwägen, falls die Restlatenz danach noch dominiert.

**Erwarteter Gewinn:** rund 0,69 s pro Diktat durch den entfallenen Space-Prozess; 10/7 ms halbiert
die variable Zeichenzeit (34 Zeichen: 1,19 → 0,58 s). Live-Typing profitiert ebenfalls.

**Risiko:** Groß-/Kleinschreibung, Shift-Prellen, verlorene Zeichen, Dead-Keys; kein stilles
Aktivieren ohne Qualitätsgate.

**Messplan:** deterministische Textmatrix mit `So`, Großschreibung, Umlauten, ß, Akzenten,
Satzzeichen und 300 Zeichen; sichtbaren Zieltext zeichengenau vergleichen. Erst bei null Fehlern über
mehrere Ziel-Apps die produktive Latenzmessung wiederholen.

### 3) Live-Ausgabe dem Android-Prinzip annähern und Präfixkonflikte beseitigen

**Skizze:** Nicht nur den stable Präfix zu betrachten: Entweder kontrolliert `live_typing_mode="full"`
als Experiment oder gezielt beim derzeitigen Konflikt einen begrenzten Live-Diff mit Backspaces
ausführen. Die 15-s-Totzone nicht einfach mit FP32 beliebig verlängern, solange Whole-Buffer-Ticks
dann länger als ihr Intervall sind.

**Erwarteter Gewinn:** größter Gewinn am **Live-Gefühl**; Text erscheint wie bei Voice Keyboard auch
vor Stabilisierung. Kleinere finale Deltas reduzieren zusätzlich den Chime-Block. Kein Rückgriff auf
eine neue ASR-Qualität nötig.

**Risiko:** Flackern, Backspaces und Cursorfehler; bei schlechten Hypothesen mehr sichtbare
Revisionen. Genau deshalb ist ein begrenzter Konflikt-Diff weniger aggressiv als dauerhaft `full`.

**Messplan:** Zeit von gesprochenem Wort bis sichtbarem Text braucht einen neuen Trace, z. B.
Hypothesen-/Commit-/Typing-Zeit und `to_add`-Länge. Zusätzlich `prefix conflict`-Rate, Backspaces
pro Diktat, finale Restzeichen und subjektive Fehlerbewertung erfassen.

### 4) B isoliert zurücknehmen, A behalten

**Skizze:** Für eine kontrollierte Gegenprobe `asr_interval_adaptive_k=0.0` (fest 1,5 s), während A
aktiv bleibt. Kein pauschaler Revert beider Änderungen.

**Erwarteter Gewinn:** klein: bestenfalls seltene zusätzliche Live-Lücken von etwa 0,5–1,5 s
vermeiden. Auf die gemessene mediane Tickkadenz und den dotool-dominierten Chime ist kaum Gewinn zu
erwarten.

**Risiko:** mehr CPU-Backpressure bei langen FP32-Ticks; A mildert die Folge beim Release, aber nicht
die Live-Last.

**Messplan:** gleiche Clips/Längen und ruhige Maschine; Tick-Ende-Kadenz p50/p90, Tickdauer,
`finalizer exec acquired`, Live-Backlog. Nur bei reproduzierbarem Gewinn B dauerhaft ändern.

### 5) INT8 mit einem neuen, belastbaren Qualitätsgate erneut prüfen

**Skizze:** Zunächst Offline-A/B desselben vorhandenen Korpus, danach Opt-in-Produktion. Optional
INT8 für Live und FP32 fürs Finale, falls RAM und sichtbare Live/Final-Differenzen akzeptiert sind.
Erst danach das Live-Limit von 15 s auf z. B. 30 s erweitern.

**Argument dafür:** Die APK verwendet exakt dieselbe INT8-Modellfamilie/-quelle und fühlt sich für
den Betreiber gleichwertig an. Frühere lokale Messungen gaben RTF ≈ 0,05 statt ≈ 0,12. Die alte
WER-Grenze (laut Vorgabe INT8 0,094 vs. FP32 0,067 auf nur 10 Clips, single-speaker) ist ein ernster
Hinweis, aber statisch und populationsschwach. Die 1683 FP32-Diktate liefern keine INT8-Qualitäts-
aussage; ihre vorhandenen Audios können jedoch paarweise neu bewertet werden.

**Erwarteter Gewinn:** ungefähr Faktor 2 bei Tick/Final-ASR, mehr Live-Reserve und kleinere Release-
ASR; bei langen Diktaten oft 0,5–1 s. Der 11-s-dotool-Block verschwindet dadurch **nicht**.

**Risiko:** WER-/Sprachwechsel-/Satzzeichenqualität; Hybrid kann Live/Final-Konflikte erhöhen und
benötigt beide Modelle im RAM.

**Messplan:** paarweise Clips, mehrere Sprecher und DE/EN-Mix; WER-Differenz mit Konfidenzintervall,
nicht nur Mittel über 10 Clips. Zusätzlich Satzzeichen-, Zahl-/Eigennamen- und Sprachwechsel-Fehler,
sowie manuelle Bewertung einer festen Stichprobe. Erst nach Gate Live-RTF, Tickdauer und tatsächliche
Textqualität im Betrieb beobachten.

### 6) Minimaler echter Streaming-Decode nur als spätere, separate Option

**Skizze:** Cache-aware Parakeet-/FastConformer-Export mit Encoder-State, kleine Chunks und
persistente Decoder-State-Verwaltung; kein erneuter Versuch der bereits gescheiterten freien
Fenster-Stitching-Variante.

**Erwarteter Gewinn:** konstante statt quadratische Live-Arbeit und strukturell keine FP32-
Totzone; wichtig vor allem für sehr lange Diktate.

**Risiko/Aufwand:** großer Engine-Rewrite, neuer Export, mögliche WER-Änderung. **Nicht nötig, um
Voice Keyboard 0.1.1 zu erreichen**, denn diese APK arbeitet selbst whole-buffer. Daher erst nach
(1)–(3) und einem geklärten INT8-Gate.

**Messplan:** erst Offline-Machbarkeit: identische Ausgabe gegen Whole-Buffer auf kurzen Clips,
dann RTF/Live-Wortlatenz über 15/30/60/120 s und Naht-/Halluzinationsgate. Kein Produktionswechsel
ohne Qualitäts- und Langdiktatnachweise.

---

## QWEN-SUBAGENTEN-BITTE

Der Orchestrator möge die folgenden **rein lesenden** Prüfungen auf `my/my-qwen38-flash-next`
ausführen; keine Subagents durch diesen Berichtsautor, keine Tests, kein Service-Eingriff und keine
Repo-Änderungen:

1. **Unabhängige Journal-Rechnung:** Mit `journalctl --user -u stt-button` und `nice -n 19` die
   Fenster 08:00–11:00 und 12:36–12:55 erneut aggregieren. Zu prüfen: N=114/61/53/16, p50/Mittel
   für `finalizer exec acquired`, `finalize end`, `output`, `space`, `paste`; Live-Tickzahl und
   Tick-Ende-Kadenz. `type`/`copy` strikt trennen und die Queue-/Global-Trace-Problematik explizit
   kennzeichnen. Erwartetes Ergebnis: Abweichungen zu Tabelle 1.2/1.3 mit exakten Logbelegen.
2. **APK-Gegenprobe:** `/home/salmon/Downloads/voice-keyboard-0.1.1.apk` nur statisch lesen
   (Entpacken/Analyse ausschließlich `/tmp`). Bestätigen oder widerlegen: Paket/Version, INT8-
   Modellmanifest und Bytegrößen, `window()=copyOf(pcm,totalSamples)` für Partial und Final,
   16000-Sample-Tick, `typer.revise(...)` sowie direkte `deleteSurroundingText`/`commitText`.
   Ausdrücklich nach echten Encoder-Cache-Eingängen, `DecodePace` und Satzpausen-Reset suchen;
   Abwesenheit nur für diese APK-Version behaupten.
3. **Guard-sichere Ausgabeoptionen:** Callgraph in `src/stt_button/typing.py` lesen und die
   kleinsten hypothetischen Patchpunkte benennen, damit (a) `focus=other` lange Deltas pasten kann,
   (b) Terminal weiterhin nur `copy` ohne Chord erhält, (c) Bracketed-Paste-Marker weiterhin vor der
   Delta-Berechnung entfernt werden und (d) kein Retry nach unbekanntem Paste-Teilerfolg entsteht.
   Zusätzlich prüfen, ob Trailing Space bei `type` sicher in denselben dotool-Payload kann.
   Nur Design und Risiken, kein Code und keine Dateischreibung im Repo.
