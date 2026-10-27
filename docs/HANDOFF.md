# Handoff — STT-Button (Push-to-Talk-Diktat)

**Stand:** 2026-10-27, Session unterbrochen (auf Wunsch des Nutzers).
**Zweck dieses Dokuments:** Übergabe an eine neue Session/Person, damit die Arbeit
ohne Kontextverlust fortgesetzt werden kann.

---

## 1. Was das Projekt ist

Push-to-Talk-Diktat auf einem ThinkPad X1 (Ubuntu 24.04, GNOME/Wayland, CPU-only):
Copilot-Taste halten → sprechen → Text erscheint an der Cursor-Position.

| Komponente | Technik |
|---|---|
| Trigger | Copilot-Taste → `keyd`-Remap → `KEY_F24` → evdev-Read (User, kein root) |
| Audio | `pw-record` Subprozess, 16 kHz mono s16le, eigener Capture-Thread |
| ASR | NVIDIA **Parakeet TDT 0.6B v3** (transformers, CPU, float32) |
| Streaming | `whole_buffer`: wachsender Puffer, Re-Transkription alle 1 s, LocalAgreement-Commit |
| Ausgabe | `dotool` (uinput) mit `DOTOOL_XKB_LAYOUT=de`, `DOTOOL_XKB_VARIANT=nodeadkeys` |
| Dienst | systemd-User-Unit `stt-button.service` |

**Repo:** `git@github.com:tsii/Parakeet-on-copilot-key.git`, Branch `master`
**Letzter Commit:** `4e6b496` — *CapsLock: invertierte LED-Semantik korrigiert (LED an = normal)*
**Achtung:** Dieser Commit enthält eine **widerlegte** Prämisse (siehe §4.1). Danach
gibt es **uncommittete** Änderungen im Working Tree.

---

## 2. Aktueller technischer Stand

### 2.1 Läuft / funktioniert

| | Status |
|---|---|
| Testsuite | **232 passed** (`.venv/bin/python -m pytest`) |
| Langdiktat-Fixes | ✅ behoben & committet (`e7d590e`) |
| Zweites-Diktat-verschluckt | ✅ behoben & committet (`5826f26`) |
| CapsLock K1–K8 + VAD-Gate + Trigger-Logging | ✅ committet (`2dbc79b`) |
| Groß-/Kleinschreibung **und** Umlaute | ✅ korrekt (Live-Diktat bestätigt) |
| keyd: dotool ausgeschlossen | ✅ `-4711:0815`, Journal verifiziert |
| Phantom-F24 | ✅ nicht mehr reproduzierbar (§3.3) |
| **Dienst** | ▶️ **läuft** (`systemctl --user start stt-button`) — Autostart (`enable`) bewusst noch nicht gesetzt |

### 2.2 Uncommittete Änderungen (Working Tree)

```
 M config.example.toml        M src/stt_button/asr.py       M tests/test_config.py
 M docs/KONZEPT.md            M src/stt_button/config.py    M tests/test_typing.py
 M src/stt_button/__main__.py M src/stt_button/keys.py
 M src/stt_button/app.py      M src/stt_button/typing.py
?? tests/test_app_wiring.py  ?? tests/test_cli.py  ?? tests/test_docs.py
?? tests/test_keys_logging.py ?? tests/test_stream_vad.py
```

**Diese Änderungen sind getestet (222 grün), aber NICHT committet.** Sie enthalten
u. a. die Entfernung des CapsLock-Toggles, das VAD-Gate, Trigger-Logging und die
Doku-Anpassung an die (falsche) Prämisse.

### 2.3 Live-Konfiguration

`~/.config/stt-button/config.toml` — relevant:
```toml
backend = "parakeet"
model = "nvidia/parakeet-tdt-0.6b-v3"
streaming_mode = "whole_buffer"
min_audio_s = 1.0
asr_interval_s = 1.5
max_record_s = 300.0
typer = "dotool"
xkb_layout = "de"
xkb_variant = "nodeadkeys"
typedelay_ms = 20
typehold_ms = 15
capslock_autocorrect = false     # (Zeile 41, manuell gefixt; vorher: capslock_led_inverted)
```

---

## 3. Die behobenen Probleme (chronologisch, mit Ursache)

### 3.1 Langdiktate wurden gekappt / Teile fehlten

**Drei Ursachen, alle behoben (Commit `e7d590e`):**

