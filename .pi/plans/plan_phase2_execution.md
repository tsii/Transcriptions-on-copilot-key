# Phase 2 – Execution Guide (verified against HEAD `0177ab3`)

Author: the plan author. Verified 2026-11-04, read-only, against `0177ab3` with the full suite green (`nice -n 19 .venv/bin/python -m pytest -q`, 100 %).
Audience: coder (my/claude-opus-5-5) and reviewer (cortecs/deepseek-v4-pro-0813). Work autonomously, one block at a time. **This guide wins over `plan_phase2_block*.md`** wherever they differ. Everything not corrected here applies as written in the block file.

## 0. Ground truth on HEAD 0177ab3

| Fact | Evidence |
|---|---|
| `asr.py`, `controller.py`, `config.py`, `app.py` are **byte-identical** to the parent of 6b0e721 | `git diff 6b0e721^ 0177ab3 -- src/stt_button/{asr,controller,config,app}.py` gives 0 lines |
| 0177ab3 only touched `focus.py`, `extension.js`, `test_focus.py`, CHANGELOG | `git diff --stat 6b0e721^ 0177ab3` |
| `git cherry-pick 6b0e721` is conflict-free | `git merge-tree --write-tree HEAD 6b0e721` gives a clean tree |
| `tests/test_engine_cancel.py` in 6b0e721 matches the block1 test exactly | diff against the plan snippet is empty |
| Corpus | `~/stt-korpus`: 1600 WAVs, **396 ≥ 10 s**, 130 ≥ 20 s, 56 ≥ 30 s, 214 > 15 s |
| Service | `stt-button.service` active, `-v` (DEBUG), `latency_trace = true`, FP32/HF, `asr_interval_s=1.5`, `live_asr_until_s=15`, `done_sound_mode` not set, so it defaults to `"always"` |

### Line-reference check (all blocks)
| Plan ref | HEAD 0177ab3 | Status |
|---|---|---|
| asr 17-19 imports, 64/66, 214-217, 219 `_int8_dir`, 230 `_load_int8`, 279/328 onnx | identical | ✔ |
| asr 183-185 `__init__` fields | **184-186** | cosmetic (−1) |
| asr 507 `mid_tail_anchor_words`, 546 `_mid_tail_failed`, 567 `last_text`, 574-592 `reset` | identical | ✔ |
| asr 665-667 interval check, 669 `# VAD gate`, 697 `return self._transcribe()`, 699 finalize, 721-723 cache return, 735-736 end | identical | ✔ |
| asr 874 `_new_audio`, 890 `_vad_due`, 892/899/901 `_transcribe` | identical | ✔ |
| **asr "677 live-limit check", "671 min_audio"** (block 6) | **653** live limit, **649** min_audio | **WRONG** → corrected in block 6 |
| controller 29, 142, 267, 352-364, 374-379, 445, 464-465, 496, 580-581, 627 (done sound), 651-653 | identical | ✔ |
| config 83, 164, 228-231; app 155-167 | identical | ✔ |
| `config.example.toml:40` `asr_interval_s` (block 4) | **42** (40 is `streaming_mode`) | corrected |
| `config.example.toml:76` `live_asr_until_s` | 76 | ✔ |

### Interaction with the focus fix (0177ab3)
`focus.terminal_focused()` now really returns True/False. It is consulted in only two places:
1. `DeltaTyper._final_mode()` (typing.py:647). This runs **only in `finalize()`**. The live `update()` never probes focus.
2. `PushToTalkController._play_done_sound()` (controller.py:526). This runs only when `done_sound_mode == "terminal"`, and production runs `"always"`.

`_play_done_sound()` is called **exactly once**, at controller.py:627, inside `_run_finalize_job` after `_delta_typer.finalize(...)`. Consequences:
- **C (mini-finals) never triggers the sound.** A mini-final is a live tick (`_run_once_exec` → `maybe_transcribe` → `_cut_segment`) and never reaches `_run_finalize_job`. A test pins this down (block 7, `test_done_sound_only_once_with_segments`).
- **C plus focus gating:** closed segments arrive as `committed_len` and are **typed live** via `typer.type_text` (dotool), regardless of focus. This is also true in a terminal. The clipboard/copy path (terminal focus) then only receives the delta after the last commit. That is the same behavior as today's LocalAgreement commits, so no new defect, but the L3 measurement must check it (dictate into a terminal once).
- **B and A:** no contact with focus or the sound.

