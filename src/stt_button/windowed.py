"""WindowedTranscriber: windowed Streaming mit dem StreamTranscriber-Interface.

Erfüllt dasselbe Interface wie ``StreamTranscriber`` (``feed`` /
``maybe_transcribe`` / ``finalize`` / ``reset``), damit der Controller den
Modus per Konfiguration tauschen kann.

Intern: gleitende Fenster [links|chunk|rechts] + Zeitstempel-Commit über
:class:`TimestampCommitter`. Kein direkter Typer-Zugriff - die Ausgabe
übernimmt weiterhin der Controller über ``DeltaTyper.update()``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

from .committer import TimestampCommitter
from .streaming_types import StreamWindowPolicy

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000


def _to_float_audio(data: bytes) -> np.ndarray:
    """s16le-Bytes robust in float32 [-1,1] wandeln (auch bei ungerader Laenge)."""
    if len(data) % 2:
        data = data[:-1]
    return np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0


@dataclass
class WindowedTranscriber:
    """Fensterbasiertes Streaming-Transkriptions-Objekt."""

    engine: object  # hat transcribe_with_timestamps + transcribe
    interval_s: float = 1.0
    min_audio_s: float = 3.0
    min_final_audio_s: float = 0.5
    #: Maximale Segmentlaenge fuer die finale Rest-Transkription (Sekunden).
    #: Lange Reste werden geschnitten, damit Token-Budget/Encoder-Speicher
    #: nicht zum Engpass werden.
    max_final_segment_s: float = 30.0
    sample_rate: int = SAMPLE_RATE
    commit_agreements: int = 2
    left_context_s: float = 4.0
    chunk_s: float = 2.0
    right_context_s: float = 1.0
    vad: object | None = None

    _buffer: bytearray = field(default_factory=bytearray, init=False, repr=False)
    _committer: TimestampCommitter = field(
        default_factory=TimestampCommitter, init=False, repr=False
    )
    _last_partial: str = field(default="", init=False, repr=False)
    _committed_text: str = field(default="", init=False, repr=False)
    _last_transcribed_audio_s: float = field(default=0.0, init=False, repr=False)
    _silence_misses: int = field(default=0, init=False, repr=False)

    # ------------------------------------------------------------------
    # Zustand
    # ------------------------------------------------------------------
    @property
    def audio_seconds(self) -> float:
        return len(self._buffer) / (self.sample_rate * 2)

    @property
    def committed_len(self) -> int:
        """Laenge des committeten Textpraefixes (fuer DeltaTyper)."""
        return len(self._committed_text)

    @property
    def committed_text(self) -> str:
        return self._committed_text

    @property
    def last_text(self) -> str:
        return self._committed_text + self._last_partial

    def _policy(self) -> StreamWindowPolicy:
        return StreamWindowPolicy(
            left_context_s=self.left_context_s,
            chunk_s=self.chunk_s,
            right_context_s=self.right_context_s,
            min_audio_s=self.min_audio_s,
        )

    # ------------------------------------------------------------------
    # Steuerung
    # ------------------------------------------------------------------
    def reset(self) -> None:
        self._buffer.clear()
        self._committer = TimestampCommitter(commit_agreements=self.commit_agreements)
        self._last_partial = ""
        self._committed_text = ""
        self._last_transcribed_audio_s = 0.0

    def feed(self, chunk: bytes) -> None:
        if chunk:
            self._buffer.extend(chunk)

    def _flush_pending_text(self) -> None:
        """Sichert den letzten Partial in den committeten Text.

        Wird vor dem Ueberspringen einer Pause aufgerufen, damit der bereits
        erkannte Text nicht beim Vorruecken von committed_s verloren geht.
        """
        if self._last_partial.strip():
            self._committed_text += self._last_partial.strip() + " "
            self._last_partial = ""

    def maybe_transcribe(self):
        """Transkribiert das nächste Fenster, wenn eines fällig ist.

        Returns:
            Ein ``TranscriptionResult`` mit dem VOLLEN Text (committed +
            partial) und ``committed_len``, oder None.
        """
        # Fix 4: interval_s durchsetzen - nicht bei jedem Tick transkribieren.
        if self._last_transcribed_audio_s > 0:
            if self.audio_seconds - self._last_transcribed_audio_s < self.interval_s:
                return None

        policy = self._policy()
        window = policy.next_window(self._committer.committed_s, self.audio_seconds)
        if window is None:
            return None

        start_byte = int(window.start * self.sample_rate * 2)
        end_byte = int(window.end * self.sample_rate * 2)
        slice_data = bytes(self._buffer[start_byte:end_byte])
        if not slice_data:
            return None

        # Fix 5: VAD nur auf das NEUE Segment (ab committed_s) pruefen,
        # nicht auf den ganzen Slice inkl. Sprach-Links-Kontext.
        if self.vad is not None:
            committed_byte = int(self._committer.committed_s * self.sample_rate * 2)
            vad_data = bytes(self._buffer[committed_byte:end_byte])
            if vad_data:
                arr = _to_float_audio(vad_data)
                if len(arr) and not self.vad.has_speech(arr):
                    # Fix A (Pause-Stall): Nach 2 aufeinanderfolgenden
                    # VAD-Misses ist das eine echte Pause -> committed_s
                    # vorruecken, sonst bleibt der Commit-Punkt ewig stehen
                    # und alles nach der Pause geht verloren.
                    self._silence_misses += 1
                    if self._silence_misses >= 2:
                        # Sicherung: nur ueberspringen, wenn im Fenster
                        # wirklich KEINE Sprache ist UND der Puffer schon
                        # weiter reicht (sonst wuerde nur "noch nicht
                        # dran"-Sprache uebersprungen).
                        self._flush_pending_text()
                        skip_to = min(
                            self._committer.committed_s + self.chunk_s,
                            self.audio_seconds,
                        )
                        # Nur vorruecken, wenn der Puffer mindestens bis
                        # Fensterende gefuellt ist (kein verfruehtes Skip).
                        if self.audio_seconds >= window.end - 0.01:
                            self._committer.skip_silence(skip_to)
                            log.info(
                                "Pause erkannt -> committed_s auf %.1fs vorgert",
                                self._committer.committed_s,
                            )
                        self._silence_misses = 0
                        log.info(
                            "Pause erkannt -> committed_s auf %.1fs vorgert",
                            self._committer.committed_s,
                        )
                    else:
                        log.debug(
                            "Fenster %.1f-%.1fs: VAD still (Miss %d)",
                            window.start, window.end, self._silence_misses,
                        )
                    return None
            # Sprache erkannt -> Miss-Zaehler zuruecksetzen
            self._silence_misses = 0

        tr = self.engine.transcribe_with_timestamps(slice_data, window.start)
        self._last_transcribed_audio_s = self.audio_seconds
        commit_text, partial = self._committer.update(tr, window.commit_horizon)
        log.debug(
            "Fenster %.1f-%.1fs -> commit=%r partial=%r",
            window.start, window.end, commit_text.strip(), partial.strip(),
        )

        if commit_text.strip():
            self._committed_text += commit_text.strip() + " "
        self._last_partial = partial.strip()

        from .asr import TranscriptionResult

        # Fix 3 (RC-3): Dem DeltaTyper den VOLLEN Text liefern (committed +
        # partial), nicht nur das Fenster-Delta. Sonst verwirft
        # DeltaTyper.update() ab dem 2. Commit alles (len(delta) < committed_len).
        committed_clean = self._committed_text.rstrip()
        full_text = (committed_clean + " " + self._last_partial).strip()
        return TranscriptionResult(
            text=full_text,
            committed_len=len(committed_clean),
        )

    def finalize(self):
        """Finaler Qualitäts-Pass: Resttranskription ab committed_s.

        Fix 1 (RC-1): Rest NUR transkribieren, wenn er lang genug ist und
        Sprache enthaelt. Sonst wuerde Stille/Rauschen (bei committed_s==0)
        blind transkribiert und halluziniert (GROSS, Semikolons).
        """
        from .asr import TranscriptionResult

        # P1-Review-Fix: Rest IMMER mit dem committeten Prefix zusammenfuehren,
        # damit der DeltaTyper den VOLLEN Text bekommt (sonst Datenverlust).
        # committed_len durchgehend = len(rstrip(praefix)).
        def _full(text: str) -> TranscriptionResult:
            committed_clean = self._committed_text.rstrip()
            full = (committed_clean + " " + text).strip() if text else committed_clean
            return TranscriptionResult(
                text=full, committed_len=len(committed_clean)
            )

        committed_byte = int(self._committer.committed_s * self.sample_rate * 2)
        rest = bytes(self._buffer[committed_byte:])
        if not rest:
            return _full(self._last_partial)

        # 1) Laengen-Guard: zu kurzer Rest wird nicht transkribiert.
        min_bytes = int(self.min_final_audio_s * self.sample_rate * 2)
        if len(rest) < min_bytes:
            log.debug("finalize: Rest %.2fs zu kurz -> uebersprungen",
                      len(rest) / (self.sample_rate * 2))
            return _full(self._last_partial)

        # 2) VAD-Guard: Nur KURZE Reste (< 2s) bei VAD-False verwerfen.
        #    Ein langer Rest ist mit hoher Wahrscheinlichkeit echte Sprache -
        #    Datenverlust ist schlimmer als Halluzinationsrisiko (RC-B1).
        if self.vad is not None and len(rest) < 2 * self.sample_rate * 2:
            arr = _to_float_audio(rest)
            if len(arr) and not self.vad.has_speech(arr):
                log.info("finalize: kurzer Rest ist Stille -> uebersprungen")
                return _full(self._last_partial)
        elif self.vad is not None and len(rest) >= 2 * self.sample_rate * 2:
            # Langer Rest: bei VAD-False trotzdem transkribieren, aber warnen.
            arr = _to_float_audio(rest)
            if len(arr) and not self.vad.has_speech(arr):
                log.warning(
                    "finalize: langer Rest (%.1fs) ist laut VAD stumm, "
                    "transkribiere trotzdem (Datenverlust vermeiden)",
                    len(rest) / (self.sample_rate * 2),
                )

        log.debug("finalize: transkribiere Rest %.2fs",
                  len(rest) / (self.sample_rate * 2))

        # RC-A2-Fix: Rest mit Links-Kontext + Zeitstempeln transkribieren statt
        # kontextlos (verhindert GROSS am Restanfang mitten im Satz).
        # Fix B: Sehr lange Reste werden in <=30s-Segmente geschnitten, damit
        # weder Token-Budget noch Encoder-Speicher zum Engpass werden.
        ctx_byte = int(self.left_context_s * self.sample_rate * 2)
        slice_start = max(0, committed_byte - ctx_byte)
        max_seg_bytes = int(self.max_final_segment_s * self.sample_rate * 2)

        try:
            texts: list[str] = []
            seg_start = slice_start
            total = len(self._buffer)
            while seg_start < total:
                seg_end = min(seg_start + max_seg_bytes, total)
                seg_data = bytes(self._buffer[seg_start:seg_end])
                offset = seg_start / (self.sample_rate * 2)
                tr = self.engine.transcribe_with_timestamps(seg_data, offset)
                rest_tokens = [
                    t for t in tr.tokens
                    if t.start >= self._committer.committed_s - 1e-3
                ]
                seg_text = "".join(t.text for t in rest_tokens).strip()
                if seg_text:
                    texts.append(seg_text)
                # naechstes Segment ohne Links-Kontext-Overlap
                seg_start = seg_end

            text = " ".join(texts).strip()
            if not text:
                # Fallback auf whole-transcribe, falls keine Timestamps kamen.
                result = self.engine.transcribe(rest)
                text = getattr(result, "text", str(result)).strip()
        except Exception as exc:  # noqa: BLE001
            log.warning("finalize: timestamp-Pfad fehlgeschlagen (%s), Fallback", exc)
            result = self.engine.transcribe(rest)
            text = getattr(result, "text", str(result)).strip()

        return _full(text)