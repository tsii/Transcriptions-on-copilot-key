"""BufferedTranscriber: Hybrid aus Volllpuffer (Phase A) und bounded Fenstern (Phase B).

STUFE 3 des Latenz-/Qualitaetsplans. Erfüllt dasselbe Interface wie
``StreamTranscriber`` / ``WindowedTranscriber`` (``feed`` /
``maybe_transcribe`` / ``finalize`` / ``reset``), damit der Controller den
Modus per Konfiguration tauschen kann.

Strategie
---------
* **Phase A** (Audio <= ``whole_buffer_until_s``, Default 8s): der ganze
  Puffer wird transkribiert - wie beim bewaehrten ``StreamTranscriber``, aber
  bereits MIT Zeitstempeln und einem einzigen
  :class:`~stt_button.committer.TimestampCommitter`
  (``require_agreement_always=True``). Ein Committer, kein zweites Seeding:
  von Anfang an gilt "zwei Identische am selben committed_s", damit eine fruehe
  Halluzination nie eingefroren wird.
* **Phase B** (> ``whole_buffer_until_s``): pro Tick genau EIN fest begrenztes
  Fenster ``[committed_s - left_context_s | chunk_s | right_context_s]`` mit
  Commit-Horizont bei ``committed_s + chunk_s``. Zustandlos: keine
  Decoder-/Encoder-Zustaende ueber Fenstergrenzen, jedes Fenster wird
  unabhaengig dekodiert. Ueberlappende Tokens werden ueber ihre ABSOLUTEN
  Zeitstempel entdoppelt (der Committer verwirft alles vor ``committed_s``).
  Das Fenster ist hart auf ``left + chunk + right`` gedeckelt; bleibt der
  Commit-Punkt an einer Pause stehen, sorgt ``skip_silence`` fuer Fortschritt.
* **finalize()**: keine neuen Bytes -> ``committed + last_partial`` ohne
  Engine-Call. Neue Bytes -> nur der uncommitted Tail plus Links-Kontext in
  begrenzten Fenstern (``max_final_window_s``); NIEMALS der Vollpuffer.

Der Controller bekommt weiter den VOLLEN Text (committed + partial) samt
``committed_len``, genau wie beim ``WindowedTranscriber``.
"""
from __future__ import annotations

import logging
import numpy as np

from .committer import TimestampCommitter
from .streaming_types import StreamWindow

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000


def _to_float_audio(data: bytes) -> np.ndarray:
    """s16le-Bytes robust in float32 [-1,1] wandeln (auch bei ungerader Laenge)."""
    if len(data) % 2:
        data = data[:-1]
    return np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0