1. **`max_new_tokens=256`** war ein hartes Limit. Beweis: dasselbe 60-s-Audio lieferte
   mit 256 nur **123 Zeichen**, mit 2048 **294 Zeichen**.
   → Fix: `_token_budget(audio_s) = max(256, min(audio_s*12 + 64, 4096))`.
2. **`max_record_s=60`** brach Aufnahmen nach 60 s ab. → Fix: **300 s**.
3. **Pause-Stall im `windowed`-Modus:** Das Fenster war auf `committed_s+3s` gedeckelt;
   bei einer Sprechpause ≥ 3 s rückte `committed_s` nie vor → alles danach ging verloren.
   → Fix: Silence-Skip (`skip_silence`) + Text-Sicherung vor dem Sprung.
   **Wichtiger Befund:** Der `windowed`-Modus hat zu viele Deadlock-Möglichkeiten.
   Messvergleich (13,4 s Audio mit 4 s Pause): Referenz 256 Zeichen ·
   `whole_buffer` **256 Zeichen** ✅ · `windowed` 137 Zeichen ❌.
   → **`whole_buffer` ist jetzt der Default.**

### 3.2 Zweites Diktat wurde verschluckt

**Ursache:** `start_recording()` hatte `if self.state != IDLE: return`. Während der
finalen Transkription (blockierte im Key-Thread, mehrere Sekunden) war der Zustand
`FINALIZING` → der Tastendruck wurde **verworfen**.

**Fix (Commit `5826f26`):** Finale Transkription läuft im Hintergrund-Thread.

**⚠️ Folge-Bug (Geisteraufnahme), ebenfalls GELÖST (TDD, openrouter/Kimi-k3.1-pro
+ GLM 5.3):** Der Vorläufer-Mechanismus `_pending_start` (Bool) merkte nur
„irgendwann gab es einen Press“; das Release während FINALIZING ging verloren →
nach Finalisierungsende wurde für eine längst losgelassene Taste eine
Geisteraufnahme gestartet (im Live-Log belegt). **Heutiger Stand:**
`_key_down` (physischer Tastenzustand) ersetzt den Bool — Release aktualisiert
IMMER den Tastenzustand, Start nach Finalisierung nur bei gehaltener Taste,
Prüfung+Start atomar unter `_lock`. Tests (deterministisch, BlockingTranscriber
statt Sleeps): `test_release_during_finalizing_updates_key_state`,
`test_key_held_through_finalize_restarts_recording`,
`test_start_after_finalizing_only_when_key_held`,
`test_release_after_finalized_start_stops_fresh_recording`,
`test_press_during_recording_is_idempotent`. Alter Sleep-Test entfernt.
Bekannte Grenze: Kurzhub während FINALIZING wird verworfen (kein Audio erfasst).
Nebenwirkung (Design-konform): bei **gehaltener** Taste nach Max-Dauer-Finalize
startet automatisch die nächste Aufnahme.

### 3.3 Phantom-Aufnahmen im Leerlauf ✅ GELÖST (27.10., verifiziert)

**Beobachtung (alt, 17:00):** Der Dienst startete im Leerlauf Aufnahmen und
schickte **14× `backspace`** in 30 s in das Terminal; 3× `f24 down` ohne
Tastendruck.

**Ursache 1 (Stille-Halluzination):** Im Default-Modus `whole_buffer` war das
**VAD-Gate nicht verdrahtet** (nur im `windowed`-Modus). Stille/Rauschen wurde
transkribiert → ASR halluzinierte → DeltaTyper tippte und nahm per Backspace
zurück. **Fix:** `StreamTranscriber` hat jetzt ein optionales `vad`-Argument,
`app.py` verdrahtet `SileroVadGate` für beide Modi (committet in `2dbc79b`).

**Ursache 2 (Phantom-`f24`):** keyd grabbte mit `[ids] *` auch **dotools
uinput-Tastatur** (`4711:0815`). Die Rückkopplung dotool→keyd→virtual keyboard
erzeugte Geister-Events. **Fix:** `-4711:0815` in `/etc/keyd/default.conf`
(Nutzer, `sudo keyd reload`). Verifiziert per keyd-Journal:
`DEVICE: ignoring 4711:0815 ... (dotool keyboard)`.

**Verifikation (27.10, nach dem Fix):** Passiver Wächter (`/tmp/f24_watch.py`,
90 s Leerlauf): **0 Events**. Positivkontrolle (1× Copilot): exakt 1 Press+Release.
Live-Betrieb mit laufendem Dienst: nur echter Trigger im Log.