---

## Block 1 (A1) – **GO** (cherry-pick)
**Decision: `git cherry-pick 6b0e721`, with TDD verification and a mandatory review. Do not re-implement.**

Reasons:
- The content was checked line by line. It matches the plan exactly: `import threading`, `InferenceCancelled`, `_cancel_event`/`_cancel_hook_handles`, `_install_cancel_hooks()` at the end of `load()`, `request_cancel`/`clear_cancel`, and the ONNX comment.
- There are two small additions, both harmless and good: the docstring note "an ORT run is atomic", and the comment on **both** ONNX paths (279 and 328).
- The test file is identical to the plan.
- The code base under it is byte-identical, so a fresh implementation would produce the same diff with more risk of typos.
- The missing review gets done now (reviewer brief R1). The missing RED proof gets done now too, see below.

Steps:
```bash
# RED proof against HEAD (test before code)
git show 6b0e721:tests/test_engine_cancel.py > tests/test_engine_cancel.py
nice -n 19 .venv/bin/python -m pytest tests/test_engine_cancel.py -q   # expected: ImportError InferenceCancelled (1 error)
rm tests/test_engine_cancel.py
# GREEN
git cherry-pick 6b0e721            # keeps message "Phase2 1: cooperative cancel hook for the HF engine (A1)"
nice -n 19 .venv/bin/python -m pytest tests/test_engine_cancel.py -q   # 6 passed
nice -n 19 .venv/bin/python -m pytest -q
# Abort criterion from block 1 (real model has >= 10 encoder layers, expected 24):
nice -n 19 .venv/bin/python -c "from transformers import AutoModelForTDT as M; print(len(M.from_pretrained('nvidia/parakeet-tdt-0.6b-v3').encoder.layers))"
```
Write the RED/GREEN output into the review note, not into the commit (the commit already exists).

## Block 2 (A2) – **GO-fix**
The implementation from the block file stays valid (all line references hit). Two corrections:

**Fix 2a – test race (mandatory).** `ctrl.run_once()` runs exactly once in the ticker thread. If the capture thread has not yet queued audio at that moment, `maybe_transcribe` returns None, `entered` never fires, and the test flakes after 5 s. Use this helper in `TestReleaseCancelsLiveTick` and call it in both live-tick tests in place of `ticker = threading.Thread(target=ctrl.run_once, ...)`:
```python
    @staticmethod
    def _start_ticker(ctrl, engine) -> threading.Thread:
        """Tick until the engine is entered (capture may lag the first tick)."""
        def loop() -> None:
            deadline = time.monotonic() + 5.0
            while not engine.entered.is_set() and time.monotonic() < deadline:
                ctrl.run_once()
                time.sleep(0.01)
        t = threading.Thread(target=loop, daemon=True)
        t.start()
        return t
```
`ticker = self._start_ticker(ctrl, engine)`, then `assert engine.entered.wait(timeout=5.0)` as before.

