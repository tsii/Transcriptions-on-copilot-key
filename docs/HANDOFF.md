# Handoff — STT-Button (Push-to-Talk Dictation)

**Status:** 2026-10-27, session interrupted (at the user's request).
**Purpose of this document:** Handoff to a new session/person so that work
can continue without loss of context.

---

## 1. What the Project Is

Push-to-talk dictation on a ThinkPad X1 (Ubuntu 24.04, GNOME/Wayland, CPU-only):
Hold the Copilot key → speak → text appears at the cursor position.

| Component | Technology |
|---|---|
| Trigger | Copilot key → `keyd` remap → `KEY_F24` → evdev read (user, no root) |
| Audio | `pw-record` subprocess, 16 kHz mono s16le, dedicated capture thread |
| ASR | NVIDIA **Parakeet TDT 0.6B v3** (transformers, CPU, float32) |
| Streaming | `whole_buffer`: growing buffer, re-transcription every 1 s, LocalAgreement commit |
| Output | `dotool` (uinput) with `DOTOOL_XKB_LAYOUT=de`, `DOTOOL_XKB_VARIANT=nodeadkeys` |
| Service | systemd user unit `stt-button.service` |

**Repo:** `git@github.com:tsii/Parakeet-on-copilot-key.git`, branch `master`
**Latest commit:** `4e6b496` — *CapsLock: corrected inverted LED semantics (LED on = normal)*
**Warning:** This commit contains a **disproven** premise (see §4.1). After it,
there are **uncommitted** changes in the working tree.

---

## 2. Current Technical Status

### 2.1 Running / Working

| | Status |
|---|---|
| Test suite | **232 passed** (`.venv/bin/python -m pytest`) |
| Long-dictation fixes | ✅ fixed & committed (`e7d590e`) |
| Second dictation swallowed | ✅ fixed & committed (`5826f26`) |
| CapsLock K1–K8 + VAD gate + trigger logging | ✅ committed (`2dbc79b`) |
| Uppercase/lowercase **and** umlauts | ✅ correct (confirmed by live dictation) |
| keyd: dotool excluded | ✅ `-4711:0815`, verified in journal |
| Phantom F24 | ✅ no longer reproducible (§3.3) |
| **Service** | ▶️ **running** (`systemctl --user start stt-button`) — autostart (`enable`) intentionally not set yet |

### 2.2 Uncommitted Changes (Working Tree)

```
 M config.example.toml        M src/stt_button/asr.py       M tests/test_config.py
 M docs/KONZEPT.md            M src/stt_button/config.py    M tests/test_typing.py
 M src/stt_button/__main__.py M src/stt_button/keys.py
 M src/stt_button/app.py      M src/stt_button/typing.py
?? tests/test_app_wiring.py  ?? tests/test_cli.py  ?? tests/test_docs.py
?? tests/test_keys_logging.py ?? tests/test_stream_vad.py
```

**These changes are tested (222 green), but NOT committed.** Among other things, they include
the removal of the CapsLock toggle, the VAD gate, trigger logging, and the
documentation adjustment to the (incorrect) premise.

### 2.3 Live Configuration

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
capslock_autocorrect = false     # (line 41, fixed manually; previously: capslock_led_inverted)
```

---

## 3. The Fixed Problems (Chronological, with Causes)

### 3.1 Long Dictations Were Truncated / Parts Were Missing

**Three causes, all fixed (commit `e7d590e`):**

1. **`max_new_tokens=256`** was a hard limit. Proof: the same 60-s audio produced
   only **123 characters** with 256, but **294 characters** with 2048.
   → Fix: `_token_budget(audio_s) = max(256, min(audio_s*12 + 64, 4096))`.
2. **`max_record_s=60`** stopped recordings after 60 s. → Fix: **300 s**.
3. **Pause stall in `windowed` mode:** The window was capped at `committed_s+3s`;
   during a speech pause ≥ 3 s, `committed_s` never advanced → everything after it was lost.
   → Fix: Silence skip (`skip_silence`) + text preservation before the jump.
   **Important finding:** `windowed` mode has too many potential deadlocks.
   Measurement comparison (13.4 s audio with 4 s pause): reference 256 characters ·
   `whole_buffer` **256 characters** ✅ · `windowed` 137 characters ❌.
   → **`whole_buffer` is now the default.**

### 3.2 Second Dictation Was Swallowed

**Cause:** `start_recording()` had `if self.state != IDLE: return`. During
final transcription (blocked in the key thread for several seconds), the state was
`FINALIZING` → the key press was **discarded**.

**Fix (commit `5826f26`):** Final transcription runs in a background thread.

**⚠️ Resulting bug (ghost recording), also SOLVED (TDD, openrouter/Kimi-k3.1-pro
+ GLM 5.3):** The provisional `_pending_start` mechanism (Bool) only remembered
that "there was a press at some point"; the release during FINALIZING was lost →
after finalization ended, a ghost recording was started for a key that had long
since been released (proven in the live log). **Current status:**
`_key_down` (physical key state) replaces the Bool — release ALWAYS updates
the key state, start after finalization only while the key is held,
check+start atomic under `_lock`. Tests (deterministic, BlockingTranscriber
instead of sleeps): `test_release_during_finalizing_updates_key_state`,
`test_key_held_through_finalize_restarts_recording`,
`test_start_after_finalizing_only_when_key_held`,
`test_release_after_finalized_start_stops_fresh_recording`,
`test_press_during_recording_is_idempotent`. Old sleep test removed.
Known limitation: A short press during FINALIZING is discarded (no audio captured).
Side effect (by design): If the key is **held** after max-duration finalization,
the next recording starts automatically.

### 3.3 Phantom Recordings While Idle ✅ SOLVED (10/27, verified)

**Observation (old, 17:00):** The service started recordings while idle and
sent **14× `backspace`** in 30 s to the terminal; 3× `f24 down` without
a key press.

**Cause 1 (silence hallucination):** In default `whole_buffer` mode, the
**VAD gate was not wired** (only in `windowed` mode). Silence/noise was
transcribed → ASR hallucinated → DeltaTyper typed and undid it via backspace.
**Fix:** `StreamTranscriber` now has an optional `vad` argument,
`app.py` wires `SileroVadGate` for both modes (committed in `2dbc79b`).

**Cause 2 (phantom `f24`):** With `[ids] *`, keyd also grabbed **dotool's
uinput keyboard** (`4711:0815`). The dotool→keyd→virtual keyboard
feedback loop generated ghost events. **Fix:** `-4711:0815` in `/etc/keyd/default.conf`
(user, `sudo keyd reload`). Verified via the keyd journal:
`DEVICE: ignoring 4711:0815 ... (dotool keyboard)`.

**Verification (10/27, after the fix):** Passive watcher (`/tmp/f24_watch.py`,
90 s idle): **0 events**. Positive control (1× Copilot): exactly 1 press+release.
Live operation with the service running: only genuine triggers in the log.

> **Important lesson:** The "open phantom log" was 3.5 h OLDER than the keyd fix.
> Before any further diagnosis, check the age of the measurements — the problem was
> already solved.

---

## 4. CapsLock Inversion ✅ SOLVED (10/27, conclusively verified)

> **CONCLUSION (10/27, 21:03):** After the keyd fix (`-4711:0815`, dotool no
> longer grabbed), the system is **conventional** again: LED off = CapsLock off
> = normal capitalization (confirmed by dictating "Eins, zwei, drei, viereinhalb" + visual LED check).
> Live config set to `capslock_led_inverted = false`, no more warnings. The following documentation of the *temporarily inverted*
> semantics is retained as a case study — precisely these sequences are the
> reason the app must never toggle without asking.


### 4.1 What Has Been Empirically PROVEN

A controlled experiment (three text markers, typed under defined LED states,
user confirmed the on-screen output):

| LED | Typed | Received | Result |
|---|---|---|---|
| **off** | `[[C-marker-drei]]` | `[[c-MARKER-DREI]]` | **inverted** |
| **on** | (marker) | normal | correct |

**CONCLUSION (proven):** On this system,
**LED off = CapsLock active = inverted text**;
**LED on = normal capitalization.**

The LED semantics are therefore **inverted relative to the usual convention**. The user was
right from the beginning; two experts (Kimi K3) were wrong in assuming the opposite.

### 4.2 The Flaw in the Previous "Solution"

Commit `4e6b496` encodes the **incorrect** premise (`LED on = CapsLock on = inverted`)
and removed the correction mechanism. The reviewer (Deepseek V4 Pro)
rejected it with **REQUEST-CHANGES**:

> *P0-1: The user is left alone with inverted text. The change removes the
> only correction mechanism and leaves them with permanently inverted text in
> either conceivable scenario.*

### 4.3 Why No App-Side Fix Solves the Problem

**Architectural fact:** Keycode→character conversion (including case) happens **once,
seat-wide, in mutter's XKB state**. A CapsLock modifier latched there inverts
letters from **all** keyboards and **all** Wayland clients — including the physical
keyboard. The app **cannot reliably read** this state:

- Kernel LED: does **not** reliably show the state (desync proven)
- `xset q` (XWayland): separate state, differs
- There are **two** LEDs (`input3` = physical, `input19` = keyd virtual)
- `caps:none` (gsettings **and** setxkbmap) had **no** effect
- `key capslock` toggles change the LED, but not reliably the behavior

### 4.4 The Actual System Fix (User Task, Requires sudo)

**Highly likely suspicion:** With `[ids] *`, `keyd` grabs **all**
devices — including dotool's uinput keyboard. This routes dictation output through a
remapping daemon that handles lock state differently from mutter.

```bash
# 1) Determine dotool's device ID (while dotool is typing)
sudo keyd monitor

# 2) Add an exception under [ids] in /etc/keyd/default.conf:
#    -<vendor>:<product>        (dotool's device)

# 3) Restart
sudo systemctl restart keyd
```

**Additionally recommended:** Log out and back in once — this is guaranteed to reset mutter's
seat state.

### 4.5 Umlauts: Systemic, Not App-Side

The user currently reports: **ASCII correct, umlauts incorrect.**
Kimi K3 checked the compiled keymap (`xkbcli compile-keymap --layout de
--variant nodeadkeys`):

- `de+nodeadkeys` inherits `de(basic)`, overriding only 9 keys
- Umlauts are separate keysyms: `AD11` Level1=`udiaeresis`, Level2=`Udiaeresis` (analogous for lowercase a and o with diaeresis)
- **Lowercase sharp S (AE11) has type `FOUR_LEVEL_PLUS_LOCK` with 5 symbols**, the last of which is `U1E9E` (capital sharp S)
  → therefore lowercase sharp S becomes capital sharp S when lock is active. **Expected XKB behavior, not a bug.**

**Consequence:** Changing the layout/variant solves nothing. **No
special umlaut logic in the app** — the only required handling (DE layout env for
dotool) is already implemented.

---

## 5. What to Do Next

> **STATUS 10/27, ~21:05:** Priorities 1–3 are **done** (keyd fix applied
> and verified; K1–K8 committed in `2dbc79b`; phantom F24 ruled out via 90-s test +
> live operation). Only §5.4 cleanup remains.

### 5.1 Priority 1 — Restore System State ✅ DONE

- keyd exclusion `-4711:0815` set, `keyd check` passes without errors,
  journal shows `ignoring 4711:0815 (dotool keyboard)`.
- No relogin was needed — after the fix, capitalization was immediately correct
  and LED semantics were conventional again (`capslock_led_inverted = false`).

### 5.2 Priority 2 — Implement Corrections K1–K8 (Code) ✅ DONE (`2dbc79b`)

The implementation round (GLM 5.3) was **stopped in the middle of its work**. The plan
is fully available and ready to implement:

| ID | Task |
|---|---|
| **K1** | `_capslock_leds() -> dict[str,str]` per device; `_capslock_state()` builds on it (OR remains). Diagnostic logs output **per device**. |
| **K2** | **Reintroduce** `capslock_led_inverted: bool = True` (proven semantics in docstring). `_ensure_capslock_normal()`: target = `"an"` if inverted, otherwise `"aus"`; toggle only if `capslock_autocorrect=True`; max 2 attempts, verified. |
| **K3** | Reverse `test_capslock_led_inverted_key_rejected` → `..._accepted` (key is valid again) |
| **K4** | New subcommand `stt-button caps-diagnose`: LED per device, `xset` view, valid target state, test typing marker `[[diagnose-OK]]` |
| **K5** | `docs/KONZEPT.md`: Rewrite CapsLock section to reflect **proven** semantics |
| **K6** | Characterization test for dotool env (`DOTOOL_XKB_LAYOUT=de`, `..._VARIANT=nodeadkeys`) |
| **K7** | `tests/test_keymap_reference.py` with `xkbcli` tests (skipif missing): umlaut levels, ẞ-FOUR_LEVEL_PLUS_LOCK, variant comparison |
| **K8** | Name `_capslock_leds()` accurately; log note "LED is not a reliable state" |

**Core rule for all changes:** The app must **never** toggle CapsLock without asking.
Default `capslock_autocorrect=False` → warn only.

### 5.3 Priority 3 — Resolve Phantom F24 ✅ DONE

The source was the dotool→keyd feedback loop (§3.3). Verification: 90-s idle
with 0 events, positive control exactly 1 press+release, clean live dictation.
Re-test if needed:
```bash
/tmp/f24_watch.py 90   # passive watcher (service must be stopped)
journalctl --user -u stt-button -f | grep -i "Trigger"
```
If `Trigger press` appears without a key press → keyd combo leakage (system config).

### 5.4 Priority 4 — Cleanup

- Review uncommitted changes, run tests, **commit**
- Add `capslock_*` options to `README.md` (the config is not documented there)
- `docs/KONZEPT.md` is the handoff file for an Android project (see §6)

---

## 6. Important Project Artifacts

| File | Purpose |
|---|---|
| `docs/KONZEPT.md` | **Handoff document for reimplementation** (including Android): 7 core mechanisms, 6 pitfalls, what did NOT work, test strategy, checklist |
| `config.example.toml` | Template with all options and comments |
| `src/stt_button/streaming_types.py` | `Token`, `Transcription`, `StreamWindowPolicy` |
| `src/stt_button/committer.py` | `TimestampCommitter` (LocalAgreement + word boundaries) |
| `src/stt_button/asr.py` | `ParakeetEngine`, `QwenEngine`, `WhisperEngine`, `StreamTranscriber`, `_token_budget` |
| `src/stt_button/typing.py` | `DeltaTyper` (core output logic), `DotoolTyper`, `WtypeTyper`, `ClipboardTyper` |
| `src/stt_button/controller.py` | State machine, 3-thread architecture, shutdown sequence |
| `src/stt_button/keys.py` | evdev reader for the trigger key |
| `src/stt_button/vad.py` | `SileroVadGate` |

### Test Helpers (Reusable for New Tests)

- `tests/test_controller.py`: `FakeAudioSource`, `FakeEngine`, `FakeTyper`, `make_controller()`
- `tests/test_asr.py`: `FakeEngine`, `audio(seconds)`
- `tests/test_typing.py`: `FakeTyper`, `_capture()`, `_capture_stdin()`, `subprocess_result()`, `AlwaysSpeech`/`SilenceVad`/`NeverSpeech`
- `tests/test_keys.py`: `FakeDevice` (`push(type, code, value)`, `read()`)

---

## 7. Working Approach in This Session (Proven)

**Established division of roles:**

1. **Kimi K3** — Debugging/analysis/research (Oracle template)
2. **GLM 5.3** — TDD implementation (implement/delegate template)
3. **Deepseek V4 Pro 0813** — Review (review template)

**Most important lesson:** **Verify expert plans; do not trust them blindly.** Two
Kimi analyses of CapsLock semantics were wrong; only an empirical
ground-truth experiment (typing markers under defined states) clarified it.

**Additional lessons:**
- ASR fixes were often correct — but several problems lay **outside** the app
  (keyd, mutter, CapsLock state)
- A fix can **cause** the problem (the CapsLock toggle created the inversion
  and kept it in place)
- Always send a **saved test audio** through the pipeline and compare it with
  single transcription — reliably exposes errors

---

## 8. Open Questions / Risks

1. ~~**keyd-`[ids] *`**~~ — **done:** Device exclusion `-4711:0815` is
   set and verified via the keyd journal (`ignoring 4711:0815`).
2. **Phantom F24** — **done:** The source was the dotool→keyd feedback loop.
   90-s idle test after the fix: 0 events; clean live operation (see §3.3).
3. **Uncommitted changes** — Documentation updates (KONZEPT/HANDOFF) and the
   live config (`capslock_led_inverted = false`) are the last remaining items.
4. **Service autostart** — currently started manually (`start`, but `disabled`).
   Permanent: `systemctl --user enable stt-button` (intentionally not set yet,
   so that a restart does not do anything unexpected at first).
5. **`windowed` mode is unfinished** — it has the documented deadlocks.
   `whole_buffer` is the default and works; anyone who wants word-by-word live output
   must finish `windowed` with the fixes from §3.1.