> **Wichtige Lehre:** Das „offene Phantom-Log" war 3,5 h ÄLTER als der keyd-Fix.
> Vor jeder weiteren Diagnose Alter der Messungen prüfen — das Problem war
> bereits gelöst.

---

## 4. CapsLock-Inversion ✅ GELÖST (27.10., abschließend verifiziert)

> **ABSCHLUSS (27.10, 21:03):** Nach dem keyd-Fix (`-4711:0815`, dotool nicht
> mehr gegrabbt) ist das System wieder **konventionell**: LED aus = CapsLock aus
> = normale Schreibung (per Diktat „Eins, zwei, drei, viereinhalb" + LED-Sicht
> bestätigt). Live-Config auf `capslock_led_inverted = false` gesetzt, keine
> Warnung mehr. Die nachfolgende Dokumentation der *zeitweise invertierten*
> Semantik bleibt als Fallstudie erhalten — genau diese Verläufe sind der
> Grund, warum die App nie ungefragt toggeln darf.


### 4.1 Was empirisch BEWIESEN ist

Ein kontrolliertes Experiment (drei Text-Marker, getippt bei definierten LED-Zuständen,
Nutzer hat die Bildschirmausgabe bestätigt):

| LED | Getippt | Angekommen | Ergebnis |
|---|---|---|---|
| **aus** | `[[C-marker-drei]]` | `[[c-MARKER-DREI]]` | **invertiert** |
| **an** | (Marker) | normal | korrekt |

**FAZIT (belegt):** Auf diesem System gilt
**LED aus = CapsLock aktiv = invertierter Text**;
**LED an = normale Schreibung.**

Die LED-Semantik ist also **invertiert zur üblichen Konvention**. Der Nutzer hatte
von Anfang an recht; zwei Experten (Kimi K3) lagen mit der Gegenannahme falsch.

### 4.2 Der Fehler in der bisherigen "Lösung"

Commit `4e6b496` kodiert die **falsche** Prämisse (`LED an = CapsLock an = invertiert`)
und entfernte den Korrekturmechanismus. Das wurde vom Reviewer (Deepseek V4 Pro)
mit **REQUEST-CHANGES** abgelehnt:

> *P0-1: Der Nutzer bleibt mit invertiertem Text allein. Die Änderung entfernt den
> einzigen Korrekturmechanismus und lässt ihn in beiden denkbaren Szenarien mit
> dauerhaft invertiertem Text zurück.*

### 4.3 Warum kein App-seitiger Fix das Problem löst

**Architektur-Fakt:** Die Keycode→Zeichen-Umwandlung (inkl. Case) passiert **einmal,
seat-global, in mutters XKB-State**. Ein dort gelatchter CapsLock-Modifier invertiert
Buchstaben **aller** Keyboards und **aller** Wayland-Clients — also auch der physischen
Tastatur. Die App kann diesen Zustand **nicht zuverlässig lesen**:

- Kernel-LED: zeigt den State **nicht** verlässlich (Desync belegt)
- `xset q` (XWayland): eigener State, weicht ab
- Es existieren **zwei** LEDs (`input3` = physisch, `input19` = keyd virtual)
- `caps:none` (gsettings **und** setxkbmap) wirkte **nicht**
- `key capslock`-Toggles ändern die LED, aber nicht zuverlässig das Verhalten

### 4.4 Der eigentliche Systemfix (Nutzer-Aufgabe, braucht sudo)

**Verdacht mit hoher Wahrscheinlichkeit:** `keyd` grabbt mit `[ids] *` **alle**
Devices — auch dotools uinput-Tastatur. Damit läuft die Diktat-Ausgabe durch einen
remappenden Daemon, der den Lock-State anders behandelt als mutter.

```bash
# 1) Device-ID von dotool ermitteln (während dotool tippt)
sudo keyd monitor

# 2) In /etc/keyd/default.conf unter [ids] eine Ausnahme eintragen:
#    -<vendor>:<product>        (dotools Device)

# 3) Neu starten
sudo systemctl restart keyd
```

**Zusätzlich empfohlen:** Einmal ab- und wieder anmelden — das setzt mutters
Seat-State garantiert zurück.

### 4.5 Umlaute: systemisch, nicht App-seitig

Der Nutzer berichtet aktuell: **ASCII korrekt, Umlaute falsch.**
Kimi K3 hat die kompilierte Keymap geprüft (`xkbcli compile-keymap --layout de
--variant nodeadkeys`):