**Fix 2b – merge the lock sections and move clear_cancel inside them (mandatory, replaces steps 3 and 7).** controller.py:350-364 becomes:
```python
        # Brief state check BEFORE transcription: a release (FINALIZING) or
        # abort makes this tick a no-op (its result would be stale and would
        # overwrite the final text).
        with self._lock:
            if self.state != ControllerState.RECORDING:
                return
            # Phase 2 (A): mark the tick as cancellable atomically with the
            # state check; drop a stray cancel left without a finalizer job
            # (_finish with transcriber None). _finish only cancels while this
            # flag is True, so a clear here can never swallow a real cancel.
            self._live_tick_active = True
            clear_cancel = getattr(self._engine, "clear_cancel", None)
            if clear_cancel is not None:
                clear_cancel()

        try:
            self._trace("live asr start")
            result = transcriber.maybe_transcribe()
            self._trace("live asr end")
        except InferenceCancelled:
            # Phase 2 (A): release cancelled this tick on purpose; the
            # finalizer owns the recording now. Not an error, no abort.
            log.info("Live tick cancelled by release")
            return
        except Exception as exc:  # noqa: BLE001 - ASR error -> clean abort
            log.error("Transcription failed: %s", exc)
            self.abort()
            return
        finally:
            with self._lock:
                self._live_tick_active = False
```
Steps 1 (import), 2 (flag at :142), 4 (`_finish` after `self._capture_stop.set()` at :464), 5 (`clear_cancel` after `"finalizer exec acquired"` at :581) and 6 (`abort()` unchanged) stay as written in the block.
Known non-production gap, document it in the commit body: `_mid_tail_step` (asr.py:811) catches `Exception`, so it also swallows `InferenceCancelled` and sets `_mid_tail_failed`. That only matters with `finalize_mode="cached_mid_tail"` (off), and it leads to the correct fallback anyway.

Commit: `Phase2 2: release cancels a running live tick (A2)`.

## Block 3 (A3) – **GO-fix**
`pyproject` has **no** `-m "not slow"` in `addopts`, so the slow test also runs in the full suite (like `test_buffered.py:416`). Add the same skip guard (mandatory) so the suite stays green without the model:
```python
import os

def _model_available() -> bool:
    root = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface"))
    return (root / "hub" / "models--nvidia--parakeet-tdt-0.6b-v3").is_dir()

pytestmark = [pytest.mark.slow,
              pytest.mark.skipif(not _model_available(), reason="Parakeet model missing")]
```
The rest is as written in the block. Run: `nice -n 19 OMP_NUM_THREADS=4 .venv/bin/python -m pytest tests/test_engine_cancel_slow.py -q -m slow`. Record the measured cancel latency in the commit body (print it with `-s`, or put the assert message in the log).
Commit: `Phase2 3: real-model cancel verification (A3)`. **→ Checkpoint R-A (reviewer), then live measurement L1 (operator).**

## Block 4 (B) – **GO-fix**
**Fix 4a – validation only when B is active.** Without this, any existing user config with `asr_interval_s > 6` would suddenly be rejected. That would be a default-path regression.
```python
        if self.asr_interval_adaptive_k < 0:
            raise ConfigError("asr_interval_adaptive_k must not be negative")
        if self.asr_interval_adaptive_k > 0 and self.asr_interval_max_s < self.asr_interval_s:
            raise ConfigError("asr_interval_max_s must be >= asr_interval_s")
```
Adjust the test to match:
```python
    def test_max_below_interval_rejected(self, tmp_path) -> None:
        p = write_config(tmp_path, "asr_interval_adaptive_k = 2.0\n"
                                   "asr_interval_s = 2.0\nasr_interval_max_s = 1.0\n")
        with pytest.raises(ConfigError):
            load_config(p)

    def test_max_below_interval_ok_when_off(self, tmp_path) -> None:
        cfg = load_config(write_config(tmp_path, "asr_interval_s = 8.0\n"))
        assert cfg.asr_interval_s == 8.0
```
**Fix 4b:** the `config.example.toml` insertion goes after **line 42** (`asr_interval_s = 1.0 ...`), not after 40.
**Fix 4c:** remove the `itertools`/`clock`/`del clock` dead code from the `_t` helper. It does nothing.
`import time` goes into asr.py:17-20 (after the `threading` line from block 1). Everything else is as written in the block. `test_docs.py` checks no config keys, so a KONZEPT entry is optional.
Commit: `Phase2 4: adaptive live interval (B, opt-in)`. **→ Checkpoint R-B, then L2.**

