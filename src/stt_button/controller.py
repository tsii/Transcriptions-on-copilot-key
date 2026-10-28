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
        dump_path: str = "/tmp/stt_last.wav",
    ) -> None:
        self._source_factory = source_factory
        self._engine = engine
        self._delta_typer = delta_typer
        self._min_audio_s = min_audio_s
        self._asr_interval_s = asr_interval_s
        self._max_record_s = max_record_s
        self._no_audio_timeout_s = no_audio_timeout_s
        self._transcriber_factory = transcriber_factory or StreamTranscriber
        # Ziel fuer den Debug-WAV-Dump; Modi aus config.audio_dump_mode.
        self._dump_path = dump_path
        self._audio_dump_mode = str(
            getattr(config, "audio_dump_mode", "sync") or "sync"
        )

        self.state = ControllerState.IDLE
        self._source: AudioSource | None = None
        self._transcriber: StreamTranscriber | None = None
        self._started_at = 0.0
        self._last_audio_at = 0.0
        self._total_bytes = 0
        # Serialisiert KURZE Zustandswechsel: state, _key_down, _source/
        # _transcriber-Zeiger, Zaehler. Wird NIEMALS waehrend einer ASR-
        # Inferenz gehalten - sonst wartet der Release (stop_recording)
        # auf die laufende Live-Inferenz (Latenz STUFE 1).
        self._lock = threading.RLock()
        # Weckt den Worker (Application.run_forever) gezielt auf, statt
        # alle 20 ms blind zu pollen: notified wird bei jedem Zustands-
        # wechsel und jedem nicht-leeren Capture-put. Teilt _lock - damit
        # ist die Nicht-Leer-Pruefung der Queue und das wait() atomar
        # gegen den Notify-Pfad (kein Lost-Wakeup).
        self._work_cv = threading.Condition(self._lock)
        # Serialisiert die Live-/Final-Ausfuehrungskette (Transcriber +
        # DeltaTyper): der Live-Tick und der Finalizer nehmen dieses Lock,
        # der Key-Thread NIE. Verschachtelung immer: _exec_lock -> _lock.
        self._exec_lock = threading.Lock()
        # Latenz-Messung (config.latency_trace); 0.0 = kein laufender Release.
        self._latency_trace = bool(getattr(config, "latency_trace", False))
        self._stop_entered_at = 0.0
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
            self._notify_work()

    def _notify_work(self) -> None:
        """Weckt wartende Worker auf (Condition-Notify unter ``_lock``).

        Muss unter ``_lock`` bzw. ``_work_cv`` gehalten werden; nach dem
        Weckruf prueft wait_for_work() seine Bedingungen erneut (kein
        Lost-Wakeup, spurious Wakeups unschaedlich).
        """
        self._work_cv.notify_all()

    def wait_for_work(self) -> None:
        """Blockt, bis neue Arbeit ansteht (statt 20-ms-Polling).

        Ersetzt ``time.sleep(0.02)`` in ``Application.run_forever()``.
        Wird NIE unter ``_exec_lock`` aufgerufen (der wartende Worker
        darf keine Live-/Final-Inferenz festhalten).

        * IDLE/FINALIZING: bedingungslos blocken, bis ein Notify
          (Tastendruck, Finalisierungsende, Abbruch) den Worker weckt.
        * RECORDING mit leerer Capture-Queue: blocken bis zur naechsten
          Frist - neues Audio (Notify vom Capture-Pfad), No-Audio-Timeout
          oder maximale Aufnahmedauer, je nachdem was zuerst faellig ist.
        * RECORDING mit nicht-leerer Queue: sofort zurueckkehren
          (Lost-Wakeup-Schutz: Die Pruefung erfolgt VOR dem wait(); ein
          bereits verstrichenes Notify verpasst dadurch nichts).
        """
        with self._work_cv:
            if self.state != ControllerState.RECORDING:
                self._work_cv.wait()
                return
            if not self._audio_queue.empty():
                return
            now = time.monotonic()
            timeout = min(
                self._started_at + self._max_record_s - now,
                self._last_audio_at + self._no_audio_timeout_s - now,
            )
            self._work_cv.wait(timeout=max(0.0, timeout))

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
            args=(self._source, self._audio_queue),
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

        Latenz STUFE 1: Der Zustand wechselt nur kurz unter ``_lock`` auf
        FINALIZING; das eigentliche Stoppen (``_finish``) laeuft OHNE beide
        Locks, damit die Audioquelle auch bei laufender Live-Inferenz in
        < 0,3 s stoppt.
        """
        if self._latency_trace:
            self._stop_entered_at = time.monotonic()
        self._trace("stop entered")
        with self._lock:
            self._key_down = False
            if self.state != ControllerState.RECORDING:
                return
        self._finish()

    def _trace(self, point: str) -> None:
        """Debug-Messpunkt der Release->Ausgabe-Latenz (nur bei latency_trace).

        Misst die monotone Zeit seit dem Betreten von ``stop_recording()``;
        ohne Release (z.B. Max-Dauer) seit Aufnahmestart.
        """
        if not self._latency_trace:
            return
        base = self._stop_entered_at or self._started_at
        log.debug("latency[%s] +%.3fs", point, time.monotonic() - base)

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
            self._notify_work()
            log.info("Aufnahme abgebrochen")

    def shutdown(self) -> None:
        """Beendet alles (SIGTERM / Prozessende)."""
        self.abort()

    # ------------------------------------------------------------------
    # Betrieb
    # ------------------------------------------------------------------
    def run_once(self) -> None:
        """Verarbeitet einen Zeitschritt (liest Audio, transkribiert ggf.).

        Laeuft unter ``_exec_lock`` (NICHT unter ``_lock``): die Live-
        Inferenz darf den Key-Thread (Press/Release) nie blockieren.
        """
        with self._exec_lock:
            self._run_once_exec()

    def _run_once_exec(self) -> None:
        """Ein Live-Tick unter ``_exec_lock``; Zustand wird kurz unter ``_lock`` geprueft."""
        with self._lock:
            if self.state != ControllerState.RECORDING:
                return
            source = self._source
            transcriber = self._transcriber
            audio_queue = self._audio_queue
        if source is None or transcriber is None:
            # Nur abbrechen, solange die Aufnahme noch laeuft: ein Release
            # (FINALIZING) uebergibt an den Finalizer, abort wuerde dessen
            # Zustand sloschen.
            with self._lock:
                live = self.state == ControllerState.RECORDING
            if live:
                self.abort()
            return

        # Chunks aus der Capture-Queue leeren (nicht direkt von source lesen,
        # damit die ASR-Inferenz das Einlesen nicht blockiert).
        got_audio = False
        while True:
            try:
                chunk = audio_queue.get_nowait()
            except queue.Empty:
                break
            got_audio = True
            with self._lock:
                self._total_bytes += len(chunk)
            transcriber.feed(chunk)

        now = time.monotonic()
        if got_audio:
            with self._lock:
                self._last_audio_at = now
        else:
            with self._lock:
                stalled = now - self._last_audio_at > self._no_audio_timeout_s
            if stalled:
                log.warning("Keine Audiodaten seit %.0fs - Abbruch",
                            self._no_audio_timeout_s)
                self.abort()
                return

        self._enforce_max_duration()

        # Kurzer Zustandstest VOR der Transkription: ein Release
        # (FINALIZING) oder Abbruch laesst diesen Tick ins Leere laufen
        # (Ergebnis waere ueberholt und wuerde den Finaltext overseizen).
        with self._lock:
            if self.state != ControllerState.RECORDING:
                return

        try:
            self._trace("live asr start")
            result = transcriber.maybe_transcribe()
            self._trace("live asr end")
        except Exception as exc:  # noqa: BLE001 - ASR-Fehler -> sauberer Abbruch
            log.error("Transkription fehlgeschlagen: %s", exc)
            self.abort()
            return

        # STUFE 2: Post-Release-Discard. Der Live-Tick kann Sekunden in der
        # Engine geblockt haben; ein Release in dieser Zeit hat die Aufnahme
        # bereits an den Finalizer uebergeben. Das jetzt vorliegende Live-
        # Ergebnis waere ueberholt: Es wuerde dem Finaltext vorgreifen und
        # der Finalizer muesste es per Backspace zuruecknehmen (gefuelte
        # Latenz). Verwerfen statt tippen - der Finalizer liefert den
        # vollstaendigen Text gegen den effektiven Stand.
        with self._lock:
            stale = (
                self.state != ControllerState.RECORDING
                or self._transcriber is not transcriber
            )
        if stale:
            log.info("Livetick verwerfen: Aufnahme bereits abgeschlossen")
            return

        # STUFE 4 (Pausen-Indikator): Meldet der Transkriber, dass die
        # Live-ASR das live_asr_until_s-Limit erreicht hat, tippt der
        # DeltaTyper GENAU EINMAL den Indikator ("ich hoere noch zu,
        # schreibe aber gerade nicht mit"). finalize() entfernt ihn ueber
        # die normale Differenz wieder. Nur StreamTranscriber hat ein
        # Live-Limit (windowed/buffered/Fakes nicht) -> getattr-Guard.
        # Mit live_asr_until_s=0.0 (Default) wird NIE gemeldet.
        take_live_paused = getattr(transcriber, "take_live_paused", None)
        if take_live_paused is not None and take_live_paused():
            self._trace("live paused")
            self._delta_typer.show_paused()

        if result is not None:
            self._trace("live typing start")
            self._delta_typer.update(result.text, result.committed_len)
            self._trace("live typing end")

    def _capture_loop(
        self, source: AudioSource, audio_queue: "queue.Queue[bytes]"
    ) -> None:
        """Liest kontinuierlich Audio aus der Quelle in die Queue.

        Quelle und Queue sind als Argumente gebunden, damit ein verspaetet
        endender Capture-Thread einer alten Aufnahme nie in die Queue
        einer neuen Aufnahme legt.
        """
        while self.state == ControllerState.RECORDING:
            try:
                chunk = source.read(block=False)
            except AudioSourceError:
                break
            if chunk:
                audio_queue.put(chunk)
                # Worker wecken: NUR das Notify - keine ASR-/Typer-
                # Operationen im Capture-Thread (sie laufen exklusiv
                # unter _exec_lock im Worker).
                with self._lock:
                    self._notify_work()
            else:
                time.sleep(0.02)

    def _enforce_max_duration(self) -> None:
        """Bricht nach ``_max_record_s`` ab, wie beim Loslassen."""
        if self.state != ControllerState.RECORDING:
            return
        with self._lock:
            expired = time.monotonic() - self._started_at >= self._max_record_s
        if expired:
            log.info("Maximale Aufnahmedauer erreicht - Finalisierung")
            self._finish()

    def _finish(self) -> None:
        """Stoppt die Aufnahme sofort und stoesst die Finalisierung an.

        Laeuft OHNE _lock und OHNE _exec_lock (AUSSER dem kurzen
        Zustandswechsel): der Release stoppt die Audioquelle auch dann in
        < 0,3 s, wenn gerade eine mehrsekundige Live-Inferenz unter
        _exec_lock laeuft.

        Die RC-B2/B3-Reihenfolge 'Queue-drain vor stop()-Rest' bleibt
        gewahrt: beide Feeds passieren im Finalizer UNTER _exec_lock, also
        nach jedem noch laufenden Live-Tick am selben Transcriber.
        """
        with self._lock:
            if self.state != ControllerState.RECORDING:
                return
            self.state = ControllerState.FINALIZING
            source = self._source
            transcriber = self._transcriber
            audio_queue = self._audio_queue
            capture_thread = self._capture_thread
            self._capture_thread = None
            self._notify_work()

        # RC-B2/B3: Erst den Capture-Thread beenden (er prueft den Zustand,
        # der bereits FINALIZING ist, und endet praktisch sofort), DANN
        # die Queue drainen, DANN den stop()-Rest feeden.
        if capture_thread is not None:
            capture_thread.join(timeout=1.0)
        self._trace("capture stopped")

        # Audioquelle SOFORT stoppen; der Pipe-Rest (neueste Bytes) wird
        # mitgenommen und erst im Finalizer ans Pufferende gefuettert.
        rest = b""
        if source is not None:
            try:
                rest = source.stop()
            except AudioSourceError:
                rest = b""
            with self._lock:
                self._source = None
        self._trace("source stopped")

        # Die finale Transkription kann mehrere Sekunden dauern (CPU). Sie
        # laeuft deshalb im HINTERGRUND, damit der Key-Thread sofort wieder
        # bereit ist und ein schnell folgendes zweites Diktat nicht verloren
        # geht.
        if transcriber is not None:
            finalizer = threading.Thread(
                target=self._finalize_worker,
                args=(transcriber, audio_queue, rest),
                name="finalize",
                daemon=True,
            )
            finalizer.start()
        else:
            with self._lock:
                self.state = ControllerState.IDLE

    def _finalize_worker(
        self, transcriber, audio_queue: "queue.Queue[bytes]", rest: bytes
    ) -> None:
        """Fuehrt die finale Transkription aus (Hintergrund-Thread).

        Laeuft unter ``_exec_lock``: keine Live-Inferenz und kein
        DeltaTyper-Zugriff kann mehr parallel zum finalen Feed/finalize/
        Ausgabe am selben Transcriber arbeiten. State/Log kurz unter _lock.
        """
        try:
            with self._exec_lock:
                self._trace("finalizer exec acquired")
                # 1) Queue in zeitlicher Reihenfolge drainen (aeltere
                #    Bytes zuerst), 2) stop()-Rest ans Ende.
                while True:
                    try:
                        chunk = audio_queue.get_nowait()
                    except queue.Empty:
                        break
                    with self._lock:
                        self._total_bytes += len(chunk)
                    transcriber.feed(chunk)
                if rest:
                    transcriber.feed(rest)

                # BUG 1 (Fix b): Der Live-Pfad kann das Limit NIE gesehen
                # haben - der letzte Live-Tick startete unter dem Limit,
                # seine Inferenz ueberdauerte die Grenze, und der Release
                # kam vor dem naechsten Tick (Realtest 11:56). Erst der
                # Queue-/Rest-Feed oben hat den Puffer jetzt ueber die
                # Grenze gedrueckt. take_live_paused() erkennt das Limit
                # SELBST - hier wird es spaetestens zu Beginn der finalen
                # Inferenz gemeldet. Der Indikator wird von der finalen
                # Differenz (3 Backspaces) automatisch entfernt.
                # Nur StreamTranscriber hat ein Live-Limit -> getattr-Guard.
                take_live_paused = getattr(transcriber, "take_live_paused", None)
                if take_live_paused is not None and take_live_paused():
                    self._trace("live paused")
                    self._delta_typer.show_paused()

                # Vollstaendige Aufnahme zu Debug-Zwecken abspeichern.
                self._trace("dump start")
                self._dump_audio(transcriber)
                self._trace("dump end")

                self._trace("finalize start")
                result = transcriber.finalize()
                self._trace("finalize end")
                self._delta_typer.finalize(result.text, result.committed_len)
                self._trace("paste")
        except Exception as exc:  # noqa: BLE001
            log.error("Schluss-Transkription fehlgeschlagen: %s", exc)
        finally:
            with self._lock:
                self._transcriber = None
                self.state = ControllerState.IDLE
                self._stop_entered_at = 0.0
                log.info("Aufnahme beendet (%d bytes Audio)", self._total_bytes)
                self._total_bytes = 0
                # Nachgeholt wird der Start nur, wenn die Taste noch
                # gehalten wird - Pruefung und Start atomar unter dem
                # selben Lock (kein Handoff-Fenster fuer Races).
                if self._key_down:
                    log.info("Taste gehalten - neue Aufnahme starten")
                    self._start_now()
                self._notify_work()

    def _dump_audio(self, transcriber) -> None:
        """Speichert die komplette Aufnahme als Debug-WAV.

        Modi (config.audio_dump_mode):
        * ``"sync"`` (Default, bisheriges Verhalten): schreibt sofort im
          Aufrufer-Thread (Finalizer).
        * ``"async"``: Snapshot der Bytes, Schreiben in einem eigenen
          Thread - der Finalizer wartet auf keinen File-I/O. Der Thread
          haelt ausschliesslich das Immutables Snapshot, NIEMALS eine
          Referenz auf den (ggf. weiter genutzten) Transcriber-Puffer.
        * ``"off"``: kein Dump.
        """
        if self._audio_dump_mode == "off" or transcriber is None:
            return
        try:
            if not transcriber._buffer:
                return
            snapshot = bytes(transcriber._buffer)
            sample_rate = transcriber.sample_rate
        except Exception as exc:  # noqa: BLE001
            log.debug("Audio-Dump fehlgeschlagen: %s", exc)
            return

        if self._audio_dump_mode == "async":
            threading.Thread(
                target=self._write_wav,
                args=(snapshot, sample_rate),
                name="audio-dump",
                daemon=True,
            ).start()
        else:
            self._write_wav(snapshot, sample_rate)

    def _write_wav(self, pcm: bytes, sample_rate: int) -> None:
        """Schreibt s16le-Mono-Bytes als WAV (Debug-Dump)."""
        try:
            import wave

            with wave.open(self._dump_path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(sample_rate)
                w.writeframes(pcm)
            log.info("Aufnahme gespeichert: %s", self._dump_path)
        except Exception as exc:  # noqa: BLE001
            log.debug("Audio-Dump fehlgeschlagen: %s", exc)