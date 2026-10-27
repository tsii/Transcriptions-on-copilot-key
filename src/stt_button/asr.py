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
    #: Optionales VAD-Gate (z.B. :class:`~stt_button.vad.SileroVadGate`).
    #: Meldet es im NEUEN Audio-Bereich keine Sprache, wird nicht transkribiert
    #: (verhindert Halluzinationen auf Stille -> Phantom-Backspaces).
    vad: object | None = None

    _buffer: bytearray = field(default_factory=bytearray, init=False, repr=False)
    _last_transcribed_at: float = field(default=0.0, init=False, repr=False)
    _has_transcribed: bool = field(default=False, init=False, repr=False)
    _committed_text: str = field(default="", init=False, repr=False)
    _committed_len: int = field(default=0, init=False, repr=False)
    _history: list[str] = field(default_factory=list, init=False, repr=False)
    _last_text: str = field(default="", init=False, repr=False)
    #: Audio-Stand (Sekunden), zu dem das VAD zuletzt befragt wurde.
    _vad_checked_at: float = field(default=0.0, init=False, repr=False)

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
        """Verwirft den Puffer und den Commit-Zustand."""
        self._buffer.clear()
        self._last_transcribed_at = 0.0
        self._has_transcribed = False
        self._committed_text = ""
        self._committed_len = 0
        self._history = []
        self._last_text = ""
        self._vad_checked_at = 0.0

    def feed(self, chunk: bytes) -> None:
        """Legt Audiodaten in den Puffer."""
        if chunk:
            self._buffer.extend(chunk)

    def maybe_transcribe(self) -> TranscriptionResult | None:
        """Transkribiert, wenn genug neue Audiodaten vorliegen.

        Returns:
            Das Ergebnis oder ``None``, wenn noch nicht transkribiert wurde.
        """
        if self.audio_seconds < self.min_audio_s:
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
        """
        result = self._transcribe()
        return TranscriptionResult(text=result.text, committed_len=0)

    # ------------------------------------------------------------------
    # Intern
    # ------------------------------------------------------------------
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

        result = self.engine.transcribe(data, initial_prompt=self._committed_text)
        text = result.text.strip()
        self._last_text = text

        self._update_commit(text)

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