## Block 5 (C1) – **GO-fix**
All references hit. I traced every test against the implementation by hand: green.
**Fix 5a – give `_cut_segment` the `force` parameter now** (block 6 then only needs the cap call):
```python
    def _cut_segment(self, cut_nbytes: int, *, force: bool = False) -> TranscriptionResult:
        ...
        if not force and cut_nbytes == len(self._buffer) and not text.endswith(self._SENTENCE_END):
```
**Fix 5b – put `_last_tick_s` into `_reset_segment`** (block 4 is in place): `self._last_tick_s = 0.0`. Remove it from `reset()`, because `reset()` calls `_reset_segment()`.
**Fix 5c – the normal-tick branch of `_cut_segment` must keep the B pace and the cache consistent.** Measure the duration the same way as `_transcribe`:
```python
        clock = self._clock or time.monotonic
        t0 = clock()
        result = self.engine.transcribe(seg, initial_prompt=self._committed_text)
        self._last_tick_s = clock() - t0
```
Position of the pause-cut check: after asr.py:667, before `# VAD gate:` at 669. That is correct, because the live limit (653) sits **before** it and does not normally fire with segmentation on (the segment buffer resets).
Commit: `Phase2 5: sentence-pause segmentation core (C1, opt-in)`.

## Block 6 (C2) – **GO-fix (line references corrected)**
The insertion point given in the block ("before 677, min_audio checked at 671") is **wrong**. Correct: in `maybe_transcribe`, directly after the min_audio return (asr.py:649-650) and **before** the `# STAGE 3: Live ASR limit` comment (651/653):
```python
        if self.audio_seconds < self.min_audio_s:
            return None
        # PHASE 2 (C): hard cap per segment; precedes the live limit so a
        # segmented recording never reaches the pause indicator.
        if self.segment_max_s > 0.0 and self.audio_seconds >= self.segment_max_s:
            return self._cut_segment(self._quiet_cut_nbytes(), force=True)
        # STAGE 3: Live ASR limit reached -> no more live engine calls.
```
Note: the cap fires without regard to `interval_s` (one engine call, after which the buffer is < cap). That is intended.
I traced the tests by hand. In `test_hard_cap_cuts_without_punctuation`, all-zero audio gives the cut point at the **first** frame of the window (4.6 s), and the remainder of 1.9 s stays. The assert holds. `_quiet_cut_nbytes` as written in the block; move `import numpy as np` to module level only if asr.py already imports numpy at module level (it does not, so keep the local import).
Commit: `Phase2 6: hard segment cap at the quietest point (C2)`.

## Block 7 (C3) – **GO-fix**
**Fix 7a – the app.py kwargs from block 4 and block 7 together** (app.py:155-168, final state):
```python
                live_asr_until_s=cfg.live_asr_until_s,
                # Phase 2 (B): adaptive live interval (0.0 = off).
                asr_interval_adaptive_k=cfg.asr_interval_adaptive_k,
                asr_interval_max_s=cfg.asr_interval_max_s,
                # Phase 2 (C): sentence-pause segmentation (0.0 = off).
                segment_pause_s=cfg.segment_pause_s,
                segment_min_s=cfg.segment_min_s,
                segment_max_s=cfg.segment_max_s,
            )
```
**Fix 7b – extra test pinning the done-sound invariant** (append to `tests/test_controller.py`). It must be green right away; it is a guard test:
```python
class TestSegmentsDoneSound:
    """Phase 2 (C): mini-finals are live ticks - the done sound plays once."""

    def test_done_sound_only_once_with_segments(self, monkeypatch) -> None:
        from stt_button.asr import StreamTranscriber

        class Vad:
            def has_speech(self, arr) -> bool:
                return False  # every tail is a pause

        class Eng:
            def transcribe(self, data, initial_prompt=""):
                return TranscriptionResult(text="Satz.")

        plays: list[int] = []
        typer = FakeTyper()
        ctrl = PushToTalkController(
            config=None,
            source_factory=lambda: FakeAudioSource([b"\x00" * 32000] * 12),
            engine=Eng(),
            delta_typer=DeltaTyper(typer, append_space=True),
            transcriber_factory=lambda eng, **kw: StreamTranscriber(
                eng, interval_s=0.5, min_audio_s=0.5, vad=Vad(),
                segment_pause_s=1.0, segment_min_s=1.0, segment_max_s=4.0),
        )
        monkeypatch.setattr(ctrl, "_play_done_sound", lambda: plays.append(1))
        ctrl.start_recording()
        for _ in range(60):
            ctrl.run_once()
            time.sleep(0.01)
        ctrl.stop_recording()
        _wait_state(ctrl, ControllerState.IDLE, timeout=5.0)
        assert plays == [1]
```
(If no cut happens within the 60 ticks the test is still correct. To make the assumption visible, assert first that the transcriber cut once: keep a reference via a factory closure, then `assert t.closed_text`.)
**Fix 7c:** `config.example.toml` B keys after line 42 and C keys after line 76. Everything else is as written in the block, and the validation snippet and dump fix are correct.
Commit: `Phase2 7: segmentation config, validation, wiring, full dump (C3)`. **→ Checkpoint R-C.**

