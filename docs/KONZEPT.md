# Concept: Push-to-Talk Dictation with Local ASR

**Purpose of this document:** A transferable description of a proven architecture for
push-to-talk dictation (hold the key → speak → text appears at the cursor position).
The mechanisms were implemented, tested, and iteratively debugged on a Linux desktop
(ThinkPad, CPU-only). This document describes **what works, why, and which pitfalls
must be avoided**—so that the same principle can be implemented on another platform
(e.g., Android) without repeating the mistakes.

Reference implementation: `stt_button` (Python 3.12, ~3,300 LOC, 179 tests).

---

## 1. Overview

```
 ┌──────────────┐    ┌───────────────┐    ┌──────────────────┐    ┌──────────────┐
 │ Trigger      │───▶│ Audio-Capture │───▶│ ASR (Streaming)  │───▶│ Text-Ausgabe │
 │ (Taste halten)│    │ (16 kHz mono) │    │ (lokales Modell) │    │ (Cursor)     │
 └──────────────┘    └───────────────┘    └──────────────────┘    └──────────────┘
      press/release      Ringpuffer         Transkribiert den       Differenz-Tippen
      = Start/Stop       + Thread           wachsenden Puffer       + Backspace-Revision
```

**Core principle:** The system is a **dictation tool**, not a chat interface.
The text must appear *live*, be correctable, and must not destroy the user's text.

---

## 2. The Seven Core Mechanisms (All Proven)

### 2.1 Push-to-Talk as a State Machine

```
IDLE ──press──▶ RECORDING ──release──▶ FINALIZING ──▶ IDLE
                    │
                    └── abort() (Fehler/Timeout) ──▶ IDLE
```

* **Hold the key = record, release = finalize.** No toggle, no double-click.
* `start_recording()` is **idempotent** (a duplicate key press does not restart it).
* A lock (`threading.RLock`) serializes access from the key thread and ASR loop.
* **The key state is always tracked:** `_key_down` is updated on *every* press/release,
  regardless of the controller's current state. After finalization completes, the next
  recording starts automatically **only if** the key is **still held** (check and start
  are atomic under the same lock). A short press completed during FINALIZING is
  discarded—it never captured audio. (Previously: Boolean `_pending_start`
  = "a press was seen at some point" → ghost recording; see pitfall 3.6.)

> **Android:** Start `AudioRecord` on `ACTION_DOWN`, stop it on `ACTION_UP`.
> `onKeyDown` can fire multiple times → idempotency is mandatory. Likewise:
> `ACTION_UP` must ALWAYS clear the key state, even while the previous
> transcription is still running—otherwise the next recording starts for a key
> that was released long ago.

### 2.2 Trigger Detection Separate from the Microphone

The trigger key is detected **independently** of the recording path.

* Desktop: physical key → `keyd` remap → `F24` on a virtual device → direct
  reads from `/dev/input/event*` (pure Python, no C dependency).
* **Filter autorepeat:** Ignore events with `value == 2`; otherwise recording
  restarts on every repeat.
* **Handle press and release separately**—release detection is the critical path
  (see 2.3).
* Resolve the device **by name**, not by path (numbers change after reboot).

> **Android:** Hardware key via `KeyEvent` (e.g., `KEYCODE_HEADSETHOOK` for
> headset buttons, or a `MediaButtonReceiver`). Note: Volume keys are reserved by
> the system; headset/Bluetooth buttons are the more reliable approach.

### 2.3 Key Detection MUST Run in Its Own Thread ⚠️

**This is the most important architectural point and was a real bug.**

Problem: ASR inference blocks (CPU-only: 1–3 s per transcription). If key detection
runs in the same loop, **releasing the key during inference is not processed** →
recording continues even though the user is finished.

Solution (two stages):

```python
# 1) Key listener in its own thread
threading.Thread(target=self._listener.run, name="key-listener", daemon=True).start()

# 2) Audio capture in YET ANOTHER thread that writes to a queue
def _capture_loop(self):
    while self.state == RECORDING:
        chunk = source.read(block=False)
        if chunk:
            self._audio_queue.put(chunk)   # decoupled from inference
```

The main thread now only consumes the queue and transcribes.

**Why three threads?** Key thread (never block) · capture thread (audio continues
even while inference is running) · main thread (inference + output).

> **Android:** `AudioRecord.read()` in a `HandlerThread`/coroutine, KeyEvent handling
> on the UI thread (set flags only, never block). Inference on `Dispatchers.Default`.

