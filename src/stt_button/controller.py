"""State machine connecting recording, transcription, and output.

States::

    IDLE --start_recording()--> RECORDING --stop_recording()--> IDLE
      ^                          |  ^
      |                          |  (max_record_s reached)
      +---abort()/error/timeout--+

During RECORDING, ``run_once()`` steps read audio, feed it to the
StreamTranscriber, and transcribe as needed. Results flow into DeltaTyper,
which types the difference at the cursor position. On release, output is
finalized and terminated with a space.
"""
from __future__ import annotations

import enum
import logging
import os
import queue
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

from .asr import InferenceCancelled, StreamTranscriber, TranscriptionResult
from .audio import AudioSource, AudioSourceError
from . import focus
from .typing import DeltaTyper

#: Freedesktop sound directory for the completion signal (config.done_sound).
SOUND_DIR = "/usr/share/sounds/freedesktop/stereo"

log = logging.getLogger(__name__)


class ControllerState(enum.Enum):
    IDLE = "idle"
    RECORDING = "recording"
    FINALIZING = "finalizing"


@dataclass
class FinalizeJob:
    """One released recording waiting for final transcription."""

    transcriber: object
    audio_queue: "queue.Queue[bytes]"
    rest: bytes
    started_at: float
    epoch: int = 0
    total_bytes: int = 0