## Block 8 (C4) – **GO-fix**
The seam functions and tests are correct (traced by hand: all 8 green). The block only described `main()` in prose. Here is the ready-to-use segment branch. Insert it in `main()` after the model load (`run_case.engine = ...`, gate_eval.py:151), before `rows = []`. The existing `mid_tail` loop stays unchanged in the `else` branch.
```python
    if args.mode == "segment":
        rows = []
        for w in wavs:
            pcm = load_wav(w)
            ref = run_case(pcm, "full")
            cand = run_case_segment(pcm)
            defects = seam_defects(ref["text"], cand["text"], cand["seams"])
            rows.append({
                "file": w.name, "audio_s": round(cand["audio_s"], 1),
                "wer_kand": round(wer(ref["text"], cand["text"]), 4),
                "seams": cand["seams"], "seam_defects": defects,
                "cap_after_seam": cap_after_seam(cand["text"], cand["seams"]),
                "fin_ref_s": round(ref["finalize_s"], 2),
                "fin_kand_s": round(cand["finalize_s"], 2),
                "ref": ref["text"], "cand": cand["text"],
            })
            print(json.dumps(rows[-1], ensure_ascii=False), flush=True)
            Path(args.out).write_text(json.dumps(rows, ensure_ascii=False, indent=1))
        allv = [d for r in rows for d in r["seam_defects"]]
        n = len(allv)
        lost, dbl, chg = allv.count("lost"), allv.count("doubled"), allv.count("changed")
        mean_wer = statistics.mean(r["wer_kand"] for r in rows) if rows else 0.0
        max_wer = max((r["wer_kand"] for r in rows), default=0.0)
        long_ = [r for r in rows if r["audio_s"] > 20]
        g5 = (statistics.median(r["fin_kand_s"] for r in long_)
              <= 0.5 * statistics.median(r["fin_ref_s"] for r in long_)) if long_ else False
        gates = {"G1_lost": lost == 0, "G2_doubled": dbl == 0,
                 "G3_changed": n > 0 and chg / n <= 0.05,
                 "G4_wer": mean_wer <= 0.005 and max_wer <= 0.03,
                 "G5_speed": g5, "G6_seams": n >= 30}
        summary = {"clips": len(rows), "seams": n, "lost": lost, "doubled": dbl,
                   "changed": chg, "mean_wer": mean_wer, "max_wer": max_wer,
                   "gates": gates}
        Path(args.out).write_text(json.dumps({"summary": summary, "rows": rows},
                                             ensure_ascii=False, indent=1))
        print("\n== GATE (segment) ==")
        print(json.dumps(summary, indent=1))
        if not gates["G6_seams"]:
            print("GATE INCONCLUSIVE")
        elif all(gates[k] for k in ("G1_lost", "G2_doubled", "G3_changed", "G4_wer")):
            print("GATE PASS" if gates["G5_speed"] else "GATE PASS (no speed gain: G5 fail)")
        else:
            print("GATE FAIL")
        return
```
Plus the arguments: `--mode {mid_tail,segment}` (default mid_tail), `--min-seconds` (float, default 0), and the filter `wavs = [p for p in wavs if p.stat().st_size / 32000 >= args.min_seconds]` **before** `--limit`. Note that G4 compares the *mean* WER difference against the reference; since `ref` is the reference itself, the cand WER *is* the difference. Thresholds: 0.005 = 0.5 pp, 0.03 = 3 pp. G5 FAIL with G1-G4 PASS means C is correct but brings no gain. Decide on OPT2 documentation and do **not** recommend it.
Commit: `Phase2 8: segment gate (seam analysis, --mode segment) (C4)`. Then run the gate protocol (§3).

