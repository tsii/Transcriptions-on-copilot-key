# Konzept: Push-to-Talk-Diktat mit lokalem ASR

**Zweck dieses Dokuments:** Übertragbare Beschreibung einer erprobten Architektur für
Push-to-Talk-Diktat (Taste halten → sprechen → Text erscheint an der Cursor-Position).
Die Mechanismen wurden auf einem Linux-Desktop (ThinkPad, CPU-only) implementiert,
getestet und iterativ debuggt. Dieses Dokument beschreibt **was funktioniert, warum,
und welche Fallstricke vermieden werden müssen** — damit dasselbe Prinzip auf einer
anderen Plattform (z. B. Android) umgesetzt werden kann, ohne die Fehler zu wiederholen.

Referenz-Implementierung: `stt_button` (Python 3.12, ~3.300 LOC, 179 Tests).

---

## 1. Überblick

```
 ┌──────────────┐    ┌───────────────┐    ┌──────────────────┐    ┌──────────────┐
 │ Trigger      │───▶│ Audio-Capture │───▶│ ASR (Streaming)  │───▶│ Text-Ausgabe │
 │ (Taste halten)│    │ (16 kHz mono) │    │ (lokales Modell) │    │ (Cursor)     │
 └──────────────┘    └───────────────┘    └──────────────────┘    └──────────────┘
      press/release      Ringpuffer         Transkribiert den       Differenz-Tippen
      = Start/Stop       + Thread           wachsenden Puffer       + Backspace-Revision
```

**Kernprinzip:** Das System ist ein **Diktier-Werkzeug**, kein Chat-Interface.
Der Text muss *live* entstehen, korrigierbar sein und darf den Nutzer-Text nicht zerstören.

---

## 2. Die sieben Kernmechanismen (alle erprobt)

### 2.1 Push-to-Talk als Zustandsmaschine

```
IDLE ──press──▶ RECORDING ──release──▶ FINALIZING ──▶ IDLE
                    │
                    └── abort() (Fehler/Timeout) ──▶ IDLE
```

* **Taste halten = aufnehmen, loslassen = finalisieren.** Kein Toggle, kein Doppelklick.
* `start_recording()` ist **idempotent** (doppelter Tastendruck startet nicht neu).
* Ein Lock (`threading.RLock`) serialisiert Zugriffe aus Key-Thread und ASR-Schleife.

> **Android:** `AudioRecord` beim `ACTION_DOWN` starten, beim `ACTION_UP` stoppen.
> `onKeyDown` kann mehrfach feuern → Idempotenz ist Pflicht.

### 2.2 Trigger-Erkennung getrennt vom Mikrofon

Die Trigger-Taste wird **unabhängig** vom Aufnahmeweg erkannt.

* Desktop: physische Taste → `keyd`-Remap → `F24` auf virtuellem Device → direktes
  Lesen von `/dev/input/event*` (reines Python, keine C-Abhängigkeit).
* **Autorepeat filtern:** Events mit `value == 2` ignorieren, sonst startet die
  Aufnahme bei jedem Repeat neu.
* **Press und Release getrennt behandeln** — die Release-Erkennung ist der
  kritische Pfad (siehe 2.3).
* Device **per Name** auflösen, nicht per Pfad (Nummern ändern sich bei Neustart).

> **Android:** Hardware-Taste via `KeyEvent` (z. B. `KEYCODE_HEADSETHOOK` für
> Headset-Tasten, oder ein `MediaButtonReceiver`). Achtung: Lautstärketasten sind
> vom System belegt; Headset-/Bluetooth-Tasten sind der zuverlässigere Weg.

### 2.3 Key-Erkennung MUSS in einem eigenen Thread laufen ⚠️

**Das ist der wichtigste Architektur-Punkt und war ein realer Bug.**

Problem: Die ASR-Inferenz blockiert (CPU-only: 1–3 s pro Transkription). Läuft die
Key-Erkennung in derselben Schleife, wird das **Loslassen der Taste während der
Inferenz nicht verarbeitet** → die Aufnahme läuft weiter, obwohl der Nutzer fertig ist.