### 2.4 Audio Pipeline: 16 kHz Mono, s16le

* **Format:** 16 kHz, 1 channel, 16-bit signed little-endian (`s16le`).
  This is the native input format of Whisper/Parakeet—no resampling required.
* **Recording as a subprocess/stream**, chunks of ~0.2–1 s into a ring buffer.
* When stopping, **observe the order** (⚠️ real bug, see 2.8).

### 2.5 ASR: Model Selection and Decoding Parameters

| Model | Type | Parameters | CPU Latency (RTF) | Recommendation |
|---|---|---|---|---|
| **Parakeet TDT 0.6B v3** | Transducer (non-autoregressive) | 0.6 B | **~0.1–0.5** | ✅ **best choice** |
| faster-whisper large-v3-turbo | seq2seq (autoregressive) | 0.8 B | ~0.5–1.8 | Fallback |
| Qwen3-ASR 1.7B | LLM (autoregressive) | 1.7 B | ~2–3 | only for maximum quality |

**Why Parakeet:** Transducers decode **greedily and in parallel** (no
token-by-token sampling) → 5–10× faster than Whisper on CPU with better
mixed DE/EN quality. Automatically recognizes 25 European languages.

**Decoding parameters that have proven effective:**

```python
model.transcribe(
    audio,
    language = None,                      # None = automatic detection ("auto")
    task = "transcribe",
    beam_size = 1,                        # greedy: throughput > marginal quality
    temperature = 0.0,                    # deterministic (important for streaming!)
    condition_on_previous_text = False,   # prevents repetition loops
    vad_filter = True,                    # filter silence (with Whisper engine)
    initial_prompt = None,
)
```

**`temperature = 0.0` is critical:** With streaming, the same buffer is transcribed
multiple times. Sampling would produce different text each time → flickering.
Determinism is a prerequisite for the commit logic (2.6).

### 2.6 Streaming: "Growing Buffer" + LocalAgreement Commit

**The centerpiece.** Without this mechanism, the result is either latency or flickering.

```
Audio: [============ wachsender Puffer ============]
                ↓ alle asr_interval_s (1.0 s Audio)
Transkription: "So, da bin ich"  →  "So, da bin ich mal"  →  "So, da bin ich mal gespannt"
                ↓ LocalAgreement: nur stabilen Praefix committen
Getippt:       "So, da bin ich"  →  " mal"                →  " gespannt"
                └── committed ──┘   └───── korrigierbar (Partial) ─────┘
```

**Algorithm (LocalAgreement):**

1. Keep a history of the last `commit_agreements = 2` transcriptions.
2. Compute the **common prefix** of all history entries.
3. Commit only **up to the last word boundary** (the current word remains correctable).
4. Committed text is **never revoked**—this prevents flickering.

```python
common_len = len(history[0])
for candidate in history[1:]:
    common_len = common_prefix_len(history[0][:common_len], candidate)
common_str = history[0][:common_len]
cut = common_str.rfind(" ")          # only up to the word boundary
committed = common_str[:cut + 1]
```

**Why 2 agreements?** A single run can be a hallucination (especially with short
chunks). Only when two consecutive runs produce the same text is it considered stable.

**Why only up to the word boundary?** Otherwise, a partially spoken word would be
truncated and frozen—exactly the bug that produced "This should now be **de**"
instead of "This should now be **definitely**."

### 2.7 Delta Output (DeltaTyper)

The transcription stream repeatedly returns **the entire text**. Only the
**difference** may be typed.

```python
old = zuletzt_getippter_text
committed = len(committeter_praefix)     # unantastbar

# Safety net: committed region must never be lost
if len(new) < committed: return

# Compare only the NON-committed tail
old_tail, new_tail = old[committed:], new[committed:]
common = common_prefix_len(old_tail, new_tail)
to_remove = len(old_tail) - common        # Backspaces
to_add    = new_tail[common:]             # new text

if to_remove: backspace(to_remove)
if to_add:    type_text(to_add)
```

**Three protective rules that proved necessary:**

1. **Never touch the committed prefix**—prevents stable text from flickering.
2. **`max_revision_chars` (60):** If more than 60 characters would have to be revoked,
   the cursor is usually no longer after our text (the user clicked) → then **append
   only** instead of destroying text.
3. **Final transcription fully revises** (`force_revision=True`,
   `committed_len=0`): On release, the model may correct everything because the user
   has finished dictating and the cursor is reliably after our own text.