---

## 1. Execution order and commits
| # | Commit message | Gate after commit |
|---|---|---|
| 1 | `Phase2 1: cooperative cancel hook for the HF engine (A1)` (cherry-pick) | suite green |
| 2 | `Phase2 2: release cancels a running live tick (A2)` | suite green |
| 3 | `Phase2 3: real-model cancel verification (A3)` | **R-A**, then L1 (operator) |
| 4 | `Phase2 4: adaptive live interval (B, opt-in)` | **R-B**, then L2 (operator) |
| 5 | `Phase2 5: sentence-pause segmentation core (C1, opt-in)` | suite green |
| 6 | `Phase2 6: hard segment cap at the quietest point (C2)` | suite green |
| 7 | `Phase2 7: segmentation config, validation, wiring, full dump (C3)` | **R-C** |
| 8 | `Phase2 8: segment gate (seam analysis, --mode segment) (C4)` | gate run, **R-G**, decision |

Per block: test first, record RED, implement, then GREEN (`nice -n 19 .venv/bin/python -m pytest -q`), then commit with test and code together.
- Push only `git push origin master` (P7), and only after the checkpoint review is OK.
- The coder does not wait for L1/L2 to finish blocks 5-8. The live measurements are operator-bound and run in parallel. Only L3 needs gate PASS.
- **Service:** new code only takes effect after a restart. `systemctl --user restart stt-button` needs operator approval (P6). The coder asks for it; it is not automatic.

## 2. Reviewer briefs (copy-paste, for cortecs/deepseek-v4-pro-0813)

**R1/R-A (after block 3, covers 1+2+3):**
```
READ-ONLY review, repo /home/salmon/pi-web-ui/STT-Button, do not modify files, do not commit.
Scope: git diff 0177ab3..HEAD (commits Phase2 1-3). Guide: .pi/plans/plan_phase2_execution.md §Block 1-3.
Check and report each point as OK/FINDING (file:line):
1. Block 1 was cherry-picked unreviewed from 6b0e721: hooks idempotent, only on encoder.layers, _check raises only while the event is set, no lock inside request_cancel/clear_cancel.
2. controller._run_once_exec: _live_tick_active set in the SAME _lock section as the RECORDING check; clear_cancel there too; finally resets the flag; except InferenceCancelled comes BEFORE except Exception and calls no abort().
3. _finish: request_cancel only when _live_tick_active, under _lock, only Event.set (no lock-order change _exec_lock -> _lock).
4. _run_finalize_job: clear_cancel under _exec_lock BEFORE feed/finalize.
5. abort() unchanged; source.stop() still before the finalizer (test_source_stopped_before_finalizer_starts green).
6. Race: construct one interleaving in which a cancel hits a finalizer inference, or argue why none exists.
7. Tests: no sleep-only synchronization; slow test has the skipif guard; run `nice -n 19 .venv/bin/python -m pytest -q` and report the result.
Output: table of points + verdict PASS / PASS-with-nits / FAIL.
```
**R-B (after block 4):**
```
READ-ONLY review, repo as above. Scope: git show HEAD (Phase2 4).
1. Default k=0.0 gives byte-identical behavior: effective_interval_s == interval_s; only asr.py:666 and :890 switched.
2. _last_tick_s only set after a successful engine call; reset() resets it.
3. Validation max>=interval ONLY when k>0 (no regression for old configs).
4. Wiring app.py only for StreamTranscriber (not windowed/buffered); config.example.toml after line 42.
5. Run the suite. Verdict PASS/FAIL with file:line.
```
**R-C (after block 7, covers 5-7):**
```
READ-ONLY review, repo as above. Scope: git diff <sha of Phase2 4>..HEAD.
1. Every state change in _cut_segment comes AFTER the engine call (InferenceCancelled/error -> state unchanged; test exists).
2. Defaults segment_pause_s=0/segment_max_s=0: maybe_transcribe/finalize/reset behave exactly as before (_compose/_finalize_compose are no-ops without _closed_text).
3. Cap check sits between the min_audio return (asr ~649) and the live-limit check (~653); force=True.
4. _closed_text always ends with exactly one space; finalize with an empty rest -> rstrip, committed_len=0.
5. Validation: whole_buffer required, cached_mid_tail excluded, cap>0, cap<=live_asr_until_s.
6. _dump_audio uses full_audio(); windowed/buffered fallback via getattr.
7. Done sound: _play_done_sound only in _run_finalize_job (controller ~627), test TestSegmentsDoneSound green; no focus probe in the live path.
8. Lock hierarchy and FIFO queue untouched. Run the suite. Verdict.
```
**R-G (after the gate run):**
```
READ-ONLY. Read .pi/reports/gate_segment.json (summary + rows).
1. Check the G1-G6 values in summary against the rows (recount).
2. Pick 10 seams (prefer defect != ok, fill up with random ones): for each, file, seam index, ref/cand words ±4 around the seam; your own judgment ok/lost/doubled/changed.
3. Assess cap_after_seam distribution (info only).
4. Recommendation PASS / FAIL / INCONCLUSIVE per guide §3, with justification.
Do not change any files.
```

