"""Whisper engine and streaming logic.

faster-whisper has no native streaming. The approach used here is
"growing buffer + periodic retranscription" (LocalAgreement-like):

* Audio accumulates in the buffer.
* Every ``interval_s`` seconds of audio, the entire buffer is transcribed.
* The portion of text that was identical across ``commit_agreements``
  consecutive runs is considered *committed* and is never retracted.
* The remainder is passed to the output as a delta.

This makes words appear live, while later model corrections are applied
through backspaces instead of duplicating text.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from .typing import _common_prefix_len

log = logging.getLogger(__name__)

DEFAULT_SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2  # s16le

#: Audio shorter than this (seconds) is never transcribed; prevents
#: hallucinations on excessively short noise (RC-1 from the debug research).
MIN_AUDIO_S = 0.5

#: ANSI escape sequences that an accidentally forwarded terminal progress
#: bar (e.g., ``ESC[2K``) could write into the transcript.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_ansi(text: str) -> str:
    """Removes ANSI escape sequences (e.g., ESC[2K) from a transcript."""
    return _ANSI_RE.sub("", text)


def _pcm_to_float(data: bytes) -> "object":
    """Reliably convert s16le bytes to float32 [-1,1], even for odd lengths."""
    import numpy as np
    if len(data) % 2:
        data = data[:-1]
    return np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0


def _token_budget(audio_seconds: float) -> int:
    """Calculates the required ``max_new_tokens`` for an audio duration.

    Fix B (long-dictation truncation): A fixed limit of 256 tokens hard-
    truncates longer dictations. German requires roughly 12 subword tokens
    per second (2-4 tokens/word, ~2.5 words/s + headroom).
    Minimum budget 256, cap 4096 (the TDT decoder has no internal limit;
    the cap only protects against memory/runtime outliers).
    """
    budget = int(audio_seconds * 12) + 64
    return max(256, min(budget, 4096))


class InferenceCancelled(Exception):
    """Raised inside engine inference when a cooperative cancel was requested.

    Only live ticks are ever cancelled (Phase 2, idea A): on release the
    controller cancels a live tick whose result would be discarded anyway,
    so the finalizer does not queue behind it on _exec_lock.
    Only the HF/FP32 path is cancellable; an ONNX/ORT run is atomic.
    """


@dataclass(frozen=True)
class TranscriptionResult:
    """Result of a transcription."""

    text: str
    #: Length of the committed (final) prefix.
    committed_len: int = 0


@runtime_checkable
class TranscriberEngine(Protocol):
    """Transcription engine interface (mockable for tests)."""

    def transcribe(
        self, data: bytes, initial_prompt: str = ""
    ) -> TranscriptionResult: ...


class WhisperEngine:
    """Loads faster-whisper and transcribes audio.

    The model is loaded only on the first :meth:`load` call so that the service
    starts quickly. The first call loads approximately 1.6 GB from the
    HuggingFace cache.
    """

    #: Capability flags (B2): live ticks are affordable on CPU.
    supports_live = True

    def __init__(
        self,
        model: str = "large-v3-turbo",
        *,
        language: str = "de",
        compute_type: str = "int8",
        cpu_threads: int | None = None,
        device: str = "cpu",
        download_root: str | Path | None = None,
    ) -> None:
        self.model = model
        self.language = language
        self.compute_type = compute_type
        self.cpu_threads = cpu_threads or (os.cpu_count() or 4)
        self.device = device
        self.download_root = download_root
        self._model = None

    @property
    def loaded(self) -> bool:
        """Is the model already loaded?"""
        return self._model is not None

    def load(self) -> None:
        """Loads the model (idempotent)."""
        if self._model is not None:
            return
        from faster_whisper import WhisperModel  # expensive import; defer until here

        log.info("Loading Whisper model %s (%s) ...", self.model, self.compute_type)
        self._model = WhisperModel(
            self.model,
            device=self.device,
            compute_type=self.compute_type,
            cpu_threads=self.cpu_threads,
            download_root=str(self.download_root) if self.download_root else None,
        )
        log.info("Model loaded.")

    def transcribe(
        self, data: bytes, initial_prompt: str = ""
    ) -> TranscriptionResult:
        """Transcribes s16le mono audio (16 kHz)."""
        if self._model is None:
            self.load()

        import numpy as np

        audio = _pcm_to_float(data)

        segments, _info = self._model.transcribe(
            audio,
            language=None if self.language == "auto" else self.language,
            task="transcribe",
            beam_size=1,
            temperature=0.0,
            condition_on_previous_text=False,
            vad_filter=True,
            initial_prompt=initial_prompt or None,
        )
        text = "".join(segment.text for segment in segments).strip()
        text = _strip_ansi(text)
        return TranscriptionResult(text=text)


class ParakeetEngine:
    """Transcribes with NVIDIA Parakeet TDT (transformers, CPU-only).

    Parakeet TDT 0.6B v3 is a transducer model (FastConformer encoder +
    TDT decoder) that automatically detects 25 European languages, including
    German and English. On CPU, it is significantly faster than faster-whisper
    and provides the best quality for mixed German/English dictation.

    The model does not support ``initial_prompt`` (unlike Whisper), so the
    parameter is silently ignored. This is not critical for streaming deltas
    because transcription processes the entire buffer anyway.
    """

    #: Capability flags (B2): live ticks + TDT token timestamps.
    supports_live = True
    supports_timestamps = True

    def __init__(
        self,
        model: str = "nvidia/parakeet-tdt-0.6b-v3",
        *,
        language: str = "auto",
        compute_type: str = "float32",
        cpu_threads: int | None = None,
        device: str = "cpu",
    ) -> None:
        self.model = model
        self.language = language
        self.compute_type = compute_type
        self.cpu_threads = cpu_threads or (os.cpu_count() or 4)
        self.device = device
        self._model = None
        self._processor = None
        self._onnx_model = None
        #: Phase 2 (A): cooperative cancel, checked before every encoder layer.
        self._cancel_event = threading.Event()
        self._cancel_hook_handles: list = []

    @property
    def loaded(self) -> bool:
        return self._model is not None or getattr(self, "_onnx_model", None) is not None

    def load(self) -> None:
        """Loads the model and processor (idempotent).

        compute_type='int8': proven end-to-end onnx_asr/ORT CPU path
        (A/B log tools/onnx_tdt_ab_probe_20261028.log: RTF 0.043-0.060,
        1.7-2.5x vs FP32, improved WER). OpenVINO/NPU has been rejected
        (ground truth, 29.10.: NPU under dictation load reports
        'No available devices'; OV encoder static bucket incompatible with
        actual lengths; zero RTF gain vs CPU INT8). If the int8 ONNX files
        are missing, the HF FP32 path takes over (never crashes).
        """
        if self._model is not None or getattr(self, "_onnx_model", None) is not None:
            return
        if self.compute_type == "int8" and self._load_int8():
            return
        import torch
        from transformers import AutoModelForTDT, AutoProcessor

        log.info("Loading Parakeet model %s (FP32 fallback path) ...", self.model)
        self._processor = AutoProcessor.from_pretrained(self.model)
        self._model = AutoModelForTDT.from_pretrained(
            self.model, dtype=torch.float32
        ).to(self.device).eval()
        if self.device == "cpu":
            torch.set_num_threads(self.cpu_threads)
        self._install_cancel_hooks()
        log.info("Parakeet model loaded.")

    def _install_cancel_hooks(self) -> None:
        """Register one forward pre-hook per encoder layer (idempotent).

        The hook raises InferenceCancelled while the cancel event is set;
        generate() unwinds like on any exception (each call builds its own
        tensors, so no shared state is left half-updated).
        """
        if self._cancel_hook_handles or self._model is None:
            return
        layers = getattr(getattr(self._model, "encoder", None), "layers", None)
        if layers is None:
            log.debug("cancel hooks: no encoder.layers - inference not cancellable")
            return

        def _check(_module, _args):
            if self._cancel_event.is_set():
                raise InferenceCancelled("live inference cancelled")

        self._cancel_hook_handles = [
            layer.register_forward_pre_hook(_check) for layer in layers
        ]

    def request_cancel(self) -> None:
        """Ask a running HF inference to stop at the next encoder layer.

        No-op for the ONNX/int8 path (an ORT run is atomic).
        """
        self._cancel_event.set()

    def clear_cancel(self) -> None:
        """Re-enable inference after a cancel."""
        self._cancel_event.clear()

    def _int8_dir(self):
        """Directory containing the int8 ONNX files (tests may redirect it)."""
        hook = getattr(type(self), "_int8_dir_for_test", None)
        if hook is not None:
            return hook(self)
        from pathlib import Path as _P
        # Robust path: relative to the repo root (the parent directory of the
        # stt_button package), NOT to the service's working directory
        _repo_root = _P(__file__).resolve().parent.parent.parent
        return _repo_root / ".models" / "parakeet-onnx-fp32"

    def _load_int8(self) -> bool:
        """Attempts the proven end-to-end int8 path (onnx_asr + ORT CPU).

        True = int8 model ready (transcribe uses it); False = files missing
        or loading failed (caller falls back to HF FP32).
        """
        d = self._int8_dir()
        enc = d / "encoder-model.int8.onnx"
        dec = d / "decoder_joint-model.int8.onnx"
        if not (enc.exists() and dec.exists()):
            log.warning("int8 ONNX missing (%s) - FP32 fallback.", d)
            return False
        try:
            import onnx_asr
            import onnxruntime as ort

            opts = ort.SessionOptions()
            opts.intra_op_num_threads = self.cpu_threads
            opts.inter_op_num_threads = 1
            log.info("Loading Parakeet int8 ONNX (ORT CPU) from %s ...", d)
            self._onnx_model = onnx_asr.load_model(
                "nemo-conformer-tdt", path=d, quantization="int8",
                sess_options=opts, providers=["CPUExecutionProvider"],
            )
            log.info("Parakeet int8 ONNX ready (RTF measurement 0.043-0.060).")
            return True
        except Exception as exc:  # noqa: BLE001 - fallback must never crash
            log.warning("Failed to load int8 ONNX (%s) - FP32 fallback.", exc)
            self._onnx_model = None
            return False

    def transcribe(
        self, data: bytes, initial_prompt: str = ""
    ) -> TranscriptionResult:
        """Transcribes s16le mono audio (16 kHz)."""
        # Fix 2: Audio that is too short is never transcribed (prevents
        # hallucinations on < 0.5s of noise).
        if len(data) < MIN_AUDIO_S * 16000 * 2:
            return TranscriptionResult(text="")
        if self._model is None and getattr(self, "_onnx_model", None) is None:
            self.load()

        # INT8 primary path (operator, 29.10.): proven end-to-end
        # onnx_asr/ORT CPU path (A/B log tools/onnx_tdt_ab_probe_20261028.log:
        # RTF 0.043-0.060, 1.7-2.5x vs FP32, improved WER). OpenVINO/NPU has
        # been rejected (ground truth: NPU under dictation load reports
        # 'No available devices'; static bucket incompatible; zero RTF gain).
        # initial_prompt is omitted because the full buffer is transcribed
        # anyway; FP32 HF remains the fallback (never crashes).
        # Not cancellable (Phase 2 A): an ORT run is atomic.
        onnx_model = getattr(self, "_onnx_model", None)
        if onnx_model is not None:
            import numpy as np

            audio = _pcm_to_float(data)
            if not isinstance(audio, np.ndarray):
                audio = np.asarray(audio, dtype=np.float32)
            text = _strip_ansi(str(onnx_model.recognize(audio)).strip())
            return TranscriptionResult(text=text)

        import numpy as np

        audio = _pcm_to_float(data)
        inputs = self._processor([audio], sampling_rate=16000)
        inputs.to(self.device, dtype=self._model.dtype)
        # Set max_new_tokens explicitly; otherwise, the default max_length
        # grows with the audio and truncates long dictations.
        # Fix B: Budget scales with audio duration.
        audio_s = len(data) / (16000 * 2)
        output = self._model.generate(
            **inputs,
            return_dict_in_generate=True,
            max_new_tokens=_token_budget(audio_s),
        )
        text = self._processor.decode(output.sequences, skip_special_tokens=True)[0]
        text = _strip_ansi(text.strip())
        return TranscriptionResult(text=text)

    def transcribe_with_timestamps(
        self, data: bytes, window_offset_s: float = 0.0
    ) -> "Transcription":
        """Transcribes and returns token timestamps.

        Uses TDT durations (80ms frames) to assign an absolute timestamp to
        each token. ``window_offset_s`` is the start time of the audio window
        in the overall stream (for windowed streaming).
        """
        from .streaming_types import Token, Transcription

        # Fix 2: Audio that is too short produces no tokens.
        if len(data) < MIN_AUDIO_S * 16000 * 2:
            return Transcription(tokens=[])
        if self._model is None and getattr(self, "_onnx_model", None) is None:
            self.load()

        # INT8 primary path: without TDT durations, there are no token
        # timestamps -> one text token spanning the entire window length
        # (integer-correct; windowed streaming is intentionally disabled
        # with int8).
        # Not cancellable (Phase 2 A): an ORT run is atomic.
        onnx_model = getattr(self, "_onnx_model", None)
        if onnx_model is not None:
            import numpy as np

            audio = _pcm_to_float(data)
            if not isinstance(audio, np.ndarray):
                audio = np.asarray(audio, dtype=np.float32)
            text = _strip_ansi(str(onnx_model.recognize(audio)).strip())
            dur = len(data) / (16000 * 2)
            return Transcription(
                tokens=[Token(text=text, start=window_offset_s,
                              end=window_offset_s + dur)]
            )

        import numpy as np

        audio = _pcm_to_float(data)
        inputs = self._processor([audio], sampling_rate=16000)
        inputs.to(self.device, dtype=self._model.dtype)
        output = self._model.generate(
            **inputs,
            return_dict_in_generate=True,
            max_new_tokens=_token_budget(len(data) / (16000 * 2)),
        )
        _text, stamps = self._processor.decode(
            output.sequences,
            durations=output.durations,
            skip_special_tokens=True,
        )
        tokens = []
        for seq in stamps:
            for item in seq:
                tokens.append(
                    Token(
                        text=item["token"],
                        start=window_offset_s + float(item["start"]),
                        end=window_offset_s + float(item["end"]),
                    )
                )
        return Transcription(tokens=tokens)


#: Pinned snapshot of the default Qwen model (spike 2026-11-04).
QWEN_DEFAULT_MODEL = "Qwen/Qwen3-ASR-1.7B-hf"
QWEN_DEFAULT_REVISION = "bcd2b5b7f32b480ab5790554cfa8347f246a14f3"


class QwenEngine:
    """Transcribes with Qwen3-ASR (transformers, CPU-only).

    Qwen3-ASR-1.7B is a multimodal model that runs natively through
    ``AutoModelForMultimodalLM`` + ``apply_transcription_request``.
    It provides very high quality (including punctuation and language
    detection) but is significantly slower than Parakeet on CPU.

    This model also does not support ``initial_prompt``; the parameter is
    ignored.

    Final-only engine: whole-buffer re-transcription per live tick is far
    too slow (spike 2026-11-04: RTF ~0.77 at 20 s, BF16), so
    ``supports_live`` is False and the app disables live ticks. BF16 is
    the default (4.7-5.0 GB RSS); float32 upcasts to ~11.7 GB.
    """

    supports_live = False
    supports_timestamps = False
    _DTYPES = ("bfloat16", "float32")

    def __init__(
        self,
        model: str = QWEN_DEFAULT_MODEL,
        *,
        language: str = "auto",
        compute_type: str = "bfloat16",
        cpu_threads: int | None = None,
        device: str = "cpu",
    ) -> None:
        if compute_type not in self._DTYPES:
            raise ValueError(
                f"QwenEngine compute_type must be one of {self._DTYPES}, "
                f"is {compute_type!r}"
            )
        self.model = model
        self.language = language
        self.compute_type = compute_type
        self.cpu_threads = cpu_threads or (os.cpu_count() or 4)
        self.device = device
        self._model = None
        self._processor = None

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def load(self) -> None:
        """Loads the model and processor (idempotent)."""
        if self._model is not None:
            return
        import torch
        from transformers import AutoModelForMultimodalLM, AutoProcessor

        log.info("Loading Qwen3-ASR model %s (%s) ...", self.model, self.compute_type)
        pin = {"revision": QWEN_DEFAULT_REVISION} if self.model == QWEN_DEFAULT_MODEL else {}
        dtype = torch.bfloat16 if self.compute_type == "bfloat16" else torch.float32
        self._processor = AutoProcessor.from_pretrained(self.model, **pin)
        self._model = AutoModelForMultimodalLM.from_pretrained(
            self.model, dtype=dtype, **pin
        ).to(self.device).eval()
        if self.device == "cpu":
            torch.set_num_threads(self.cpu_threads)
        log.info("Qwen3-ASR model loaded.")

    def transcribe(
        self, data: bytes, initial_prompt: str = ""
    ) -> TranscriptionResult:
        """Transcribes s16le mono audio (16 kHz)."""
        if self._model is None:
            self.load()

        import tempfile
        import wave as _wave

        import numpy as np

        audio = _pcm_to_float(data)

        # The Qwen processor accepts a file path, so we write the buffer
        # to a temporary WAV file.
        with tempfile.NamedTemporaryFile(suffix=".wav") as f:
            with _wave.open(f.name, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
            inputs = self._processor.apply_transcription_request(
                audio=f.name
            ).to(self.device, self._model.dtype)
            output_ids = self._model.generate(
                **inputs, max_new_tokens=_token_budget(len(data) / (16000 * 2))
            )
            generated = output_ids[:, inputs["input_ids"].shape[1]:]
            text = self._processor.decode(
                generated, return_format="transcription_only"
            )[0]
        text = _strip_ansi(text.strip())
        return TranscriptionResult(text=text)


@dataclass(frozen=True)
class _MidTailCheckpoint:
    """PHASE 2: Background checkpoint for finalize_mode="cached_mid_tail".

    Stores the buffer end (bytes) of a successful live snapshot or background
    stitching run together with the stitched working hypothesis. Subsequent
    windows start at ``end_nbytes - mid_tail_context_s``.
    """

    #: Buffer length (bytes) at the time of the snapshot.
    end_nbytes: int
    #: Stitched working hypothesis at this buffer position.
    text: str


@dataclass
class StreamTranscriber:
    """Accumulates audio and periodically produces transcriptions.

    The class contains no I/O logic and is therefore fully testable.
    """

    engine: TranscriberEngine
    interval_s: float = 1.5
    min_audio_s: float = 0.8
    sample_rate: int = DEFAULT_SAMPLE_RATE
    commit_agreements: int = 2
    #: Finalization mode:
    #: ``"full"`` (default, previous behavior): Finalize retranscribes the
    #: entire buffer.
    #: ``"cached"``: if the buffer has not changed since the last SUCCESSFUL
    #: inference (same length, no feed()/reset() since then), Finalize returns
    #: the cached text without an engine call. Useful only for deterministic
    #: engines (Parakeet: fixed decoding parameters).
    finalize_mode: str = "full"
    #: Optional VAD gate (e.g., :class:`~stt_button.vad.SileroVadGate`).
    #: If it reports no speech in the NEW audio region, transcription is skipped
    #: (prevents hallucinations on silence -> phantom backspaces).
    vad: object | None = None
    #: B3: False for final-only engines (``engine.supports_live`` is False,
    #: e.g. Qwen3-ASR): maybe_transcribe() never calls the engine, no pause
    #: indicator; finalize() transcribes the complete buffer as usual.
    live_enabled: bool = True
    #: STAGE 3: Stop live ticks after this buffer duration (seconds).
    #: The whole-buffer approach becomes more expensive per tick (RTF grows
    #: with the buffer); beyond this point, only finalize() produces text.
    #: feed() continues accumulating, finalize() transcribes the entire buffer,
    #: and reset() reactivates live ticks. 0.0 (default) = always active.
    live_asr_until_s: float = 0.0
    #: PHASE 2 (only finalize_mode="cached_mid_tail"): left context
    #: (seconds) of mid-tail windows before the checkpoint end.
    mid_tail_context_s: float = 2.5
    #: PHASE 2: Interval (seconds of new audio since the checkpoint) between
    #: two background mid-tail engine calls.
    mid_tail_step_s: float = 4.0
    #: PHASE 2: Minimum number of word anchors for stitch_word_overlap.
    mid_tail_anchor_words: int = 2
    #: PHASE 2 (B): adaptive live interval. 0.0 (default) = fixed interval_s
    #: (previous behavior). >0: next interval = clamp(k * last engine call
    #: duration, interval_s, asr_interval_max_s) - like TalkType DecodePace.
    asr_interval_adaptive_k: float = 0.0
    asr_interval_max_s: float = 6.0
    #: PHASE 2 (C): hard-cut segmentation. >0: a VAD pause of at least this
    #: many seconds after a hypothesis ending in .?! closes the segment
    #: (mini-final, permanent commit, buffer reset). 0.0 (default) = off.
    segment_pause_s: float = 0.0
    #: PHASE 2 (C): minimum segment length (seconds) before a pause cut.
    segment_min_s: float = 3.0
    #: PHASE 2 (C): hard cap per segment (seconds, block 6); 0 = no cap.
    segment_max_s: float = 0.0

    _buffer: bytearray = field(default_factory=bytearray, init=False, repr=False)
    _last_transcribed_at: float = field(default=0.0, init=False, repr=False)
    _has_transcribed: bool = field(default=False, init=False, repr=False)
    _committed_text: str = field(default="", init=False, repr=False)
    _committed_len: int = field(default=0, init=False, repr=False)
    _history: list[str] = field(default_factory=list, init=False, repr=False)
    _last_text: str = field(default="", init=False, repr=False)
    #: Audio position (seconds) at which VAD was last queried.
    _vad_checked_at: float = field(default=0.0, init=False, repr=False)
    # --- Cache for finalize_mode="cached" ---------------------------------
    #: Buffer size (bytes) after the last SUCCESSFUL inference.
    _last_completed_nbytes: int = field(default=-1, init=False, repr=False)
    #: Text from the last SUCCESSFUL inference.
    _last_completed_text: str = field(default="", init=False, repr=False)
    #: True only after a successful engine run (not before the call); proof
    #: of completion for the cache.
    _last_completed_ok: bool = field(default=False, init=False, repr=False)
    #: Counts every non-empty feed(); invalidates the cache.
    _feed_epoch: int = field(default=0, init=False, repr=False)
    #: feed counter value at the time of the last successful inference.
    _completed_feed_epoch: int = field(default=-1, init=False, repr=False)
    # --- STUFE 4: Pausen-Indikator (live_asr_until_s) ---------------------
    #: Live limit has already been reached and reported (since the last
    #: reset()); prevents a second report on subsequent limit ticks.
    _live_paused_seen: bool = field(default=False, init=False, repr=False)
    #: Live limit has been reached, but the notification has not yet been
    #: retrieved (take_live_paused()).
    _live_paused_pending: bool = field(default=False, init=False, repr=False)
    # --- PHASE 2: cached_mid_tail ----------------------------------------
    #: Valid background checkpoint (full-buffer bytes + stitched working
    #: hypothesis). None = no stitching attempt possible.
    _mid_tail_checkpoint: "_MidTailCheckpoint | None" = field(
        default=None, init=False, repr=False
    )
    #: True after a background mid-tail run has permanently failed (engine
    #: exception or stitch None): no further attempts; finalize() falls back
    #: to the full buffer.
    _mid_tail_failed: bool = field(default=False, init=False, repr=False)
    #: Phase 2 (B): duration of the last successful live engine call.
    _last_tick_s: float = field(default=0.0, init=False, repr=False)
    #: PHASE 2 (C): text of closed segments, always ends with one space.
    _closed_text: str = field(default="", init=False, repr=False)
    #: PHASE 2 (C): audio of closed segments (for the debug dump).
    _closed_audio: bytearray = field(default_factory=bytearray, init=False, repr=False)
    #: Injectable clock (tests).
    _clock: object = field(default=None, init=False, repr=False)

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------
    @property
    def audio_seconds(self) -> float:
        """Number of seconds of audio currently in the buffer."""
        return len(self._buffer) / (self.sample_rate * BYTES_PER_SAMPLE)

    @property
    def committed_len(self) -> int:
        """Length of the committed (final) text prefix."""
        return self._committed_len

    @property
    def committed_text(self) -> str:
        """The final text prefix."""
        return self._committed_text

    @property
    def last_text(self) -> str:
        """The most recently transcribed full text."""
        return self._last_text

    @property
    def closed_text(self) -> str:
        """Text of permanently closed segments (Phase 2, C)."""
        return self._closed_text

    def full_audio(self) -> bytes:
        """Whole recording: closed segments followed by the current buffer."""
        return bytes(self._closed_audio) + bytes(self._buffer)

    @property
    def effective_interval_s(self) -> float:
        """Live interval honoring the adaptive pace (Phase 2, B)."""
        if self.asr_interval_adaptive_k <= 0.0 or self._last_tick_s <= 0.0:
            return self.interval_s
        paced = self.asr_interval_adaptive_k * self._last_tick_s
        return max(self.interval_s, min(paced, self.asr_interval_max_s))

    # ------------------------------------------------------------------
    # Control
    # ------------------------------------------------------------------
    def reset(self) -> None:
        """Discards the buffer and commit state (invalidates the cache).

        Also resets the live-limit notification (pause indicator): the next
        recording can reach the limit again and report it exactly once.
        """
        self._reset_segment()
        self._closed_text = ""
        self._closed_audio.clear()
        self._live_paused_seen = False
        self._live_paused_pending = False

    def _reset_segment(self) -> None:
        """Clear the CURRENT segment only (buffer, commit, history, cache)."""
        self._buffer.clear()
        self._last_transcribed_at = 0.0
        self._has_transcribed = False
        self._committed_text = ""
        self._committed_len = 0
        self._history = []
        self._last_text = ""
        self._vad_checked_at = 0.0
        self._mid_tail_checkpoint = None
        self._mid_tail_failed = False
        self._last_tick_s = 0.0
        self._invalidate_cache()

    def take_live_paused(self) -> bool:
        """Retrieves the live-ASR limit notification (exactly once per recording).

        Minimal signature (intentionally no callback or extra property):
        ``take_live_paused() -> bool``. Returns ``True`` exactly once, after
        the live limit is first exceeded (``live_asr_until_s > 0`` and buffer
        >= limit), and consumes the notification. Subsequent calls return
        ``False`` until ``reset()`` is followed by another limit crossing.

        The limit is detected HERE (not merely recorded in
        ``maybe_transcribe()``): the last live tick starts normally below the
        limit and still returns a result; the limit is crossed only during its
        multi-second inference (audio continues), and release can occur BEFORE
        the next tick. In that case, ``maybe_transcribe()`` never sees the
        limit; the finalizer (which feeds last) must still be able to retrieve
        the notification (real-world test 11:56).

        With ``live_asr_until_s=0.0`` (default), this method NEVER returns
        ``True`` because the limit is disabled.

        Returns:
            ``True`` iff the limit notification had not yet been retrieved.
        """
        if (
            self.live_enabled
            and self.live_asr_until_s > 0.0
            and self.audio_seconds >= self.live_asr_until_s
            and not self._live_paused_seen
        ):
            self._live_paused_seen = True
            self._live_paused_pending = True
        pending = self._live_paused_pending
        self._live_paused_pending = False
        return pending

    def _invalidate_cache(self) -> None:
        """Invalidates the finalize cache (after reset() or an error)."""
        self._last_completed_nbytes = -1
        self._last_completed_text = ""
        self._last_completed_ok = False
        self._completed_feed_epoch = -1

    def feed(self, chunk: bytes) -> None:
        """Adds audio data to the buffer and invalidates the finalize cache."""
        if chunk:
            self._buffer.extend(chunk)
            self._feed_epoch += 1

    def maybe_transcribe(self) -> TranscriptionResult | None:
        """Transcribes when enough new audio data is available.

        Returns:
            The result, or ``None`` if no transcription has occurred yet.
        """
        if not self.live_enabled:
            return None
        if self.audio_seconds < self.min_audio_s:
            return None
        # PHASE 2 (C): hard cap per segment; precedes the live limit so a
        # segmented recording never reaches the pause indicator.
        if self.segment_max_s > 0.0 and self.audio_seconds >= self.segment_max_s:
            return self._cut_segment(self._quiet_cut_nbytes(), force=True)
        # STAGE 3: Live ASR limit reached -> no more live engine calls.
        # The buffer continues growing (feed()); finalize() transcribes all of it.
        # STAGE 4: The first crossing is recorded EXACTLY ONCE as a notification
        # (take_live_paused()); the controller types the pause indicator in response.
        if self.live_asr_until_s > 0.0 and self.audio_seconds >= self.live_asr_until_s:
            if not self._live_paused_seen:
                self._live_paused_seen = True
                self._live_paused_pending = True
            # PHASE 2: Background mid-tail stitching (opt-in mode only).
            # ALWAYS returns None: no typing, no _update_commit, and no changes
            # to _history/_committed_text/live cache.
            if self.finalize_mode == "cached_mid_tail" and not self._mid_tail_failed:
                self._mid_tail_step()
            return None
        # The first transcription may occur immediately; thereafter, only
        # newly added audio is considered.
        if self._has_transcribed:
            if self.audio_seconds - self._last_transcribed_at < self.effective_interval_s:
                return None

        # PHASE 2 (C): sentence-pause cut takes precedence over the VAD skip
        # (the pause IS silence; the normal gate would return None here).
        if self.segment_pause_s > 0.0 and self._pause_cut_due():
            return self._cut_segment(len(self._buffer))

        # VAD gate: Silence/noise must never reach ASR. Only new audio (since
        # the last transcription) is checked, saving computation time and
        # preventing phantom text and backspace retractions.
        # A gate error must not prevent dictation: it is an optimization,
        # not a requirement.
        if self.vad is not None:
            if not self._vad_due():
                return None
            self._vad_checked_at = self.audio_seconds
            new_audio = self._new_audio()
            arr = _pcm_to_float(new_audio) if new_audio else None
            # Below 1s, VAD cannot make a determination -> do not block.
            if arr is not None and len(arr) >= self.sample_rate:
                try:
                    speech = self.vad.has_speech(arr)
                except Exception as exc:  # noqa: BLE001 - gate is optional
                    log.warning(
                        "VAD gate failed (%s) - transcribing anyway", exc
                    )
                else:
                    if not speech:
                        log.debug(
                            "VAD: no speech in %.1fs of new audio - "
                            "transcription skipped",
                            len(new_audio) / (self.sample_rate * BYTES_PER_SAMPLE),
                        )
                        return None

        return self._compose(self._transcribe())

    def finalize(self) -> TranscriptionResult:
        """Transcribes the entire buffer as the final result.

        The result is intentionally fully correctable (``committed_len=0``):
        the model may still revise its last word.

        With ``finalize_mode="cached"``, the engine call is skipped if the
        buffer has not changed since the last SUCCESSFUL inference (same byte
        length, no feed()/reset() since then): with fixed decoding parameters
        (temperature=0, beam=1), retranscription would produce no new result.
        The text comes from the cache, and the agreement history is unchanged.
        """
        if (
            self.finalize_mode == "cached"
            and self._last_completed_ok
            and len(self._buffer) == self._last_completed_nbytes
            and self._feed_epoch == self._completed_feed_epoch
        ):
            log.debug(
                "Finalize result taken from cache (%d bytes, no engine call)",
                len(self._buffer),
            )
            return self._finalize_compose(self._last_completed_text)
        # PHASE 2: Small tail instead of full buffer if the mid-tail chain is
        # valid; otherwise, preserve current behavior (full-buffer fallback).
        if self.finalize_mode == "cached_mid_tail":
            mid_tail = self._mid_tail_finalize()
            if mid_tail is not None:
                log.debug(
                    "Finalize from mid-tail checkpoint (%d-byte buffer, "
                    "tail stitch)",
                    len(self._buffer),
                )
                return mid_tail
        result = self._transcribe()
        return self._finalize_compose(result.text)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------
    # --- PHASE 2: cached_mid_tail ---------------------------------------
    def _mid_tail_valid_snapshot(self) -> "_MidTailCheckpoint | None":
        """Initial checkpoint from the last successful live snapshot.

        Valid if live inference succeeded and its text starts consistently
        with the committed prefix. OPT2 (operator, 29.10.; Kimi requirements):
        pure PUNCTUATION drift in the committed region (ground truth 07:08:
        comma disappeared) no longer invalidates the snapshot, but ONLY on a
        word-by-word, position-fixed basis (no searching); letters and case
        must match EXACTLY, no fuzzy matching, and inflection is not
        punctuation. The checkpoint text receives the committed prefix back
        character-for-character and therefore preserves it exactly; all
        downstream invariants (startswith checks, stitching invariants)
        remain unchanged. No match -> None (full buffer).
        """
        if not self._last_completed_ok or self._last_completed_nbytes < 0:
            return None
        completed = self._last_completed_text
        committed = self._committed_text
        if completed.startswith(committed):
            return _MidTailCheckpoint(
                end_nbytes=self._last_completed_nbytes, text=completed
            )
        repaired = _checkpoint_des_punktationsdrift(committed, completed)
        if repaired is None:
            return None
        log.debug(
            "mid_tail: snapshot punctuation drift repaired (%r <- %r)",
            repaired[:40], completed[:40],
        )
        return _MidTailCheckpoint(
            end_nbytes=self._last_completed_nbytes, text=repaired
        )

    def _mid_tail_window(self, end_nbytes: int) -> tuple[bytes, int]:
        """Window [checkpoint.end - context | buffer end] as a byte slice.

        The start is clamped to 0 (short buffer). Returns the slice and start
        byte (for tests/logs).
        """
        context_bytes = int(self.mid_tail_context_s * self.sample_rate * BYTES_PER_SAMPLE)
        start = max(0, end_nbytes - context_bytes)
        return bytes(self._buffer[start:]), start

    def _mid_tail_step(self) -> None:
        """One background stitching run (never types, never commits).

        (1) Create an initial checkpoint from the last live snapshot if none
        exists yet. Invalid snapshot -> no attempt.
        (2) Throttle: wait for mid_tail_step_s of new audio since the checkpoint.
        (3) Transcribe the window and stitch it to the working hypothesis with
        stitch_word_overlap; success -> new checkpoint at the buffer end
        snapshotted BEFORE the engine call. Engine exception or stitch None ->
        _mid_tail_failed=True (permanent, no abort).
        """
        checkpoint = self._mid_tail_checkpoint
        if checkpoint is None:
            checkpoint = self._mid_tail_valid_snapshot()
            if checkpoint is None:
                return
            self._mid_tail_checkpoint = checkpoint

        end_nbytes_snapshot = len(self._buffer)  # BEFORE the engine call
        new_bytes = end_nbytes_snapshot - checkpoint.end_nbytes
        if new_bytes < self.mid_tail_step_s * self.sample_rate * BYTES_PER_SAMPLE:
            return

        window, _start = self._mid_tail_window(checkpoint.end_nbytes)
        try:
            result = self.engine.transcribe(window, initial_prompt=self._committed_text)
        except Exception:  # noqa: BLE001 - background run must never abort
            self._mid_tail_failed = True
            log.debug("mid_tail: engine exception - no further attempts")
            return
        window_text = result.text.strip()
        stitched = stitch_word_overlap(
            checkpoint.text,
            window_text,
            protected_prefix=self._committed_text,
            min_anchor_words=self.mid_tail_anchor_words,
        )
        if stitched is None:
            self._mid_tail_failed = True
            log.debug("mid_tail: stitch without anchor - no further attempts")
            return
        self._mid_tail_checkpoint = _MidTailCheckpoint(
            end_nbytes=end_nbytes_snapshot, text=stitched
        )
        log.debug(
            "mid_tail: Checkpoint bei %.1fs -> %r",
            end_nbytes_snapshot / (self.sample_rate * BYTES_PER_SAMPLE),
            stitched,
        )

    def _mid_tail_finalize(self) -> TranscriptionResult | None:
        """Finalize in cached_mid_tail mode: small tail instead of full buffer.

        None = mid-tail unavailable/failed -> caller performs the previous
        full-buffer fallback.
        """
        checkpoint = self._mid_tail_checkpoint
        if self._mid_tail_failed or checkpoint is None:
            return None
        if not checkpoint.text.startswith(self._committed_text):
            return None

        end_nbytes_snapshot = len(self._buffer)
        if end_nbytes_snapshot == checkpoint.end_nbytes:
            # Buffer unchanged since checkpoint: NO engine call.
            return TranscriptionResult(
                text=checkpoint.text, committed_len=len(self._committed_text)
            )

        window, _start = self._mid_tail_window(checkpoint.end_nbytes)
        try:
            result = self.engine.transcribe(window, initial_prompt=self._committed_text)
        except Exception:  # noqa: BLE001 - caller falls back to full buffer
            log.debug("mid_tail finalize: engine exception - full-buffer fallback")
            return None
        window_text = result.text.strip()
        stitched = stitch_word_overlap(
            checkpoint.text,
            window_text,
            protected_prefix=self._committed_text,
            min_anchor_words=self.mid_tail_anchor_words,
        )
        if stitched is None:
            log.debug("mid_tail finalize: stitch failed - full-buffer fallback")
            return None
        return TranscriptionResult(
            text=stitched, committed_len=len(self._committed_text)
        )

    _SENTENCE_END = (".", "?", "!")

    def _compose(self, result: TranscriptionResult) -> TranscriptionResult:
        """Prefix closed segments to a live result (Phase 2, C)."""
        if not self._closed_text:
            return result
        return TranscriptionResult(
            text=self._closed_text + result.text,
            committed_len=len(self._closed_text) + result.committed_len,
        )

    def _finalize_compose(self, text: str) -> TranscriptionResult:
        """Final result: closed prefix is committed, the segment is open."""
        if not self._closed_text:
            return TranscriptionResult(text=text, committed_len=0)
        if not text:
            return TranscriptionResult(text=self._closed_text.rstrip(),
                                       committed_len=0)
        return TranscriptionResult(text=self._closed_text + text,
                                   committed_len=len(self._closed_text))

    def _pause_cut_due(self) -> bool:
        """Sentence pause: punctuation at the hypothesis end + silent tail."""
        if self.vad is None or not self._last_text.endswith(self._SENTENCE_END):
            return False
        if self.audio_seconds < self.segment_min_s:
            return False
        tail_s = max(1.0, self.segment_pause_s)
        n = int(tail_s * self.sample_rate) * BYTES_PER_SAMPLE
        if len(self._buffer) < n:
            return False
        try:
            return not self.vad.has_speech(_pcm_to_float(bytes(self._buffer[-n:])))
        except Exception as exc:  # noqa: BLE001 - gate is optional
            log.warning("VAD pause check failed (%s) - no segment cut", exc)
            return False

    def _quiet_cut_nbytes(self, window_s: float = 2.0, frame_s: float = 0.1) -> int:
        """Byte offset of the quietest 100 ms frame END in the last window_s.

        Falls back to the buffer end if the window is too short.
        """
        import numpy as np

        frame = int(frame_s * self.sample_rate)
        win = int(window_s * self.sample_rate)
        samples = np.frombuffer(bytes(self._buffer[: len(self._buffer) // 2 * 2]),
                                dtype=np.int16)
        if len(samples) < win + frame:
            return len(self._buffer)
        start = len(samples) - win
        best, best_rms = len(samples), None
        for pos in range(start, len(samples) - frame + 1, frame):
            rms = float(np.sqrt(np.mean(samples[pos:pos + frame].astype(np.float32) ** 2)))
            if best_rms is None or rms < best_rms:
                best, best_rms = pos + frame, rms
        return best * BYTES_PER_SAMPLE

    def _cut_segment(self, cut_nbytes: int, *, force: bool = False) -> TranscriptionResult:
        """Mini-final on buffer[:cut_nbytes]; close it if it ends a sentence.

        State is changed ONLY after the engine call succeeded (an
        InferenceCancelled or engine error leaves everything untouched).
        ``force=True`` (hard cap, block 6) closes the segment regardless of
        punctuation.
        """
        seg = bytes(self._buffer[:cut_nbytes])
        clock = self._clock or time.monotonic
        t0 = clock()
        result = self.engine.transcribe(seg, initial_prompt=self._committed_text)
        self._last_tick_s = clock() - t0
        text = result.text.strip()
        if not force and cut_nbytes == len(self._buffer) and not text.endswith(self._SENTENCE_END):
            # Not a sentence end after all: behave like a normal tick and keep
            # the finalize cache consistent (same proof as _transcribe).
            self._last_transcribed_at = self.audio_seconds
            self._has_transcribed = True
            self._last_text = text
            self._update_commit(text)
            self._last_completed_nbytes = len(seg)
            self._last_completed_text = text
            self._last_completed_ok = True
            self._completed_feed_epoch = self._feed_epoch
            return self._compose(
                TranscriptionResult(text=text, committed_len=self._committed_len)
            )
        rest = bytes(self._buffer[cut_nbytes:])
        if text:
            self._closed_text += text + " "
        self._closed_audio.extend(seg)
        self._reset_segment()
        self._buffer.extend(rest)
        self._feed_epoch += 1
        log.debug("Segment closed (%d bytes) -> %r", len(seg), text)
        return TranscriptionResult(text=self._closed_text,
                                   committed_len=len(self._closed_text))

    def _new_audio(self) -> bytes:
        """Audio since the last transcription (otherwise, the entire buffer)."""
        start_byte = int(
            self._last_transcribed_at * self.sample_rate * BYTES_PER_SAMPLE
        )
        return bytes(self._buffer[start_byte:])

    def _vad_due(self) -> bool:
        """Is another VAD run due?

        During silence, ``_last_transcribed_at`` never advances; without this
        throttle, the gate would repeatedly check the entire buffer at the
        controller interval (20 ms).
        """
        if self._vad_checked_at <= 0.0:
            return True
        return self.audio_seconds - self._vad_checked_at >= self.effective_interval_s

    def _transcribe(self) -> TranscriptionResult:
        self._last_transcribed_at = self.audio_seconds
        self._has_transcribed = True
        data = bytes(self._buffer)

        # Reset proof of completion: it is set only AFTER a successful run;
        # a tick that raises an exception must NOT mark the cache as valid.
        self._invalidate_cache()

        clock = self._clock or time.monotonic
        t0 = clock()
        result = self.engine.transcribe(data, initial_prompt=self._committed_text)
        self._last_tick_s = clock() - t0
        text = result.text.strip()
        self._last_text = text

        self._update_commit(text)

        # Set proof of completion ONLY after a successful run; if the engine
        # call raises an exception, ok=False remains (cache invalid).
        self._last_completed_nbytes = len(data)
        self._last_completed_text = text
        self._last_completed_ok = True
        self._completed_feed_epoch = self._feed_epoch

        log.debug(
            "Transcription of %.1fs audio -> %r (committed=%d)",
            self.audio_seconds, text, self._committed_len,
        )

        return TranscriptionResult(text=text, committed_len=self._committed_len)

    def _update_commit(self, text: str) -> None:
        """Extends the committed prefix once it has been stably confirmed.

        The committed prefix is the portion that was identical across the last
        ``commit_agreements`` transcriptions (LocalAgreement). It is committed
        only through the last word boundary, leaving the current word
        correctable.

        A committed prefix during streaming prevents flicker. The FINAL
        transcription, by contrast, is fully correctable (it returns
        ``committed_len=0``), allowing the model to revise its last word.
        """
        self._history.append(text)
        if len(self._history) > self.commit_agreements:
            del self._history[: len(self._history) - self.commit_agreements]

        if len(self._history) < self.commit_agreements:
            return

        # Common prefix of all hypotheses in the window.
        common_len = len(self._history[0])
        for candidate in self._history[1:]:
            common_len = _common_prefix_len(self._history[0][:common_len], candidate)
            if not common_len:
                break

        if not common_len:
            return

        common_str = self._history[0][:common_len]
        # Commit only through the last word boundary.
        cut = common_str.rfind(" ")
        if cut < 0:
            # No complete word yet -> commit nothing.
            return
        common_str = common_str[: cut + 1]

        if common_str.startswith(self._committed_text):
            self._committed_text = common_str
            self._committed_len = len(common_str)


def _completes(incomplete: str, word: str) -> bool:
    """Checks whether ``word`` continues the incomplete word ``incomplete``.

    An incomplete word must be continued with LETTERS ('de' -> 'deutlich');
    punctuation alone appended afterward ('Welt' -> 'Welt,') does not count
    as word completion (no fuzzy matching across a comma).
    """
    if len(incomplete) >= len(word):
        return False
    return word.startswith(incomplete) and word[len(incomplete)].isalnum()


def _checkpoint_des_punktationsdrift(committed: str, completed: str) -> str | None:
    """Repairs pure PUNCTUATION drift in the mid-tail snapshot (OPT2).

    The only permitted case is: the committed prefix (word-by-word, must end
    with a space) matches the first words of the live text after punctuation
    is removed from BOTH sides. Letters and case are compared EXACTLY
    (inflection and capitalization are not punctuation), and positions are
    fixed (no searching, fuzzy matching, or word reordering).

    Returns:
        The checkpoint text: committed prefix CHARACTER-FOR-CHARACTER plus
        the remaining live words (word-by-word, never mid-word), so the result
        once again satisfies startswith(committed) and all downstream
        invariants remain intact. None for any difference beyond punctuation.
    """
    import string

    if not committed or not committed.endswith(" "):
        return None
    c_words = committed.split()
    w_words = completed.split()
    if len(w_words) < len(c_words):
        return None
    strip = lambda word: "".join(
        ch for ch in word if ch not in string.punctuation
    )
    for c_word, w_word in zip(c_words, w_words):
        c_plain, w_plain = strip(c_word), strip(w_word)
        if not c_plain or not w_plain or c_plain != w_plain:
            return None
        # Only genuine punctuation drift (or identity) is permitted:
        # identical raw text was already handled by the startswith branch;
        # anything other than c_word != w_word with the same stripped form
        # would be a silent word substitution.
        if c_word != w_word and c_plain == w_plain:
            continue  # punctuation only: permitted
    rest = w_words[len(c_words):]
    return committed + " ".join(rest)


def stitch_word_overlap(
    previous: str,
    window: str,
    *,
    protected_prefix: str = "",
    min_anchor_words: int = 2,
) -> str | None:
    """Appends the window transcript ``window`` to the working hypothesis ``previous``.

    Stitching occurs ONLY at exact word anchors; if no unambiguously safe
    result exists, ``None`` is returned. Phase 1, isolated: this function is
    NOT called in the production path (test imports only).

    Rule 1 (words): contiguous non-whitespace sequences (``str.split``);
    capitalization and punctuation are compared EXACTLY -- no normalization
    or fuzzy matching. Every result is reconstructed with exactly ONE space
    between words because this is the only way to deterministically preserve
    the anchor's word boundaries.

    Candidates (rules 2, 4, 5): The window head covers a previous region
    ``P[m0:m0+t]``; every coverage position ``m0`` is checked:

    * exact: ``W[:t] == P[m0:m0+t]``; anchors are the last
      ``min_anchor_words`` words of the sequence (character-identical and
      consecutive). In the canonical case (``m0 + t == n``), the anchor is at
      the END of previous.
    * Fragment (rule 4): only the VERY FIRST window word may be a true SUFFIX
      fragment of the previous word ``P[m0]`` ('unkte' for 'Punkte'); a prefix
      fragment ('punkt' for 'Punkte') is invalid and yields no candidate.
      Previous retains the full word, and the fragment is dropped; skipped
      words are not allowed (context coverage must be contiguous).
    * Completion (rule 3, EXCEPTION without minimum anchor size): the context
      covers ``P[m0:n-1]`` exactly, and window word ``W[t]`` replaces exactly
      the LAST previous word, provided it was incomplete (a true prefix of
      ``W[t]`` continued with letters, 'de' -> 'deutlich'). Two or more
      differing old words therefore yield no candidate.

    Result per candidate (rule 5): previous through the anchor end plus the
    window after the anchor end -- exact word boundaries, exactly one space
    between words.

    Rule 6: Discard candidates that do not begin character-for-character with
    ``protected_prefix``.

    Rule 7: If exactly ONE distinct result remains among all candidates,
    return it; zero or multiple distinct results yield ``None``.

    Rule 8: ``previous == ''`` returns ``window`` (provided the
    ``protected_prefix`` requirement is satisfied; otherwise ``None``);
    ``window == ''`` returns ``previous``.
    """
    # Rule 8: Edge cases.
    if previous == "":
        return window if window.startswith(protected_prefix) else None
    if window == "":
        return previous

    prev_words: list[str] = previous.split()
    win_words: list[str] = window.split()
    n = len(prev_words)
    m = len(win_words)

    if n == 0:
        # previous consists only of whitespace: same as previous == "" (rule 8).
        joined = " ".join(win_words)
        return joined if joined.startswith(protected_prefix) else None
    if m == 0:
        # Window without words: same as window == "" (rule 8).
        return previous

    candidates: set[tuple[str, ...]] = set()

    # Exact and fragment coverage (rules 2/4/5): window head covers P[m0:...].
    for m0 in range(n):
        # Exact sequence: anchor = last >= min_anchor_words words of sequence.
        t = 0
        while t < m and m0 + t < n and win_words[t] == prev_words[m0 + t]:
            t += 1
        if t >= min_anchor_words:
            candidates.add(tuple(prev_words[: m0 + t]) + tuple(win_words[t:]))

        # Suffix fragment on the VERY FIRST window word (rule 4).
        if len(win_words[0]) < len(prev_words[m0]) and prev_words[m0].endswith(win_words[0]):
            t = 0
            while 1 + t < m and m0 + 1 + t < n and win_words[1 + t] == prev_words[m0 + 1 + t]:
                t += 1
            if 1 + t >= min_anchor_words:
                candidates.add(
                    tuple(prev_words[: m0 + 1 + t]) + tuple(win_words[1 + t :])
                )

    # Rule 3 (EXCEPTION): exactly ONE final incomplete previous word is
    # replaced by the window word ('de' -> 'deutlich').
    for m0 in range(n):
        t = n - 1 - m0  # context length: covers P[m0 : n-1] exactly.
        if t >= m:
            continue
        if win_words[:t] == prev_words[m0 : n - 1] and _completes(prev_words[-1], win_words[t]):
            candidates.add(tuple(prev_words[: n - 1]) + tuple(win_words[t:]))

    # Rule 6 (protected_prefix) + rule 7 (uniqueness).
    results = {" ".join(candidate) for candidate in candidates}
    surviving = [result for result in results if result.startswith(protected_prefix)]
    if len(surviving) == 1:
        return surviving[0]
    return None