Lösung (zweistufig):

```python
# 1) Key-Listener in eigenem Thread
threading.Thread(target=self._listener.run, name="key-listener", daemon=True).start()

# 2) Audio-Capture in NOCH einem Thread, der in eine Queue schreibt
def _capture_loop(self):
    while self.state == RECORDING:
        chunk = source.read(block=False)
        if chunk:
            self._audio_queue.put(chunk)   # entkoppelt von der Inferenz
```

Der Hauptthread konsumiert nur noch die Queue und transkribiert.

**Warum drei Threads?** Key-Thread (nie blockieren) · Capture-Thread (Audio läuft
weiter, auch wenn die Inferenz rechnet) · Main-Thread (Inferenz + Ausgabe).

> **Android:** `AudioRecord.read()` in einem `HandlerThread`/Coroutine, KeyEvent-Handling
> im UI-Thread (nur Flags setzen, nie blockieren). Inferenz in `Dispatchers.Default`.

### 2.4 Audio-Pipeline: 16 kHz mono, s16le

* **Format:** 16 kHz, 1 Kanal, 16-bit signed little-endian (`s16le`).
  Das ist das native Eingabeformat von Whisper/Parakeet — kein Resampling nötig.
* **Aufnahme als Subprozess/Stream**, Chunks à ~0,2–1 s in einen Ringpuffer.
* Beim Stoppen **Reihenfolge beachten** (⚠️ realer Bug, siehe 2.8).

### 2.5 ASR: Modellwahl und Decoding-Parameter

| Modell | Typ | Parameter | CPU-Latenz (RTF) | Empfehlung |
|---|---|---|---|---|
| **Parakeet TDT 0.6B v3** | Transducer (nicht-autoregressiv) | 0,6 B | **~0,1–0,5** | ✅ **beste Wahl** |
| faster-whisper large-v3-turbo | seq2seq (autoregressiv) | 0,8 B | ~0,5–1,8 | Fallback |
| Qwen3-ASR 1.7B | LLM (autoregressiv) | 1,7 B | ~2–3 | nur für Maximalqualität |

**Warum Parakeet:** Transducer decodieren **greedy und parallel** (kein
Token-für-Token-Sampling) → auf CPU 5–10× schneller als Whisper bei besserer
DE/EN-Mischqualität. Erkennt 25 europäische Sprachen automatisch.

**Decoding-Parameter, die sich bewährt haben:**

```python
model.transcribe(
    audio,
    language = None,                      # None = automatische Erkennung ("auto")
    task = "transcribe",
    beam_size = 1,                        # greedy: Durchsatz > Qualität marginal
    temperature = 0.0,                    # deterministisch (wichtig für Streaming!)
    condition_on_previous_text = False,   # verhindert Wiederholungsschleifen
    vad_filter = True,                    # Stille filtern (bei Whisper-Engine)
    initial_prompt = None,
)
```

**`temperature = 0.0` ist kritisch:** Bei Streaming wird derselbe Puffer mehrfach
transkribiert. Mit Sampling käme jedes Mal anderer Text → Flackern. Deterministisch
ist Voraussetzung für die Commit-Logik (2.6).

### 2.6 Streaming: „wachsender Puffer" + LocalAgreement-Commit

**Das Herzstück.** Ohne diesen Mechanismus entstehen entweder Verzögerung oder Flackern.

```
Audio: [============ wachsender Puffer ============]
                ↓ alle asr_interval_s (1.0 s Audio)
Transkription: "So, da bin ich"  →  "So, da bin ich mal"  →  "So, da bin ich mal gespannt"
                ↓ LocalAgreement: nur stabilen Präfix committen
Getippt:       "So, da bin ich"  →  " mal"                →  " gespannt"
                └── committed ──┘   └───── korrigierbar (Partial) ─────┘
```

**Algorithmus (LocalAgreement):**