## 3. Gate protocol (block 8) – autonomous; the operator has approved nice -n 19 at load < 4
```bash
cd /home/salmon/pi-web-ui/STT-Button
# 0) Load guard (repeat before every run; abort/wait if >= 4)
python3 - <<'E'
import os, sys; l=os.getloadavg()[0]; print("load1", l); sys.exit(0 if l < 4 else 1)
E
# 1) Unit tests
nice -n 19 .venv/bin/python -m pytest tests/test_gate_seams.py -q
# 2) Smoke (20 clips >= 10 s, ~10-15 min)
nice -n 19 env OMP_NUM_THREADS=4 .venv/bin/python tools/gate_eval.py ~/stt-korpus \
    --mode segment --min-seconds 10 --limit 20 --out .pi/reports/gate_segment_smoke.json
#    Plausibility: summary.seams > 0, no traceback, wer_kand not systematically > 0.1
# 3) Full run (396 clips, estimate 2-4 h) in the background, sleep-proof (R6e), log file:
systemd-run --user --unit=pi-awake --collect systemd-inhibit --what=sleep --mode=block \
    --why="phase2 gate" sleep infinity
systemd-inhibit --list | grep -i "phase2 gate"
nohup nice -n 19 env OMP_NUM_THREADS=4 .venv/bin/python tools/gate_eval.py ~/stt-korpus \
    --mode segment --min-seconds 10 --out .pi/reports/gate_segment.json \
    > .pi/reports/gate_segment.log 2>&1 &
echo $! > .pi/reports/gate_segment.pid
# Monitor (schedule recurring 5-min, R5b): tail -1 .pi/reports/gate_segment.log ; grep -c '"file"' .pi/reports/gate_segment.log
# Afterwards: systemctl --user stop pi-awake ; tail -15 .pi/reports/gate_segment.log
```
The load check applies at start time. If an operator dictation competes during the run, nice 19 keeps the service in priority; do not abort.

**Report:** `.pi/reports/gate_segment.json` (`summary` + `rows`), log `.pi/reports/gate_segment.log`.

| # | Criterion | Threshold | Source in JSON | Result |
|---|---|---|---|---|
| G1 | lost at seams | = 0 (hard) | `summary.lost` | ☐ |
| G2 | doubled at seams | = 0 (hard) | `summary.doubled` | ☐ |
| G3 | changed | ≤ 5 % of seams | `summary.changed / summary.seams` | ☐ |
| G4 | WER cand vs ref | mean ≤ 0.005, max ≤ 0.03 | `summary.mean_wer/max_wer` | ☐ |
| G5 | finalize median clips > 20 s | ≤ 50 % of ref | `summary.gates.G5_speed` | ☐ |
| G6 | seam count | ≥ 30 | `summary.seams` | ☐ |