- `de+nodeadkeys` erbt `de(basic)`, überschreibt nur 9 Tasten
- Umlaute sind eigene Keysyms: `AD11` Level1=`udiaeresis`, Level2=`Udiaeresis` (analog `ä`, `ö`)
- **`ß` (AE11) hat Typ `FOUR_LEVEL_PLUS_LOCK` mit 5 Symbolen**, das letzte ist `U1E9E` (ẞ)
  → deshalb wird `ß` bei aktivem Lock zu `ẞ`. **Erwartetes XKB-Verhalten, kein Bug.**

**Konsequenz:** Ein Layout-/Variantenwechsel löst nichts. **Keine
Umlaut-Sonderlogik in der App** — die einzige nötige Behandlung (DE-Layout-Env für
dotool) ist bereits implementiert.

---

## 5. Was als Nächstes zu tun ist

> **STATUS 27.10, ~21:05:** Prioritäten 1–3 sind **erledigt** (keyd-Fix gesetzt
> und verifiziert; K1–K8 committet in `2dbc79b`; Phantom-F24 via 90-s-Test +
> Live-Betrieb ausgeschlossen). Übrig bleibt nur noch §5.4 Aufräumen.

### 5.1 Priorität 1 — Systemzustand heilen ✅ ERLEDIGT

- keyd-Ausschluss `-4711:0815` gesetzt, `keyd check` fehlerfrei,
  Journal zeigt `ignoring 4711:0815 (dotool keyboard)`.
- Kein Relogin nötig geworden — nach dem Fix war die Schreibung sofort korrekt
  und die LED-Semantik wieder konventionell (`capslock_led_inverted = false`).

### 5.2 Priorität 2 — Korrekturen K1–K8 umsetzen (Code) ✅ ERLEDIGT (`2dbc79b`)

Die Implementer-Runde (GLM 5.3) wurde **mitten in der Arbeit gestoppt**. Der Plan
liegt vollständig vor und ist umsetzbar:

| ID | Aufgabe |
|---|---|
| **K1** | `_capslock_leds() -> dict[str,str]` pro Device; `_capslock_state()` baut darauf auf (OR bleibt). Diagnose-Logs geben **pro Device** aus. |
| **K2** | `capslock_led_inverted: bool = True` **wieder einführen** (belegte Semantik im Docstring). `_ensure_capslock_normal()`: Ziel = `"an"` wenn inverted, sonst `"aus"`; nur bei `capslock_autocorrect=True` togglen; max 2 Versuche, verifiziert. |
| **K3** | `test_capslock_led_inverted_key_rejected` → `..._accepted` umdrehen (Schlüssel ist wieder gültig) |
| **K4** | Neues Subcommand `stt-button caps-diagnose`: LED pro Device, `xset`-Sicht, gültiger Zielzustand, Probetyp-Marker `[[diagnose-OK]]` |
| **K5** | `docs/KONZEPT.md`: CapsLock-Abschnitt auf **belegte** Semantik umschreiben |
| **K6** | Charakterisierungstest für dotool-Env (`DOTOOL_XKB_LAYOUT=de`, `..._VARIANT=nodeadkeys`) |
| **K7** | `tests/test_keymap_reference.py` mit `xkbcli`-Tests (skipif fehlt): Umlaut-Level, ẞ-FOUR_LEVEL_PLUS_LOCK, Variantenvergleich |
| **K8** | `_capslock_leds()` ehrlich benennen; Log-Hinweis "LED ist kein verlässlicher State" |

**Kernregel für alle Änderungen:** Die App darf CapsLock **nie ungefragt** togglen.
Default `capslock_autocorrect=False` → nur warnen.

### 5.3 Priorität 3 — Phantom-F24 klären ✅ ERLEDIGT

Quelle war die dotool→keyd-Rückkopplung (§3.3). Verifikation: 90-s-Leerlauf
mit 0 Events, Positivkontrolle exakt 1 Press+Release, Live-Diktat sauber.
Wiedervorlage bei Bedarf:
```bash
/tmp/f24_watch.py 90   # passiver Wächter (Dienst muss aus sein)
journalctl --user -u stt-button -f | grep -i "Trigger"
```
Kommt `Trigger press` ohne Tastendruck → keyd-Combo-Leckage (Systemconfig).

### 5.4 Priorität 4 — Aufräumen

