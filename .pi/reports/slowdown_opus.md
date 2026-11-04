# Slowdown-Diagnose nach Phase-2 A+B (2026-11-04, read-only)

Grundlage: `journalctl --user` 12:30–12:58, `src/stt_button/controller.py` (L395–420, L600–665), `typing.py` (L164–321), `asr.py` (L655–658), `~/.config/stt-button/config.toml`. Nichts gemessen, nichts geändert.

## 0. Kernbefund (TL;DR)
**Der gefühlte Zeitverlust liegt nicht bei der ASR, sondern beim Tippen über dotool.** Jedes Zeichen kostet `typedelay 20 + typehold 15 = 35 ms`, und jeder dotool-Aufruf kostet zusätzlich ~0,7 s Prozessstart. Das Signal (Chime) kommt erst, wenn alles fertig getippt ist **und danach noch ein eigener dotool-Prozess für die Leertaste** gelaufen ist.

| Dictation | finalize | output (dotool type) | space | Chime nach Release |
|---|---|---|---|---|
| 12:52:54 (34 Zeichen) | 0,287 s | 1,885 s | 0,686 s | **+2,86 s** |
| 12:52:2x (34 Zeichen) | 0,350 s | 1,879 s | 0,702 s | +2,94 s |
| 12:50:47 (23,7 s Audio, 296 Zeichen) | 1,349 s | **11,193 s** (+0,711 s Backspace) | 0,710 s | **+13,97 s** |

Rechnung: 34 × 35 ms = 1,19 s + ~0,7 s Prozessstart = 1,89 s ✔ (passt exakt zum Log). Beim langen Text: ~0,7 s + ~300 × 35 ms ≈ 11 s ✔.

## 1. Ursachen der drei Beschwerden

### (a) Chime kommt spät
- Reihenfolge in `_finalize_worker`: `finalize()` → `_delta_typer.finalize()` (Backspaces in einem dotool-Prozess, den Rest tippen in einem zweiten, die Leertaste in einem dritten) → `_trace("paste")` → `_play_done_sound()`. Der Chime wird also **nach** der letzten Leertaste abgespielt.
- Warum der Text schon „fertig“ aussieht, bevor der Chime kommt: Das Auge sieht die letzten Wörter, aber danach läuft noch der Rest-Payload (Tippen mit 35 ms pro Zeichen bis zum Prozessende, plus dotools eigene Wartezeit beim Beenden). Danach kommt der **separate `key space`-Prozess mit ~0,7 s**. Allein dadurch kommt der Chime ~0,7 s nach dem sichtbaren Ende.
- Dazu wartet die finale Ausgabe auf einen noch laufenden Live-Tipp-Vorgang. Beispiel 12:30:57: Release, dann `live typing end +1,365 s` und erst danach `finalizer exec acquired`. Laufendes Live-dotool-Tippen wird **nicht** abgebrochen; A bricht nur den ASR-Tick ab.

### (b) Ab ~10–15 s keine Live-Updates mehr, das finale Ergebnis kommt sehr spät
Belegt durch den Lauf 12:50:24:
1. **Totzone schon vor 15 s:** Ab +10,9 s steht im Log `committed live: prefix conflict - update discarded` (4×). Die Neu-Transkription des ganzen Buffers hat bereits committeten Text umgeschrieben („gehen, und“ → „gehen. Und“ → „gehen und“). Der committed-Modus verwirft solche Updates, deshalb steht der Live-Text schon ab ~8 s still (letzter erfolgreicher Typ-Vorgang bei +10,05 s).
2. Bei +15,6 s greift `live paused` und es wird „...“ getippt. Danach gibt es bis zum Release (23,7 s) **keine ASR mehr**.
3. Nach dem Release dauert finalize nur 1,35 s (FP32 bei 23,7 s Audio, RTF ≈ 0,057). Danach folgen aber **3 Backspaces (0,71 s) plus ~216–300 Zeichen mit 35 ms (11,2 s) plus Leertaste (0,71 s)**. Der „späte Blob“ kommt also zu ~90 % vom dotool-Tippen und nur zu ~10 % von FP32.
4. FP32 trägt indirekt bei: Live-Ticks dauern bei 7–15 s Audio 0,6–1,6 s. Dadurch wächst der Abstand zwischen den Ticks, und die Neuschreibungen des ganzen Buffers erzeugen genau die Präfix-Konflikte, die die Live-Ausgabe stoppen.