### 2.7.1 Final Delta Paste (final_paste)

Typing character by character costs ~35 ms per character (typedelay+typehold)—300
characters ≈ 10.5 s. With `final_paste = true`, the **computed delta** is instead
inserted on completion (`finalize`) via `wl-copy` + paste chord (`paste_key`, default
`ctrl+v`, `ctrl+shift+v` for terminals).

**Delta rule:** `paste()` receives exclusively `to_add`—the suffix after the common
prefix of what has already been typed—never the full text. Backspaces still go through
the normal typer (dotool), and `append_space` comes afterward.

**Limits (deliberate):**

* No restoring the old clipboard contents—that races with clipboard managers; the
  dictation history ends up there instead. No `wl-paste`.
* `wl-copy` is missing or copying fails → fall back to character-by-character typing
  (`type_text`), no startup error.
* If the paste chord fails **after** a successful copy, it is NOT retried by typing
  (risk of duplicate insertion)—the error is logged.
* Live output during recording always remains dotool; only `finalize()` pastes.
* Password fields: like any clipboard output, the dictation briefly ends up in the
  clipboard—disable `final_paste` for sensitive input.

### 2.8 Shutdown Order on Release ⚠️

**A real bug that swallowed audio.** Incorrect order:

```python
rest = source.stop()        # ❌ newest bytes first
transcriber.feed(rest)
drain_queue()               # older bytes afterward → timeline reversed!
```

**Correct:**

```python
state = FINALIZING          # 1) capture thread terminates
capture_thread.join(1.0)    # 2) wait until no more chunks arrive
drain_queue()               # 3) OLDER bytes first
rest = source.stop()        # 4) NEWEST bytes last
transcriber.feed(rest)
```

A reversed audio timeline → hallucinations and missing sentence fragments.

---

## 3. Pitfalls (Learned from Real Bugs)

### 3.1 `max_new_tokens` Must Scale with Audio Length ⚠️

A **fixed** token limit abruptly truncates long dictations. Measured: the same 60-s
audio produced only **123 characters** with `max_new_tokens=256`, but **294 characters**
with 2048.

```python
def token_budget(audio_seconds: float) -> int:
    # German ≈ 12 subword tokens/second (2–4 tokens/word,
    # ~2.5 words/s + headroom)
    budget = int(audio_seconds * 12) + 64
    return max(256, min(budget, 4096))    # minimum and maximum limits
```

**Additional pitfall:** When a slice with left context is transcribed, the context
consumes token budget even though it is discarded afterward → always calculate the
budget based on the **total length of the audio passed in**.

### 3.2 Set the Recording Duration Limit Deliberately

A default of 60 s cuts off dictations. For dictation, **300 s** (5 min) is a good
compromise (RAM: 16 kHz × 2 bytes = 32 kB/s → 300 s ≈ 9.6 MB).

### 3.3 VAD Guard Must Not Discard Real Speech ⚠️

Silero VAD always returns **`False` for audio < 1 s**. A naive guard of
"if VAD is False → do not transcribe" then completely swallows short dictations.

**Rule:** Discard only **short** remainders (< 2 s) when VAD is False. Transcribe long
remainders **anyway** (data loss is worse than the risk of hallucination), but log a
warning.

### 3.4 Parakeet Hallucinates on Very Short Chunks

Measured: A 1-s chunk produced `"So it's uh big"` (English!) for German text;
at 1.5 s it correctly produced `"So, now I'm curious."`.

**Consequence:** `min_audio_s` (minimum audio before the first transcription)
**≥ 1.0 s**, preferably 2.5–3.0 s. Also: The **first** commit requires agreement
(2.6); otherwise, a hallucination from the first chunk gets frozen.

According to NVIDIA's model card, Parakeet is *"not recommended for word-for-word/incomplete
sentences"*—that is the reason.

### 3.5 Two Error Patterns in Text Output (Keyboard Emulation) ⚠️

When typing via keyboard emulation (uinput), **modifier states** can become corrupted.
The two patterns are **distinguishable character by character**—which is diagnostically
valuable:

| Error Pattern | Example | Cause | Identifying Feature |
|---|---|---|---|
| **Shift stuck** | `SO; DA BIN ICH` | Shift latch | Punctuation **also transformed**: `,`→`;` `,`→`:` |
| **CapsLock on** | `dAS sKRIPT` | CapsLock inverted | Punctuation **unchanged** |

