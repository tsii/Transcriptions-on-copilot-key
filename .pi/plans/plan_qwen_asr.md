# EXECUTION PLAN — Qwen3-ASR 1.7B as switchable backend (Opus 5.5, 2026-11-04)

Base: HEAD 5288840 (master). Implementation chain follows this file blindly.
Operator away → all decisions taken here, each with rationale (decide-conservatively:
**production default stays `backend="parakeet"`, `compute_type="float32"`, `finalize_mode="full"`**).

## VERDICT: **GO (conditional) — merged, opt-in, FINAL-ONLY mode (no live preview)**

---

## 1. MODEL GROUND TRUTH

| Item | Finding (evidence) |
|---|---|
| What operator means by "Qwen ASR 1.6b" | **`Qwen/Qwen3-ASR-1.7B-hf`** — there is no 1.6B release; the Qwen org has exactly `Qwen3-ASR-1.7B`, `-1.7B-hf`, `-0.6B`, `-0.6B-hf` (HfApi.list_models, 2026-11-04). 1.7B is the only one near 1.6. The `-hf` variant is the native-transformers port; the non-hf repo needs the `qwen-asr` package (extra dep, rejected). |
| Pinned revision | `bcd2b5b7f32b480ab5790554cfa8347f246a14f3` (snapshot already cached in `~/.cache/huggingface/hub`) |
| License | apache-2.0 (HF card_data) |
| Files | single `model.safetensors` 4.08 GB (BF16) + processor/tokenizer |
| Runtime | `transformers` 5.20.2 (already in .venv) — `AutoModelForMultimodalLM` + `AutoProcessor.apply_transcription_request(audio=...)`, `decode(..., return_format="transcription_only")`. torch 2.14.1+cpu. **No new dependency.** |
| GPU needed? | No. Runs on CPU (measured below). Audio encoder + autoregressive Qwen3 LLM decode. |
| RAM | 30 GB total, ~13 GB available while service runs. |

**Existing code (important!):** `QwenEngine` already exists in `src/stt_button/asr.py:421`,
`"qwen"` is in `config.VALID_BACKENDS`, `app._build_engine` and `__main__` (compare cmd) branch on it.
It is **untested** (no `qwen` in tests/) and has defects this plan fixes:
1. loads `dtype=torch.float32` → upcasts BF16 weights → **11.7 GB RSS** (fails gate);
2. no `transcribe_with_timestamps` → windowed/buffered modes would crash;
3. no cooperative cancel; 4. temp-WAV roundtrip (processor accepts arrays? — verify in B2, keep tempfile if not);
5. config allows `backend="qwen"` with Parakeet model id / incompatible streaming modes silently.

## 2. SPIKE — ALREADY RUN (by planner, nice -n 19, 8 threads, HF_HUB_OFFLINE=1)

Clips (fixed, reuse in any re-run): `~/stt-korpus/20261030-100510_320156.wav` (10.0 s),
`20261102-090453_640328.wav` (20.0 s), `20261029-233118_1284082.wav` (40.1 s). Script: `/tmp/qspike.py` (to be recreated as `tools/model_switch_check.py`, B6).

| dtype | clip | elapsed | RTF | max RSS |
|---|---|---|---|---|
| FP32 (current code) | 10 s | 8.9 s | 0.89 | **11.68 GB ❌** |
| FP32 | 20 s | 18.0 s | 0.90 | 11.68 GB |
| FP32 | 40 s | 27.0 s | 0.67 | 11.68 GB |
| **BF16** | 20 s | 15.4 s | **0.77 ✅** | **4.65 GB ✅** |
| **BF16** | 40 s | 20.0 s | 0.50 | **5.01 GB ✅** |

Load: 5.8 s (FP32), 0.7 s (BF16, warm page cache). Transcripts look correct German with punctuation; FP32 and BF16 first 150 chars identical.

**Gate result:** BF16 passes RSS<6 GB and RTF<1.5. **But** live use is still infeasible: whole-buffer
re-transcription per 1 s tick with a 15 s buffer costs ~12 s per tick (Parakeet: ~1-2 s).
**Decision D1:** Qwen is restricted to **final-only dictation** (no live ticks; text is typed once on
release; latency ≈ 0.5–0.9 × utterance length) plus offline compare. Rationale: honest about cost,
zero risk to live path, still a usable "max-quality" mode the operator can switch to.

## 3. BACKEND ABSTRACTION DESIGN

