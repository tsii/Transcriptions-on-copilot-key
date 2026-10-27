"""Benchmark mehrerer ASR-Modelle auf deutschen Test-WAVs.

Misst pro Modell und Datei:
  * real wall-clock Ladezeit (Modell + Processor)
  * Inferenzzeit (nur generate)
  * RTF = Inferenzzeit / Audiosekunden (kleiner = schneller)
  * den erkannten Text

Nutzung:
    .venv/bin/python tools/benchmark_asr.py [--models parakeet,qwen,whisper] \
        [--files /tmp/de_short.wav,/tmp/de_med.wav,/tmp/de_long.wav] \
        [--skip-download]

Module werden nacheinander geladen und pro Datei gemessen (kein Caching-Effekt
zwischen Dateien: load() wird je Modell einmal gezaehlt, infer wird pro Datei
gewarmt durch einen Vorlauf von 0 fuers Modell-Caching).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import wave


def read_wav(path: str) -> tuple[bytes, float]:
    """Lies 16kHz mono PCM16 WAV -> (s16le bytes, sekunden)."""
    with wave.open(path, "rb") as wf:
        assert wf.getnchannels() == 1, "nur mono"
        assert wf.getframerate() == 16000, "nur 16 kHz"
        data = wf.readframes(wf.getnframes())
    return data, len(data) / 2 / 16000.0


class ParakeetBench:
    name = "parakeet-tdt-0.6b-v3"

    def __init__(self) -> None:
        import torch
        from transformers import AutoModelForTDT, AutoProcessor

        self.torch = torch
        self.model_id = "nvidia/parakeet-tdt-0.6b-v3"
        t0 = time.time()
        self.processor = AutoProcessor.from_pretrained(self.model_id)
        self.model = AutoModelForTDT.from_pretrained(
            self.model_id, dtype=torch.float32
        ).to("cpu").eval()
        torch.set_num_threads(torch.get_num_threads())
        self.load_time = time.time() - t0

    def infer(self, data: bytes) -> str:
        audio = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
        inputs = self.processor([audio], sampling_rate=16000)
        inputs.to("cpu", dtype=self.model.dtype)
        out = self.model.generate(**inputs, return_dict_in_generate=True, max_new_tokens=256)
        return self.processor.decode(out.sequences, skip_special_tokens=True)[0].strip()


class QwenBench:
    name = "qwen3-asr-1.7b"

    def __init__(self) -> None:
        import tempfile
        import torch
        from transformers import AutoModelForMultimodalLM, AutoProcessor

        self.torch = torch
        self.model_id = "Qwen/Qwen3-ASR-1.7B-hf"
        t0 = time.time()
        self.processor = AutoProcessor.from_pretrained(self.model_id)
        self.model = AutoModelForMultimodalLM.from_pretrained(
            self.model_id, dtype=torch.float32
        ).to("cpu").eval()
        self.load_time = time.time() - t0

    def infer(self, data: bytes) -> str:
        import tempfile
        import wave as _wave

        audio = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
        with tempfile.NamedTemporaryFile(suffix=".wav") as f:
            with _wave.open(f.name, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
            inputs = self.processor.apply_transcription_request(audio=f.name).to(
                self.model.device, self.model.dtype
            )
            out = self.model.generate(**inputs, max_new_tokens=256)
            gen = out[:, inputs["input_ids"].shape[1]:]
            return self.processor.decode(gen, return_format="transcription_only")[0].strip()


class WhisperBench:
    name = "whisper-large-v3-turbo"

    def __init__(self) -> None:
        import sys

        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
        from stt_button.asr import WhisperEngine

        t0 = time.time()
        self.engine = WhisperEngine("large-v3-turbo", language="de", compute_type="int8")
        self.engine.load()
        self.load_time = time.time() - t0

    def infer(self, data: bytes) -> str:
        return self.engine.transcribe(data).text.strip()


BENCHMARKS = {
    "parakeet": ParakeetBench,
    "qwen": QwenBench,
    "whisper": WhisperBench,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", default="parakeet,qwen,whisper")
    parser.add_argument(
        "--files",
        default="/tmp/de_short.wav,/tmp/de_med.wav,/tmp/de_long.wav",
    )
    parser.add_argument("--json", action="store_true", help="Ergebnisse als JSON")
    args = parser.parse_args(argv)

    files = [f for f in args.files.split(",") if f]
    results: dict = {}

    for key in args.models.split(","):
        key = key.strip()
        cls = BENCHMARKS[key]
        print(f"\n===== {cls.name} =====", flush=True)
        bench = cls()
        print(f"[load] {bench.load_time:.1f}s", flush=True)
        results[cls.name] = {"load_s": round(bench.load_time, 2), "files": {}}

        for path in files:
            data, dur = read_wav(path)
            # warm-up (Modell-Caching / threads)
            try:
                bench.infer(data)
            except Exception as exc:  # noqa: BLE001
                print(f"  WARMUP FEHLER {path}: {exc}", flush=True)
            t0 = time.time()
            try:
                text = bench.infer(data)
                dt = time.time() - t0
                rtf = dt / dur if dur else 0.0
                print(f"[{Path(path).name}] {dur:.2f}s -> {dt:.2f}s  RTF={rtf:.2f}", flush=True)
                print(f"    Text: {text!r}", flush=True)
                results[cls.name]["files"][Path(path).name] = {
                    "audio_s": round(dur, 2),
                    "infer_s": round(dt, 2),
                    "rtf": round(rtf, 2),
                    "text": text,
                }
            except Exception as exc:  # noqa: BLE001
                print(f"[{Path(path).name}] FEHLER: {exc}", flush=True)
                results[cls.name]["files"][Path(path).name] = {"error": str(exc)}

    if args.json:
        print("\n" + json.dumps(results, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())