**Diagnostic procedure:** Pass the expected text through the Shift layer or a CapsLock
inversion once and compare it with the visible text. If it matches character by
character, it is **not an ASR error**, but the lock state of the output pipeline.

**Hardening:**
* **Typing timing configurable** and generous: `typedelay`/`typehold` (desktop: 20/15 ms).
  Too small → key events are lost under CPU load → modifiers get stuck.
* **Log lock status** (before/after each typing operation)—otherwise the source cannot
  be attributed.
* **Detect CapsLock and warn only** (default, `capslock_autocorrect = false`).
  The app must **never** toggle CapsLock itself (rationale below).
* **Set the correct keyboard layout** (desktop: `DOTOOL_XKB_LAYOUT=de`,
  `DOTOOL_XKB_VARIANT=nodeadkeys`). Without this: `z`↔`y` swapped, `?`→`_`.

#### CapsLock: Detect and Warn—Do Not Intervene

**On this system, the mapping was empirically confirmed to be inverted for a time:**
`LED off = CapsLock active = inverted text`; `LED on = normal capitalization`
(lowercase/uppercase as typed). Measured on ThinkPad + keyd + GNOME/Wayland—this was
**inverted** relative to the usual convention.

**Cause and end of the inversion (October 27, verified):** With `[ids] *`, keyd also
grabbed dotool's uinput keyboard (`4711:0815`) and maintained its own synthetic lock
state for it—thereby decoupling the LED and the real XKB lock. After excluding it with
`-4711:0815` in `/etc/keyd/default.conf`, the system behaved **conventionally** again
(LED off = CapsLock off = normal capitalization, confirmed by a dictation test). The
config option `capslock_led_inverted` supports both semantics; on this system it is now
set to `false`. **Lesson:** A grabbing remapping daemon between the typer and compositor
can reverse the LED↔state mapping—clean up the pipeline first, then measure the semantics.

**Core rule:** **The app must never toggle CapsLock without asking.** The lock state
cannot be read reliably (`LED != State`, `xset != State`):

* `key capslock` via uinput is a **toggle**—you never know which direction it will go.
  A second toggle "just to be safe" can restore the error.
* The kernel LED can lag behind the real lock state (state write-through) or be reported
  differently by multiple devices.
* `xset q` (XWayland) is **not** a reliable source: with Wayland compositor state,
  `xset` reports "off" while the lock is active (measured myself).

Therefore: **detect and warn only**. The prior state is logged so that the source of an
inverted dictation can be attributed unambiguously:

```python
def _capslock_leds() -> dict[str, str]:
    """Read kernel LED per device - device name -> 'on'/'off'/'?'."""
    leds = glob.glob("/sys/class/leds/*capslock*/brightness")
    result = {}
    for path in leds:
        device = os.path.basename(os.path.dirname(path))
        raw = open(path).read().strip()
        result[device] = "an" if raw == "1" else ("aus" if raw == "0" else "?")
    return result

def _capslock_state() -> str:
    """Aggregate across all devices - 'on', 'off', or '?'."""
    leds = _capslock_leds()
    if not leds:
        return "?"
    return "an" if any(v == "an" for v in leds.values()) else "aus"

def _ensure_capslock_normal(self):
    """By default: warn only - NO toggle."""
    target = "an" if self._capslock_led_inverted else "aus"
    state = _capslock_state()
    if state == "?" or state == target:
        return
    log.warning(
        "CapsLock-LED steht auf %r, Ziel %r - Text kann invertiert "
        "erscheinen. Es wird NICHT umgeschaltet.", state, target,
    )
```

Only those who **explicitly** want the intervention set `capslock_autocorrect = true`:
it then switches to the configurable target state—`capslock_led_inverted = true`
(default) means target LED "on", `false` means target LED "off"—with verification and
at most two attempts. The toggle uses exclusively `_send_raw()` (without the CapsLock
check); otherwise, the check would call itself (recursion risk).

**Manual remedy** (the user's responsibility, not the app's):

* `stt-button reset-capslock` — sets the LED to the configured target state
  (max. 3 toggles) and states the next steps.
* `stt-button caps-diagnose` — lists all LEDs per device, the `xset q` Caps Lock line,
  and the effective target state, and types a marker `[[diagnose-OK]]`.
* Press the **physical CapsLock key** once or log out/in (resets the seat-global XKB state).
* If it remains inverted: `sudo systemctl restart keyd` and log in again.