Decision:
- **PASS** (G1-G6): live test L3 with 1.2/3.0/15.0 as opt-in.
- **FAIL** (G1-G4): CHANGELOG entry plus a row in `docs/ABSCHLUSSBERICHT.md` (OPT2 table), config stays 0. If only cap seams are defective → `segment_max_s` option per block 6.
- **INCONCLUSIVE** (G6): C stays off.
- G5 alone FAIL: correct but useless → document as FAIL-light, no recommendation.

## 4. Live measurements L0-L3
**Operator action required:** service restart (P6) after each code state, and speaking the dictations (mix per overview: 20× 5-15 s, 10× 30-60 s; L3: 10× 30-90 s with sentence pauses, 5× without, 1× into a terminal for the focus check). For L2, the operator sets `asr_interval_adaptive_k = 2.0` in `~/.config/stt-button/config.toml`. For L3, set `segment_pause_s=1.2`, `segment_min_s=3.0`, `segment_max_s=15.0`.
**Prepared by the coder:** note a timestamp before each phase, then run the evaluation.

L0 baseline: the journal before the first Phase 2 restart already contains it (today, PID 6188). Evaluate with `--since` / `--until` of that period.

```bash
# Note the phase marker
date -Iseconds > .pi/reports/phase2_L1.since
# Counters (after the phase)
S=$(cat .pi/reports/phase2_L1.since)
journalctl --user -u stt-button --since "$S" --no-pager | grep -c "Live tick cancelled by release"
journalctl --user -u stt-button --since "$S" --no-pager | grep -c "Final transcription failed"      # must be 0
journalctl --user -u stt-button --since "$S" --no-pager | grep -c "Segment closed"                  # L3 only
journalctl --user -u stt-button --since "$S" --no-pager -o cat > /tmp/phase2_L1.log
```
Evaluation (release→paste per dictation, split by audio length):
```python
# .venv/bin/python - /tmp/phase2_L1.log  (stdin script)
import re, statistics, sys
paste = None; rows = []
for line in open(sys.argv[1], encoding="utf-8", errors="replace"):
    m = re.search(r"latency\[paste\] \+([\d.]+)s", line)
    if m:
        paste = float(m.group(1)); continue
    m = re.search(r"Recording finished \((\d+) bytes of audio", line)
    if m and paste is not None:
        rows.append((int(m.group(1)) / 32000, paste)); paste = None
def stats(sel, name):
    v = sorted(p for a, p in rows if sel(a))
    if not v:
        print(name, "n=0"); return
    p95 = v[min(len(v) - 1, int(round(0.95 * (len(v) - 1))))]
    print(f"{name}: n={len(v)} p50={statistics.median(v):.2f}s p95={p95:.2f}s")
stats(lambda a: True, "all")
stats(lambda a: a <= 15, "<=15s")
stats(lambda a: a > 15, ">15s")
stats(lambda a: a > 30, ">30s")
```
Write the results to `.pi/reports/phase2_live.md` (table L0-L3, targets from the overview). L3 seam quality is judged manually by the operator: lost, doubled or garbled words at seams, capital letter after the seam, no stray pause indicator, terminal dictation pasted completely.

## 5. Rollback
- Per block: `git revert <sha>` in reverse order (8 → 1). Every block is a separate commit.
- **A misbehaves live** (`Final transcription failed` ≥ 1, or a hang): `git revert <Phase2 2>` immediately, plus a CHANGELOG note, then service restart (operator). Block 1 may stay (dead code, ~24 `is_set()` per inference).
- **B:** `asr_interval_adaptive_k = 0.0` (config only).
- **C:** `segment_pause_s = 0.0`, `segment_max_s = 0.0` (default). On gate FAIL, the code stays opt-in unless R-G recommends reverting 5-8.
- The gate run is aborted with `kill $(cat .pi/reports/gate_segment.pid); systemctl --user stop pi-awake`.
- Before every push: `git log origin/master..HEAD` contains only `Phase2 N:` commits.
