"""State-Maschine, die Aufnahme, Transkription und Ausgabe verbindet.

Zustaende::

    IDLE --start_recording()--> RECORDING --stop_recording()--> IDLE
      ^                          |  ^
      |                          |  (max_record_s erreicht)
      +---abort()/Fehler/Timeout-+

Waehrend RECORDING wird in ``run_once()``-Schritten Audio gelesen, dem
StreamTranscriber gefuettert und bei Bedarf transkribiert. Die Ergebnisse
fliessen in den DeltaTyper, der die Differenz an die Cursor-Position tippt.
Beim Loslassen wird finalisiert und mit einem Leerzeichen abgeschlossen.
"""
from __future__ import annotations

import enum
import logging
import queue
import threading
import time
from typing import Callable

from .asr import StreamTranscriber, TranscriptionResult
from .audio import AudioSource, AudioSourceError
from .typing import DeltaTyper

log = logging.getLogger(__name__)


class ControllerState(enum.Enum):
    IDLE = "idle"
    RECORDING = "recording"
    FINALIZING = "finalizing"


class PushToTalkController:
    """Steuert den Ablauf Push-to-Talk -> Transkription -> Ausgabe."""

    def __init__(
        self,
        *,
        config=None,
        source_factory: Callable[[], AudioSource],
        engine,
        delta_typer: DeltaTyper,
        min_audio_s: float = 0.8,
        asr_interval_s: float = 1.5,
        max_record_s: float = 60.0,
        no_audio_timeout_s: float = 5.0,
        transcriber_factory: Callable[..., StreamTranscriber] | None = None,
    ) -> None:
        self._source_factory = source_factory
        self._engine = engine
        self._delta_typer = delta_typer
        self._min_audio_s = min_audio_s
        self._asr_interval_s = asr_interval_s
        self._max_record_s = max_record_s
        self._no_audio_timeout_s = no_audio_timeout_s
        self._transcriber_factory = transcriber_factory or StreamTranscriber

        self.state = ControllerState.IDLE
        self._source: AudioSource | None = None
        self._transcriber: StreamTranscriber | None = None
        self._started_at = 0.0
        self._last_audio_at = 0.0
        self._total_bytes = 0
        # Serialisiert Zugriffe aus dem Key-Thread und der ASR-Schleife.
        self._lock = threading.RLock()
        # Audio-Chunks, die der Capture-Thread kontinuierlich einliest.
        self._audio_queue: queue.Queue[bytes] = queue.Queue()
        self._capture_thread: threading.Thread | None = None
        # Physischer Zustand der Trigger-Taste (press/release). Er wird
        # bei JEDEM Tastenereignis aktualisiert - ein Release geht nie
        # verloren, auch nicht waehrend der Finalisierung.
        self._key_down: bool = False

    # ------------------------------------------------------------------
    # Trigger
    # ------------------------------------------------------------------
    def start_recording(self) -> None:
        """Beginnt eine Aufnahme (aufgerufen bei Tastendruck).

        Wichtig: Der Tastenzustand (``_key_down``) wird IMMER gesetzt,
        auch waehrend der Finalisierung. Ein Start erfolgt nur bei IDLE
        (atomar unter dem Lock); bei FINALIZING startet die Aufnahme
        erst nach Abschluss - und nur, wenn die Taste dann noch gehalten
        wird. Sonst ginge ein schnell folgendes Diktat verloren
        (Datenverlust) bzw. es entstuende eine Geisteraufnahme ohne
        gehaltene Taste.
        """
        with self._lock:
            self._key_down = True
            if self.state == ControllerState.RECORDING:
                return  # bereits aktiv -> idempotent
            if self.state == ControllerState.FINALIZING:
                # Start nach Finalisierungsende, nur falls Taste gehalten.
                log.info("Taste gehalten - Start nach Finalisierung")
                return
            self._start_now()

    def _start_now(self) -> None:
        """Startet die Aufnahme sofort (Zustand bereits geprueft)."""
        try:
            self._source = self._source_factory()
            self._source.start()
        except AudioSourceError as exc:
            log.error("Audioquelle nicht startbar: %s", exc)
            self._source = None
            return

        self._transcriber = self._transcriber_factory(
            self._engine,
            interval_s=self._asr_interval_s,
            min_audio_s=self._min_audio_s,
        )
        self._delta_typer.reset()
        self.state = ControllerState.RECORDING
        self._started_at = time.monotonic()
        self._last_audio_at = time.monotonic()
        self._total_bytes = 0

        self._audio_queue = queue.Queue()
        self._capture_thread = threading.Thread(
            target=self._capture_loop,
            name="audio-capture",
            daemon=True,
        )
        self._capture_thread.start()
        log.info("Aufnahme gestartet")

    def stop_recording(self) -> None:
        """Beendet die Aufnahme und finalisiert (aufgerufen beim Loslassen).

        Der Tastenzustand wird IMMER aktualisiert - auch wenn gerade
        finalisiert wird. Sonst wuerde der Release-Edge verloren gehen und
        eine waehrend der Finalisierung gedrueckte Taste eine Geister-
        aufnahme ausloesen.
        """
        with self._lock:
            self._key_down = False
            if self.state != ControllerState.RECORDING:
                return
            self._finish()

    def abort(self) -> None:
        """Bricht ab, ohne Text auszugeben (Fehlerfall)."""
        with self._lock:
            if self._source is not None:
                try:
                    self._source.stop()
                except AudioSourceError:
                    pass
                self._source = None
            self._transcriber = None
            self.state = ControllerState.IDLE
            log.info("Aufnahme abgebrochen")

    def shutdown(self) -> None:
        """Beendet alles (SIGTERM / Prozessende)."""
        self.abort()

    # ------------------------------------------------------------------
    # Betrieb
    # ------------------------------------------------------------------
    def run_once(self) -> None:
        """Verarbeitet einen Zeitschritt (liest Audio, transkribiert ggf.)."""
        with self._lock:
            self._run_once_locked()

    def _run_once_locked(self) -> None:
        """run_once ohne Lock - wird aus run_once aufgerufen."""
        if self.state != ControllerState.RECORDING:
            return
        source = self._source
        transcriber = self._transcriber
        if source is None or transcriber is None:
            self.abort()
            return

        # Chunks aus der Capture-Queue leeren (nicht direkt von source lesen,
        # damit die ASR-Inferenz das Einlesen nicht blockiert).
        got_audio = False
        while True:
            try:
                chunk = self._audio_queue.get_nowait()
            except queue.Empty:
                break
            got_audio = True
            self._total_bytes += len(chunk)
            transcriber.feed(chunk)

        if got_audio:
            self._last_audio_at = time.monotonic()
        else:
            if time.monotonic() - self._last_audio_at > self._no_audio_timeout_s:
                log.warning("Keine Audiodaten seit %.0fs - Abbruch",
                            self._no_audio_timeout_s)
                self.abort()
                return

        self._enforce_max_duration()

        try:
            result = transcriber.maybe_transcribe()
        except Exception as exc:  # noqa: BLE001 - ASR-Fehler -> sauberer Abbruch
            log.error("Transkription fehlgeschlagen: %s", exc)
            self.abort()
            return

        if result is not None:
            self._delta_typer.update(result.text, result.committed_len)

    def _capture_loop(self) -> None:
        """Liest kontinuierlich Audio aus der Quelle in die Queue."""
        source = self._source
        if source is None:
            return
        while self.state == ControllerState.RECORDING:
            try:
                chunk = source.read(block=False)
            except AudioSourceError:
                break
            if chunk:
                self._audio_queue.put(chunk)
            else:
                time.sleep(0.02)

    def _enforce_max_duration(self) -> None:
        """Bricht nach ``_max_record_s`` ab, wie beim Loslassen."""
        if self.state != ControllerState.RECORDING:
            return
        if time.monotonic() - self._started_at >= self._max_record_s:
            log.info("Maximale Aufnahmedauer erreicht - Finalisierung")
            self._finish()

    def _finish(self) -> None:
        """Stoppt die Quelle, transkribiert final und gibt den Text aus."""
        self.state = ControllerState.FINALIZING
        source = self._source
        transcriber = self._transcriber

        # RC-B2/B3: Erst den Capture-Thread beenden (damit er keine Chunks
        # mehr in die Queue legt), DANN die Queue in richtiger Reihenfolge
        # drainen, DANN den stop()-Rest (neueste Bytes) feeden.
        if self._capture_thread is not None:
            self._capture_thread.join(timeout=1.0)
            self._capture_thread = None

        # 1) Queue in zeitlicher Reihenfolge drainen (aeltere Bytes zuerst).
        if transcriber is not None:
            while True:
                try:
                    chunk = self._audio_queue.get_nowait()
                except queue.Empty:
                    break
                self._total_bytes += len(chunk)
                transcriber.feed(chunk)

        # 2) stop()-Rest (die neuesten Bytes vom Pipe-Drain) ans Ende.
        if source is not None:
            try:
                rest = source.stop()
            except AudioSourceError:
                rest = b""
            self._source = None
            if rest and transcriber is not None:
                transcriber.feed(rest)

        # Vollstaendige Aufnahme zu Debug-Zwecken abspeichern.
        self._dump_audio(transcriber)

        # Die finale Transkription kann mehrere Sekunden dauern (CPU). Sie
        # laeuft deshalb im HINTERGRUND, damit der Key-Thread sofort wieder
        # bereit ist und ein schnell folgendes zweites Diktat nicht verloren
        # geht.
        if transcriber is not None:
            finalizer = threading.Thread(
                target=self._finalize_worker,
                args=(transcriber,),
                name="finalize",
                daemon=True,
            )
            finalizer.start()
        else:
            self.state = ControllerState.IDLE

    def _finalize_worker(self, transcriber) -> None:
        """Fuehrt die finale Transkription aus (Hintergrund-Thread)."""
        try:
            result = transcriber.finalize()
            self._delta_typer.finalize(result.text, result.committed_len)
        except Exception as exc:  # noqa: BLE001
            log.error("Schluss-Transkription fehlgeschlagen: %s", exc)
        finally:
            with self._lock:
                self._transcriber = None
                self.state = ControllerState.IDLE
                log.info("Aufnahme beendet (%d bytes Audio)", self._total_bytes)
                self._total_bytes = 0
                # Nachgeholt wird der Start nur, wenn die Taste noch
                # gehalten wird - Pruefung und Start atomar unter dem
                # selben Lock (kein Handoff-Fenster fuer Races).
                if self._key_down:
                    log.info("Taste gehalten - neue Aufnahme starten")
                    self._start_now()

    def _dump_audio(self, transcriber) -> None:
        """Speichert die komplette Aufnahme nach /tmp/stt_last.wav (Debug)."""
        try:
            import wave

            if transcriber is None or not transcriber._buffer:
                return
            path = "/tmp/stt_last.wav"
            with wave.open(path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(transcriber.sample_rate)
                w.writeframes(bytes(transcriber._buffer))
            log.info("Aufnahme gespeichert: %s", path)
        except Exception as exc:  # noqa: BLE001
            log.debug("Audio-Dump fehlgeschlagen: %s", exc)