### (c) Hat B (k = 2.0) die Lage verschlechtert? Gab es eine Wechselwirkung mit dem Restart um 12:46?
- B setzt das Intervall auf `max(1,5, min(2 × letzte_Tickdauer, 6,0))`. Beobachtete Tick-Abstände im Lauf 12:50: 1,65 / 1,96 / 3,4 / 2,5 / 1,55 / 1,57 / 1,62 s. Gegenüber festen 1,5 s ist das **leicht spärlicher (+0,1 bis +1,9 s pro Update)**. Dazu kommt das Commit-Agreement (2 Übereinstimmungen), also ≈ 2 × Intervall Rückstand. B verschlechtert die Live-Wahrnehmung **plausibel, aber nur geringfügig**. Der Haupteffekt bleibt (a) und (b). Die Totzone durch Präfix-Konflikte ist ein eigenständiger Effekt; B verstärkt ihn nur wenig.
- **12:46-Restart = kein Absturz, kein OOM.** Um 12:45:34 lief ein vollständiger GNOME-Session-Logout (pipewire, wireplumber, gnome-shell, dbus wurden gestoppt und der user-systemd beendet). Um 12:46:10 startete der neue user-Manager (PID 1874) alle Units neu, auch stt-button (PID 1960). Ab 12:46:14 gab es für einige Sekunden einen DNS-Fehler (HF-Lookup). Die Konfiguration ist dieselbe (gleiche Datei), deshalb gibt es **keine Wechselwirkung mit B**. Hinweis: Neben stt-button startet auch `voxtype.service`, ein zweiter Push-to-Talk-Daemon. Möglicher CPU- oder Tasten-Konkurrent, kurz prüfen.

## 2. Vergleich STT-Button vs. Android-Voice-Typing (Voice-Translayer-Klasse)
Das Verzeichnis `~/voice-translayer` ist nicht vorhanden (`ls ~ | grep -i voice` und `find / -maxdepth 4 -iname '*translayer*'` liefern nichts). Die Android-Spalte ist deshalb **abgeleitet (inferred)**, nicht aus dem Code belegt.

| Dimension | STT-Button (belegt) | Android-typisch (inferred) |
|---|---|---|
| Decode | ganzen Buffer bei jedem Tick neu transkribieren (O(n²)), Live-Grenze 15 s | Streaming-Encoder mit Cache/Chunks, O(n) |
| Emission | committed nach 2 Übereinstimmungen, Konflikt → Update verworfen | Partial-Text als Composing-Region, wird in place ersetzt |
| Modell | FP32 auf der CPU | INT8/quantisiert, NNAPI/GPU |
| **Output-Pfad** | **dotool uinput, 35 ms/Zeichen + 0,7 s/Prozess** | **IME `commitText()`/`setComposingText()`: ganzer String atomar, ~ms** |
| Abschluss | Chime nach Leertaste | sofort |

**Dominanter Unterschied: der Output-Pfad.** Die Android-IME schreibt Text praktisch ohne Zeitkosten. STT-Button simuliert Tastenanschläge mit 35 ms pro Zeichen. Bei 300 Zeichen macht das 11 s gegenüber ~0 s aus. Das ist mehr als alle ASR-Unterschiede zusammen (finalize ≤ 1,35 s).