1. Historie der letzten `commit_agreements = 2` Transkriptionen halten.
2. **Gemeinsamen Präfix** aller Historie-Einträge berechnen.
3. Nur **bis zur letzten Wortgrenze** committen (das laufende Wort bleibt korrigierbar).
4. Der committete Text wird **nie mehr zurückgenommen** — das verhindert Flackern.

```python
common_len = len(history[0])
for candidate in history[1:]:
    common_len = common_prefix_len(history[0][:common_len], candidate)
common_str = history[0][:common_len]
cut = common_str.rfind(" ")          # nur bis zur Wortgrenze
committed = common_str[:cut + 1]
```

**Warum 2 Übereinstimmungen?** Ein einzelner Lauf kann eine Halluzination sein
(besonders bei kurzen Chunks). Erst wenn zwei aufeinanderfolgende Läufe denselben
Text liefern, gilt er als stabil.

**Warum nur bis zur Wortgrenze?** Sonst würde ein halb gesprochenes Wort
abgeschnitten und eingefroren — genau der Bug, der „Das müsste jetzt **de**"
statt „Das müsste jetzt **deutlich**" erzeugte.

### 2.7 Die Delta-Ausgabe (DeltaTyper)

Der Transkriptions-Stream liefert wiederholt **den ganzen Text**. Getippt werden darf
nur die **Differenz**.

```python
old = zuletzt_getippter_text
committed = len(committeter_praefix)     # unantastbar

# Sicherheitsnetz: committeter Bereich darf nie verloren gehen
if len(new) < committed: return

# Nur den NICHT-committeten Tail vergleichen
old_tail, new_tail = old[committed:], new[committed:]
common = common_prefix_len(old_tail, new_tail)
to_remove = len(old_tail) - common        # Backspaces
to_add    = new_tail[common:]             # neuer Text

if to_remove: backspace(to_remove)
if to_add:    type_text(to_add)
```

**Drei Schutzregeln, die sich als notwendig erwiesen haben:**

1. **Committed-Präfix nie anfassen** — verhindert, dass stabiler Text flackert.
2. **`max_revision_chars` (60):** Müsste mehr als 60 Zeichen zurückgenommen werden,
   ist meist der Cursor nicht mehr hinter unserem Text (Nutzer hat geklickt) →
   dann **nur anhängen** statt zu zerstören.
3. **Finale Transkription ist voll korrigierend** (`force_revision=True`,
   `committed_len=0`): Beim Loslassen darf das Modell alles korrigieren, denn der
   Nutzer diktiert fertig und der Cursor steht sicher hinter dem eigenen Text.

### 2.8 Shutdown-Reihenfolge beim Loslassen ⚠️

**Realer Bug, der Audio verschluckte.** Falsche Reihenfolge:

```python
rest = source.stop()        # ❌ neueste Bytes zuerst
transcriber.feed(rest)
drain_queue()               # ältere Bytes danach → Zeitachse verdreht!
```

**Korrekt:**

```python
state = FINALIZING          # 1) Capture-Thread beendet sich
capture_thread.join(1.0)    # 2) warten, dass keine Chunks mehr kommen
drain_queue()               # 3) ÄLTERE Bytes zuerst
rest = source.stop()        # 4) NEUESTE Bytes zuletzt
transcriber.feed(rest)
```

Verdrehte Audio-Zeitachse → Halluzinationen und fehlende Satzteile.

---

## 3. Fallstricke (aus realen Bugs gelernt)

### 3.1 `max_new_tokens` muss mit der Audiolänge skalieren ⚠️

Ein **fixes** Token-Limit kappt lange Diktate hart. Gemessen: dasselbe 60-s-Audio
lieferte mit `max_new_tokens=256` nur **123 Zeichen**, mit 2048 **294 Zeichen**.

```python
def token_budget(audio_seconds: float) -> int:
    # Deutsche Sprache ≈ 12 Subword-Tokens/Sekunde (2–4 Tokens/Wort,
    # ~2,5 Wörter/s + Reserve)
    budget = int(audio_seconds * 12) + 64
    return max(256, min(budget, 4096))    # Mindest- und Höchstgrenze
```

