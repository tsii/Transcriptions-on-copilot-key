#!/usr/bin/env python3
"""Korpus-Sammler: sammelt die Debug-WAV-Dumps als wachsenden Korpus-Ordner.

Der Diktat-Dienst schreibt jede Aufnahme nach ``/tmp/stt_last.wav``
(audio_dump_mode != "off"). Dieses Werkzeug beobachtet die Datei und
sichert jede *neue* Aufnahme (mtime/Groessen-Aenderung) zeitgestempelt in
den Korpusordner, damit das ``cached_mid_tail``-Qualitaets-Gate
(Kimi-Gutachten) mit echten Diktaten gemessen werden kann.

Reines Werkzeug, keine Produktlogik, nur stdlib.
Aufruf: systemd-run --user --unit=stt-corpus-watch \\
            python3 tools/corpus_watch.py [Zielordner]
"""
from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

SRC = Path("/tmp/stt_last.wav")
MIN_BYTES = 2 * 16000 * 2  # 2 s @ 16 kHz, 16 Bit mono


def main(dst_dir: str) -> None:
    """Endlos-Schleife: neue Dumps nach ``dst_dir`` kopieren (blockierend)."""
    dst = Path(dst_dir).expanduser()
    dst.mkdir(parents=True, exist_ok=True)
    last_sig: tuple[float, int] | None = None
    print(f"korpus-watch: beobachte {SRC} -> {dst}", flush=True)
    while True:
        try:
            st = SRC.stat()
            sig = (st.st_mtime, st.st_size)
            if sig != last_sig and st.st_size >= MIN_BYTES:
                last_sig = sig
                time.sleep(1.0)  # Finalizer koennte noch schreiben
                name = f"{datetime.now():%Y%m%d-%H%M%S}_{st.st_size}.wav"
                (dst / name).write_bytes(SRC.read_bytes())
                print(f"korpus-watch: gesichert {name} "
                      f"({st.st_size / 32000:.1f}s)", flush=True)
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"korpus-watch: Fehler {exc}", flush=True)
        time.sleep(2.0)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else str(Path.home() / "stt-korpus"))