class PushToTalkController:
    """Control the push-to-talk -> transcription -> output flow."""

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
        # Destination for the debug WAV dump; modes from config.audio_dump_mode.
        self._dump_path = dump_path
        self._audio_dump_mode = str(
            getattr(config, "audio_dump_mode", "sync") or "sync"
        )
        # Completion signal (operator, 31.10.): name of the freedesktop sound
        # (e.g., 'complete') played quietly once after SUCCESSFUL finalization;
        # the user hears that the text is on the clipboard and can be pasted.
        # '' = off.
        self._done_sound = str(getattr(config, "done_sound", "") or "")
        # paplay --volume uses parts per 65536 (0..65536); 35000 ~= 54%,
        # "pleasantly quiet" according to the operator (31.10.), not shrill.
        self._done_sound_volume = int(
            getattr(config, "done_sound_volume", 65536) or 65536
        )
        # 'terminal' (default): sound only with terminal focus (Kimi extension
        # stt-button-focus, GetFocus via DBus); 'always': always. None
        # (extension missing/not responding) -> play sound anyway, feedback
        # instead of silence.
        self._max_queue_depth = int(getattr(config, "max_queue_depth", 4) or 4)
        self._done_sound_mode = str(
            getattr(config, "done_sound_mode", "always") or "always"
        )

        self.state = ControllerState.IDLE
        self._source: AudioSource | None = None
        self._transcriber: StreamTranscriber | None = None
        self._started_at = 0.0
        self._last_audio_at = 0.0
        self._total_bytes = 0
        # Serializes SHORT state changes: state, _key_down, _source/
        # _transcriber pointers, counters. NEVER held during ASR inference;
        # otherwise release (stop_recording) would wait for ongoing live
        # inference (latency STAGE 1).
        self._lock = threading.RLock()
        # Wakes the worker (Application.run_forever) selectively instead of
        # blindly polling every 20 ms: notified on every state change and
        # every non-empty capture put. Shares _lock, making the queue non-empty
        # check and wait() atomic with respect to the notify path (no lost
        # wakeup).
        self._work_cv = threading.Condition(self._lock)
        # Serializes the live/final execution chain (Transcriber +
        # DeltaTyper): the live tick and finalizer acquire this lock; the key
        # thread NEVER does. Nesting is always: _exec_lock -> _lock.
        self._exec_lock = threading.Lock()
        # Latency measurement (config.latency_trace); 0.0 = no active release.
        self._latency_trace = bool(getattr(config, "latency_trace", False))
        self._stop_entered_at = 0.0
        # Audio chunks continuously read by the capture thread.
        self._audio_queue: queue.Queue[bytes] = queue.Queue()
        self._capture_thread: threading.Thread | None = None
        # Physical state of the trigger key (press/release). Updated on EVERY
        # key event; a release is never lost, even during finalization.
        self._key_down: bool = False
        self._finalize_queue: deque[FinalizeJob] = deque()  # under _lock
        self._finalizer_running: bool = False  # dispatcher alive; under _lock
        self._shutdown: bool = False  # set once by shutdown(); under _lock
        self._capture_stop = threading.Event()
        self._typing_epoch = 0  # under _lock
        self._typed_epoch = 0  # under _exec_lock (read/written only in _run_once_exec and _run_finalize_job)
        # Phase 2 (A): True while a live tick is inside the engine; under _lock.
        self._live_tick_active = False

    # ------------------------------------------------------------------
    # Trigger
    # ------------------------------------------------------------------
    def start_recording(self) -> None:
        """Begin recording (called on key press).

        Important: The key state (``_key_down``) is ALWAYS set, even during
        finalization. IDLE and FINALIZING both start now; earlier recordings
        keep finalizing in the background queue.
        """
        with self._lock:
            self._key_down = True
            if self.state == ControllerState.RECORDING:
                return  # already active -> idempotent
            # Count includes the job the dispatcher is currently working on.
            pending = len(self._finalize_queue) + (1 if self._finalizer_running else 0)
            if pending >= self._max_queue_depth:
                log.warning("Finalize queue full (%d) - press ignored", pending)
                return
            self._start_now()
            self._notify_work()

    def _notify_work(self) -> None:
        """Wake waiting workers (condition notify under ``_lock``).

        Must be called while holding ``_lock`` or ``_work_cv``; after wakeup,
        wait_for_work() rechecks its conditions (no lost wakeup; spurious
        wakeups are harmless).
        """
        self._work_cv.notify_all()

    def wait_for_work(self) -> None:
        """Block until new work is available (instead of 20 ms polling).

        Replaces ``time.sleep(0.02)`` in ``Application.run_forever()``.
        NEVER called under ``_exec_lock`` (the waiting worker must not hold up
        live/final inference).

        * IDLE/FINALIZING: block unconditionally until a notify (key press,
          finalization end, abort) wakes the worker.
        * RECORDING with an empty capture queue: block until the next
          deadline: new audio (notify from capture path), no-audio timeout,
          or maximum recording duration, whichever comes first.
        * RECORDING with a non-empty queue: return immediately (lost-wakeup
          protection: the check occurs BEFORE wait(), so an earlier notify is
          not missed).
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
        """Start recording immediately (state already checked)."""
        try:
            self._source = self._source_factory()
            self._source.start()
        except AudioSourceError as exc:
            log.error("Could not start audio source: %s", exc)
            self._source = None
            return

        self._transcriber = self._transcriber_factory(
            self._engine,
            interval_s=self._asr_interval_s,
            min_audio_s=self._min_audio_s,
        )
        self._typing_epoch += 1
        self.state = ControllerState.RECORDING
        self._started_at = time.monotonic()
        self._last_audio_at = time.monotonic()
        self._total_bytes = 0

        self._audio_queue = queue.Queue()
        self._capture_stop = threading.Event()
        self._capture_thread = threading.Thread(
            target=self._capture_loop,
            args=(self._source, self._audio_queue, self._capture_stop),
            name="audio-capture",
            daemon=True,
        )
        self._capture_thread.start()
        log.info("Recording started")

    def stop_recording(self) -> None:
        """Stop recording and finalize (called on key release).

        The key state is ALWAYS updated, even during finalization. Otherwise,
        the release edge would be lost, and a key pressed during finalization
        would trigger a phantom recording.

        Latency STAGE 1: The state changes to FINALIZING only briefly under
        ``_lock``; the actual stop (``_finish``) runs WITHOUT either lock so
        that the audio source stops in < 0.3 s even during live inference.
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
        """Debug measurement point for release->output latency (latency_trace only).

        Measure monotonic time since entering ``stop_recording()``; without a
        release (e.g., maximum duration), measure since recording start.
        """
        if not self._latency_trace:
            return
        base = self._stop_entered_at or self._started_at
        log.debug("latency[%s] +%.3fs", point, time.monotonic() - base)

    def abort(self) -> None:
        """Abort without outputting text (error case)."""
        with self._lock:
            self._capture_stop.set()
            if self._source is not None:
                try:
                    self._source.stop()
                except AudioSourceError:
                    pass
                self._source = None
            self._transcriber = None
            self.state = ControllerState.IDLE
            self._notify_work()
            log.info("Recording aborted")

    def shutdown(self) -> None:
        """Shut down everything (SIGTERM / process exit).

        Queued finalize jobs are dropped; a job already running finishes.
        """
        with self._lock:
            self._shutdown = True
            self._finalize_queue.clear()
        self.abort()

    # ------------------------------------------------------------------
    # Operation
    # ------------------------------------------------------------------
    def run_once(self) -> None:
        """Process one time step (read audio, transcribe if needed).

        Runs under ``_exec_lock`` (NOT under ``_lock``): live inference must
        never block the key thread (press/release).
        """
        with self._exec_lock:
            self._run_once_exec()

    def _run_once_exec(self) -> None:
        """One live tick under ``_exec_lock``; state checked briefly under ``_lock``."""
        with self._lock:
            if self.state != ControllerState.RECORDING:
                return
            source = self._source
            transcriber = self._transcriber
            audio_queue = self._audio_queue
        if source is None or transcriber is None:
            # Abort only while recording is still active: a release
            # (FINALIZING) hands over to the finalizer; abort would delete its
            # state.
            with self._lock:
                live = self.state == ControllerState.RECORDING
            if live:
                self.abort()
            return

        # Drain chunks from the capture queue (do not read directly from the
        # source, so ASR inference does not block reading).
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
                log.warning("No audio data for %.0fs - aborting",
                            self._no_audio_timeout_s)
                self.abort()
                return

        self._enforce_max_duration()

        # Brief state check BEFORE transcription: a release (FINALIZING) or
        # abort makes this tick a no-op (its result would be stale and would
        # overwrite the final text).
        with self._lock:
            if self.state != ControllerState.RECORDING:
                return
            # Phase 2 (A): mark the tick as cancellable atomically with the
            # state check; drop a stray cancel left without a finalizer job
            # (_finish with transcriber None). _finish only cancels while this
            # flag is True, so a clear here can never swallow a real cancel.
            self._live_tick_active = True
            clear_cancel = getattr(self._engine, "clear_cancel", None)
            if clear_cancel is not None:
                clear_cancel()

        try:
            self._trace("live asr start")
            result = transcriber.maybe_transcribe()
            self._trace("live asr end")
        except InferenceCancelled:
            # Phase 2 (A): release cancelled this tick on purpose; the
            # finalizer owns the recording now. Not an error, no abort.
            log.info("Live tick cancelled by release")
            return
        except Exception as exc:  # noqa: BLE001 - ASR error -> clean abort
            log.error("Transcription failed: %s", exc)
            self.abort()
            return
        finally:
            with self._lock:
                self._live_tick_active = False

        # STAGE 2: post-release discard. The live tick may have been blocked
        # in the engine for seconds; a release during that time has already
        # handed the recording to the finalizer. The live result now available
        # would be stale: it would preempt the final text, and the finalizer
        # would have to remove it with backspaces (perceived latency). Discard
        # instead of typing; the finalizer supplies the complete text relative
        # to the effective state.
        with self._lock:
            stale = (
                self.state != ControllerState.RECORDING
                or self._transcriber is not transcriber
            )
        if stale:
            log.info("Discarding live tick: recording already completed")
            return

        with self._lock:
            busy = bool(self._finalize_queue) or self._finalizer_running
            epoch = self._typing_epoch
        if busy:
            return  # older recording still finalizing; audio stays buffered
        if self._typed_epoch != epoch:
            self._delta_typer.reset()
            self._typed_epoch = epoch

        # STAGE 4 (pause indicator): If the transcriber reports that live ASR
        # has reached the live_asr_until_s limit, DeltaTyper types the
        # indicator EXACTLY ONCE ("still listening, but not writing right
        # now"). finalize() removes it again through the normal difference.
        # Only StreamTranscriber has a live limit (windowed/buffered/fakes do
        # not) -> getattr guard. With live_asr_until_s=0.0 (default), this is
        # NEVER reported.
        take_live_paused = getattr(transcriber, "take_live_paused", None)
        if take_live_paused is not None and take_live_paused():
            self._trace("live paused")
            self._delta_typer.show_paused()

        if result is not None:
            self._trace("live typing start")
            self._delta_typer.update(result.text, result.committed_len)
            self._trace("live typing end")

    def _capture_loop(
        self,
        source: AudioSource,
        audio_queue: "queue.Queue[bytes]",
        stop_event: threading.Event,
    ) -> None:
        """Continuously read audio from the source into the queue.

        Source and queue are bound as arguments so that a delayed capture
        thread from an old recording never writes into a new recording's
        queue.
        """
        while not stop_event.is_set():
            try:
                chunk = source.read(block=False)
            except AudioSourceError:
                break
            if chunk:
                audio_queue.put(chunk)
                # Wake the worker: ONLY the notify, no ASR/typer operations
                # in the capture thread (they run exclusively under _exec_lock
                # in the worker).
                with self._lock:
                    self._notify_work()
            else:
                time.sleep(0.02)

    def _enforce_max_duration(self) -> None:
        """Finish after ``_max_record_s``, as on key release."""
        if self.state != ControllerState.RECORDING:
            return
        with self._lock:
            expired = time.monotonic() - self._started_at >= self._max_record_s
        if expired:
            log.info("Maximum recording duration reached - finalizing")
            self._finish()

    def _finish(self) -> None:
        """Stop recording immediately and initiate finalization.

        Runs WITHOUT _lock and WITHOUT _exec_lock (except for the brief state
        change): release stops the audio source in < 0.3 s even when
        multi-second live inference is running under _exec_lock.

        The RC-B2/B3 order "queue drain before stop() remainder" is preserved:
        both feeds occur in the finalizer UNDER _exec_lock, after every live
        tick still running on the same transcriber.
        """
        with self._lock:
            if self.state != ControllerState.RECORDING:
                return
            self.state = ControllerState.FINALIZING
            source = self._source
            transcriber = self._transcriber
            audio_queue = self._audio_queue
            capture_thread = self._capture_thread
            self._capture_stop.set()
            # Phase 2 (A): a live tick still inside the engine would only be
            # discarded (post-release discard); stop it so the finalizer can
            # take _exec_lock at once. Event.set() only - no lock taken.
            if self._live_tick_active:
                cancel = getattr(self._engine, "request_cancel", None)
                if cancel is not None:
                    cancel()
            self._capture_thread = None
            self._transcriber = None
            job = FinalizeJob(transcriber=transcriber, audio_queue=audio_queue,
                              rest=b"", started_at=self._started_at,
                              epoch=self._typing_epoch,
                              total_bytes=self._total_bytes)
            self._total_bytes = 0
            self._notify_work()

        # RC-B2/B3: First stop the capture thread (it checks the state, which
        # is already FINALIZING, and exits almost immediately), THEN drain the
        # queue, THEN feed the stop() remainder.
        if capture_thread is not None:
            capture_thread.join(timeout=1.0)
        self._trace("capture stopped")

        # Stop the audio source IMMEDIATELY; retain the pipe remainder (newest
        # bytes) and feed it at the end of the buffer only in the finalizer.
        rest = b""
        if source is not None:
            try:
                rest = source.stop()
            except AudioSourceError:
                rest = b""
            with self._lock:
                self._source = None
        self._trace("source stopped")

        # Final transcription can take several seconds (CPU). It runs in the
        # BACKGROUND via a FIFO dispatcher so the key thread is immediately
        # ready again and a quickly following dictation is not lost.
        if transcriber is None:
            with self._lock:
                if not self._finalizer_running and self.state == ControllerState.FINALIZING:
                    self.state = ControllerState.IDLE
                self._notify_work()
            return
        job.rest = rest
        with self._lock:
            self._finalize_queue.append(job)
            start_dispatcher = not self._finalizer_running
            self._finalizer_running = True
            self._notify_work()
        if start_dispatcher:  # source already stopped above (hard rule)
            threading.Thread(target=self._finalize_worker, name="finalize",
                             daemon=True).start()

    def _play_done_sound(self) -> None:
        """Play the configured completion signal (config.done_sound).

        Empty = off. Otherwise, quietly play the freedesktop sound name (e.g.,
        'complete') via paplay (done_sound_volume, parts per 65536 of the base
        volume). The sound must NEVER slow down or abort the finalizer: no
        long waits, no exceptions propagated (headless/no sink = silent).
        """
        name = str(getattr(self, "_done_sound", "") or "").strip()
        if not name:
            return
        # Focus mode (Kimi, 31.10.): terminal mode suppresses the sound when
        # a NON-terminal is definitively focused. None (extension missing/not
        # responding) -> play the sound anyway.
        if getattr(self, "_done_sound_mode", "always") == "terminal":
            state = focus.terminal_focused()
            if state is False:
                log.debug("Completion signal suppressed (no terminal focused)")
                return
        binary = shutil.which("paplay")
        if binary is None:
            log.debug("paplay unavailable - no completion signal")
            return
        # Absolute path (custom sound) or freedesktop sound name.
        path = name if os.path.isabs(name) else os.path.join(
            SOUND_DIR, f"{name}.oga"
        )
        if not os.path.exists(path):
            log.warning("Done sound %r not found (%s)", name, path)
            return
        volume = str(getattr(self, "_done_sound_volume", 65536))
        try:
            # FIRE-AND-FORGET (Ground-Truth, 31.10.): paplay BLOCKS until the
            # sound ends; in the finalizer thread, this delayed the "Recording
            # finished" log by ~1 s and held up the exec logic. Startup errors
            # (binary missing) are still caught; a hanging player is bounded
            # by playback ending, and DEVNULL prevents pipe deadlocks.
            subprocess.Popen(
                [binary, "--volume", volume, path],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as exc:  # noqa: BLE001 - sound is optional, never required
            log.debug("Completion signal failed (%s)", exc)

    def _finalize_worker(self) -> None:
        """Dispatcher thread: run queued FinalizeJobs strictly FIFO."""
        while True:
            with self._lock:
                if self._shutdown or not self._finalize_queue:
                    self._finalizer_running = False
                    if self.state != ControllerState.RECORDING:
                        self.state = ControllerState.IDLE
                    self._stop_entered_at = 0.0
                    self._notify_work()
                    return
                job = self._finalize_queue.popleft()
            self._run_finalize_job(job)

    def _run_finalize_job(self, job: FinalizeJob) -> None:
        """Final transcription of ONE job under ``_exec_lock``.

        No live inference or DeltaTyper access runs in parallel with the
        final feed/finalize/output. Errors drop only this job.
        """
        transcriber = job.transcriber
        try:
            with self._exec_lock:
                self._trace("finalizer exec acquired")
                # Phase 2 (A): no live tick can run while we hold _exec_lock;
                # drop any cancel request aimed at a (finished) live tick.
                clear_cancel = getattr(self._engine, "clear_cancel", None)
                if clear_cancel is not None:
                    clear_cancel()
                if self._typed_epoch != job.epoch:
                    self._delta_typer.reset()
                    self._typed_epoch = job.epoch
                # 1) Drain the queue in chronological order (older bytes
                #    first), 2) append the stop() remainder.
                while True:
                    try:
                        chunk = job.audio_queue.get_nowait()
                    except queue.Empty:
                        break
                    job.total_bytes += len(chunk)
                    transcriber.feed(chunk)
                if job.rest:
                    transcriber.feed(job.rest)

                # BUG 1 (fix b): The live path may NEVER have seen the limit:
                # the last live tick started below the limit, its inference
                # continued past the threshold, and release occurred before
                # the next tick (11:56 live test). Only the queue/remainder
                # feed above has now pushed the buffer past the threshold.
                # take_live_paused() detects the limit ITSELF; here it is
                # reported no later than the start of final inference. The
                # final difference (3 backspaces) removes the indicator
                # automatically. Only StreamTranscriber has a live limit ->
                # getattr guard.
                take_live_paused = getattr(transcriber, "take_live_paused", None)
                if take_live_paused is not None and take_live_paused():
                    self._trace("live paused")
                    self._delta_typer.show_paused()

                # Save the complete recording for debugging.
                self._trace("dump start")
                self._dump_audio(transcriber)
                self._trace("dump end")

                self._trace("finalize start")
                result = transcriber.finalize()
                self._trace("finalize end")
                self._delta_typer.finalize(result.text, result.committed_len)
                self._trace("paste")
                # Completion signal (operator, 31.10.): only AFTER output has
                # completed successfully. The sound means "text is ready and
                # can now be pasted" (Kimi semantics: after triggering the
                # chord, only "triggered", never "received"). On an exception
                # above: NO sound (honest feedback).
                self._play_done_sound()
        except Exception as exc:  # noqa: BLE001
            log.error("Final transcription failed: %s", exc)
        finally:
            duration = time.monotonic() - job.started_at
            self._trace(f"job done after {duration:.3f}s")
            log.info("Recording finished (%d bytes of audio, %.2f s since recording start)",
                     job.total_bytes, duration)

    def _dump_audio(self, transcriber) -> None:
        """Save the complete recording as a debug WAV.

        Modes (config.audio_dump_mode):
        * ``"sync"`` (default, previous behavior): write immediately in the
          caller thread (finalizer).
        * ``"async"``: snapshot the bytes and write in a separate thread; the
          finalizer does not wait for file I/O. The thread holds only the
          immutable snapshot, NEVER a reference to the transcriber buffer
          (which may still be in use).
        * ``"off"``: no dump.
        """
        if self._audio_dump_mode == "off" or transcriber is None:
            return
        try:
            full_audio = getattr(transcriber, "full_audio", None)
            # Phase 2 (C): dump ALL segments, not only the current buffer.
            snapshot = full_audio() if full_audio is not None else bytes(transcriber._buffer)
            if not snapshot:
                return
            sample_rate = transcriber.sample_rate
        except Exception as exc:  # noqa: BLE001
            log.debug("Audio dump failed: %s", exc)
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
        """Write s16le mono bytes as a WAV (debug dump)."""
        try:
            import wave

            with wave.open(self._dump_path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(sample_rate)
                w.writeframes(pcm)
            log.info("Recording saved: %s", self._dump_path)
        except Exception as exc:  # noqa: BLE001
            log.debug("Audio dump failed: %s", exc)