**Extra-Falle:** Wenn ein Slice mit Links-Kontext transkribiert wird, verbrennt der
Kontext Token-Budget, obwohl er danach verworfen wird → Budget immer auf die
**Gesamtlänge des übergebenen Audios** berechnen.

### 3.2 Aufnahmedauer-Limit bewusst setzen

Ein Default von 60 s bricht Diktate ab. Für Diktat **300 s** (5 min) ist ein guter
Kompromiss (RAM: 16 kHz × 2 Byte = 32 kB/s → 300 s ≈ 9,6 MB).

### 3.3 VAD-Guard darf echte Sprache nicht verwerfen ⚠️

Silero-VAD liefert für Audio **< 1 s immer `False`**. Ein naiver Guard
„wenn VAD False → nicht transkribieren" verschluckt dann kurze Diktate komplett.

**Regel:** Nur **kurze** Reste (< 2 s) bei VAD-False verwerfen. Lange Reste
**trotzdem** transkribieren (Datenverlust ist schlimmer als Halluzinationsrisiko),
aber eine Warnung loggen.

### 3.4 Parakeet halluziniert bei sehr kurzen Chunks

Gemessen: Ein 1-s-Chunk lieferte `"So it's uh big"` (englisch!) für deutschen Text;
bei 1,5 s dann korrekt `"So, jetzt bin ich gespannt."`.

**Konsequenz:** `min_audio_s` (Mindest-Audio vor der ersten Transkription) **≥ 1,0 s**,
besser 2,5–3,0 s. Und: Der **erste** Commit braucht Agreement (2.6), sonst friert
eine Halluzination des ersten Chunks ein.

Laut NVIDIA-Model-Card ist Parakeet *„not recommended for word-for-word/incomplete
sentences"* — das ist der Grund.

### 3.5 Zwei Fehlerbilder bei der Text-Ausgabe (Tastatur-Emulation) ⚠️

Beim Tippen per Tastatur-Emulation (uinput) können **Modifier-Zustände** korrumpieren.
Die beiden Bilder sind **zeichengenau unterscheidbar** — das ist diagnostisch wertvoll:

| Fehlerbild | Beispiel | Ursache | Erkennungsmerkmal |
|---|---|---|---|
| **Shift hängt** | `SO; DA BIN ICH` | Shift-Latch | Satzzeichen **mittransformiert**: `,`→`;` `,`→`:` |
| **CapsLock an** | `dAS sKRIPT` | CapsLock invertiert | Satzzeichen **unverändert** |

**Diagnose-Rezept:** Den zu erwartenden Text einmal durch die Shift-Ebene bzw. durch
eine CapsLock-Inversion schicken und mit dem sichtbaren Text vergleichen. Passt es
zeichengenau, ist es **kein ASR-Fehler**, sondern der Lock-Zustand der Ausgabe-Pipeline.

**Härtung:**
* **Tipp-Timing konfigurierbar** und großzügig: `typedelay`/`typehold` (Desktop: 20/15 ms).
  Zu klein → unter CPU-Last gehen Key-Events verloren → Modifier hängen.
* **Lock-Status loggen** (vor/nach jedem Tippvorgang) — sonst ist die Quelle nicht
  zuordenbar.
* **CapsLock nur erkennen und warnen** (Standard, `capslock_autocorrect = false`).
  Die App darf CapsLock **niemals** selbst togglen (Begründung unten).
* **Korrektes Tastaturlayout** setzen (Desktop: `DOTOOL_XKB_LAYOUT=de`,
  `DOTOOL_XKB_VARIANT=nodeadkeys`). Ohne das: `z`↔`y` vertauscht, `?`→`_`.

#### CapsLock: erkennen und warnen — nicht eingreifen

**Die Zuordnung ist auf diesem System empirisch belegt invertiert:**
`LED aus = CapsLock aktiv = invertierter Text`; `LED an = normale Schreibung`
(Klein/Groß wie getippt). Gemessen auf ThinkPad + keyd + GNOME/Wayland — das
ist **invertiert** zur ueblichen Konvention.