Config (`[asr]` keys stay flat as today — no new section, avoid migration risk):
```toml
backend = "parakeet"          # "parakeet" | "whisper" | "qwen"
model   = "nvidia/parakeet-tdt-0.6b-v3"
compute_type = "float32"      # parakeet: float32|int8 ; qwen: bfloat16|float32 ; whisper: ctranslate2 types
```
- **D2: no new `parakeet_onnx` backend name.** ONNX stays `backend="parakeet", compute_type="int8"` (existing, proven). Adding an alias = churn without value.
- **D3: model default per backend.** If `backend="qwen"` and `model` still equals the Parakeet default (or is empty) → resolve to `Qwen/Qwen3-ASR-1.7B-hf` in `Config.__post_init__`/validate (logged). Any other explicit model id is respected.
- **D4: compute_type for qwen** ∈ {`bfloat16`, `float32`}; any other value (e.g. the dataclass default `int8`) → **ConfigError** with hint (no silent remap). Engine default `bfloat16`. Operator switch line in README sets `compute_type = "bfloat16"` explicitly.
- **D5: streaming compatibility.** backend=qwen requires `streaming_mode="whole_buffer"` and `finalize_mode="full"`; others → ConfigError (no timestamps → windowed/buffered/cached_mid_tail impossible). `segment_max_s>0` with qwen → ConfigError.
- **D6: final-only.** New `Config.live_preview: bool | None = None` is NOT added; instead the engine advertises `supports_live = False` (class attribute; Parakeet/Whisper `True`). app wiring: if `not engine.supports_live` → StreamTranscriber built with `live_asr_until_s` tiny-equivalent is hacky; cleaner: `StreamTranscriber(live_enabled=False)` → `maybe_transcribe` returns None without engine call; `finalize()` runs full-buffer transcribe as today. Pause indicator: not shown (live limit concept n/a); controller unchanged (it already handles a None tick).
- Protocol: keep existing `TranscriberEngine` (`transcribe(data, initial_prompt) -> TranscriptionResult`). Add optional capabilities by attribute, not new protocol: `supports_live: bool`, `supports_timestamps: bool`, `cancel()` optional (controller/StreamTranscriber already use `getattr`-style? — B4 verifies; if not, guard with `hasattr`).
- **Degradation, stated honestly:** no TDT alignments → no committed mode (`committed_len=0` always), no windowed/buffered, no vocabulary/initial_prompt (ignored, logged once at DEBUG), no live typing, no cancel (a release during an in-flight final is not cancelled — finalize is never cancelled anyway). DeltaTyper gets one full text → types it (text-only path already exists).
- **Lazy import:** `torch`/`transformers` imported only inside `QwenEngine.load()` (already so); `app.py` top-level import of the class is fine because the module itself imports nothing heavy. Test asserts importing `stt_button.app` with `transformers` blocked works for parakeet.

## 4. TDD BLOCKS (RED documented before GREEN; test cmd:
`nice -n 19 .venv/bin/python -m pytest -q -p no:randomly -o addopts=""`; all tests use fakes — no real model in unit tests)

**B1 — config validation** · files: `src/stt_button/config.py`, `tests/test_config.py`
RED: `test_qwen_backend_defaults_model_to_qwen`, `test_qwen_explicit_model_kept`, `test_qwen_rejects_int8_compute_type`, `test_qwen_accepts_bfloat16_and_float32`, `test_qwen_rejects_windowed_and_buffered`, `test_qwen_rejects_cached_mid_tail`, `test_qwen_rejects_segmenting`, `test_unknown_backend_raises_config_error` (exists? keep/extend), `test_default_backend_is_parakeet_float32_unchanged` (reads defaults + repo default config).
Commit: `config: validate qwen backend (model default, dtype, streaming compat)`

**B2 — QwenEngine BF16 + capabilities** · files: `asr.py`, new `tests/test_qwen_engine.py`
RED: `test_qwen_loads_bfloat16_by_default` (monkeypatched fake transformers module records dtype), `test_qwen_float32_opt_in`, `test_qwen_supports_live_false_and_no_timestamps`, `test_parakeet_whisper_supports_live_true`, `test_qwen_transcribe_returns_stripped_text_committed_zero`, `test_qwen_ignores_initial_prompt`, `test_qwen_load_idempotent`, `test_qwen_token_budget_passed`.
Note: compute_type→torch dtype map `{"bfloat16": torch.bfloat16, "float32": torch.float32}`; pass `revision` pin? → **D7: yes**, constant `QWEN_DEFAULT_REVISION` used only when model == default id.
Commit: `asr: QwenEngine loads BF16 by default, advertises capabilities`

**B3 — StreamTranscriber live_enabled** · files: `asr.py`, `tests/test_asr.py`
RED: `test_live_disabled_maybe_transcribe_never_calls_engine`, `test_live_disabled_finalize_transcribes_full_buffer`, `test_live_disabled_no_pause_indicator`, `test_live_enabled_default_true_behavior_unchanged`.
Commit: `stream: live_enabled flag for final-only engines`