#### Umlauts Are a Good Litmus Test

CapsLock **also inverts umlauts**: `ae`→`AE`, `oe`→`OE`, `ue`→`UE`, `ss`→`SS`. A test string such as
`"Pruefe die Umlaute: ae oe ue ss gross"` reveals this immediately—and simultaneously checks
whether the keyboard layout (`de`) is set correctly.

> **Android:** This error pattern does **not** exist there—text is inserted via
> `InputConnection.commitText()` / `AccessibilityService`, not by key emulation.
> That is a real advantage.

### 3.6 Press/Release Edges During FINALIZING: The Ghost Recording ⚠️

**Symptom:** The user finishes dictation 1 before the (seconds-long) final transcription
completes and immediately presses the key for dictation 2—and recording starts **without
the key being held** or not at all.

**Cause (two races):**
1. The "pending" mechanism was a **Boolean** (`_pending_start`): It only remembered
   "a press occurred at some point." The corresponding release arrived during FINALIZING
   and was silently discarded → after finalization ended, a recording started for a key
   that had long been released.
2. "Consuming pending" and `state = RECORDING` were **not atomic**—even a release shortly
   after finalization ended could be lost in the handoff window.

**Fix (236 tests, TDD):** Boolean → **`_key_down`** (physical key state).
Invariants:
* Release **always** updates the key state, regardless of controller state.
* After finalization ends, recording starts **only while the key is held**.
* Check + start run **atomically under the same lock** (RLock permits `_start_now()`
  inside the block; `_finish()` already blocks there anyway).

**Deliberate limit:** A short press completed entirely during FINALIZING is **discarded**—
it never captured audio (no capture runs during FINALIZING). Lossless dictations in this
phase require raw audio buffering across state transitions; this is deliberately NOT
implemented (cost/benefit).

> **Android:** Exactly the same pitfall with `ACTION_DOWN`/`ACTION_UP` during an ongoing
> transcription. Track key state, never a Boolean "was pressed at some point."

### 3.7 Summary: Do's & Don'ts

| ✅ Do | ❌ Don't |
|---|---|
| Key detection in its own thread | Key handling in the inference loop |
| `temperature=0.0` (deterministic) | Sampling during streaming |
| Scale token budget with audio length | Fixed `max_new_tokens=256` |
| Commit only up to the word boundary | Freeze partial words |
| Never touch the committed prefix | Allow stable text to flicker |
| VAD only on **new** segments | VAD on left context (always True) |
| Transcribe long silent remainders anyway | Discard long remainders as "silence" |
| Shutdown: queue → then stop() remainder | Feed stop() remainder before the queue |
| Log lock state | Suspect ASR for text errors |

---

## 4. Configuration (Reference Values)

```toml
# Trigger
input_device_name = "keyd virtual keyboard"   # device with the trigger key
key_codes         = ["KEY_F24"]               # Linux keycode(s)

# Audio
audio_source = "auto"        # PipeWire source name or "auto"
sample_rate  = 16000         # 16 kHz mono s16le (native ASR format)

# ASR
backend      = "parakeet"    # "parakeet" | "whisper" | "qwen"
model        = "nvidia/parakeet-tdt-0.6b-v3"
language     = "auto"        # "auto" | "de" | "en"
compute_type = "int8"        # CPU: int8/float32; GPU: float16
cpu_threads  = 8             # = physical cores (no hyperthreading)

# Streaming
streaming_mode   = "whole_buffer"  # more robust than "windowed" (see 5.)
min_audio_s      = 3.0   # transcribe only after 3 s of audio (hallucination guard)
asr_interval_s   = 1.0   # re-transcribe every 1 s of audio
max_record_s     = 300.0 # 5 minutes
commit_agreements = 2    # LocalAgreement confirmations

# Output
typer            = "dotool"   # "dotool" | "wtype" | "clipboard"
xkb_layout       = "de"
xkb_variant      = "nodeadkeys"
typedelay_ms     = 20
typehold_ms      = 15
append_space     = true   # space after the dictation
max_revision_chars = 60   # safety limit for backspace revisions
```

---

## 5. What Did NOT Work (Important for Reimplementation!)

### 5.1 Window-Based Streaming (`windowed`)