**Kernregel:** **Die App darf CapsLock niemals ungefragt togglen.** Der
Lock-State ist nicht zuverlaessig lesbar (`LED != State`, `xset != State`):

* `key capslock` per uinput ist ein **Toggle** — man weiß nie, in welche Richtung er
  geht. Ein zweiter Toggle „zur Sicherheit“ kann den Fehler wieder herstellen.
* Die Kernel-LED kann dem echten Lock-State nachhängen (State-Writethrough) oder
  von mehreren Devices unterschiedlich gemeldet werden.
* `xset q` (XWayland) ist **keine** verlaessliche Quelle: bei Wayland-Compositor-State
  meldet `xset` „off“, während der Lock aktiv ist (selbst gemessen).

Deshalb: **nur detektieren und warnen**. Der Vorher-Zustand wird geloggt, damit
die Quelle eines invertierten Diktats eindeutig zuordenbar ist:

```python
def _capslock_leds() -> dict[str, str]:
    """Kernel-LED pro Device lesen - Device-Name -> 'an'/'aus'/'?'."""
    leds = glob.glob("/sys/class/leds/*capslock*/brightness")
    result = {}
    for path in leds:
        device = os.path.basename(os.path.dirname(path))
        raw = open(path).read().strip()
        result[device] = "an" if raw == "1" else ("aus" if raw == "0" else "?")
    return result

def _capslock_state() -> str:
    """Aggregiert ueber alle Devices - 'an', 'aus' oder '?'."""
    leds = _capslock_leds()
    if not leds:
        return "?"
    return "an" if any(v == "an" for v in leds.values()) else "aus"

def _ensure_capslock_normal(self):
    """Im Default: nur warnen - KEIN Toggle."""
    target = "an" if self._capslock_led_inverted else "aus"
    state = _capslock_state()
    if state == "?" or state == target:
        return
    log.warning(
        "CapsLock-LED steht auf %r, Ziel %r - Text kann invertiert "
        "erscheinen. Es wird NICHT umgeschaltet.", state, target,
    )
```

Nur wer den Eingriff **explizit** will, setzt `capslock_autocorrect = true`:
dann wird auf den konfigurierbaren Zielzustand geschaltet — `capslock_led_inverted =
true` (Default) bedeutet Ziel LED „an“, `false` bedeutet Ziel LED „aus“ — mit
Verifikation und höchstens zwei Versuchen. Der Toggle nutzt ausschließlich
`_send_raw()` (ohne CapsLock-Prüfung), sonst ruft sich die Prüfung selbst auf
(Rekursionsgefahr).

**Manuelle Abhilfe** (Aufgabe des Nutzers, nicht der App):

* `stt-button reset-capslock` — setzt die LED auf den konfigurierten
  Zielzustand (max. 3 Toggles) und nennt die nächsten Schritte.
* `stt-button caps-diagnose` — listet alle LEDs pro Device, die `xset q`
  Caps-Lock-Zeile und den gültigen Zielzustand und tippt einen Marker
  `[[diagnose-OK]]`.
* Einmal die **physische CapsLock-Taste** drücken oder ab-/anmelden (setzt den
  seat-globalen XKB-State zurück).
* Bleibt es invertiert: `sudo systemctl restart keyd` und Re-Login.

#### Umlaute sind ein guter Lackmustest

Bei CapsLock werden **auch Umlaute invertiert**: `ä`→`Ä`, `ö`→`Ö`, `ü`→`Ü`,
`ß`→`ẞ`. Ein Test-String wie
`"Prüfe die Umlaute: ä ö ü ß groß"` deckt das sofort auf — und prüft gleichzeitig,
ob das Tastaturlayout (`de`) korrekt gesetzt ist.

> **Android:** Dieses Fehlerbild existiert dort **nicht** — Text wird über
> `InputConnection.commitText()` / `AccessibilityService` eingefügt, nicht per
> Tasten-Emulation. Das ist ein echter Vorteil.