- Uncommittete Änderungen prüfen, Tests laufen lassen, **committen**
- `README.md` um `capslock_*`-Optionen ergänzen (dort ist die Config nicht dokumentiert)
- `docs/KONZEPT.md` ist die Übergabe-Datei für ein Android-Projekt (siehe §6)

---

## 6. Wichtige Projekt-Artefakte

| Datei | Zweck |
|---|---|
| `docs/KONZEPT.md` | **Übergabedokument für Nachimplementierung** (u. a. Android): 7 Kernmechanismen, 6 Fallstricke, was NICHT funktionierte, Teststrategie, Checkliste |
| `config.example.toml` | Vorlage mit allen Optionen und Kommentaren |
| `src/stt_button/streaming_types.py` | `Token`, `Transcription`, `StreamWindowPolicy` |
| `src/stt_button/committer.py` | `TimestampCommitter` (LocalAgreement + Wortgrenzen) |
| `src/stt_button/asr.py` | `ParakeetEngine`, `QwenEngine`, `WhisperEngine`, `StreamTranscriber`, `_token_budget` |
| `src/stt_button/typing.py` | `DeltaTyper` (Kern-Ausgabelogik), `DotoolTyper`, `WtypeTyper`, `ClipboardTyper` |
| `src/stt_button/controller.py` | Zustandsmaschine, 3-Thread-Architektur, Shutdown-Reihenfolge |
| `src/stt_button/keys.py` | evdev-Reader für die Trigger-Taste |
| `src/stt_button/vad.py` | `SileroVadGate` |

### Test-Helfer (für neue Tests wiederverwendbar)

- `tests/test_controller.py`: `FakeAudioSource`, `FakeEngine`, `FakeTyper`, `make_controller()`
- `tests/test_asr.py`: `FakeEngine`, `audio(seconds)`
- `tests/test_typing.py`: `FakeTyper`, `_capture()`, `_capture_stdin()`, `subprocess_result()`, `AlwaysSpeech`/`SilenceVad`/`NeverSpeech`
- `tests/test_keys.py`: `FakeDevice` (`push(type, code, value)`, `read()`)

---

## 7. Arbeitsweise in dieser Session (bewährt)

**Rollenverteilung, die sich etabliert hat:**

1. **Kimi K3** — Debugging/Analyse/Recherche (Oracle-Template)
2. **GLM 5.3** — Implementierung TDD (implement/delegate-Template)
3. **Deepseek V4 Pro 0813** — Review (review-Template)

**Wichtigste Lehre:** Experten-Pläne **verifizieren, nicht glauben**. Zwei
Kimi-Analysen zur CapsLock-Semantik waren falsch; erst ein empirisches
Ground-Truth-Experiment (Marker tippen bei definierten Zuständen) hat es geklärt.

**Weitere Lehren:**
- Fixes am ASR waren oft richtig — aber mehrere Probleme lagen **außerhalb** der App
  (keyd, mutter, CapsLock-State)
- Ein Fix kann das Problem **verursachen** (der CapsLock-Toggle hat die Inversion
  erzeugt und dauerhaft gehalten)
- Immer ein **gespeichertes Test-Audio** durch die Pipeline schicken und mit der
  Einzeltranskription vergleichen — deckt Fehler zuverlässig auf

---

## 8. Offene Fragen / Risiken

1. ~~**keyd-`[ids] *`**~~ — **erledigt:** Device-Ausschluss `-4711:0815` ist
   gesetzt und per keyd-Journal verifiziert (`ignoring 4711:0815`).
2. **Phantom-F24** — **erledigt:** Quelle war die dotool→keyd-Rückkopplung.
   90-s-Leerlauf-Test nach dem Fix: 0 Events; Live-Betrieb sauber (siehe §3.3).
3. **Uncommittete Änderungen** — Doku-Updates (KONZEPT/HANDOFF) und die
   Live-Config (`capslock_led_inverted = false`) sind die letzten Reste.
4. **Dienst-Autostart** — läuft aktuell manuell (`start`, aber `disabled`).
   Dauerhaft: `systemctl --user enable stt-button` (bewusst noch nicht
   gesetzt, damit ein Neustart first nichts Uberraschendes tut).
5. **Der `windowed`-Modus ist unfertig** — er hat die dokumentierten Deadlocks.
   `whole_buffer` ist Default und funktioniert; wer wortweises Live-Erscheinen will,
   muss `windowed` mit den Fixes aus §3.1 fertigstellen.
