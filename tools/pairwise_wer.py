#!/usr/bin/env python3
"""Pairwise WER gate: Qwen3-ASR (BF16) vs production Parakeet FP32.

Comparative only (no ground truth): the SAME audio is transcribed whole-buffer
by the reference (Parakeet TDT 0.6B v3, FP32 HF path) and the candidate
(Qwen3-ASR 1.7B, BF16). Engines run SEQUENTIALLY (reference first, then it is
released before the candidate loads) to keep RAM low next to the service.

Reports WER both directions, mean/median/max, median RTF, the 5 clips with
the highest WER for manual inspection, and a verdict (plan_qwen_asr.md §5):
  viable_opt_in  iff mean WER(cand|ref) <= 15 % and no clip > 50 %
  experimental   otherwise
The verdict never changes the production default.

Usage:  nice -n 19 OMP_NUM_THREADS=4 .venv/bin/python tools/pairwise_wer.py \
            ~/stt-korpus [--limit 40] [--out docs/reports/qwen_pairwise_wer_<date>]
"""
from __future__ import annotations

import argparse
import gc
import json
import re
import statistics
import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

MEAN_LIMIT = 0.15
CLIP_LIMIT = 0.50
MIN_S, MAX_S = 10.0, 60.0


def normalize(text: str) -> list[str]:
    """Lowercase, drop punctuation (keep apostrophes/hyphens inside words)."""
    text = re.sub(r"[^\w\s'\-]", " ", text.lower())
    return [w.strip("'-") for w in text.split() if w.strip("'-")]


def wer(ref: list[str], hyp: list[str]) -> float:
    """Word error rate of ``hyp`` against ``ref`` (Levenshtein on words)."""
    if not ref:
        return 0.0 if not hyp else 1.0
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i] + [0] * len(hyp)
        for j, h in enumerate(hyp, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h))
        prev = cur
    return prev[-1] / len(ref)


def _duration(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / w.getframerate()


def _pcm(path: Path) -> bytes:
    with wave.open(str(path), "rb") as w:
        return w.readframes(w.getnframes())


def select_clips(corpus: Path, limit: int = 40) -> list[Path]:
    """First ``limit`` WAVs (sorted by name) with 10 s <= duration <= 60 s."""
    out = []
    for p in sorted(Path(corpus).glob("*.wav")):
        if MIN_S <= _duration(p) <= MAX_S:
            out.append(p)
            if len(out) >= limit:
                break
    return out


def verdict(wers: list[float]) -> str:
    if not wers:
        return "experimental"
    ok = statistics.mean(wers) <= MEAN_LIMIT and max(wers) <= CLIP_LIMIT
    return "viable_opt_in" if ok else "experimental"


def _transcribe_all(factory, clips):
    engine = factory()
    engine.load()
    texts, rtfs = [], []
    for c in clips:
        data = _pcm(c)
        t0 = time.perf_counter()
        texts.append(engine.transcribe(data).text)
        rtfs.append((time.perf_counter() - t0) / (len(data) / 32000 or 1))
    del engine
    gc.collect()
    return texts, rtfs


def run(clips, *, ref_factory, cand_factory) -> dict:
    ref_texts, ref_rtf = _transcribe_all(ref_factory, clips)
    cand_texts, cand_rtf = _transcribe_all(cand_factory, clips)
    rows = []
    for c, r, q in zip(clips, ref_texts, cand_texts):
        rn, qn = normalize(r), normalize(q)
        rows.append({"clip": Path(c).name, "ref": r, "cand": q,
                     "wer_cand_vs_ref": wer(rn, qn), "wer_ref_vs_cand": wer(qn, rn)})
    a = [x["wer_cand_vs_ref"] for x in rows]
    b = [x["wer_ref_vs_cand"] for x in rows]
    summary = {
        "n": len(rows),
        "mean_wer_cand_vs_ref": statistics.mean(a) if a else 0.0,
        "median_wer_cand_vs_ref": statistics.median(a) if a else 0.0,
        "max_wer_cand_vs_ref": max(a) if a else 0.0,
        "mean_wer_ref_vs_cand": statistics.mean(b) if b else 0.0,
        "median_wer_ref_vs_cand": statistics.median(b) if b else 0.0,
        "max_wer_ref_vs_cand": max(b) if b else 0.0,
        "median_rtf_ref": statistics.median(ref_rtf) if ref_rtf else 0.0,
        "median_rtf_cand": statistics.median(cand_rtf) if cand_rtf else 0.0,
    }
    top = sorted(rows, key=lambda x: x["wer_cand_vs_ref"], reverse=True)[:5]
    return {"summary": summary, "verdict": verdict(a), "clips": rows,
            "manual_inspection": [x["clip"] for x in top]}


def to_markdown(report: dict) -> str:
    s = report["summary"]
    lines = [
        "# Pairwise WER: Qwen3-ASR 1.7B (BF16) vs Parakeet FP32 (reference)",
        "",
        f"Clips: {s['n']} — verdict: **{report['verdict']}** (default unchanged)",
        "",
        "| Direction | mean | median | max |", "|---|---|---|---|",
        f"| WER(qwen \\| ref=parakeet) | {s['mean_wer_cand_vs_ref']:.3f} | "
        f"{s['median_wer_cand_vs_ref']:.3f} | {s['max_wer_cand_vs_ref']:.3f} |",
        f"| WER(parakeet \\| ref=qwen) | {s['mean_wer_ref_vs_cand']:.3f} | "
        f"{s['median_wer_ref_vs_cand']:.3f} | {s['max_wer_ref_vs_cand']:.3f} |",
        "",
        f"Median RTF: parakeet {s['median_rtf_ref']:.2f}, qwen {s['median_rtf_cand']:.2f}",
        "", "## Manual inspection (top-5 WER)", "",
    ]
    by = {r["clip"]: r for r in report["clips"]}
    for name in report["manual_inspection"]:
        r = by[name]
        lines += [f"### {name} (WER {r['wer_cand_vs_ref']:.3f})", "",
                  f"- parakeet: {r['ref']}", f"- qwen: {r['cand']}", ""]
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("corpus", type=Path)
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--out", type=Path, default=None, help="output stem (.json + .md)")
    args = ap.parse_args(argv)

    from stt_button.asr import ParakeetEngine, QwenEngine

    clips = select_clips(args.corpus.expanduser(), args.limit)
    if not clips:
        print("no clips selected", file=sys.stderr)
        return 2
    report = run(
        clips,
        ref_factory=lambda: ParakeetEngine("nvidia/parakeet-tdt-0.6b-v3", compute_type="float32"),
        cand_factory=lambda: QwenEngine(compute_type="bfloat16"),
    )
    md = to_markdown(report)
    print(md)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.with_suffix(".json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        args.out.with_suffix(".md").write_text(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