### 3.6 Zusammenfassung: Do's & Don'ts

| ✅ Do | ❌ Don't |
|---|---|
| Key-Erkennung in eigenem Thread | Key-Handling in der Inferenz-Schleife |
| `temperature=0.0` (deterministisch) | Sampling beim Streaming |
| Token-Budget mit Audiolänge skalieren | Fixes `max_new_tokens=256` |
| Nur bis zur Wortgrenze committen | Halbe Wörter einfrieren |
| Committeten Präfix nie anfassen | Stabiles Flackern lassen |
| VAD nur auf **neue** Segmente | VAD auf Links-Kontext (immer True) |
| Lange stille Reste trotzdem transkribieren | Lange Reste als „Stille" verwerfen |
| Shutdown: Queue → dann stop()-Rest | stop()-Rest vor der Queue feeden |
| Lock-Zustand loggen | Bei Textfehlern den ASR verdächtigen |

---

## 4. Konfiguration (Referenzwerte)

```toml
# Trigger
input_device_name = "keyd virtual keyboard"   # Gerät mit der Trigger-Taste
key_codes         = ["KEY_F24"]               # Linux-Keycode(s)

# Audio
audio_source = "auto"        # PipeWire-Quellenname oder "auto"
sample_rate  = 16000         # 16 kHz mono s16le (natives ASR-Format)

# ASR
backend      = "parakeet"    # "parakeet" | "whisper" | "qwen"
model        = "nvidia/parakeet-tdt-0.6b-v3"
language     = "auto"        # "auto" | "de" | "en"
compute_type = "int8"        # CPU: int8/float32; GPU: float16
cpu_threads  = 8             # = physische Kerne (kein Hyperthreading)

# Streaming
streaming_mode   = "whole_buffer"  # robuster als "windowed" (siehe 5.)
min_audio_s      = 3.0   # erst ab 3 s Audio transkribieren (Halluzinations-Schutz)
asr_interval_s   = 1.0   # Re-Transkription alle 1 s Audio
max_record_s     = 300.0 # 5 Minuten
commit_agreements = 2    # LocalAgreement-Bestätigungen

# Ausgabe
typer            = "dotool"   # "dotool" | "wtype" | "clipboard"
xkb_layout       = "de"
xkb_variant      = "nodeadkeys"
typedelay_ms     = 20
typehold_ms      = 15
append_space     = true   # Leerzeichen nach dem Diktat
max_revision_chars = 60   # Schutzgrenze für Backspace-Revisionen
```

---

## 5. Was NICHT funktioniert hat (wichtig für die Nachimplementierung!)

### 5.1 Fenster-basiertes Streaming (`windowed`)

