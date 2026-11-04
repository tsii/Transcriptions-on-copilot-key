#!/usr/bin/env python3
"""One-shot sanity check for an ASR backend switch (no service restart).

Builds the engine exactly like the app (``stt_button.app.build_engine``)
from a config file, loads it, transcribes fixed corpus clips and reports
load time, elapsed time, RTF, max RSS and text per clip.

Gates (plan_qwen_asr.md, spike protocol):
  * max RSS < 6 GB
  * RTF < 1.5 for clips of the 20 s class (15-30 s)
Exit code 0 = gates pass, 1 = gate failed, 2 = usage/input error.

Usage:  nice -n 19 .venv/bin/python tools/model_switch_check.py \
            --config my.toml [--clips a.wav b.wav ...]
"""
from __future__ import annotations

import argparse
import resource
import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

CORPUS = Path.home() / "stt-korpus"
#: Spike clips (10 s / 20 s / 40 s), see .pi/plans/plan_qwen_asr.md.
DEFAULT_CLIPS = (
    "20261030-100510_320156.wav",
    "20261102-090453_640328.wav",
    "20261029-233118_1284082.wav",
)
RSS_LIMIT_GB = 6.0
RTF_LIMIT = 1.5
RTF_CLASS_S = (15.0, 30.0)


def max_rss_gb() -> float:
    """Peak resident set size of this process in GB (Linux: ru_maxrss in KiB)."""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6


def engine_from_config(path: Path | str | None):
    """Builds (does not load) the engine the app would use for ``path``."""
    from stt_button.app import build_engine
    from stt_button.config import load_config

    return build_engine(load_config(path))


def _read_pcm(path: Path) -> bytes:
    with wave.open(str(path), "rb") as w:
        if w.getnchannels() != 1 or w.getframerate() != 16000 or w.getsampwidth() != 2:
            raise ValueError(f"{path}: expected 16 kHz mono s16le")
        return w.readframes(w.getnframes())


def run_check(engine, clips, *, rss_fn=max_rss_gb, clock=time.perf_counter):
    """Transcribes ``clips``; returns (rows, gates_ok)."""
    ok = True
    rows = []
    for clip in clips:
        clip = Path(clip)
        data = _read_pcm(clip)
        duration = len(data) / 32000
        t0 = clock()
        text = engine.transcribe(data).text
        elapsed = clock() - t0
        rtf = elapsed / duration if duration else 0.0
        rss = rss_fn()
        row = {"clip": clip.name, "duration": duration, "elapsed": elapsed,
               "rtf": rtf, "rss_gb": rss, "text": text}
        rows.append(row)
        if rss >= RSS_LIMIT_GB:
            ok = False
        if RTF_CLASS_S[0] <= duration <= RTF_CLASS_S[1] and rtf >= RTF_LIMIT:
            ok = False
    return rows, ok


def format_row(row) -> str:
    return (f"{row['clip']}  {row['duration']:.1f}s  elapsed {row['elapsed']:.1f}s  "
            f"RTF {row['rtf']:.2f}  maxRSS {row['rss_gb']:.2f} GB  {row['text'][:120]!r}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", default=None, help="config TOML (default: user config)")
    ap.add_argument("--clips", nargs="*", default=None, help="WAV files (16 kHz mono)")
    ap.add_argument("--dry-run", action="store_true", help="validate inputs only")
    args = ap.parse_args(argv)

    clips = [Path(c) for c in (args.clips or [CORPUS / c for c in DEFAULT_CLIPS])]
    missing = [str(c) for c in clips if not c.is_file()]
    if missing:
        print("missing clips: " + ", ".join(missing), file=sys.stderr)
        return 2
    engine = engine_from_config(args.config)
    if args.dry_run:
        print(f"engine: {type(engine).__name__} ({engine.model})")
        return 0
    t0 = time.perf_counter()
    engine.load()
    print(f"engine: {type(engine).__name__} ({engine.model}) load {time.perf_counter() - t0:.1f}s")
    rows, ok = run_check(engine, clips)
    for row in rows:
        print(format_row(row))
    print("GATES: PASS" if ok else "GATES: FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
