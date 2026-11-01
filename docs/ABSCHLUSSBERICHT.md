# Final Report — Latency & INT8/NPU (Status 10/29, Commit a669f23)

## Final Result (Live Proof from Real Dictation, 07:08)

| Metric | Old (FP32, Baseline Session Week) | New (int8 ONNX, Live 07:08) |
|---|---|---|
| Final ASR, 31.1 s dictation | 5–7 s total release→text | **1.54 s** finalize (RTF **0.050**) |
| Release→paste complete | 5–7 s | **3.10 s** (incl. 0.73 s backspaces + 0.80 s paste) |
| Model cold start (boot) | ~7 s (HF-FP32) | **3.2 s** (int8 ONNX) |
| Single encoder, 30-s bucket (CPU) | 0.94 s (FP32 A/B log) | ~0.57 s (int8) |

RTF 0.050 with a 16.4-s reference clip (warm, measured on your exact
07:08 dictation audio /tmp/stt_last.wav) — matches the
A/B log finding of 0.043–0.060 and the live finalize exactly.

## Gate Assessment

| Gate | Target | Result |
|---|---|---|
| WER int8 vs. fp32 (5 corpus clips) | ≤ +0.5 pp | **passed** (better: int8 reads sentence endings completely) |
| Corpus gate cached_mid_tail (31 clips) | 0 dup, 0 frag | **passed** (WER 0.00 pp difference) |
| Suite | 100% green | **420 passed** |
| Live latency target, finalize | < 2 s | **1.54 s ✔** |

## NPU: Tested and Rejected (Ground Truth, Not Opinion)

1. Encoder fp32 on NPU: 0.129 s (30-s bucket, exclusive, warm) — works.
2. Encoder int8 on NPU: **0.569 s median** (9 warm runs) — no gain over CPU int8 (~0.45 s).
3. NPU **under real dictation load**: 55× `No available devices` in the journal —
   the device is exclusive; capture + benchmark + service compete for it.
4. Decoder/joint NPU compile: fails (`Transpose_192/48372 cannot
   compute output bounds dimension`) — AR loop with dynamic dims.
5. OpenVINO encoder in the production branch: static bucket incompatible with
   real feature lengths; result was never consumed → removed.

**Conclusion:** On Lunar Lake under this workload mix (dictation + capture +
AI load in parallel), CPU int8 (ORT) is the fastest stable path. The
NPU infrastructure (Level Zero, firmware, render group) remains installed
and is immediately usable for suitable static workloads (e.g., long-running batch jobs).

## Keep-Warm Question (Operator 06:4x)

The blob cache already serves this purpose: NPU reload 53 s → 2.4 s.
A permanent keepalive process costs 1.3 GB RAM, blocks the
exclusive NPU device for the production service (exactly the 55× error),
and provides no benefit over CPU int8 — **intentionally not built**.

## Production Path Now (config: compute_type="int8")

`ParakeetEngine.load()` → `onnx_asr.load_model('nemo-conformer-tdt',
int8, ORT-CPU, threads from config)`. If the files are missing or loading
fails: HF-FP32 fallback, never a crash. `transcribe()` and
`transcribe_with_timestamps()` detect the int8 branch before any
HF usage.

## OPT Follow-Up (10/29, After Final Report)

| Option | Result | Evidence |
|---|---|---|
| OPT1 Combined Backspace+Paste (`48e7610`) | live after restart | 7 tests, suite 435 |
| OPT2 Mid-Tail Punctuation Drift (`3483d64`) | **rejected** (config `full`) | Gate 243 clips: 2 stitch drifts (16.2 s/17.1 s, WER 0.13/0.18 through end of sentence), no latency gain (2.44->2.59 s median); code nevertheless remains in repo |
| OPT3 live_asr_until_s 15 s (`fd50acb`) | live, validation ongoing | p50/p95 against 8-s reference; rollback = 1 config line |

Production configuration therefore: `compute_type=int8`, `finalize_mode=full`,
`live_asr_until_s=15.0`, `live_typing_mode=committed`. Mid-tail is only worthwhile
for very long dictations (>60 s), not in the everyday mix — gate_report
/tmp/gate_report_opt2_prod.json (241 clips, WER median 0.0000, 0 dup).

## Operator Decision 10/29 (Final, Documented in Live Config)

`compute_type = \"float32\"` — FP32 HF path as the production standard. INT8 was
measurably faster (RTF 0.05, p50 1.2 s across 4 dictations), but the operator
prefers FP32 recognition quality; the int8 ONNX files remain
in place, and opt-in is one config line. OPT1 (combined backspace+paste)
and OPT3 (live_asr_until_s 15.0) remain active, finalize_mode full.
The rollback switch (live_asr_until_s 8.0) is NOT triggered
automatically — only at the operator's explicit request.

## Known Open Items (Not Blockers)

- `live_asr_until_s=8`: Live ticks run until 8 s, then pause indicator;
  mid-tail checkpoints did not engage in the 07:08 dictation (no `mid_tail:` log) —
  the finalizer fell back to the full buffer, which still took only 1.54 s
  thanks to int8. Possible refinement only if there is measurable benefit.
- Backspace cost (0.73 s for 3 characters): `max_revision_chars`/committed
  cadence are the adjustment knobs if it becomes subjectively disruptive.
