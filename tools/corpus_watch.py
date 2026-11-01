#!/usr/bin/env python3
"""Corpus collector: collects debug WAV dumps in a growing corpus directory.

The dictation service writes every recording to ``/tmp/stt_last.wav``
(audio_dump_mode != "off"). This tool watches the file and saves every
*new* recording (mtime/size change) with a timestamp in the corpus
directory so that the ``cached_mid_tail`` quality gate (Kimi assessment)
can be measured using real dictations.

Standalone tool, no production logic, stdlib only.
Usage: systemd-run --user --unit=stt-corpus-watch \\
           python3 tools/corpus_watch.py [destination_directory]
"""
from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

SRC = Path("/tmp/stt_last.wav")
MIN_BYTES = 2 * 16000 * 2  # 2 s @ 16 kHz, 16 Bit mono


def main(dst_dir: str) -> None:
    """Infinite loop: copy new dumps to ``dst_dir`` (blocking)."""
    dst = Path(dst_dir).expanduser()
    dst.mkdir(parents=True, exist_ok=True)
    last_sig: tuple[float, int] | None = None
    print(f"corpus-watch: watching {SRC} -> {dst}", flush=True)
    while True:
        try:
            st = SRC.stat()
            sig = (st.st_mtime, st.st_size)
            if sig != last_sig and st.st_size >= MIN_BYTES:
                last_sig = sig
                time.sleep(1.0)  # Finalizer may still be writing
                name = f"{datetime.now():%Y%m%d-%H%M%S}_{st.st_size}.wav"
                (dst / name).write_bytes(SRC.read_bytes())
                print(f"corpus-watch: saved {name} "
                      f"({st.st_size / 32000:.1f}s)", flush=True)
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"corpus-watch: error {exc}", flush=True)
        time.sleep(2.0)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else str(Path.home() / "stt-korpus"))
