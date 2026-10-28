#!/usr/bin/env python3
"""Korpus-Gate für ``finalize_mode="cached_mid_tail"`` (Kimi-Gutachten).

Misst pro WAV im Korpusordner zwei Laeufe mit dem ECHTEN Modell
(nvidia/parakeet-tdt-0.6b-v3, HF, FP32):

* Referenz:      finalize_mode="full"  (Vollpuffer-Finalize, Produktivpfad)
* Kandidat:      finalize_mode="cached_mid_tail" mit live_asr_until_s=8.0
                 und 4-s-Hintergrund-Checkpoints (Replay der Live-Ticks
                 wie im Dienst: 0,5-s-Audio pro Tick, interval 2 s)

Harte Gate-Kriterien (Kimi, sa-6dd6f8c0):
  1. committed-Praefix zeichengleich in jeder Ausgabe
  2. null Duplikate / null Fragmente an Stitch-Grenzen
  3. letzte 5 Woerter wie Referenz in den Boundary-Clips
  4. mittlere CER gegen Referenz <= +0,2 Prozentpunkte, kein Clip > +1,0 pp
  5. Fallback-Quote bei langen Clips <= 10 %
  6. finale ASR-Zeit: Median <= 1,3 s, p95 <= 1,8 s (30-s-Klasse)

Nur Werkzeug (keine Produktlogik). Laeuft mit ``nice -n 19
OMP_NUM_THREADS=4``, damit der Diktatdienst priorisiert bleibt.

Aufruf:  nice -n 19 OMP_NUM_THREADS=4 .venv/bin/python tools/gate_eval.py \
             ~/stt-korpus [--limit N] [--out report.json]
"""
from __future__ import annotations

import argparse
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
LIMIT_S = 8.0
STEP_S = 4.0
CONTEXT_S = 2.5
TICK_S = 0.5           # Live-Tick-Intervall des Dienstes
LIVE_INTERVAL_S = 2.0  # maybe_transcribe-Intervall (Konfiguration)


def load_wav(path: Path) -> bytes:
    """Liest 16-kHz-mono-16bit-WAV und gibt die PCM-Bytes zurueck."""
    with wave.open(str(path), "rb") as w:
        assert w.getnchannels() == 1 and w.getframerate() == 16000, \
            f"{path}: 16kHz mono erwartet"
        assert w.getsampwidth() == 2, f"{path}: 16-bit erwartet"
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
    """Wortwiederholungen 'wort wort' (Direkt-Duplikat, Stitch-Artefakt)."""
    w = words(text)
    return [f"{w[i]} {w[i+1]}" for i in range(len(w) - 1) if w[i] == w[i + 1]]


def fragment_grenze(ref: str, cand: str) -> bool:
    """Prueft Stitch-Fragment-Artefakt: kurzes Anhaengsel (>1 Buchstabe,
    kein Wort) direkt vor einem Wort, das die Referenz so nicht kennt."""
    rw = set(w.lower().strip(".,!?;:") for w in words(ref))
    for m in re.finditer(r"(?:^| )([A-Za-zAeoueAEeouUE]{2,})(?=[A-ZAeou])", cand):
        if m.group(1).lower() not in rw:
            return True
    return False


def replay_live(t: StreamTranscriber, pcm: bytes,
                until_bytes: int) -> None:
    """Fuettert wie der Dienst (0,5-s-Ticks) und haelt die 2-s-Tick-Regel
    ein; stoppt bei ``until_bytes`` (Punkt des Release)."""
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
    """Ein Lauf: Live-Replay bis Limit + 2 s, dann finalize(); Zeiten messen."""
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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("corpus", help="Ordner mit WAVs")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default="gate_report.json")
    args = ap.parse_args()

    wavs = sorted(Path(args.corpus).glob("*.wav"))
    if args.limit:
        wavs = wavs[:args.limit]
    if not wavs:
        print("KEINE WAVs — Korpus erst fuellen (tools/corpus_watch.py)")
        return

    print(f"Lade Modell {MODEL} ...", flush=True)
    run_case.engine = ParakeetEngine(MODEL)

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
    print(f"Clips: {len(rows)} (lange >=20s: {len(lange)})")
    print(f"mittlere WER(Kandidat vs Referenz): "
          f"{statistics.mean(r['wer_kand'] for r in rows)*100:.2f} pp")
    print(f"Clips mit Duplikat: {sum(1 for r in rows if r['duplikate'])}")
    print(f"Clips mit Fragment: {sum(1 for r in rows if r['fragment'])}")
    print(f"letzte-5-Woerter-Faelle: {sum(1 for r in rows if not r['letzte5_ok'])}")
    if lange:
        print(f"Fallback-Quote lange: "
              f"{sum(1 for r in lange if r['fallback'])/len(lange)*100:.0f} %")
    print(f"finale ASR Median: {med:.2f}s")
    Path(args.out).write_text(json.dumps(rows, ensure_ascii=False, indent=1))
    print(f"Report: {args.out}")


if __name__ == "__main__":
    main()
