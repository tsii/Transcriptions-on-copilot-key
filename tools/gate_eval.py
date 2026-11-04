#!/usr/bin/env python3
"""Corpus gate for ``finalize_mode="cached_mid_tail"`` (Kimi assessment).

Measures two runs per WAV in the corpus directory using the REAL model
(nvidia/parakeet-tdt-0.6b-v3, HF, FP32):

* Reference: finalize_mode="full"  (full-buffer finalize, production path)
* Candidate: finalize_mode="cached_mid_tail" with live_asr_until_s=8.0
             and 4-second background checkpoints (replaying live ticks
             as in the service: 0.5 seconds of audio per tick, 2-second interval)

Hard gate criteria (Kimi, sa-6dd6f8c0):
  1. Committed prefix character-identical in every output
  2. Zero duplicates / zero fragments at stitch boundaries
  3. Last 5 words identical to the reference in the boundary clips
  4. Mean CER against reference <= +0.2 percentage points, no clip > +1.0 pp
  5. Fallback rate for long clips <= 10%
  6. Final ASR time: median <= 1.3 s, p95 <= 1.8 s (30-second class)

Tool only (no production logic). Runs with ``nice -n 19
OMP_NUM_THREADS=4`` so that the dictation service retains priority.

Usage:  nice -n 19 OMP_NUM_THREADS=4 .venv/bin/python tools/gate_eval.py \
            ~/stt-korpus [--limit N] [--out report.json]
            [--mode {mid_tail,segment}] [--min-seconds S]
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import statistics
import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from stt_button.asr import ParakeetEngine, StreamTranscriber  # noqa: E402

MODEL = "nvidia/parakeet-tdt-0.6b-v3"
LIMIT_S = 15.0  # OPT3: Production default 29.10. (gate must replicate the real path)
STEP_S = 4.0
CONTEXT_S = 2.5
TICK_S = 0.5           # Live tick interval of the service
LIVE_INTERVAL_S = 2.0  # maybe_transcribe interval (configuration)


def load_wav(path: Path) -> bytes:
    """Reads a 16 kHz mono 16-bit WAV and returns the PCM bytes."""
    with wave.open(str(path), "rb") as w:
        assert w.getnchannels() == 1 and w.getframerate() == 16000, \
            f"{path}: expected 16 kHz mono"
        assert w.getsampwidth() == 2, f"{path}: expected 16-bit"
        return w.readframes(w.getnframes())


def words(text: str) -> list[str]:
    return re.findall(r"\S+", text)


def edit_distance(a: list[str], b: list[str]) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[len(b)]


def wer(ref: str, hyp: str) -> float:
    r, h = words(ref), words(hyp)
    if not r:
        return 0.0 if not h else 1.0
    return edit_distance(r, h) / len(r)


def duplikate(text: str) -> list[str]:
    """Word repetitions 'word word' (direct duplicate, stitch artifact)."""
    w = words(text)
    return [f"{w[i]} {w[i+1]}" for i in range(len(w) - 1) if w[i] == w[i + 1]]


def fragment_grenze(ref: str, cand: str) -> bool:
    """Checks for stitch fragment artifacts: a short suffix (>1 letter,
    not a word) immediately before a word absent from the reference."""
    rw = set(w.lower().strip(".,!?;:") for w in words(ref))
    for m in re.finditer(r"(?:^| )([A-Za-zAeoueAEeouUE]{2,})(?=[A-ZAeou])", cand):
        if m.group(1).lower() not in rw:
            return True
    return False


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


def replay_live(t: StreamTranscriber, pcm: bytes,
                until_bytes: int) -> None:
    """Feeds audio like the service (0.5-second ticks) and observes the
    2-second tick rule; stops at ``until_bytes`` (release point)."""
    chunk = int(16000 * 2 * TICK_S)
    fed = 0
    since = 0
    while fed < until_bytes:
        piece = pcm[fed:fed + chunk]
        t.feed(piece)
        fed += len(piece)
        since += len(piece)
        if since >= int(16000 * 2 * LIVE_INTERVAL_S):
            t.maybe_transcribe()
            since = 0


def run_case(pcm: bytes, mode: str) -> dict:
    """One run: live replay up to limit + 2 s, then finalize(); measure timings."""
    engine = run_case.engine
    t = StreamTranscriber(engine=engine, sample_rate=16000,
                          interval_s=LIVE_INTERVAL_S,
                          live_asr_until_s=LIMIT_S, finalize_mode=mode,
                          mid_tail_step_s=STEP_S,
                          mid_tail_context_s=CONTEXT_S)
    limit_bytes = int(LIMIT_S * 32000)
    replay_bytes = min(len(pcm), limit_bytes + int(2.0 * 32000))
    t0 = time.monotonic()
    replay_live(t, pcm, replay_bytes)
    live_s = time.monotonic() - t0
    t.feed(pcm[replay_bytes:])
    t0 = time.monotonic()
    res = t.finalize()
    fin_s = time.monotonic() - t0
    return {"text": res.text, "finalize_s": fin_s, "live_s": live_s,
            "mode": mode, "committed_len": res.committed_len,
            "mid_tail_failed": getattr(t, "_mid_tail_failed", None),
            "audio_s": len(pcm) / 32000}


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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("corpus", help="Directory containing WAV files")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default="gate_report.json")
    ap.add_argument("--mode", choices=["mid_tail", "segment"], default="mid_tail")
    ap.add_argument("--min-seconds", type=float, default=0.0)
    args = ap.parse_args()

    wavs = sorted(Path(args.corpus).glob("*.wav"))
    wavs = [p for p in wavs if p.stat().st_size / 32000 >= args.min_seconds]
    if args.limit:
        wavs = wavs[:args.limit]
    if not wavs:
        print("NO WAV FILES — populate the corpus first (tools/corpus_watch.py)")
        return

    print(f"Loading model {MODEL} ...", flush=True)
    run_case.engine = ParakeetEngine(MODEL)

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

    rows = []
    for w in wavs:
        pcm = load_wav(w)
        ref = run_case(pcm, "full")
        cand = run_case(pcm, "cached_mid_tail")
        row = {
            "file": w.name, "audio_s": round(ref["audio_s"], 1),
            "wer_kand": round(wer(ref["text"], cand["text"]), 4),
            "duplikate": duplikate(cand["text"]),
            "fragment": fragment_grenze(ref["text"], cand["text"]),
            "letzte5_ref": words(ref["text"])[-5:],
            "letzte5_kand": words(cand["text"])[-5:],
            "fin_ref_s": round(ref["finalize_s"], 2),
            "fin_kand_s": round(cand["finalize_s"], 2),
            "fallback": bool(cand["mid_tail_failed"]),
        }
        row["letzte5_ok"] = row["letzte5_ref"] == row["letzte5_kand"]
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
        Path(args.out).write_text(json.dumps(rows, ensure_ascii=False, indent=1))

    lange = [r for r in rows if r["audio_s"] >= 20]
    med = statistics.median(r["fin_kand_s"] for r in rows) if rows else -1
    print("\n== GATE ==")
    print(f"Clips: {len(rows)} (long >=20s: {len(lange)})")
    print(f"Mean WER (candidate vs reference): "
          f"{statistics.mean(r['wer_kand'] for r in rows)*100:.2f} pp")
    print(f"Clips with duplicates: {sum(1 for r in rows if r['duplikate'])}")
    print(f"Clips with fragments: {sum(1 for r in rows if r['fragment'])}")
    print(f"Last-5-words failures: {sum(1 for r in rows if not r['letzte5_ok'])}")
    if lange:
        print(f"Fallback rate for long clips: "
              f"{sum(1 for r in lange if r['fallback'])/len(lange)*100:.0f} %")
    print(f"Final ASR median: {med:.2f}s")
    Path(args.out).write_text(json.dumps(rows, ensure_ascii=False, indent=1))
    print(f"Report: {args.out}")


if __name__ == "__main__":
    main()