class BufferedTranscriber:
    """Hybride Puffer-/Fenster-Transkription mit Zeitstempel-Commit."""

    def __init__(
        self,
        engine: object,
        *,
        interval_s: float = 1.0,
        min_audio_s: float = 3.0,
        whole_buffer_until_s: float = 8.0,
        left_context_s: float = 8.0,
        chunk_s: float = 2.0,
        right_context_s: float = 2.0,
        commit_agreements: int = 2,
        max_final_window_s: float = 18.0,
        sample_rate: int = SAMPLE_RATE,
        vad: object | None = None,
    ) -> None:
        #: Engine mit transcribe_with_timestamps (+ transcribe als Fallback)
        self.engine = engine
        self.interval_s = interval_s
        self.min_audio_s = min_audio_s
        #: Bis zu dieser Pufferlaenge wird der ganze Puffer transkribiert (Phase A).
        self.whole_buffer_until_s = whole_buffer_until_s
        #: Links-Kontext des Phase-B-Fensters (Qualitaetsanker vor committed_s).
        self.left_context_s = left_context_s
        #: Neues stabiles Segment pro Tick.
        self.chunk_s = chunk_s
        #: Rechts-Kontext (Lookahead) nach dem Commit-Horizont.
        self.right_context_s = right_context_s
        #: Aufeinanderfolgende Identische, bis ein Kandidat committet wird.
        self.commit_agreements = commit_agreements
        #: Hartes Budget fuer Finalize-/Catch-up-Fenster (Sekunden Audio).
        self.max_final_window_s = max_final_window_s
        self.sample_rate = sample_rate
        #: Optionales VAD-Gate (z.B. SileroVadGate).
        self.vad = vad

        self._buffer = bytearray()
        self._committer = self._new_committer()
        self._committed_text = ""
        self._last_partial = ""
        #: Audio-Stand (Sekunden) des letzten Engine-Aufrufs (Intervall-Throttle).
        self._last_transcribed_at = 0.0
        self._has_transcribed = False
        #: Aufeinanderfolgende VAD-Misses im neuen Bereich (Pause-Erkennung).
        self._silence_misses = 0
        #: True, nachdem das Live-Gate im neuen Bereich "stumm" meldete.
        self._vad_live_silent = False
        #: Puffergroesse nach dem letzten erfolgreichen Engine-Call: Nachweis
        #: fuer "keine neuen Bytes" im Finalize-Cache.
        self._completed_nbytes = -1

    # ------------------------------------------------------------------
    # Zustand
    # ------------------------------------------------------------------
    @property
    def audio_seconds(self) -> float:
        return len(self._buffer) / (self.sample_rate * 2)

    @property
    def committed_len(self) -> int:
        """Laenge des committeten Textpraefixes (fuer DeltaTyper)."""
        return len(self._committed_text.rstrip())

    @property
    def committed_text(self) -> str:
        return self._committed_text.rstrip()

    @property
    def last_partial(self) -> str:
        return self._last_partial

    @property
    def last_text(self) -> str:
        """Voller zuletzt bekannter Text (committed + partial)."""
        return (self._committed_text + self._last_partial).strip()

    @property
    def committed_seconds(self) -> float:
        return self._committer.committed_s

    # ------------------------------------------------------------------
    # Steuerung
    # ------------------------------------------------------------------
    def reset(self) -> None:
        """Vergisst Puffer, Committer und alle Zaehler (neue Aufnahme)."""
        self._buffer.clear()
        self._committer = self._new_committer()
        self._committed_text = ""
        self._last_partial = ""
        self._last_transcribed_at = 0.0
        self._has_transcribed = False
        self._silence_misses = 0
        self._vad_live_silent = False
        self._completed_nbytes = -1

    def feed(self, chunk: bytes) -> None:
        """Legt Audio in den Puffer und entwertet den Finalize-Cache."""
        if chunk:
            self._buffer.extend(chunk)
            self._vad_live_silent = False
            self._silence_misses = 0

    def _new_committer(self) -> TimestampCommitter:
        """Ein Committer fuer beide Phasen - Agreement-Zwang gilt immer."""
        return TimestampCommitter(
            commit_agreements=self.commit_agreements,
            require_agreement_always=True,
        )

    # ------------------------------------------------------------------
    # Live-Tick
    # ------------------------------------------------------------------
    def maybe_transcribe(self):
        """Transkribiert das naechst faellige Fenster, wenn eines faellig ist.

        Returns:
            ``TranscriptionResult`` mit dem VOLLEN Text (committed + partial)
            und ``committed_len``, oder ``None`` wenn nichts faellig war.
        """
        from .asr import TranscriptionResult

        audio_s = self.audio_seconds
        if audio_s < self.min_audio_s:
            return None
        if self._has_transcribed and \
                audio_s - self._last_transcribed_at < self.interval_s:
            return None

        window = self._next_window(audio_s)
        if window is None:
            return None
        if self._vad_blocks(window):
            return None

        tr = self._decode(window)
        self._last_transcribed_at = audio_s
        self._has_transcribed = True
        self._completed_nbytes = len(self._buffer)

        commit_text, _ = self._committer.update(tr, window.commit_horizon)
        if commit_text.strip():
            self._committed_text += commit_text.strip() + " "
        # Live-Partial = alles, was NACH dem Commit-Punkt bekannt ist: der
        # noch nicht bestaetigte Kandidat UND die Tokens jenseits des
        # Horizonts. Wuerde man nur den Committer-Partial nehmen, fehlen die
        # zurueckgehaltenen Kandidaten-Woerter im Live-Text -> der DeltaTyper
        # wuerde sie mit Backspaces loeschen und spaeter zuruecktippen
        # (sichtbares Flackern, verlorene Woerter).
        if tr.tokens:
            fresh = [t for t in tr.tokens
                     if t.start >= self._committer.committed_s - 1e-3]
            self._last_partial = "".join(t.text for t in fresh).strip()
        else:
            self._last_partial = self._committer.last_partial.strip()

        committed_clean = self._committed_text.rstrip()
        full = (committed_clean + " " + self._last_partial).strip()
        log.debug(
            "buffered Fenster %.1f-%.1fs horizon=%.1fs -> committed=%d text=%r",
            window.start, window.end, window.commit_horizon,
            len(committed_clean), full[:60],
        )
        return TranscriptionResult(text=full, committed_len=len(committed_clean))

    # ------------------------------------------------------------------
    # Finalisierung
    # ------------------------------------------------------------------
    def finalize(self):
        """Finaler Pass: unveraendertes Audio aus dem Cache, sonst Tail+Kontext.

        Der committete Praefix wird NIE neu dekodiert oder ueberschrieben;
        ``committed_len`` bleibt die Praefixlaenge, damit der DeltaTyper den
        committeten Teil weder anfasst noch dupliziert.
        """
        from .asr import TranscriptionResult

        def _full(tail_text: str):
            committed_clean = self._committed_text.rstrip()
            text = (committed_clean + " " + tail_text).strip() if tail_text \
                else committed_clean
            return TranscriptionResult(text=text, committed_len=len(committed_clean))

        tail_start_byte = int(self._committer.committed_s * self.sample_rate * 2)
        tail = bytes(self._buffer[tail_start_byte:])

        if not tail:
            return _full(self._last_partial)
        if len(self._buffer) == self._completed_nbytes:
            log.debug("buffered finalize: keine neuen Bytes -> Cache (%d Bytes)",
                      len(self._buffer))
            return _full(self._last_partial)

        # VAD-Reste nicht blind verwerfen: meldete das Live-Gate im neuen
        # Bereich zweimal "stumm", war der Tail also eine echte Pause,
        # uebernehmen wir den letzten Partial statt Muell zu erfinden.
        if self.vad is not None and self._vad_live_silent:
            log.info("buffered finalize: Tail ist laut VAD stumm -> letzter "
                     "Partial uebernommen (kein Vollpuffer, kein Datenverlust)")
            return _full(self._last_partial)

        tail_s = len(tail) / (self.sample_rate * 2)
        log.debug("buffered finalize: dekodiere uncommitted Tail %.2fs ab %.1fs",
                  tail_s, self._committer.committed_s)

        pieces: list[str] = []
        # Ueberlappung wird ueber absolute Token-Zeiten entdoppelt: alles
        # unterhalb von ``emitted_s`` wurde bereits geliefert.
        emitted_s = self._committer.committed_s
        for window in self._catchup_windows(self.audio_seconds):
            tr = self._decode(window)
            text = "".join(
                t.text for t in tr.tokens if t.start >= emitted_s - 1e-3
            ).strip()
            if text:
                pieces.append(text)
                emitted_s = max(t.end for t in tr.tokens) + 1e-3
        tail_text = " ".join(pieces).strip()
        if not pieces:
            # Fallback ohne Zeitstempel: NUR der uncommitted Tail, nie der
            # Vollpuffer.
            result = self.engine.transcribe(tail)
            tail_text = getattr(result, "text", str(result)).strip()
        return _full(tail_text)

    # ------------------------------------------------------------------
    # Fensterberechnung
    # ------------------------------------------------------------------
    def _next_window(self, audio_s: float) -> StreamWindow | None:
        """Naechstes Live-Fenster (Phase A: Volllpuffer, Phase B: bounded)."""
        committed_s = self._committer.committed_s

        if audio_s <= self.whole_buffer_until_s:
            # Phase A: wachsender Volllpuffer von 0 bis zum Audioende. Es wird
            # nur committet, was sicher vor dem Horizont liegt und danach noch
            # Rechtskontext hat (RC-A3 wie in StreamWindowPolicy).
            horizon = min(audio_s - self.right_context_s,
                          committed_s + self.chunk_s)
            if horizon <= committed_s:
                return None
            return StreamWindow(start=0.0, end=audio_s, commit_horizon=horizon)

        # Phase B: genau EIN fest begrenztes Fenster pro Tick, hart auf
        # left + chunk + right gedeckelt (konstante Inferenzkosten).
        start = max(0.0, committed_s - self.left_context_s)
        horizon = committed_s + self.chunk_s
        if audio_s < horizon + self.right_context_s:
            # Noch kein Rechtskontext hinter dem Horizont -> nichts committen.
            return None
        end = horizon + self.right_context_s
        if end <= start:
            return None
        return StreamWindow(start=start, end=end, commit_horizon=horizon)

    def _catchup_windows(self, audio_s: float):
        """Begrenzte Fenster fuer finalize: Tail + Kontext, nie Vollpuffer."""
        budget = min(self.max_final_window_s,
                     self.left_context_s + self.chunk_s + self.right_context_s)
        ctx = min(self.left_context_s, self._committer.committed_s)
        pos = self._committer.committed_s
        while pos < audio_s - 1e-9:
            start = max(0.0, pos - ctx)
            end = min(audio_s, start + budget)
            if end <= pos:
                # Links-Kontext opfern, um Fortschritt zu garantieren.
                end = min(audio_s, pos + max(1.0, budget - ctx))
                if end <= pos:
                    break
            yield StreamWindow(start=start, end=end, commit_horizon=None)
            pos = end

    # ------------------------------------------------------------------
    # Intern
    # ------------------------------------------------------------------
    def _decode(self, window: StreamWindow):
        # Auf Byte-Grenzen runden: Start AUF-, Ende ABruenden, damit die
        # effektive Fensterlaenge nie ueber das Budget (left+chunk+right)
        # wachsaedt.
        start_byte = -(-int(window.start * self.sample_rate * 2) // 1)
        start_byte = int(window.start * self.sample_rate * 2 + 0.999999)
        end_byte = int(window.end * self.sample_rate * 2)
        data = bytes(self._buffer[start_byte:end_byte])
        return self.engine.transcribe_with_timestamps(data, window.start)

    def _vad_blocks(self, window: StreamWindow) -> bool:
        """VAD-Gate auf dem NEUEN Bereich; erkennt Pausen und rueckt vor.

        Wie beim ``WindowedTranscriber`` fuehren zwei aufeinanderfolgende
        "stumm"-Meldungen zu ``skip_silence``: ohne das bliebe der Commit-Punkt
        an jeder Pause stehen und alles Dahinterginge fuer immer verloren.
        Der bereits erkannte Text bleibt erhalten (committed + letzter Partial).
        """
        if self.vad is None:
            return False
        committed_byte = int(self._committer.committed_s * self.sample_rate * 2)
        end_byte = int(window.end * self.sample_rate * 2)
        data = bytes(self._buffer[committed_byte:end_byte])
        if not data:
            return False
        arr = _to_float_audio(data)
        if len(arr) < self.sample_rate:
            # Unter 1s kann das Gate nichts beurteilen -> nicht blocken.
            self._silence_misses = 0
            return False
        try:
            speech = self.vad.has_speech(arr)
        except Exception as exc:  # noqa: BLE001 - Gate ist optional
            log.warning("buffered: VAD-Gate fehlgeschlagen (%s) - transkribiere", exc)
            return False
        if speech:
            self._silence_misses = 0
            self._vad_live_silent = False
            return False

        self._silence_misses += 1
        self._vad_live_silent = True
        log.debug("buffered: VAD stumm im neuen Bereich (Miss %d)",
                  self._silence_misses)
        if self._silence_misses >= 2:
            # Echte Pause: Commit-Punkt vorruecken, aber nur bis zum
            # Fensterende und nur wenn das Fenster bis dahin gefuellt war.
            # Der letzte Partial bleibt als Text erhalten (last_text).
            if self.audio_seconds >= window.end - 0.01:
                self._flush_pending_text()
                self._committer.skip_silence(window.end)
                self._silence_misses = 0
                log.info("buffered: Pause erkannt -> committed_s auf %.1fs",
                         self._committer.committed_s)
        return True

    def _flush_pending_text(self) -> None:
        """Sichert den letzten Partial in den committeten Text (Pause-Schutz).

        ``skip_silence`` loescht den Partial des Committers; der bereits
        erkannte Text wuerde sonst beim Vorruecken des Commit-Punkts verloren
        gehen. Getrennt durch Leerzeichen, exakt wie bei einem regulaeren
        Commit.
        """
        if self._last_partial.strip():
            self._committed_text += self._last_partial.strip() + " "
            self._last_partial = ""
