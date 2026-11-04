# Phase 2 – Block 8 (C4): Quality gate `gate_eval.py --mode segment` (decides on C)

Depends on blocks 5–7. This is a **gate block**: the result decides whether C stays in the code (default off) and whether we recommend it.

## Goal
1. Pure, unit-tested seam analysis in `tools/gate_eval.py`:
   - `seam_defects(ref, cand, seam_word_idx, radius=2)`
   - word alignment with `difflib.SequenceMatcher` on normalized words (lowercase, `.,!?;:` stripped)
   - Classification per seam:
     - `lost`: a delete opcode in the ±radius window
     - `doubled`: an insert opcode, or the same word on both sides of the seam
     - `changed`: replace
     - `ok`
   - Capitalization at the segment start is logged separately (`cap_after_seam`). It is informational only, because the mini-final always ends in .?! and the capital letter is therefore correct.
2. `--mode segment`:
   - Replay each WAV like the service, using real `SileroVadGate`, `segment_pause_s=1.2`, `segment_min_s=3.0`, `segment_max_s=15.0`, `live_asr_until_s=15.0`.
   - Run finalize.
   - Record the seams as word indices of `closed_text` after each closed segment, by comparing the `closed_text` length before and after each tick.
   - Reference = `finalize_mode="full"` without segmentation, which is the existing production path.

### Hard gate criteria for C
| # | Criterion | Threshold |
|---|---|---|
| G1 | `lost` at seams | **0** over the whole corpus |
| G2 | `doubled` at seams | **0** |
| G3 | `changed` at seams | ≤ 5 % of all seams |
| G4 | WER cand vs ref (clips > 10 s) | mean ≤ +0.5 pp, no clip > +3 pp |
| G5 | Final ASR time, clips > 20 s | median ≤ 50 % of reference (the purpose of C) |
| G6 | Seam count | ≥ 30 seams in the corpus (otherwise the gate is not meaningful → "inconclusive", C stays off) |

Corpus: `~/stt-korpus`, 1573 WAVs. 391 are > 10 s, 128 > 20 s, 55 > 30 s (byte size / 32000). The run uses `--min-seconds 10`.

## Test first – `tests/test_gate_seams.py` (new)
```python
"""Unit tests for the seam analysis of tools/gate_eval.py (Phase 2, C4)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

_PATH = Path(__file__).resolve().parent.parent / "tools" / "gate_eval.py"
_spec = importlib.util.spec_from_file_location("gate_eval", _PATH)
gate_eval = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate_eval)

REF = "Das ist der erste Satz. Und hier kommt der zweite Satz."


def test_identical_is_ok() -> None:
    assert gate_eval.seam_defects(REF, REF, [5]) == ["ok"]


def test_lost_word_at_seam() -> None:
    cand = "Das ist der erste Satz. hier kommt der zweite Satz."
    assert gate_eval.seam_defects(REF, cand, [5]) == ["lost"]


def test_doubled_word_at_seam() -> None:
    cand = "Das ist der erste Satz. Satz. Und hier kommt der zweite Satz."
    assert gate_eval.seam_defects(REF, cand, [5]) == ["doubled"]


def test_changed_word_at_seam() -> None:
    cand = "Das ist der erste Satz. Und dort kommt der zweite Satz."
    assert gate_eval.seam_defects(REF, cand, [5]) == ["changed"]


def test_capitalization_and_punctuation_ignored() -> None:
    cand = "Das ist der erste Satz und hier kommt der zweite Satz."
    assert gate_eval.seam_defects(REF, cand, [5]) == ["ok"]


def test_defect_far_from_seam_ignored() -> None:
    cand = "Das ist der erste Satz. Und hier kommt der zweite."
    assert gate_eval.seam_defects(REF, cand, [5]) == ["ok"]


def test_seam_indices_from_closed_texts() -> None:
    assert gate_eval.seam_word_indices(["Eins zwei. ", "Eins zwei. Drei. "]) == [2, 3]


def test_cap_after_seam() -> None:
    cand = "Eins zwei. drei vier."
    assert gate_eval.cap_after_seam(cand, [2]) == [False]
```

## Expected RED
`AttributeError: module 'gate_eval' has no attribute 'seam_defects'`.

## Implementation, `tools/gate_eval.py`
**1. Imports (gate_eval.py:25-35):** add `import difflib`.

