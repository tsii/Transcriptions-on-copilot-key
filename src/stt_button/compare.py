"""Vergleiche mehrere ASR-Modelle auf derselben Audio-Datei.

Nutzung:
    stt-button compare /tmp/test_aufnahme.wav [--models qwen,parakeet,granite,whisper]

Lädt die Modelle nacheinander (nicht parallel), transkribiert die WAV und
gibt nummerierte Absätze aus:
    ## Modellname
    <transkript>
"""
from __future__ import annotations

import argparse
import time
import wave
from dataclasses import dataclass

import numpy as np


def load_wav(path: str) -> np.ndarray:
    """Liest eine WAV und liefert 16 kHz mono float32 (normiert auf [-1, 1])."""
    with wave.open(path, "rb") as wf:
        assert wf.getnchannels() == 1, "nur mono unterstützt"
        assert wf.getsampwidth() == 2, "nur 16-bit PCM"
        assert wf.getframerate() == 16000, "nur 16 kHz"
        data = wf.readframes(wf.getnframes())
    audio = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
    return audio


@dataclass
class ModelRunner:
    name: str
    run: object  # callable(audio: np.ndarray) -> str


def run_qwen(audio: np.ndarray) -> str:
    """Qwen3-ASR-1.7B-hf (transformers)."""
    import torch
    from transformers import AutoModelForMultimodalLM, AutoProcessor

    model_id = "Qwen/Qwen3-ASR-1.7B-hf"
    processor = AutoProcessor.from_pretrained(model_id)
    model = AutoModelForMultimodalLM.from_pretrained(
        model_id, torch_dtype=torch.float32
    )
    model = model.to("cpu").eval()

    # numpy-Array -> temporäre WAV (processor akzeptiert Pfad oder Array)
    import tempfile
    import wave as _wave

    with tempfile.NamedTemporaryFile(suffix=".wav") as f:
        with _wave.open(f.name, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
        inputs = processor.apply_transcription_request(audio=f.name).to(
            model.device, model.dtype
        )
        output_ids = model.generate(**inputs, max_new_tokens=256)
        generated = output_ids[:, inputs["input_ids"].shape[1]:]
        text = processor.decode(
            generated, return_format="transcription_only"
        )[0]
    return text.strip()


def run_granite(audio: np.ndarray) -> str:
    """Granite Speech 4.1-2B (transformers)."""
    import torch
    from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor

    model_id = "ibm-granite/granite-speech-4.1-2b"
    processor = AutoProcessor.from_pretrained(model_id)
    model = AutoModelForSpeechSeq2Seq.from_pretrained(
        model_id, torch_dtype=torch.float32
    ).to("cpu").eval()

    prompt = "<|audio|>transcribe the speech with proper punctuation and capitalization."
    chat = [{"role": "user", "content": prompt}]
    text_prompt = processor.tokenizer.apply_chat_template(
        chat, tokenize=False, add_generation_prompt=True
    )
    # wav muss ein Tensor (1, N) sein
    wav = torch.from_numpy(audio).unsqueeze(0)
    inputs = processor(
        text_prompt, wav, return_tensors="pt", sampling_rate=16000
    )
    outputs = model.generate(**inputs, max_new_tokens=200, do_sample=False, num_beams=1)
    num_in = inputs["input_ids"].shape[-1]
    text = processor.tokenizer.batch_decode(
        outputs[:, num_in:], skip_special_tokens=True
    )[0]
    return text.strip()


def run_parakeet(audio: np.ndarray) -> str:
    """Parakeet TDT 0.6B v3 (transformers, falls verfügbar)."""
    import torch
    from transformers import AutoModelForTDT, AutoProcessor

    model_id = "nvidia/parakeet-tdt-0.6b-v3"
    processor = AutoProcessor.from_pretrained(model_id)
    model = AutoModelForTDT.from_pretrained(model_id, torch_dtype=torch.float32).to(
        "cpu"
    ).eval()
    inputs = processor(
        audio, sampling_rate=16000, return_tensors="pt"
    )
    outputs = model.generate(**inputs, max_length=256)
    text = processor.batch_decode(outputs, skip_special_tokens=True)[0]
    return text.strip()


RUNNERS: dict[str, ModelRunner] = {
    "qwen": ModelRunner("Qwen3-ASR-1.7B", run_qwen),
    "parakeet": ModelRunner("Parakeet TDT 0.6B v3", run_parakeet),
    "granite": ModelRunner("Granite Speech 4.1-2B", run_granite),
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("wav")
    parser.add_argument(
        "--models", default="qwen,parakeet,granite",
        help="kommagetrennt: qwen,parakeet,granite",
    )
    args = parser.parse_args(argv)

    audio = load_wav(args.wav)
    print(f"Audio: {len(audio)/16000:.1f}s\n")

    for key in args.models.split(","):
        runner = RUNNERS[key.strip()]
        print(f"## {runner.name}")
        t0 = time.time()
        try:
            text = runner.run(audio)
            dt = time.time() - t0
            print(text)
            print(f"({dt:.1f}s)")
        except Exception as exc:  # noqa: BLE001
            print(f"[FEHLER] {exc}")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())