## 3. Maßnahmen (nach Wirkung geordnet)
1. **Langen Finaltext per Clipboard + Paste-Chord statt tippen** (z. B. `final_paste_threshold_chars = 40`: darüber `wl-copy` + `ctrl+v` in einem dotool-Prozess; die Copy-Logik ist bereits vorhanden, Mode `copy`). Gewinn: **5–11 s** bei langen Texten, ~1 s bei kurzen. Risiko: Terminals (ctrl+shift+v), Clipboard wird überschrieben. Messen: `latency[output]` p50/p95 im Journal.
2. **Leertaste in den Type-Payload aufnehmen** (`type …text \n` bzw. `text + " "` in einem Prozess) und Backspaces mit Type kombinieren (wie bei OPT1 `backspace_and_key`). Gewinn: **0,7–1,4 s** bei jedem Diktat, auch beim Chime. Risiko: gering. Messen: `latency[space]` entfällt, `paste` wird um ~0,7 s kürzer.
3. **typedelay/typehold senken** (20/15 → z. B. 8/6 ms). Gewinn: ~60 % der Tippzeit (34 Zeichen: 1,19 → 0,48 s; 300 Zeichen: 10,5 → 4,2 s). Risiko: verschluckte oder vertauschte Zeichen; deswegen wurden die Werte ursprünglich angehoben. Messen: A/B mit festem Testtext in einem Editor, Zeichenfehlerrate.
4. **Bei Release das laufende Live-Tippen abbrechen** (A auf dotool erweitern: Live-Typer-Prozess kill/terminate, danach finalize per Diff). Gewinn: bis **~1,4 s** (12:30:57). Risiko: DeltaTyper-Zustand weicht vom tatsächlich getippten Text ab, deshalb muss er danach neu synchronisiert werden. Messen: `stop entered → finalizer exec acquired`.
5. **Präfix-Konflikt-Totzone beheben**: Bei einem Konflikt nicht verwerfen, sondern in der Live-Phase ebenfalls per Diff korrigieren. Alternativ commit-agreement mit einer Normalisierung von Satzzeichen und Groß-/Kleinschreibung vergleichen. Gewinn: Live-Text läuft bis 15 s weiter, der Final-Diff wird kleiner (weniger Zeichen nach dem Release, also **mehrere Sekunden** bei langen Texten). Risiko: Backspaces in der Live-Phase. Messen: Anzahl `prefix conflict` pro Diktat.
6. **Ehrliche Option: INT8 oder echtes Streaming.** INT8 halbiert grob die Tick-Kosten, erlaubt eine Live-Grenze von 30 s und weniger Neuschreibungen. Echtes Streaming (Cache-Aware Parakeet/FastConformer-Chunks) beseitigt O(n²) und die Totzone grundsätzlich. Gewinn auf den Live-Eindruck: hoch. Auf die Release→Chime-Zeit: nur ≤ 1 s, **solange (1)–(3) nicht umgesetzt sind**. Die FP32-Entscheidung muss daher **nicht zuerst** angefasst werden. Risiko: Qualität (das Gate hatte INT8 zuvor abgelehnt) und Aufwand. Messen: WER auf dem Gate-Korpus plus Tick-Dauer.
- Optional: B zurück auf k = 0 (feste 1,5 s). Gewinn ≤ 0,5 s Live-Gefühl. Risiko null. Damit lässt sich B isoliert prüfen.

## 4. QWEN-SUBAGENTEN-BITTE (read-only)
1. Über alle Diktate seit 12:36 aus dem Journal je Diktat extrahieren: Audio-Dauer, Anzahl Zeichen (`rev x/N`), `output`, `space`, `backspace`, `paste`. Regression `output ≈ a + b·N` fitten; erwartet a ≈ 0,7 s, b ≈ 0,035 s.
2. Anzahl `prefix conflict - update discarded` pro Diktat und Audiosekunde, in der der erste Konflikt auftritt; vor (bis 12:35) gegenüber nach B (ab 12:36).
3. Tick-Abstände (`Transcription of` Zeitstempel) vor und nach 12:36 vergleichen (p50/p95), um B zu quantifizieren.
4. `voxtype.service`: Unit-Datei und Journal lesen. Hört es auf dieselbe Taste oder dasselbe Mikrofon, und wie viel CPU verbraucht es?
5. In `typing.py` und `delta_typer` die Pfade auflisten, in denen pro Diktat mehr als ein dotool-Prozess gestartet wird (Kandidaten für das Zusammenlegen).

## 5. Nachtrag nach Kimi-Abgleich (slowdown_kimi.md §1–3)
- Einig: A wirkt (Exec-Lock-Wartezeit p50 0,194 → 0,032 s). Release→Chime p50 stieg trotzdem von 2,95 auf 4,41 s. Ursache: dotool-Tippen (35 ms/Zeichen + 0,7 s Start + 0,69 s separate Leertaste) und ein doppelt so langer finaler Rest (Mittel 42 → 87 Zeichen). Das bestätigt meine eigenen Einzelmessungen (1,885 s für 34 Zeichen, 11,19 s für ~300 Zeichen).
- Präzisierung zu B: Der Median-Abstand zwischen Live-Updates blieb laut Kimi gleich (1,746 s gegenüber 1,755 s). Meine Aussage „B macht den Live-Text spürbar träger“ nehme ich zurück; B ist am Median nicht messbar.
- Ursache für den längeren Rest: Präfix-Konflikte werden verworfen, ab ~10 s steht der Live-Text still, ab 15 s gibt es keine Live-Erkennung mehr. Beides vergrößert den Rest, der nach dem Loslassen getippt werden muss. Der Rest hat sich also nicht durch A/B verdoppelt, sondern hängt an Diktatlänge und diesem Mechanismus.
- voxtype: gestrichen (im Leerlauf). Der Neustart um 12:46 war der GNOME-Session-Neustart; Neuladen des Modells 15–19 s.