**2. New functions after `fragment_grenze` (≈ gate_eval.py:96):**
```python
def _norm(text: str) -> list[str]:
    return [w.lower().strip(".,!?;:") for w in words(text) if w.strip(".,!?;:")]


def seam_word_indices(closed_texts: list[str]) -> list[int]:
    """Word index of each seam (end of every successive closed prefix)."""
    return [len(_norm(t)) for t in closed_texts]


def seam_defects(ref: str, cand: str, seams: list[int], radius: int = 2) -> list[str]:
    """Classify each seam: ok | lost | doubled | changed (normalized words)."""
    r, c = _norm(ref), _norm(cand)
    ops = difflib.SequenceMatcher(a=r, b=c, autojunk=False).get_opcodes()
    out = []
    for s in seams:
        lo, hi = s - radius, s + radius
        if 0 < s < len(c) and c[s - 1] == c[s]:
            out.append("doubled")
            continue
        kind = "ok"
        for tag, _i1, _i2, j1, j2 in ops:
            if tag == "equal":
                continue
            # Deletions have j1 == j2 (a point in cand); widen to touch it.
            if j2 < lo or j1 > hi:
                continue
            kind = {"delete": "lost", "insert": "doubled", "replace": "changed"}[tag]
            if kind != "changed":
                break
        out.append(kind)
    return out


def cap_after_seam(cand: str, seams: list[int]) -> list[bool]:
    """True if the first word after each seam starts upper-case (info only)."""
    w = words(cand)
    return [s < len(w) and w[s][:1].isupper() for s in seams]
```
Check before GREEN: `test_doubled_word_at_seam` uses seam index 5 in the candidate, where `c[4]=="satz"` and `c[5]=="satz"`, so it is detected as doubled. In `test_defect_far_from_seam_ignored`, the delete at index 10 is outside 3..7.

**3. Mode `segment`.** Add `run_case_segment(pcm)` next to `run_case`:
```python
def run_case_segment(pcm: bytes) -> dict:
    """Segmented live replay (Phase 2 C) + finalize; collects seams."""
    from stt_button.vad import SileroVadGate

    t = StreamTranscriber(engine=run_case.engine, sample_rate=16000,
                          interval_s=LIVE_INTERVAL_S, live_asr_until_s=LIMIT_S,
                          finalize_mode="full", vad=SileroVadGate(),
                          segment_pause_s=1.2, segment_min_s=3.0,
                          segment_max_s=LIMIT_S)
    chunk, closed = int(16000 * 2 * TICK_S), []
    for off in range(0, len(pcm), chunk):
        t.feed(pcm[off:off + chunk])
        before = t.closed_text
        t.maybe_transcribe()
        if t.closed_text != before:
            closed.append(t.closed_text)
    t0 = time.monotonic()
    res = t.finalize()
    return {"text": res.text, "finalize_s": time.monotonic() - t0,
            "seams": seam_word_indices(closed), "audio_s": len(pcm) / 32000}
```
In `main()`:
- `ap.add_argument("--mode", choices=["mid_tail", "segment"], default="mid_tail")` and `ap.add_argument("--min-seconds", type=float, default=0.0)`.
- Filter the WAVs with `p.stat().st_size / 32000 >= args.min_seconds`.
- For `segment`, compare against `run_case(pcm, "full")` and evaluate G1–G6.
- Write the report as JSON, with a `seam_defects` list per clip.
- Print a `GATE PASS`/`GATE FAIL`/`GATE INCONCLUSIVE` line at the end.

The existing `mid_tail` path stays unchanged, as the default.

## GREEN
```bash
nice -n 19 .venv/bin/python -m pytest tests/test_gate_seams.py -q
nice -n 19 .venv/bin/python -m pytest -q
```
Gate run (long, ~1–2 h on 391 clips; only with the operator's OK, and the service keeps priority):
```bash
nice -n 19 OMP_NUM_THREADS=4 .venv/bin/python tools/gate_eval.py ~/stt-korpus \
    --mode segment --min-seconds 10 --out .pi/reports/gate_segment.json
```
Smoke run first: `--limit 20`.

## Abort criterion (= the C decision)
**GATE FAIL** means any of G1–G4 is violated:
- C stays in code with default 0.0 (off).
- Document it like OPT2: a CHANGELOG entry "Phase 2 C (segmentation) — gate failed: N lost / M doubled at K seams; config stays off" and one row in `docs/ABSCHLUSSBERICHT.md` (table with OPT2 at line 61) with clip names and the seam context.
- Do not create a live-test config with segmentation.
- A+B stay in place.

**GATE INCONCLUSIVE** (G6): C stays off. First collect more long dictations into the corpus.
