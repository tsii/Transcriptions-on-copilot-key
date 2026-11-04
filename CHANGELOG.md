# Changelog

All notable changes to **STT-Button** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

STT-Button is local push-to-talk dictation: hold the ThinkPad **Copilot key**,
speak, and the transcript is typed word-by-word at the cursor. Transcription runs
locally (NVIDIA Parakeet TDT 0.6B, CPU-only, automatic DE+EN).

---

## [Unreleased]

### Added
- **Dictation queue** — press during finalization starts the next recording
  immediately; jobs finalize FIFO (`max_queue_depth`, default `4`). Implemented
  as a `FinalizeJob` queue + background dispatcher in the controller. A job whose
  final transcription fails is dropped (logged, no done-sound) and never blocks
  the next job.

### Changed
- **Focus-gated final output** — the final dictation delta no longer always
  uses the clipboard. With `final_paste` enabled the focus is checked (GNOME
  Shell extension): **terminal** (or unknown) focus copies the delta to the
  clipboard *without* the paste chord (operator pastes manually, `ctrl+v` is
  inert/harmful in terminals); **browser/UI** focus types the delta directly
  and leaves the clipboard completely untouched. With no focus probe injected,
  behaviour is exactly the previous legacy clipboard+chord path.
- **Chime default** — `done_sound_mode` now defaults to `"always"` (previously
  `"terminal"`). The done-sound now plays after every successful dictation, not
  only when a terminal is focused. `"terminal"` remains a valid option. This
  changes only the default: existing configs with an explicit `done_sound_mode`
  are unaffected.

### Fixed
- **Focus chain now actually works** (live-verified 04.11.): the GNOME Shell
  extension was silently broken twice — (a) it used the legacy extension
  format and per-field `DBusMethodInfo` construction, both removed in
  GNOME 45+/GJS 1.7x (extension rewritten as an ESM module with the
  closure-based `register_object` API); (b) `focus.py` parsed a
  `{"result": ...}` shape that `busctl --json` never emits (now parses the
  real `{"type":"ss","data":[app,title]}`; 3 new parser tests using the
  real busctl output). Before this fix `terminal_focused()` always returned
  `None`, so the focus-gated behaviour silently fell back everywhere.

---

## [0.1.0] - 2026-11-01

### Added
- Push-to-talk dictation on the ThinkPad Copilot key (keyd → `F24`), with
  windowed streaming transcription (Parakeet TDT 0.6B) and per-word typing.
- **Done-sound feedback** — a soft tone after each successful finalization
  (`done_sound`, `done_sound_volume` config; fire-and-forget `Popen`).
- **Terminal-only done-sound** (`done_sound_mode = "terminal"`) — the tone plays
  only when a terminal is focused, detected via the GNOME Shell extension
  `stt-button-focus@salmon.io` (DBus `GetFocus()`). `None`/unknown still plays
  the tone (feedback over silence).
- **Pause indicator** while the live-ASR limit is reached (ASCII `...`, with a
  trailing space on paste to keep the cursor clean).
- **`cached_mid_tail`** (opt-in) — word-anchor stitching + background
  checkpoints, with `finalize_mode` (`full` default).
- **Event-driven controller ticks** (Feature 4) — a condition variable replaces
  fixed-interval polling; lower CPU, lower latency.
- **`live_asr_until_s`** — bound the live-ASR budget (default raised to `15.0`
  by OPT3).
- **Debian/Ubuntu `.deb`** — slim ~84 KB package with a self-healing bootstrap
  (per-user venv, CPU-only torch, first-run install).
- **Corpus-gate tooling** (`tools/corpus_watch.py`, `tools/gate_eval.py`) for
  A/B evaluating streaming modes.

### Changed
- **FP32 is the production standard** (operator decision, 29.10.) —
  `compute_type = "float32"` in the live config for quality; INT8 remains
  opt-in. INT8's main path is `onnx_asr`/ORT-CPU (the proven path).
- **`live_typing_mode`** default changed from `"full"` to `"committed"`.
- **Release decoupled from live-ASR** — the finalization cache lets the output
  be finalized and typed without blocking the next recording.
- Final dictation delta is pasted via the **clipboard** (final-delta-paste)
  rather than typed character-by-character, for faster, more robust output.
- **All documentation and code comments** translated from German to English
  (1.11.); functional German data (dead-key umlauts, `xkb_layout="de"`, German
  dictation fixtures) intentionally unchanged.

### Fixed
- **Bracketed-paste filter** — strips `ESC[200~`…`ESC[201~` markers (and the
  hex-text form) from the final text so terminals don't inject paste guards;
  a trailing `~` is removed only when the start marker was confirmed, so
  legitimate `~` (e.g. `~/Documents`) is preserved.
- **Subprocess hardening** — `wl-copy`/typer subprocesses get a timeout and
  `DEVNULL` to avoid hangs on a wedged clipboard daemon.
- **Indicator fixes** — ASCII `...` instead of `…` (dotool layout gap); limit
  self-detection, correct finalizer pickup, and a space in the pasted tail.

### Performance
- **OPT1** — finalization backspace + paste chord issued in a single dotool
  process (~0.7 s saved).
- **OPT3** — `live_asr_until_s` default `0.0 → 15.0` (INT8 tick budget).
- **INT8** path benchmarked; INT8-A/B measurement log formalized.
- Measured end-to-end release→text latency reduced from 5–7 s to ~1.5–2 s for
  20–30 s dictations (see `docs/ABSCHLUSSBERICHT.md`).

### Removed / Rejected (documented decisions)
- **NPU (OpenVINO) acceleration rejected** — 5 ground-truth points
  (no available devices, static-bucket incompatibility, decoder compile
  failure, result never consumed). CPU INT8/ORT is the fastest stable path.
- **ONNX FP32** — 1.7× slower than HuggingFace on this CPU; gate not passed.
- **OPT2** (mid-tail punctuation-drift tolerance) — gate failed; config rolled
  back to `finalize_mode="full"`.

### Packaging
- `packaging/runtime-requirements.txt` (pinned), `packaging/debian/control`,
  `tools/build-deb.sh` → `dist/stt-button_0.1.0_amd64.deb`.

---

## [0.0.0] - 2026-10-26

### Added
- Initial commit: push-to-talk dictation (Parakeet TDT, windowed streaming).

[Unreleased]: https://github.com/tsii/Transcriptions-on-copilot-key/compare/0.1.0...master
[0.1.0]: https://github.com/tsii/Transcriptions-on-copilot-key/compare/0.0.0...0.1.0
[0.0.0]: https://github.com/tsii/Transcriptions-on-copilot-key/releases/tag/0.0.0