**B4 — app/__main__ wiring** · files: `app.py`, `__main__.py`, `tests/test_app_wiring.py`, `tests/test_cli.py`
RED: `test_qwen_backend_builds_qwen_engine_with_compute_type`, `test_non_live_engine_wires_live_disabled`, `test_parakeet_default_wiring_unchanged`, `test_app_import_without_transformers_parakeet_ok` (sys.modules block), `test_compare_cli_qwen_passes_compute_type`. Verify controller tolerates engines without `cancel` (add `hasattr` guard + test `test_controller_release_with_engine_without_cancel` if needed).
Commit: `app: wire qwen final-only backend`

**B5 — docs** · files: `docs/KONZEPT.md` (§2.5 table + §9 add BF16 row with spike numbers, note final-only), `README.md` new section **"Modelle umschalten"** (config snippet for each backend, restart note, RAM/latency table, degradations list), `tests/test_docs.py`
RED: `test_readme_has_modelle_umschalten_section`, `test_readme_documents_qwen_bfloat16_switch`.
Commit: `docs: Modelle umschalten (parakeet/whisper/qwen)`

**B6 — `tools/model_switch_check.py`** (useful: lets operator verify a switch without restarting the service) · files: tool + `tests/test_model_switch_check.py` (argument parsing + report formatting with fake engine)
CLI: `--config PATH` (build engine exactly like app), `--clips` (default the 3 spike clips), prints load s, per clip elapsed/RTF/max-RSS/text, exits non-zero if RSS>6 GB or RTF(20 s)>1.5.
RED: `test_check_builds_engine_from_config`, `test_check_fails_on_rss_gate`, `test_check_report_lines`.
Commit: `tools: model_switch_check one-shot sanity CLI`

**B7 — quality gate run** (tool `tools/pairwise_wer.py` + report, no production code) — see §5.
Commit: `tools: pairwise WER qwen vs parakeet + report`

Final: full test suite green; push `origin master` only (P7). **No service restart (P6).** Operator's `~/.config/stt-button/config.toml` NOT touched.

## 5. QUALITY GATE (comparative, not ground truth)

- Subset: first 40 files (sorted by name) of `~/stt-korpus/*.wav` with duration 10–60 s (deterministic).
- Reference: `ParakeetEngine(compute_type="float32")` whole-buffer transcribe. Candidate: `QwenEngine(compute_type="bfloat16")` whole-buffer.
- Normalize (lowercase, strip punctuation, collapse whitespace); report WER(qwen | ref=parakeet) and WER(parakeet | ref=qwen), mean/median/max, per-clip JSON → `docs/reports/qwen_pairwise_wer_<date>.json` + md summary; median RTF/RSS for both.
- Manual list: the 5 clips with highest pairwise WER, side by side texts, in the md report (for operator review; implementer just lists, doesn't judge).
- Run with `nice -n 19 OMP_NUM_THREADS=4`; time cap 60 min (est. 40 clips × ~20 s ≈ 15 min Qwen).
- **Acceptance:** merge as opt-in if code blocks B1–B6 green (gate does not block merging opt-in code; default unchanged). Report states: if mean pairwise WER ≤ 15 % and no clip > 50 % (i.e. no hallucination/language switch) → "Qwen is a viable opt-in"; otherwise README marks Qwen "experimental" and report lists failures. **Default never changes in this chain** regardless of results.

## 6. ROLLBACK MAP
| Block | Revert | Effect |
|---|---|---|
| B1 | `git revert <B1>` | qwen accepted unvalidated (pre-plan state) |
| B2 | revert | QwenEngine FP32 again (11.7 GB) — also revert B4 |
| B3 | revert (needs B4 reverted first) | no final-only mode |
| B4 | revert | qwen built but live ticks run (slow) |
| B5/B6/B7 | revert independently | docs/tools only |
Operator rollback at runtime: set `backend = "parakeet"` (default) — no code change.

## TOP-3 RISKS (orchestrator)
1. **RAM**: BF16 5 GB + Parakeet not co-loaded (only one engine per app) — fine; but compare/gate tools loading BOTH ≈ 7–8 GB next to the running service (~13 GB free) — run gate sequentially, free engine between phases.
2. **transformers API drift**: `AutoModelForMultimodalLM` / `apply_transcription_request` / `return_format` are new in 5.x; pin revision (D7) and keep fakes matching the real call signature; B6 tool is the real-model smoke test.
3. **UX expectation**: operator asked "switchable"; final-only mode means no live text with Qwen and release latency up to ~0.9× utterance length. Clearly documented in README; must not be misread as a bug.