Attempt: Sliding windows `[4 s left context | 2 s chunk | 1 s right context]`,
adopting only tokens from the commit horizon (based on NVIDIA's "Buffered Chunked
Inference", 10-2-2).

**Result: too many deadlocks.** Specific problems:

1. **Pause stall:** The window was capped at `committed_s + 3 s`. During a speech pause
   ≥ 3 s, VAD continuously returned "no speech" and `committed_s` never advanced →
   **everything after the pause was lost.** (Fixed by silence skip, but see 2.)
2. **Window ↔ commit coupling:** The window could not extend beyond `committed_s + n`.
   If the commit stalled, the window stalled → the remaining audio was never seen.
3. **Agreement deadlock:** The commit point advanced with every commit, so the agreement
   cache never collected 2 matches → permanent standstill.

**Recommendation:** For a dictation tool, the **simpler `whole_buffer` approach** (2.6)
is more robust. It has no state machine that can run into a deadlock.

**Measurement comparison** (13.4 s test audio with a 4 s pause, reference = single
transcription of the entire audio):

| Mode | Result |
|---|---|
| Reference (entire audio) | 256 characters ✅ |
| `whole_buffer` | **256 characters** ✅ (identical) |
| `windowed` | 137–139 characters ❌ (parts missing) |

**Trade-off:** `whole_buffer` delivers text in blocks (every ~1.5 s), not word by word.
That is acceptable for dictation; anyone who wants true word-by-word output must carefully
complete the windowed approach (with the deadlock fixes mentioned above).

### 5.2 Retroactively "Normalizing" Text

Attempt to correct UPPERCASE words using regex.

**Rejected** because it treats the symptom rather than the cause (see 3.5) and destroys
legitimate capitalization (acronyms, proper nouns).

---

## 6. Porting to Android—Concrete Guidance

### 6.1 What Can Be Adopted 1:1

| Mechanism | Android Implementation |
|---|---|
| State machine (2.1) | `enum` + `MutableStateFlow` |
| LocalAgreement commit (2.6) | unchanged (pure text logic) |
| DeltaTyper logic (2.7) | unchanged (pure text logic) |
| Token budget (3.1) | unchanged |
| VAD guard rules (3.3) | unchanged |
| Parakeet model selection (2.5) | via ONNX Runtime Mobile / TensorFlow Lite / NNAPI |

### 6.2 What Must Be Replaced

| Desktop | Android |
|---|---|
| `/dev/input` + keyd | `KeyEvent` / `MediaButtonReceiver` / `AccessibilityService` |
| `pw-record` (PipeWire) | `AudioRecord` (16 kHz, MONO, PCM_16BIT) |
| `dotool` (uinput) | `InputConnection.commitText()` or `AccessibilityService.ACTION_SET_TEXT` |
| systemd user service | `ForegroundService` (mandatory for microphone from Android 14) |
| Python + transformers | Kotlin/Java + ONNX Runtime (`parakeet-tdt-0.6b-v3` is available as ONNX/int8) |

### 6.3 Android-Specific Advantages

* **No keyboard emulation problem** → 3.5 is completely eliminated.
* **No threading problem with key events** → KeyEvents come from the system UI thread.
* **NNAPI/GPU** → significantly faster inference than the CPU-only reference.

### 6.4 Android-Specific Pitfalls

1. **Microphone permission + ForegroundService** (from Android 14, `FOREGROUND_SERVICE_MICROPHONE`).
2. **Battery optimization** can kill the service → `REQUEST_IGNORE_BATTERY_OPTIMIZATIONS`.
3. **`AudioRecord` latency:** Determine buffer size via `getMinBufferSize()`;
   too small → dropouts, too large → latency.
4. **Text injection into other apps** is possible only via `AccessibilityService`
   (observe Play Store policies!). For your own apps: `InputConnection`.
5. **Model size:** Parakeet 0.6B int8 ≈ 700 MB—too large for an app bundle.
   Options: Download on first launch, or a smaller model (Parakeet 0.6B is already
   the small variant; alternatives: `whisper-small` ONNX ≈ 250 MB).

---

## 7. Testing Strategy (What Has Proven Effective)

**All core mechanisms are testable without a microphone, model, or keyboard**—through
dependency injection of `AudioSource`, `Engine`, and `Typer`.

| Test Level | What | How |
|---|---|---|
| Unit: Commit | LocalAgreement, word boundaries | Fake token lists with timestamps |
| Unit: DeltaTyper | Revision, backspace, safety limits | Fake typer that records calls |
| Unit: Token budget | Scaling with audio length | pure function |
| Integration | Pipeline with fake engine | predefined transcriptions per buffer length |
| End-to-end | real path | saved WAV through the pipeline |

**Especially valuable:** Send a saved test audio containing a **pause** through the
pipeline and compare the result with the **single transcription of the entire audio**.
This exact test uncovered the windowed defects (256 vs. 137 characters).

**Debugging aids that paid off:**
* Dump the audio of every recording to `/tmp/stt_last.wav` → reproducibly analyzable.
* Log every transcription step (`audio_seconds → text, committed_len`).
* Log the lock state (CapsLock) before/after each typing operation, and log trigger
  press/release (3.5).

---

## 8. Quick Checklist for Reimplementation

```
[ ] Push-to-Talk-State-Maschine, idempotent, mit Lock
[ ] Key-Erkennung: eigenener Thread, Autorepeat (value==2) filtern
[ ] Audio-Capture: eigener Thread → Queue → 16 kHz mono s16le
[ ] ASR: Parakeet TDT (oder Whisper), temperature=0, deterministisch
[ ] Streaming: wachsender Puffer, Re-Transkription alle ~1 s Audio
[ ] Commit: LocalAgreement (2×), nur bis Wortgrenze
[ ] Ausgabe: DeltaTyper mit committed-Praefix-Schutz + max_revision_chars
[ ] Finale Transkription: voll korrigierend (force_revision)
[ ] Shutdown: join → Queue drainen → stop()-Rest
[ ] Guards: min_audio_s ≥ 1 s, Token-Budget skaliert, VAD nur kurze Reste
[ ] max_record_s bewusst setzen (z. B. 300 s)
[ ] Tests ohne I/O (Fake AudioSource/Engine/Typer)
[ ] Test-Audio MIT PAUSE gegen Einzeltranskription vergleichen
```

---

## 9. Reference Measurements (Intel Lunar Lake, CPU-Only, 8 Cores)

| Model | 20 s Audio | RTF | Note |
|---|---|---|---|
| Parakeet TDT 0.6B (int8/float32) | ~2.5 s | **0.12** | ✅ recommended |
| faster-whisper large-v3-turbo (int8) | ~8.5 s | 0.43 | Fallback |
| Qwen3-ASR 1.7B | ~23 s | 1.15 | too slow for live use |

**Model load time:** Parakeet ~2 s (keep it warm! Preload at service startup, not on
the first key press).

**Quality comparison (German dictation with English technical terms):**

| Model | Transcript | Assessment |
|---|---|---|
| **Parakeet** | "...quite shocking... the vibes in the room..." | ✅ English terms correct |
| faster-whisper (`language="de"`) | identical | ✅ (only with forced language!) |
| faster-whisper (`language="auto"`) | completely translated into English | ❌ |
| Granite Speech 4.1 | "...the **females** in the room..." | ⚠️ misrecognition |

**Important:** With Whisper, set `language` **explicitly** when the language is known—
auto-detection switches to the wrong language with mixed DE/EN.

## 10. Latency: Release → Output (STAGE 1)

The operator's measurement was 5–7 s from release to inserted text. Two causes were
structural, not computational:

**(A) One lock for everything.** `run_once()` held the controller `RLock` throughout
the entire live full-buffer inference *and* while typing. Release (`stop_recording()`)
wanted the same lock and therefore waited for the ongoing live inference before it
could even stop the audio source.

New: two locks with a clear hierarchy (`_exec_lock` → `_lock`).

* `_lock` (RLock): only brief state transitions—`state`, `_key_down`,
  `_source`/`_transcriber` pointers, counters. Never held during inference.
* `_exec_lock` (Lock): serializes the transcriber + DeltaTyper execution chain,
  meaning both the live tick **and** finalization. The key thread never takes this lock.

This means: Release → `state=FINALIZING` (briefly under `_lock`) → `_finish()`
**without** either lock → capture thread ends, `source.stop()` immediately. The audio
source stops in < 0.3 s, even in the middle of live inference. The order "drain queue
before `stop()` remainder" (RC-B2/B3) is preserved: both feeds occur in the finalizer
under `_exec_lock`, thus after the last live tick on the same transcriber.

**(B) Duplicate inference at finalization.** `finalize()` retranscribed the entire buffer
even though the last live tick had already successfully transcribed exactly the same
buffer—with fixed decoding parameters (temperature=0, beam_size=1), an identical result
for nothing. `finalize_mode = "cached"` skips the engine call if the buffer has not changed
since the last **successful** inference (same byte length, no `feed()`/`reset()` since).
The note is set only *after* a successful run; an exception tick invalidates the cache.
The cached text comes with `committed_len = 0` (full correctability as before) and does
not touch the agreement history. The default remains `"full"`.

**(C) WAV dump on the critical path.** The debug dump ran synchronously before
finalization. `audio_dump_mode` makes it configurable: `"sync"` (default, previous
behavior), `"async"` (byte snapshot + separate thread, never a reference to the buffer),
`"off"`.

**Measuring.** `latency_trace = true` logs the measurement points at debug level:
`stop entered`, `capture stopped`, `source stopped`, `dump start`,
`dump end`, `finalizer exec acquired`, `finalize start`, `finalize end`,
`paste`—each as `latency[<point>] +<seconds>` since release. Additionally, per live tick,
`live asr start/end` and `live typing start/end`, and in `finalize()` the domino metrics
`latency[rev n/m]`, `latency[backspace x.xxxs]`, `latency[paste]`/`latency[type]`, and
`latency[space x.xxxs]`. Instructions: Watch `journalctl --user -u stt-button -f`
(or a manual run with `-v`), speak a dictation, release; the difference from
`stop entered` → `paste` is the perceived latency.

## 11. Live Preview Modes (STAGES 2/3)

Two independent switches control how much work happens **during** recording.
`live_typing_mode` defaults to "committed"; "full" is the documented opt-in for the
previous character-by-character behavior. The final text is identical in all combinations.

### 11.1 `live_typing_mode` — What Is Typed Live

* **`"committed"` (default):** Only the **confirmed increment** (LocalAgreement prefix)
  is typed live. The unstable partial continues to be tracked internally but is never
  typed and never revoked—no live backspaces, no flickering. `finalize()` computes the
  final delta against the text actually typed: `to_remove == 0`, and the suffix arrives
  as **one** paste (or one `type_text`). Zero live commits ⇒ the entire final text is
  pasted exactly once.
* **`"full"` (opt-in, previous behavior):** Every live tick types the entire current
  transcript as a delta—including unstable partials that later ticks revoke via
  backspace. A lively preview, but with visible revision costs (each character ≈ 35 ms
  of typing + backspaces).

### 11.2 `live_asr_until_s` — How Long Live Transcription Runs

The whole-buffer approach retranscribes the **entire** buffer every `asr_interval_s`
seconds—inference costs grow with dictation length (RTF ≈ 0.12 ⇒ 30 s dictation ≈ 3.6 s
per tick). `live_asr_until_s = 20.0` stops live ticks after 20 s of buffered audio:

* `maybe_transcribe()` returns `None` (no more engine calls),
* `feed()` continues collecting (nothing is lost),
* `finalize()` transcribes the **complete** buffer,
* `reset()` reactivates live ticks for the next recording,
* `0.0` = live ticks always active (previous behavior, opt-in).
* Since the int8 engine (RTF ~0.05), the default is `15.0`: a 15-s full-buffer tick costs
  ~0.75 s and therefore remains below `asr_interval_s` (1.0 s)—continuous live preview
  up to 15 s.

Release after the limit with no tick running ⇒ the finalizer acquires `_exec_lock`
without waiting—finalization starts immediately.

### 11.3 Interaction with Post-Release Discard

If a live tick returns **after** release (inference blocked across the release), its
result is discarded instead of typed: It would preempt the final text, and the finalizer
would have to revoke it via backspace. The finalizer delivers the complete text against
the effective cursor state—the committed prefix is preserved.

## 12. Segmentation (Phase 2 C, opt-in)

With `segment_pause_s > 0`, a VAD pause of at least that length after a
hypothesis ending in `.?!` closes the current segment (mini-final): its text
is committed permanently and the audio buffer restarts. `segment_max_s` is a
hard cap per segment (cut at the quietest point). `segment_min_s` is the
minimum segment length before a pause cut. `live_asr_until_s` then applies
per segment. Defaults (`0.0 / 3.0 / 0.0`) keep the previous behavior.

Validation: no negative values; requires `streaming_mode="whole_buffer"`;
excludes `finalize_mode="cached_mid_tail"`; requires `segment_max_s > 0`; with
`live_asr_until_s > 0` also `segment_max_s <= live_asr_until_s`.

Invariants: the done sound plays once, after the final output (never on a
mini-final); the debug dump (`full_audio()`) contains all segments.