Versuch: Gleitende Fenster `[4 s Links-Kontext | 2 s Chunk | 1 s Rechts-Kontext]`,
nur Tokens aus dem Commit-Horizont übernehmen (angelehnt an NVIDIAs „Buffered
Chunked Inference", 10-2-2).

**Ergebnis: zu viele Deadlocks.** Konkrete Probleme:

1. **Pause-Stall:** Das Fenster war auf `committed_s + 3 s` gedeckelt. Bei einer
   Sprechpause ≥ 3 s lieferte der VAD dauerhaft „keine Sprache" und `committed_s`
   rückte nie vor → **alles nach der Pause ging verloren.** (Behoben durch
   Silence-Skip, aber siehe 2.)
2. **Kopplung Fenster ↔ Commit:** Das Fenster konnte nicht über `committed_s + n`
   hinausreichen. Blieb der Commit stehen, blieb das Fenster stehen → das restliche
   Audio wurde nie gesehen.
3. **Agreement-Deadlock:** Der Commit-Punkt rückte bei jedem Commit vor, dadurch
   sammelte der Agreement-Cache nie 2 Treffer → dauerhafter Stillstand.

**Empfehlung:** Für ein Diktat-Werkzeug ist der **einfachere `whole_buffer`-Ansatz**
(2.6) robuster. Er hat keine Zustandsmaschine, die in einen Deadlock laufen kann.

**Messvergleich** (13,4 s Testaudio mit 4 s Pause, Referenz = Einzeltranskription
des Gesamtaudios):

| Modus | Ergebnis |
|---|---|
| Referenz (ganzes Audio) | 256 Zeichen ✅ |
| `whole_buffer` | **256 Zeichen** ✅ (identisch) |
| `windowed` | 137–139 Zeichen ❌ (Teile fehlen) |

**Trade-off:** `whole_buffer` liefert Text in Blöcken (alle ~1,5 s), nicht wortweise.
Für Diktat ist das akzeptabel; wer echtes Wort-für-Wort will, muss den
windowed-Ansatz sorgfältig zu Ende bauen (mit den o. g. Deadlock-Fixes).

### 5.2 Text nachträglich „normalisieren"

Versuch, GROSS geschriebene Wörter per Regex zu korrigieren.

**Verworfen**, weil es das Symptom statt der Ursache bekämpft (siehe 3.5) und
legitime Großschreibung (Akronyme, Eigennamen) zerstört.

---

## 6. Übertragung auf Android — konkrete Hinweise

### 6.1 Was 1:1 übernommen werden kann

| Mechanismus | Android-Umsetzung |
|---|---|
| Zustandsmaschine (2.1) | `enum` + `MutableStateFlow` |
| LocalAgreement-Commit (2.6) | unverändert (reine Text-Logik) |
| DeltaTyper-Logik (2.7) | unverändert (reine Text-Logik) |
| Token-Budget (3.1) | unverändert |
| VAD-Guard-Regeln (3.3) | unverändert |
| Modellwahl Parakeet (2.5) | via ONNX Runtime Mobile / TensorFlow Lite / NNAPI |

### 6.2 Was ersetzt werden muss

| Desktop | Android |
|---|---|
| `/dev/input` + keyd | `KeyEvent` / `MediaButtonReceiver` / `AccessibilityService` |
| `pw-record` (PipeWire) | `AudioRecord` (16 kHz, MONO, PCM_16BIT) |
| `dotool` (uinput) | `InputConnection.commitText()` oder `AccessibilityService.ACTION_SET_TEXT` |
| systemd-User-Service | `ForegroundService` (Pflicht für Mikrofon ab Android 14) |
| Python + transformers | Kotlin/Java + ONNX Runtime (`parakeet-tdt-0.6b-v3` gibt es als ONNX/int8) |

### 6.3 Android-spezifische Vorteile

* **Kein Tastatur-Emulations-Problem** → 3.5 entfällt komplett.
* **Kein Threading-Problem mit Key-Events** → KeyEvents kommen vom System-UI-Thread.
* **NNAPI/GPU** → deutlich schnellere Inferenz als die CPU-only-Referenz.

### 6.4 Android-spezifische Fallstricke

1. **Mikrofon-Berechtigung + ForegroundService** (ab Android 14 `FOREGROUND_SERVICE_MICROPHONE`).
2. **Akku-Optimierung** kann den Service töten → `REQUEST_IGNORE_BATTERY_OPTIMIZATIONS`.
3. **Latenz von `AudioRecord`:** Puffergröße über `getMinBufferSize()` bestimmen,
   zu klein → Dropouts, zu groß → Latenz.
4. **Text-Injektion in fremde Apps** ist nur via `AccessibilityService` möglich
   (Play-Store-Richtlinien beachten!). Für eigene Apps: `InputConnection`.
5. **Modellgröße:** Parakeet 0.6B int8 ≈ 700 MB – für ein App-Bundle zu groß.
   Optionen: Download beim ersten Start, oder kleineres Modell (Parakeet 0.6B ist
   bereits die kleine Variante; Alternativen: `whisper-small` ONNX ≈ 250 MB).

---

## 7. Teststrategie (was sich bewährt hat)

**Alle Kernmechanismen sind ohne Mikrofon, Modell und Tastatur testbar** — durch
Dependency Injection von `AudioSource`, `Engine` und `Typer`.

| Test-Ebene | Was | Wie |
|---|---|---|
| Unit: Commit | LocalAgreement, Wortgrenzen | Fake-Token-Listen mit Zeitstempeln |
| Unit: DeltaTyper | Revision, Backspace, Schutzgrenzen | Fake-Typer, der Calls aufzeichnet |
| Unit: Token-Budget | Skalierung mit Audiolänge | reine Funktion |
| Integration | Pipeline mit Fake-Engine | vordefinierte Transkriptionen pro Pufferlänge |
| Ende-zu-Ende | echter Pfad | gespeichertes WAV durch die Pipeline |

**Besonders wertvoll:** Ein gespeichertes Test-Audio, das eine **Pause** enthält,
durch die Pipeline schicken und das Ergebnis mit der **Einzeltranskription des
Gesamtaudios** vergleichen. Genau dieser Test hat die windowed-Defekte aufgedeckt
(256 vs. 137 Zeichen).

**Debug-Hilfen, die sich gelohnt haben:**
* Audio jeder Aufnahme nach `/tmp/stt_last.wav` dumpen → reproduzierbar analysierbar.
* Jeden Transkriptions-Schritt loggen (`audio_seconds → text, committed_len`).
* Lock-Zustand (CapsLock) vor/nach jedem Tippen loggen, Trigger-Press/Release
  loggen (3.5).

---

## 8. Kurz-Checkliste für die Nachimplementierung

```
[ ] Push-to-Talk-State-Maschine, idempotent, mit Lock
[ ] Key-Erkennung: eigenener Thread, Autorepeat (value==2) filtern
[ ] Audio-Capture: eigener Thread → Queue → 16 kHz mono s16le
[ ] ASR: Parakeet TDT (oder Whisper), temperature=0, deterministisch
[ ] Streaming: wachsender Puffer, Re-Transkription alle ~1 s Audio
[ ] Commit: LocalAgreement (2×), nur bis Wortgrenze
[ ] Ausgabe: DeltaTyper mit committed-Präfix-Schutz + max_revision_chars
[ ] Finale Transkription: voll korrigierend (force_revision)
[ ] Shutdown: join → Queue drainen → stop()-Rest
[ ] Guards: min_audio_s ≥ 1 s, Token-Budget skaliert, VAD nur kurze Reste
[ ] max_record_s bewusst setzen (z. B. 300 s)
[ ] Tests ohne I/O (Fake AudioSource/Engine/Typer)
[ ] Test-Audio MIT PAUSE gegen Einzeltranskription vergleichen
```

---

## 9. Referenz-Messwerte (Intel Lunar Lake, CPU-only, 8 Kerne)

| Modell | 20 s Audio | RTF | Anmerkung |
|---|---|---|---|
| Parakeet TDT 0.6B (int8/float32) | ~2,5 s | **0,12** | ✅ empfohlen |
| faster-whisper large-v3-turbo (int8) | ~8,5 s | 0,43 | Fallback |
| Qwen3-ASR 1.7B | ~23 s | 1,15 | zu langsam für Live |

**Modell-Ladezeit:** Parakeet ~2 s (warm halten! Beim Dienststart vorladen, nicht
beim ersten Tastendruck).

**Qualitätsvergleich (deutsches Diktat mit englischen Fachbegriffen):**

| Modell | Transkript | Bewertung |
|---|---|---|
| **Parakeet** | „…ganz schön shocking… die Vibes in dem Raum…" | ✅ engl. Begriffe korrekt |
| faster-whisper (`language="de"`) | identisch | ✅ (nur mit erzwungener Sprache!) |
| faster-whisper (`language="auto"`) | komplett englisch übersetzt | ❌ |
| Granite Speech 4.1 | „…die **Weibchen** im Raum…" | ⚠️ Fehlerkennung |

**Wichtig:** Bei Whisper `language` **explizit** setzen, wenn die Sprache bekannt ist —
die Auto-Erkennung kippt bei gemischtem DE/EN in die falsche Sprache.
