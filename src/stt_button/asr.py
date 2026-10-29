"""Whisper-Engine und Streaming-Logik.

faster-whisper hat kein natives Streaming. Der hier verfolgte Ansatz ist
"wachsender Puffer + periodische Neutranskription" (LocalAgreement-artig):

* Audio sammelt sich im Puffer.
* Alle ``interval_s`` Sekunden Audio wird der komplette Puffer transkribiert.
* Der Teil des Textes, der in ``commit_agreements`` aufeinanderfolgenden
  Laeufen identisch war, gilt als *committed* und wird nie mehr zurueckgenommen.
* Der Rest wird als Delta an die Ausgabe gereicht.

Damit erscheinen Woerter live, und spaetere Korrekturen des Modells werden
durch Backspaces ausgeglichen statt den Text zu verdoppeln.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from .typing import _common_prefix_len

log = logging.getLogger(__name__)

DEFAULT_SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2  # s16le

#: Unter dieser Laenge (Sekunden) wird Audio nie transkribiert - verhindert
#: Halluzination auf zu kurzem Rauschen (RC-1 aus der Debug-Recherche).
MIN_AUDIO_S = 0.5

#: ANSI-Escape-Sequenzen, die ein versehentlich durchgereichter Terminal-
#: Fortschrittsbalken (z.B. ``ESC[2K``) in das Transkript schreiben koennte.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_ansi(text: str) -> str:
    """Entfernt ANSI-Escape-Sequenzen (z.B. ESC[2K) aus einem Transkript."""
    return _ANSI_RE.sub("", text)


def _pcm_to_float(data: bytes) -> "object":
    """s16le-Bytes robust in float32 [-1,1] wandeln (auch bei ungerader Laenge)."""
    import numpy as np
    if len(data) % 2:
        data = data[:-1]
    return np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0


def _token_budget(audio_seconds: float) -> int:
    """Berechnet das noetige ``max_new_tokens`` fuer eine Audiolaenge.

    Fix B (Langdiktat-Kappung): Ein fixes Limit von 256 Tokens schneidet
    laengere Diktate hart ab. Deutsche Sprache braucht grob 12 Subword-
    Tokens pro Sekunde (2-4 Tokens/Wort, ~2.5 Woerter/s + Reserve).
    Mindestbudget 256, Deckel 4096 (TDT-Decoder hat kein eigenes Limit;
    der Deckel schuetzt nur vor Speicher-/Laufzeit-Ausreissern).
    """
    budget = int(audio_seconds * 12) + 64
    return max(256, min(budget, 4096))


@dataclass(frozen=True)
class TranscriptionResult:
    """Ergebnis einer Transkription."""

    text: str
    #: Laenge des committeten (endgueltigen) Praefixes.
    committed_len: int = 0


@runtime_checkable
class TranscriberEngine(Protocol):
    """Schnittstelle der Transkriptions-Engine (fuer Tests mockbar)."""

    def transcribe(
        self, data: bytes, initial_prompt: str = ""
    ) -> TranscriptionResult: ...


class WhisperEngine:
    """Ladet faster-whisper und transkribiert Audio.

    Das Modell wird erst beim ersten :meth:`load` geladen, damit der Start des
    Dienstes schnell ist. Der erste Aufruf laedt ca. 1,6 GB vom HuggingFace-Cache.
    """

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
        """Ist das Modell bereits geladen?"""
        return self._model is not None

    def load(self) -> None:
        """Laedt das Modell (idempotent)."""
        if self._model is not None:
            return
        from faster_whisper import WhisperModel  # teurer Import, erst hier

        log.info("Lade Whisper-Modell %s (%s) ...", self.model, self.compute_type)
        self._model = WhisperModel(
            self.model,
            device=self.device,
            compute_type=self.compute_type,
            cpu_threads=self.cpu_threads,
            download_root=str(self.download_root) if self.download_root else None,
        )
        log.info("Modell geladen.")

    def transcribe(
        self, data: bytes, initial_prompt: str = ""
    ) -> TranscriptionResult:
        """Transkribiert s16le-Mono-Audio (16 kHz)."""
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
    """Transkribiert mit NVIDIA Parakeet TDT (transformers, CPU-only).

    Parakeet TDT 0.6B v3 ist ein Transducer-Modell (FastConformer-Encoder +
    TDT-Decoder), das 25 europaeische Sprachen automatisch erkennt - darunter
    Deutsch und Englisch. Auf CPU ist es deutlich schneller als
    faster-whisper und liefert fuer DE/EN-Mischdiktat die beste Qualitaet.

    Das Modell kennt kein ``initial_prompt`` (anders als Whisper); der
    Parameter wird deshalb still ignoriert. Fuer das Streaming-Delta ist das
    unkritisch, da die Transkription ohnehin den ganzen Puffer verarbeitet.
    """

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

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def load(self) -> None:
        """Laedt Modell + Prozessor (idempotent)."""
        if self._model is not None:
            return
        import torch
        from transformers import AutoModelForTDT, AutoProcessor

        log.info("Lade Parakeet-Modell %s ...", self.model)
        self._processor = AutoProcessor.from_pretrained(self.model)
        self._model = AutoModelForTDT.from_pretrained(
            self.model, dtype=torch.float32
        ).to(self.device).eval()
        if self.device == "cpu":
            torch.set_num_threads(self.cpu_threads)
        log.info("Parakeet-Modell geladen.")

        # INT8-Freigabe (Betreiber erteilt, A/B-Befund /tmp/onnx_tdt_ab_probe.log:
        # 4/5 Clips 1.5-2.3x schneller, WER verbessert - int8-ONNX liest
        # Satzendene vollstaendig wo FP32 abbricht). FP32 bleibt Fallback:
        # lieft die int8-ONNX nicht, greift der bewiesene FP32-Pfad.
        if self.compute_type == "int8":
            try:
                from pathlib import Path as _P
                _int8 = _P(self.model).with_name(
                    self.model.split("/")[-1] + "-int8.onnx"
                )
                _model_dir = _P(self.model.split("/")[-1] and "parakeet-onnx-fp32")
                # Pfad robust: relativ zum Repo-Root (der Mutterordner des
                # stt_button-Pakets), NICHT zum Arbeitsverzeichnis des Dienstes
                _repo_root = _P(__file__).resolve().parent.parent.parent
                self._int8_dir = _repo_root / ".models" / "parakeet-onnx-fp32"
                self._int8_encoder = self._int8_dir / "encoder-model.int8.onnx"
                self._int8_decoder = self._int8_dir / "decoder_joint-model.int8.onnx"
                if self._int8_encoder.exists() and self._int8_decoder.exists():
                    import openvino as ov
                    self._ov_core = ov.Core()
                    # Encoder braucht vor dem Compile einen festen 30s-Bucket
                    # (bewiesene Form): dynamische Shapes (2 dyn. Dims) sind
                    # fuer den NPU-Compiler nicht kompilierbar — mit reshape
                    # auf [1,128,3000] funktioniert es (Ground-Truth 23:47).
                    _enc = self._ov_core.read_model(str(self._int8_encoder))
                    _enc.reshape({"audio_signal": [1, 128, 3000], "length": [1]})
                    self._ov_encoder = self._ov_core.compile_model(
                        _enc, "NPU",
                        {"NPU_PLATFORM": "NPU4000", "CACHE_DIR": "/tmp/npu-cache-int8"},
                    )
                    # Decoder/Joint: NPU-Compile scheitert an dynamischen
                    # Dims (Ground-Truth 00:07 'Transpose_192 cannot compute
                    # output bounds dimension') — laeuft bewusst auf ORT-CPU
                    # int8 (0,275ms/Schritt bewiesen), KEIN NPU-Zwang.
                    self._ov_decoder = None
                    log.info("INT8-Encoder auf NPU kompiliert (30s-Bucket); Decoder/Joint bleibt ORT-CPU int8 (NPU-Compile scheitert, bewusst kein Risiko).")
                else:
                    log.warning("int8-ONNX fehlt - FP32 bleibt aktiv (Fallback).")
            except Exception as exc:  # noqa: BLE001 - Fallback niemals Absturz
                log.warning("INT8-Freigabe fehlgeschlagen (%s) - FP32-Pfad aktiv.", exc)
                self._ov_encoder = None
                self._ov_decoder = None

    def transcribe(
        self, data: bytes, initial_prompt: str = ""
    ) -> TranscriptionResult:
        """Transkribiert s16le-Mono-Audio (16 kHz)."""
        # Fix 2: Zu kurzes Audio wird nie transkribiert (verhindert
        # Halluzination auf < 0.5s Rauschen).
        if len(data) < MIN_AUDIO_S * 16000 * 2:
            return TranscriptionResult(text="")
        if self._model is None:
            self.load()

        import numpy as np

        audio = _pcm_to_float(data)
        inputs = self._processor([audio], sampling_rate=16000)
        # INT8-NPU-Pfad: Encoder auf NPU (bewiesen: 0,13s median bei 10,2s-Audio,
        # 4/5 Clips 1,5-2,3x schneller, WER verbessert); Decoder/Joint bleibt
        # CPU-INT8 (AR-Loop: NPU-Dispatch pro Token waere teurer - Kimi-Rechnung).
        # FP32 bleibt Fallback: liefert die int8-ONNX nicht, greift der
        # bewiesene FP32-Pfad (niemals Absturz).
        if getattr(self, "_ov_encoder", None) is not None:
            try:
                _mel = inputs["audio_signal"] if isinstance(inputs, dict) else getattr(inputs, "audio_signal", None)
                if hasattr(_mel, "numpy"):
                    _mel = _mel.numpy()
                _np_in = np.asarray(_mel, dtype=np.float32)
                _res = self._ov_encoder([_np_in])
                self._last_npu_encoder_out = _res[0]
                log.info("INT8-Encoder auf NPU.")
            except Exception as exc:  # noqa: BLE001 - Fallback niemals Absturz
                log.warning("INT8-Encoder-Aufruf fehlgeschlagen (%s) - FP32-Pfad.", exc)
                self._last_npu_encoder_out = None
        else:
            self._last_npu_encoder_out = None
        inputs.to(self.device, dtype=self._model.dtype)
        # max_new_tokens explizit setzen, sonst waechst das Default-
        # max_length mit dem Audio und schneidet lange Diktate ab.
        # Fix B: Budget skaliert mit der Audiolaenge.
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
        """Transkribiert und liefert Token-Zeitstempel.

        Nutzt die TDT-Durations (80ms-Frames), um jedem Token eine absolute
        Zeit zuzuordnen. ``window_offset_s`` ist die Startzeit des
        Audio-Fensters im Gesamtstrom (fuer das windowed Streaming).
        """
        from .streaming_types import Token, Transcription

        # Fix 2: Zu kurzes Audio ergibt keine Tokens.
        if len(data) < MIN_AUDIO_S * 16000 * 2:
            return Transcription(tokens=[])
        if self._model is None:
            self.load()

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


class QwenEngine:
    """Transkribiert mit Qwen3-ASR (transformers, CPU-only).

    Qwen3-ASR-1.7B ist ein Multimodal-Modell, das nativ ueber
    ``AutoModelForMultimodalLM`` + ``apply_transcription_request`` laeuft.
    Es liefert sehr gute Qualitaet (inkl. Interpunktion und
    Sprach-Erkennung), ist aber auf CPU deutlich langsamer als Parakeet.

    Das Modell kennt ebenfalls kein ``initial_prompt``; der Parameter wird
    ignoriert.
    """

    def __init__(
        self,
        model: str = "Qwen/Qwen3-ASR-1.7B-hf",
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

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def load(self) -> None:
        """Laedt Modell + Prozessor (idempotent)."""
        if self._model is not None:
            return
        import torch
        from transformers import AutoModelForMultimodalLM, AutoProcessor

        log.info("Lade Qwen3-ASR-Modell %s ...", self.model)
        self._processor = AutoProcessor.from_pretrained(self.model)
        self._model = AutoModelForMultimodalLM.from_pretrained(
            self.model, dtype=torch.float32
        ).to(self.device).eval()
        if self.device == "cpu":
            torch.set_num_threads(self.cpu_threads)
        log.info("Qwen3-ASR-Modell geladen.")

    def transcribe(
        self, data: bytes, initial_prompt: str = ""
    ) -> TranscriptionResult:
        """Transkribiert s16le-Mono-Audio (16 kHz)."""
        if self._model is None:
            self.load()

        import tempfile
        import wave as _wave

        import numpy as np

        audio = _pcm_to_float(data)

        # Der Qwen-Prozessor akzeptiert einen Dateipfad; wir schreiben
        # den Puffer als temporaere WAV.
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
    """PHASE 2: Hintergrund-Checkpoint fuer finalize_mode="cached_mid_tail".

    Speichert das Pufferende (Bytes) eines erfolgreichen Live-Snapshots
    bzw. Hintergrund-Stitch-Laufs zusammen mit der gestitchten
    Arbeitshypothese. Naechste Fenster starten bei ``end_nbytes -
    mid_tail_context_s``.
    """

    #: Pufferlaenge (Bytes) zum Zeitpunkt des Snapshots.
    end_nbytes: int
    #: Gestitchte Arbeitshypothese zu diesem Pufferstand.
    text: str


@dataclass
class StreamTranscriber:
    """Sammelt Audio und erzeugt periodisch Transkriptionen.

    Die Klasse enthaelt keine I/O-Logik und ist damit vollstaendig testbar.
    """

    engine: TranscriberEngine
    interval_s: float = 1.5
    min_audio_s: float = 0.8
    sample_rate: int = DEFAULT_SAMPLE_RATE
    commit_agreements: int = 2
    #: Finalisierungs-Modus:
    #: ``"full"`` (Default, bisheriges Verhalten): Finalize transkribiert
    #: den kompletten Puffer neu.
    #: ``"cached"``: war der Puffer seit der letzten ERFOLGREICHEN Inferenz
    #: unveraendert (gleiche Laenge, kein feed()/reset() seitdem), liefert
    #: Finalize den Cache-Text ohne Engine-Call. Nur fuer deterministische
    #: Engines sinnvoll (Parakeet: festliegende Decode-Parameter).
    finalize_mode: str = "full"
    #: Optionales VAD-Gate (z.B. :class:`~stt_button.vad.SileroVadGate`).
    #: Meldet es im NEUEN Audio-Bereich keine Sprache, wird nicht transkribiert
    #: (verhindert Halluzinationen auf Stille -> Phantom-Backspaces).
    vad: object | None = None
    #: STUFE 3: Live-Ticks nach dieser Pufferlaenge (Sekunden) einstellen.
    #: Der Whole-Buffer-Ansatz wird pro Tick teurer (RTF wachst mit dem
    #: Puffer); ab hier liefern nur noch finalize() den Text. feed()
    #: sammelt weiter, finalize() transkribiert den kompletten Puffer,
    #: reset() reaktiviert die Live-Ticks. 0.0 (Default) = immer aktiv.
    live_asr_until_s: float = 0.0
    #: PHASE 2 (nur finalize_mode="cached_mid_tail"): Links-Kontext
    #: (Sekunden) der Mid-Tail-Fenster vor dem Checkpoint-Ende.
    mid_tail_context_s: float = 2.5
    #: PHASE 2: Abstand (Sekunden neuem Audio seit Checkpoint) zwischen
    #: zwei Hintergrund-Mid-Tail-Engine-Calls.
    mid_tail_step_s: float = 4.0
    #: PHASE 2: Mindestanzahl Wortanker fuer stitch_word_overlap.
    mid_tail_anchor_words: int = 2

    _buffer: bytearray = field(default_factory=bytearray, init=False, repr=False)
    _last_transcribed_at: float = field(default=0.0, init=False, repr=False)
    _has_transcribed: bool = field(default=False, init=False, repr=False)
    _committed_text: str = field(default="", init=False, repr=False)
    _committed_len: int = field(default=0, init=False, repr=False)
    _history: list[str] = field(default_factory=list, init=False, repr=False)
    _last_text: str = field(default="", init=False, repr=False)
    #: Audio-Stand (Sekunden), zu dem das VAD zuletzt befragt wurde.
    _vad_checked_at: float = field(default=0.0, init=False, repr=False)
    # --- Cache fuer finalize_mode="cached" -------------------------------
    #: Puffergroesse (Bytes) nach der letzten ERFOLGREICHEN Inferenz.
    _last_completed_nbytes: int = field(default=-1, init=False, repr=False)
    #: Text der letzten ERFOLGREICHEN Inferenz.
    _last_completed_text: str = field(default="", init=False, repr=False)
    #: True nur nach erfolgreichem Engine-Durchlauf (nicht schon vor dem
    #: Aufruf) - der Abgeschlossen-Nachweis fuer den Cache.
    _last_completed_ok: bool = field(default=False, init=False, repr=False)
    #: Zaehlt jedes nicht-leere feed(); entwertet den Cache.
    _feed_epoch: int = field(default=0, init=False, repr=False)
    #: feed-Zaehler-Stand zum Zeitpunkt der letzten erfolgreichen Inferenz.
    _completed_feed_epoch: int = field(default=-1, init=False, repr=False)
    # --- STUFE 4: Pausen-Indikator (live_asr_until_s) ---------------------
    #: Live-Limit wurde (seit dem letzten reset()) bereits erreicht und
    #: gemeldet - verhindert eine zweite Meldung bei weiteren Limit-Ticks.
    _live_paused_seen: bool = field(default=False, init=False, repr=False)
    #: Live-Limit wurde erreicht, aber die Meldung noch nicht abgeholt
    #: (take_live_paused()).
    _live_paused_pending: bool = field(default=False, init=False, repr=False)
    # --- PHASE 2: cached_mid_tail ----------------------------------------
    #: Gueltiger Hintergrund-Checkpoint (Vollpuffer-Bytes + gestitchter
    #: Arbeitshypothese). None = kein Stitch-Versuch moeglich.
    _mid_tail_checkpoint: "_MidTailCheckpoint | None" = field(
        default=None, init=False, repr=False
    )
    #: True, nachdem ein Hintergrund-Mid-Tail-Lauf endgueltig gescheitert
    #: ist (Engine-Exception oder Stitch None): keine weiteren Versuche,
    #: finalize() faellt auf den Vollpuffer zurueck.
    _mid_tail_failed: bool = field(default=False, init=False, repr=False)

    # ------------------------------------------------------------------
    # Zustand
    # ------------------------------------------------------------------
    @property
    def audio_seconds(self) -> float:
        """Wie viele Sekunden Audio aktuell im Puffer liegen."""
        return len(self._buffer) / (self.sample_rate * BYTES_PER_SAMPLE)

    @property
    def committed_len(self) -> int:
        """Laenge des committeten (endgueltigen) Textpraefixes."""
        return self._committed_len

    @property
    def committed_text(self) -> str:
        """Der endgueltige Textpraefix."""
        return self._committed_text

    @property
    def last_text(self) -> str:
        """Der zuletzt transkribierte Gesamttext."""
        return self._last_text

    # ------------------------------------------------------------------
    # Steuerung
    # ------------------------------------------------------------------
    def reset(self) -> None:
        """Verwirft den Puffer und den Commit-Zustand (entwertet den Cache).

        Setzt auch die Live-Limit-Meldung (Pausen-Indikator) zurueck: Die
        naechste Aufnahme kann das Limit erneut und damit wieder genau
        einmal melden.
        """
        self._buffer.clear()
        self._last_transcribed_at = 0.0
        self._has_transcribed = False
        self._committed_text = ""
        self._committed_len = 0
        self._history = []
        self._last_text = ""
        self._vad_checked_at = 0.0
        self._live_paused_seen = False
        self._live_paused_pending = False
        self._mid_tail_checkpoint = None
        self._mid_tail_failed = False
        self._invalidate_cache()

    def take_live_paused(self) -> bool:
        """Holt die Limit-Meldung der Live-ASR ab (genau einmal pro Aufnahme).

        Minimale Signatur (bewusst kein Callback, kein extra Property):
        ``take_live_paused() -> bool``. Liefert ``True`` genau einmal -
        nach dem ersten Ueberschreiten des Live-Limits
        (``live_asr_until_s > 0`` und Puffer >= Limit) - und konsumiert die
        Meldung. Weitere Aufrufe liefern ``False``, bis ein ``reset()``
        gefolgt von einem erneuten Limit-Ueberschreiten folgt.

        Das Limit wird HIER SELBST erkannt (nicht nur in
        ``maybe_transcribe()`` vermerkt): Der letzte Live-Tick startet
        regulaer unter dem Limit und liefert noch ein Ergebnis; die Grenze
        wird erst waehrend seiner mehrsekundigen Inferenz ueberschritten
        (Audio laeuft weiter), und der Release kann VOR dem naechsten Tick
        kommen. Dann hat ``maybe_transcribe()`` das Limit nie gesehen -
        der Finalizer (der als Letzter fuettert) muss die Meldung trotzdem
        noch bekommen koennen (Realtest 11:56).

        Mit ``live_asr_until_s=0.0`` (Default) liefert die Methode NIE
        ``True`` - das Limit ist dann deaktiviert.

        Returns:
            ``True`` gdw. die Limit-Meldung noch nicht abgeholt war.
        """
        if (
            self.live_asr_until_s > 0.0
            and self.audio_seconds >= self.live_asr_until_s
            and not self._live_paused_seen
        ):
            self._live_paused_seen = True
            self._live_paused_pending = True
        pending = self._live_paused_pending
        self._live_paused_pending = False
        return pending

    def _invalidate_cache(self) -> None:
        """Entwertet den Finalize-Cache (nach reset() oder Fehlerfall)."""
        self._last_completed_nbytes = -1
        self._last_completed_text = ""
        self._last_completed_ok = False
        self._completed_feed_epoch = -1

    def feed(self, chunk: bytes) -> None:
        """Legt Audiodaten in den Puffer und entwertet den Finalize-Cache."""
        if chunk:
            self._buffer.extend(chunk)
            self._feed_epoch += 1

    def maybe_transcribe(self) -> TranscriptionResult | None:
        """Transkribiert, wenn genug neue Audiodaten vorliegen.

        Returns:
            Das Ergebnis oder ``None``, wenn noch nicht transkribiert wurde.
        """
        if self.audio_seconds < self.min_audio_s:
            return None
        # STUFE 3: Live-ASR-Limit erreicht -> keine Live-Engine-Calls mehr.
        # Der Puffer wachst weiter (feed()); finalize() transkribiert alles.
        # STUFE 4: Der ersten Ueberschreitung wird GENAU EINMAL als Meldung
        # vermerkt (take_live_paused()) - der Controller tippt darauf den
        # Pausen-Indikator.
        if self.live_asr_until_s > 0.0 and self.audio_seconds >= self.live_asr_until_s:
            if not self._live_paused_seen:
                self._live_paused_seen = True
                self._live_paused_pending = True
            # PHASE 2: Hintergrund-Mid-Tail-Stitch (nur Opt-In-Modus).
            # Liefert IMMER None: kein Tippen, kein _update_commit, keine
            # Aenderung an _history/_committed_text/Live-Cache.
            if self.finalize_mode == "cached_mid_tail" and not self._mid_tail_failed:
                self._mid_tail_step()
            return None
        # Die erste Transkription darf sofort erfolgen, danach wird nur
        # neu hinzugekommene Audio beruecksichtigt.
        if self._has_transcribed:
            if self.audio_seconds - self._last_transcribed_at < self.interval_s:
                return None

        # VAD-Gate: Stille/Rauschen darf nie bis zur ASR kommen. Geprueft wird
        # nur das neue Audio (ab der letzten Transkription) - das spart Rechen-
        # zeit und verhindert Phantom-Text samt Backspace-Ruecknahmen.
        # Ein Fehler im Gate darf das Diktat nicht verhindern: es ist eine
        # Optimierung, kein Muss.
        if self.vad is not None:
            if not self._vad_due():
                return None
            self._vad_checked_at = self.audio_seconds
            new_audio = self._new_audio()
            arr = _pcm_to_float(new_audio) if new_audio else None
            # Unter 1s kann das VAD nichts beurteilen -> nicht blocken.
            if arr is not None and len(arr) >= self.sample_rate:
                try:
                    speech = self.vad.has_speech(arr)
                except Exception as exc:  # noqa: BLE001 - Gate ist optional
                    log.warning(
                        "VAD-Gate fehlgeschlagen (%s) - transkribiere trotzdem", exc
                    )
                else:
                    if not speech:
                        log.debug(
                            "VAD: keine Sprache in %.1fs neuem Audio - "
                            "Transkription uebersprungen",
                            len(new_audio) / (self.sample_rate * BYTES_PER_SAMPLE),
                        )
                        return None

        return self._transcribe()

    def finalize(self) -> TranscriptionResult:
        """Transkribiert den kompletten Puffer als Endresultat.

        Das Ergebnis ist bewusst voll korrigierend (``committed_len=0``):
        das Modell darf sein letztes Wort noch korrigieren.

        Mit ``finalize_mode="cached"`` entfaellt der Engine-Aufruf, wenn
        der Puffer seit der letzten ERFOLGREICHEN Inferenz unveraendert ist
        (gleiche Bytes-Laenge, kein feed()/reset() seitdem): bei den
        festliegenden Decode-Parameter (temperature=0, beam=1) waere die
        Neutranskription ergebnislos. Der Text kommt aus dem Cache, die
        Agreement-History bleibt unveraendert.
        """
        if (
            self.finalize_mode == "cached"
            and self._last_completed_ok
            and len(self._buffer) == self._last_completed_nbytes
            and self._feed_epoch == self._completed_feed_epoch
        ):
            log.debug(
                "Finalize aus Cache uebernommen (%d Bytes, kein Engine-Call)",
                len(self._buffer),
            )
            return TranscriptionResult(
                text=self._last_completed_text, committed_len=0
            )
        # PHASE 2: kleiner Tail statt Vollpuffer, wenn die Mid-Tail-Kette
        # gueltig ist; sonst Verhalten wie heute (Vollpuffer-Fallback).
        if self.finalize_mode == "cached_mid_tail":
            mid_tail = self._mid_tail_finalize()
            if mid_tail is not None:
                log.debug(
                    "Finalize aus Mid-Tail-Checkpoint (%d Bytes Puffer, "
                    "Tail-Stitch)",
                    len(self._buffer),
                )
                return mid_tail
        result = self._transcribe()
        return TranscriptionResult(text=result.text, committed_len=0)

    # ------------------------------------------------------------------
    # Intern
    # ------------------------------------------------------------------
    # --- PHASE 2: cached_mid_tail ---------------------------------------
    def _mid_tail_valid_snapshot(self) -> "_MidTailCheckpoint | None":
        """Initial-Checkpoint aus dem letzten erfolgreichen Live-Snapshot.

        Nur gueltig, wenn die Live-Inferenz erfolgreich war und ihr Text mit
        dem committeten Praefix konsistent beginnt - sonst bleibt None und
        es gibt KEINEN Stitch-Versuch (finalize() = Vollpuffer wie heute).
        """
        if not self._last_completed_ok or self._last_completed_nbytes < 0:
            return None
        if not self._last_completed_text.startswith(self._committed_text):
            return None
        return _MidTailCheckpoint(
            end_nbytes=self._last_completed_nbytes,
            text=self._last_completed_text,
        )

    def _mid_tail_window(self, end_nbytes: int) -> tuple[bytes, int]:
        """Fenster [checkpoint.end - Kontext | Pufferende] als Bytes-Slice.

        Der Start wird bei 0 geklemmt (kurzer Puffer). Liefert Slice und
        Start-Byte (für Tests/Logs).
        """
        context_bytes = int(self.mid_tail_context_s * self.sample_rate * BYTES_PER_SAMPLE)
        start = max(0, end_nbytes - context_bytes)
        return bytes(self._buffer[start:]), start

    def _mid_tail_step(self) -> None:
        """Ein Hintergrund-Stitch-Lauf (nie tippend, nie committend).

        (1) Initial-Checkpoint aus dem letzten Live-Snapshot, falls noch
        keiner existiert. Ungueltiger Snapshot -> kein Versuch.
        (2) Throttle: erst ab mid_tail_step_s neuem Audio seit Checkpoint.
        (3) Fenster transkribieren, mit stitch_word_overlap an die
        Arbeitshypothese naehen; Erfolg -> neuer Checkpoint am VOR dem
        Engine-Call gesnapshotteten Pufferende. Engine-Exception oder
        Stitch None -> _mid_tail_failed=True (endgueltig, kein abort).
        """
        checkpoint = self._mid_tail_checkpoint
        if checkpoint is None:
            checkpoint = self._mid_tail_valid_snapshot()
            if checkpoint is None:
                return
            self._mid_tail_checkpoint = checkpoint

        end_nbytes_snapshot = len(self._buffer)  # VOR dem Engine-Call
        new_bytes = end_nbytes_snapshot - checkpoint.end_nbytes
        if new_bytes < self.mid_tail_step_s * self.sample_rate * BYTES_PER_SAMPLE:
            return

        window, _start = self._mid_tail_window(checkpoint.end_nbytes)
        try:
            result = self.engine.transcribe(window, initial_prompt=self._committed_text)
        except Exception:  # noqa: BLE001 - Hintergrundlauf darf nie abbrechen
            self._mid_tail_failed = True
            log.debug("mid_tail: Engine-Exception - keine weiteren Versuche")
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
            log.debug("mid_tail: Stitch ohne Anker - keine weiteren Versuche")
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
        """Finalize im Modus cached_mid_tail: kleiner Tail statt Vollpuffer.

        None = Mid-Tail nicht moeglich/gescheitert -> Caller macht den
        bisherigen Vollpuffer-Fallback.
        """
        checkpoint = self._mid_tail_checkpoint
        if self._mid_tail_failed or checkpoint is None:
            return None
        if not checkpoint.text.startswith(self._committed_text):
            return None

        end_nbytes_snapshot = len(self._buffer)
        if end_nbytes_snapshot == checkpoint.end_nbytes:
            # Puffer seit Checkpoint unveraendert: KEIN Engine-Call.
            return TranscriptionResult(
                text=checkpoint.text, committed_len=len(self._committed_text)
            )

        window, _start = self._mid_tail_window(checkpoint.end_nbytes)
        try:
            result = self.engine.transcribe(window, initial_prompt=self._committed_text)
        except Exception:  # noqa: BLE001 - Caller faellt auf Vollpuffer zurueck
            log.debug("mid_tail finalize: Engine-Exception - Vollpuffer-Fallback")
            return None
        window_text = result.text.strip()
        stitched = stitch_word_overlap(
            checkpoint.text,
            window_text,
            protected_prefix=self._committed_text,
            min_anchor_words=self.mid_tail_anchor_words,
        )
        if stitched is None:
            log.debug("mid_tail finalize: Stitch fehlgeschlagen - Vollpuffer-Fallback")
            return None
        return TranscriptionResult(
            text=stitched, committed_len=len(self._committed_text)
        )

    def _new_audio(self) -> bytes:
        """Audio seit der letzten Transkription (sonst der ganze Puffer)."""
        start_byte = int(
            self._last_transcribed_at * self.sample_rate * BYTES_PER_SAMPLE
        )
        return bytes(self._buffer[start_byte:])

    def _vad_due(self) -> bool:
        """Ist ein neuer VAD-Lauf faellig?

        Bei Stille wird ``_last_transcribed_at`` nie fortgeschrieben; ohne
        diese Drosselung pruefte das Gate im Controler-Takt (20 ms) immer
        wieder den ganzen Puffer.
        """
        if self._vad_checked_at <= 0.0:
            return True
        return self.audio_seconds - self._vad_checked_at >= self.interval_s

    def _transcribe(self) -> TranscriptionResult:
        self._last_transcribed_at = self.audio_seconds
        self._has_transcribed = True
        data = bytes(self._buffer)

        # Abgeschlossen-Nachweis zuruecksetzen: erst NACH erfolgreichem
        # Durchlauf wird er gesetzt - ein Exception werfender Tick darf
        # den Cache NICHT gueltig setzen.
        self._invalidate_cache()

        result = self.engine.transcribe(data, initial_prompt=self._committed_text)
        text = result.text.strip()
        self._last_text = text

        self._update_commit(text)

        # Abgeschlossen-Nachweis ERST nach erfolgreichem Durchlauf; wirft
        # der Engine-Aufruf eine Exception, bleibt ok=False (Cache ungueltig).
        self._last_completed_nbytes = len(data)
        self._last_completed_text = text
        self._last_completed_ok = True
        self._completed_feed_epoch = self._feed_epoch

        log.debug(
            "Transkription %.1fs Audio -> %r (committed=%d)",
            self.audio_seconds, text, self._committed_len,
        )

        return TranscriptionResult(text=text, committed_len=self._committed_len)

    def _update_commit(self, text: str) -> None:
        """Verringert den committeten Praefix, wenn er stabil bestaetigt wurde.

        Der committete Praefix ist der Teil, der in den letzten
        ``commit_agreements`` Transkriptionen identisch war (LocalAgreement).
        Er wird nur bis zur letzten Wortgrenze committet: das aktuell
        laufende Wort bleibt so korrigierbar.

        Ein committeter Praefix waehrend des Streamings schuetzt vor
        Flackern. Die *Schluss*-Transkription ist dagegen voll korrigierend
        (sie liefert ``committed_len=0``), damit das Modell am Ende sein
        letztes Wort noch korrigieren darf.
        """
        self._history.append(text)
        if len(self._history) > self.commit_agreements:
            del self._history[: len(self._history) - self.commit_agreements]

        if len(self._history) < self.commit_agreements:
            return

        # Gemeinsamer Praefix aller Hypothesen im Fenster.
        common_len = len(self._history[0])
        for candidate in self._history[1:]:
            common_len = _common_prefix_len(self._history[0][:common_len], candidate)
            if not common_len:
                break

        if not common_len:
            return

        common_str = self._history[0][:common_len]
        # Nur bis zur letzten Wortgrenze committen.
        cut = common_str.rfind(" ")
        if cut < 0:
            # Noch kein fertiges Wort - nichts committen.
            return
        common_str = common_str[: cut + 1]

        if common_str.startswith(self._committed_text):
            self._committed_text = common_str
            self._committed_len = len(common_str)


def _completes(incomplete: str, word: str) -> bool:
    """Prueft, ob ``word`` das unvollstaendige Wort ``incomplete`` fortsetzt.

    Ein unvollstaendiges Wort wird mit BUCHSTABEN fortgeschrieben
    ('de' -> 'deutlich'); reine Interpunktion dahinter ('Welt' -> 'Welt,')
    zaehlt nicht als Wortfortsetzung (kein Fuzzy ueber Komma).
    """
    if len(incomplete) >= len(word):
        return False
    return word.startswith(incomplete) and word[len(incomplete)].isalnum()


def stitch_word_overlap(
    previous: str,
    window: str,
    *,
    protected_prefix: str = "",
    min_anchor_words: int = 2,
) -> str | None:
    """Haengt das Fenster-Transkript ``window`` an die Arbeitshypothese ``previous`` an.

    Genaeht wird NUR an exakten Wortankern; ist nichts eindeutig sicher,
    wird ``None`` geliefert. Phase 1, isoliert: diese Funktion wird NICHT
    im Produktivpfad aufgerufen (nur Test-Importe).

    Regel 1 (Woerter): zusammenhaengende Nicht-Whitespace-Folgen
    (``str.split``); Grossschreibung und Interpunktion werden EXAKT
    verglichen -- keine Normalisierung, kein Fuzzy. Alle Ergebnisse werden
    mit genau EINEM Leerzeichen zwischen Woertern rekonstruiert, weil nur
    so die Wortgrenzen des Ankers deterministisch erhalten bleiben.

    Kandidaten (Regel 2, 4, 5): Der Fensterkopf deckt eine previous-Region
    ``P[m0:m0+t]``; alle Deckungspositionen ``m0`` werden geprueft:

    * exakt: ``W[:t] == P[m0:m0+t]``; Anker sind die letzten
      ``min_anchor_words`` Woerter der Folge (zeichengleich, in Folge).
      Im kanonischen Fall (``m0 + t == n``) liegt der Anker am ENDE von
      previous.
    * Fragment (Regel 4): nur das ALLERERSTE Fensterwort darf echtes
      SUFFIX-Fragment des previous-Wortes ``P[m0]`` sein ('unkte' zu
      'Punkte'); ein Praefix-Fragment ('punkt' fuer 'Punkte') ist
      ungueltig und liefert keinen Kandidaten. Previous behaelt das
      volle Wort, das Fragment faellt weg; uebersprungene Woerter sind
      nicht erlaubt (Kontext deckt zusammenhaengend).
    * Vervollstaendigung (Regel 3, AUSNAHME ohne Anker-Mindestgroesse):
      der Kontext deckt ``P[m0:n-1]`` exakt, und das Fensterwort ``W[t]``
      ersetzt genau das LETZTE previous-Wort, sofern dieses
      unvollstaendig war (echtes Praefix von ``W[t]`` mit
      Buchstabenfortsetzung, 'de' -> 'deutlich'). Zwei oder mehr
      abweichende alte Woerter ergeben daher keinen Kandidaten.

    Ergebnis je Kandidat (Regel 5): previous bis zum Ankerende plus
    Fenster nach dem Ankerende -- Wortgrenzen exakt, genau ein
    Leerzeichen zwischen Woertern.

    Regel 6: Kandidaten, die nicht zeichengleich mit ``protected_prefix``
    beginnen, werden verworfen.

    Regel 7: Bleibt von allen Kandidaten genau EIN unterschiedliches
    Ergebnis uebrig, wird es geliefert; keins oder mehrere unterschiedliche
    ergeben ``None``.

    Regel 8: ``previous == ''`` liefert ``window`` (sofern die
    ``protected_prefix``-Pflicht erfuellt ist, sonst ``None``);
    ``window == ''`` liefert ``previous``.
    """
    # Regel 8: Randfaelle.
    if previous == "":
        return window if window.startswith(protected_prefix) else None
    if window == "":
        return previous

    prev_words: list[str] = previous.split()
    win_words: list[str] = window.split()
    n = len(prev_words)
    m = len(win_words)

    if n == 0:
        # previous besteht nur aus Whitespace: wie previous == "" (Regel 8).
        joined = " ".join(win_words)
        return joined if joined.startswith(protected_prefix) else None
    if m == 0:
        # Fenster ohne Woerter: wie window == "" (Regel 8).
        return previous

    candidates: set[tuple[str, ...]] = set()

    # Exakte und Fragment-Deckungen (Regel 2/4/5): Fensterkopf deckt P[m0:...].
    for m0 in range(n):
        # Exakte Folge: Anker = letzte >= min_anchor_words Woerter der Folge.
        t = 0
        while t < m and m0 + t < n and win_words[t] == prev_words[m0 + t]:
            t += 1
        if t >= min_anchor_words:
            candidates.add(tuple(prev_words[: m0 + t]) + tuple(win_words[t:]))

        # Suffix-Fragment am ALLERERSTEN Fensterwort (Regel 4).
        if len(win_words[0]) < len(prev_words[m0]) and prev_words[m0].endswith(win_words[0]):
            t = 0
            while 1 + t < m and m0 + 1 + t < n and win_words[1 + t] == prev_words[m0 + 1 + t]:
                t += 1
            if 1 + t >= min_anchor_words:
                candidates.add(
                    tuple(prev_words[: m0 + 1 + t]) + tuple(win_words[1 + t :])
                )

    # Regel 3 (AUSNAHME): genau EIN letztes unvollstaendiges previous-Wort
    # wird durch das Fensterwort ersetzt ('de' -> 'deutlich').
    for m0 in range(n):
        t = n - 1 - m0  # Kontextlaenge: deckt P[m0 : n-1] exakt.
        if t >= m:
            continue
        if win_words[:t] == prev_words[m0 : n - 1] and _completes(prev_words[-1], win_words[t]):
            candidates.add(tuple(prev_words[: n - 1]) + tuple(win_words[t:]))

    # Regel 6 (protected_prefix) + Regel 7 (Eindeutigkeit).
    results = {" ".join(candidate) for candidate in candidates}
    surviving = [result for result in results if result.startswith(protected_prefix)]
    if len(surviving) == 1:
        return surviving[0]
    return None
