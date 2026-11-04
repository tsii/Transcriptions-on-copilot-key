# Phase 2 – Overview: Ideas A, B, C from `.pi/reports/rtf-latency-analysis.md` §6

Baseline: HEAD `52d3eac` (Queue+Clipboard, FP32/HF, `live_asr_until_s=15`, `live_typing_mode="committed"`, `commit_agreements=2`).
- Suite is green. `test_engine_error_aborts` is flaky under parallel load and passes alone. Watch it, but it is not a blocker.
- Line references were checked against the working tree on 2026-11-04.
- Coder: my/claude-opus-5-5. Reviewer: cortecs/deepseek-v4-pro-0813. Tests: `nice -n 19 .venv/bin/python -m pytest`.

> Note: the session runs in `workspace-write-never` mode, so `/tmp` could not be written. The plans are in `.pi/plans/plan_phase2_*.md`. Copy with `cp .pi/plans/plan_phase2_*.md /tmp/` if needed.

## Block overview
| # | File | Idea | Content | Risk |
|---|---|---|---|---|
| 1 | block1 | A1 | `InferenceCancelled`, `request_cancel/clear_cancel`, pre-hooks on 24 encoder layers | low: isolated, engine only |
| 2 | block2 | A2 | Controller: `_live_tick_active`; release cancels only a running live tick; finalizer clears under `_exec_lock` | **medium**: threading. Mitigated by the race analysis in the block and existing serialization tests |
| 3 | block3 | A3 | Slow test with the real model: cancel < 0.3 s, identical text afterwards | low: verification only |
| 4 | block4 | B | `asr_interval_adaptive_k` (default 0 = off), `asr_interval_max_s=6`; clamp(k·tick, interval, max) | low: opt-in |
| 5 | block5 | C1 | Pause cut (VAD + .?! + min 3 s) → mini-final, hard cut, `_closed_text` composition | **high**: changes text paths. Opt-in, default off |
| 6 | block6 | C2 | Hard cap `segment_max_s` at the quietest point; live limit counted per segment | medium |
| 7 | block7 | C3 | Config/validation/wiring, dump of all segments, example TOML, KONZEPT | low |
| 8 | block8 | C4 | `gate_eval.py --mode segment`, seam analysis, gate G1–G6 | decides on C |

## Order and dependencies
`1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → (live test)`
- **A before C (hard dependency):** a segment cut is a live engine call of up to ~3 s (15 s buffer × RTF 0.2) under `_exec_lock`. Without A, a release during a mini-final waits for the full mini-final, so C would make the p95 worse, not better.
  - A also guarantees that a cancelled mini-final leaves no half state behind. Block 5 changes state only after a successful engine call, so the cancel test in block 5 covers exactly that.
- **B before C (soft dependency):** B is independent but touches the same `maybe_transcribe` lines (asr.py:666/890). Doing B first gives a clean diff base for C, and B is useful even if C gets dropped.
- B could be pulled earlier, before A, without risk. A stays first because it delivers the biggest p95 gain with no config change.
- Commit after each green block (TDD: test + code in one commit, message `Phase2 <block>: ...`). Only push to `origin master`.

## Risk per block (details in the block files)
- **A2:** a race between flag/cancel/clear.
  - Invariant: `cancel` only when `_live_tick_active` under `_lock`; `clear` only under `_exec_lock` (no live tick running).
  - Worst case is a stray cancel → the finalizer clears it beforehand → harmless.
  - Detection: the journal grep `Final transcription failed: live inference cancelled` must stay at 0.
- **B:** a choppy live preview at long buffers (6 s interval). Opt-in only.
- **C1/C2:** lost or doubled words at seams; a mini-final changes text the user has already seen.
  - Committed mode only types committed text, so a mini-final can at most replace the uncommitted tail. That replacement happens anyway at finalize.
- **C3:** an invalid config combination. Hard validation prevents it.

## Gate protocol for C (block 8)
1. Unit tests for the seam analysis green.
2. Smoke run: `--mode segment --min-seconds 10 --limit 20` → plausibility (seams > 0, no crash).
3. Full run: 391 clips ≥ 10 s, `nice -n 19 OMP_NUM_THREADS=4`, with operator approval. Report: `.pi/reports/gate_segment.json`.
4. Evaluation of G1–G6 (block 8 table). **G1 lost = 0 and G2 doubled = 0 are hard.**
5. Review by deepseek-v4-pro on the JSON: spot-check 10 seams by listening/reading (clip + seam index).
6. Decision:
   - PASS → live test with segmentation as opt-in, recommendation 1.2/3.0/15.0.
   - FAIL/INCONCLUSIVE → OPT2 documentation (CHANGELOG + ABSCHLUSSBERICHT), config stays at 0.

## Rollback strategy
- Every block is a separate commit. Revert in reverse order with `git revert <sha>`.
- **Config-only rollback (no code revert needed):**
  - A: no flag. If A misbehaves in the live test, revert block 2. Block 1 can stay as harmless dead code, since hooks without `request_cancel` cost ~24 `Event.is_set()` calls per inference.
  - B: `asr_interval_adaptive_k = 0.0`.
  - C: `segment_pause_s = 0.0` / `segment_max_s = 0.0` (default).
- **C fails, keep A+B:**
  - Blocks 5–8 stay in code because they are opt-in and off by default.
  - Documentation like OPT2. Alternatively, run `git revert` on 5–8 if the reviewer judges the dead code to be too much (~150 LOC in asr.py).
- Any regression in a non-opt-in default path (A) → immediately revert block 2 and post a CHANGELOG note.

## Live test plan (after block 3, after block 4, and after block 8 PASS)
Setup: service started manually (P6), `latency_trace = true`, journal via `journalctl --user -u stt-button -f`.

| Phase | Config | Dictations | Measurement |
|---|---|---|---|
| L0 baseline | HEAD before Phase 2 | 20× 5–15 s, 10× 30–60 s | p50/p95 release→text (trace "release" → "paste") |
| L1 A | blocks 1–3 | same mix | p50/p95; count of `Live tick cancelled`; 0× `Final transcription failed` |
| L2 A+B | `asr_interval_adaptive_k = 2.0` | same mix | p50/p95, subjective live fluency (1–5) |
| L3 A+B+C | `segment_pause_s=1.2, segment_min_s=3.0, segment_max_s=15.0` (gate PASS only) | 10× 30–90 s with deliberate sentence pauses, 5× without pauses (cap path) | p50/p95; **seam quality**: count lost/doubled/garbled words at seams in the target editor; capitalization after the seam; no stray pause indicator |

Targets:
- L1: p95 release→text improves for long dictations (> 15 s) by the share of the aborted live tick (expected 0.5–3 s).
- L2: p95 ≥ 10 % better than L1, otherwise default k stays 0.
- L3: release→text for > 30 s ≤ 50 % of L2, and 0 seam errors in 15 dictations.

Evaluation script: grep the trace lines from the journal, compute p50/p95 with `python -c "import statistics..."`. Store the results in `.pi/reports/phase2_live.md`.

## Invariants (checked in every block)
- English in code and comments.
- `_exec_lock → _lock` unchanged. `request_cancel` only sets an Event.
- Queue stays FIFO.
- `source.stop()` before the finalizer (test `test_source_stopped_before_finalizer_starts`).
- `abort()` unchanged and isolated.
- All new config defaults keep current behavior.
