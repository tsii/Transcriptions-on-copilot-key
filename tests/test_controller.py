"""Tests for the controller / integration (TDD chain 6).

PushToTalkController connects the key trigger, audio source, StreamTranscriber,
and DeltaTyper. Tests run end-to-end with fakes—no microphone, model, or
Wayland tool.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

import pytest

from stt_button.audio import AudioSource
from stt_button.asr import TranscriptionResult
from stt_button.controller import ControllerState, PushToTalkController
from stt_button.typing import DeltaTyper


class FakeAudioSource:
    """Schiebt vordefinierte Chunks."""

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = list(chunks)
        self.started = 0
        self.stopped = 0

    def start(self) -> None:
        self.started += 1

    def stop(self) -> bytes:
        self.stopped += 1
        out = b"".join(self.chunks)
        self.chunks = []
        return out

    def read(self, block: bool = True) -> bytes:
        if self.chunks:
            return self.chunks.pop(0)
        return b""


class FakeEngine:
    """Transcribe audio deterministically based on buffer length."""

    def __init__(self) -> None:
        self.calls = 0

    def transcribe(self, data: bytes, initial_prompt: str = "") -> TranscriptionResult:
        self.calls += 1
        # 0.5s = 16000 bytes -> one "word" per 0.5s
        words = len(data) // 16000
        text = " ".join(f"wort{i}" for i in range(words))
        return TranscriptionResult(text=text)


class FakeTyper:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def type_text(self, text: str) -> None:
        self.calls.append(("type", text))

    @property
    def typed_text_final(self) -> str:
        """Getippter Endstand: Backspaces abgezogen (wie am Cursor)."""
        text = ""
        for c in self.calls:
            if c[0] == "type":
                text += c[1]
            elif c[0] == "backspace":
                text = text[: len(text) - int(c[1])]
        return text

    def backspace(self, n: int) -> None:
        self.calls.append(("backspace", str(n)))

    def key(self, name: str) -> None:
        self.calls.append(("key", name))


class BlockingTranscriber:
    """Fake StreamTranscriber whose finalize() waits for an event.

    Makes the end of finalization deterministically controllable; without
    threading.Events, tests would have to guess sleep durations (flaky).
    """
    def __init__(self, unblock: threading.Event) -> None:
        self._unblock = unblock
        self._buffer = b"\x00" * 16000  # for a potential audio dump
        self.sample_rate = 16000
        self.finalized = threading.Event()

    def feed(self, data: bytes) -> None:
        pass

    def maybe_transcribe(self):
        return None

    def finalize(self):
        self._unblock.wait()
        return TranscriptionResult(text="fertig")


class FakeSourceFactory:
    """Count created sources (one new source per recording)."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)
        self.created: list[FakeAudioSource] = []

    def __call__(self) -> FakeAudioSource:
        source = FakeAudioSource(self._chunks)
        self.created.append(source)
        return source

    @property
    def started(self) -> int:
        return sum(1 for s in self.created if s.started > 0)

    @property
    def stopped(self) -> int:
        return sum(1 for s in self.created if s.stopped > 0)


def make_controller(**kwargs) -> tuple[PushToTalkController, dict]:
    blocking = kwargs.pop("blocking_transcriber", False)
    if blocking:
        unblock = threading.Event()
        factory = FakeSourceFactory([b"\x00" * 16000] * 6)
        engine = FakeEngine()
        typer = FakeTyper()
        delta = DeltaTyper(typer, append_space=True)
        ctrl = PushToTalkController(
            config=None,
            source_factory=factory,
            engine=engine,
            delta_typer=delta,
            transcriber_factory=lambda *a, **kw: BlockingTranscriber(unblock),
            **kwargs,
        )
        ctrl._test_unblock_finalize = unblock
        ctrl._wait_state = lambda target, timeout=5.0: _wait_state(
            ctrl, target, timeout
        )
        return ctrl, {"source": factory, "engine": engine, "typer": typer,
                      "delta": delta}
    source = FakeAudioSource([b"\x00" * 16000] * 6)
    engine = FakeEngine()
    typer = FakeTyper()
    delta = DeltaTyper(typer, append_space=True)
    ctrl = PushToTalkController(
        config=None,
        source_factory=lambda: source,
        engine=engine,
        delta_typer=delta,
        **kwargs,
    )
    return ctrl, {
        "source": source,
        "engine": engine,
        "typer": typer,
        "delta": delta,
    }


def _wait_state(ctrl: PushToTalkController, target: ControllerState,
                timeout: float = 5.0) -> None:
    """Wait (with a deadline, no blind sleep) until the state is reached."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ctrl.state == target:
            return
        time.sleep(0.005)
    raise AssertionError(
        f"Zustand {target} nicht erreicht (ist {ctrl.state})"
    )


class TestStateLifecycle:
    def test_initial_state_is_idle(self) -> None:
        ctrl, _ = make_controller()
        assert ctrl.state == ControllerState.IDLE

    def test_start_recording_changes_state(self) -> None:
        ctrl, ctx = make_controller()
        ctrl.start_recording()
        assert ctrl.state == ControllerState.RECORDING
        assert ctx["source"].started == 1

    def test_double_start_is_ignored(self) -> None:
        ctrl, ctx = make_controller()
        ctrl.start_recording()
        ctrl.start_recording()
        assert ctx["source"].started == 1

    def test_stop_recording_changes_state(self) -> None:
        ctrl, ctx = make_controller()
        ctrl.start_recording()
        ctrl.stop_recording()
        # Finalization runs in the background -> wait briefly.
        for _ in range(100):
            if ctrl.state == ControllerState.IDLE:
                break
            time.sleep(0.02)
        assert ctrl.state == ControllerState.IDLE
        assert ctx["source"].stopped == 1

    def test_release_during_finalizing_updates_key_state(self) -> None:
        """Fix: release during FINALIZING always updates _key_down.

        Previously, stop_recording() returned silently when state != RECORDING;
        the release edge was lost and a ghost recording still started later.
        """
        ctrl, ctx = make_controller(blocking_transcriber=True)
        ctrl.start_recording()
        ctrl.stop_recording()            # -> FINALIZING
        assert ctrl.state == ControllerState.FINALIZING
        ctrl.start_recording()           # press during FINALIZING
        assert ctrl._key_down is True
        ctrl.stop_recording()            # release during FINALIZING
        # The release edge must NEVER be lost:
        assert ctrl._key_down is False
        # Now allow finalization to complete.
        ctrl._test_unblock_finalize.set()
        _wait_state(ctrl, ControllerState.IDLE)
        # Press during FINALIZING started rec 2; release finalized it.
        assert ctrl.state == ControllerState.IDLE
        assert ctx["source"].started == 2

    def test_key_held_through_finalize_restarts_recording(self) -> None:
        """Key remains held: recording starts after finalization."""
        ctrl, ctx = make_controller(blocking_transcriber=True)
        ctrl.start_recording()
        ctrl.stop_recording()            # -> FINALIZING
        ctrl.start_recording()           # press (hold) during FINALIZING
        ctrl._test_unblock_finalize.set()
        ctrl._wait_state(ControllerState.RECORDING)
        # Starts only because _key_down is still True.
        assert ctrl.state == ControllerState.RECORDING
        assert ctrl._key_down is True
        assert ctx["source"].started == 2

    def test_start_after_finalizing_only_when_key_held(self) -> None:
        """Press+release during FINALIZING: rec 2 starts and finalizes, ends IDLE."""
        ctrl, ctx = make_controller(blocking_transcriber=True)
        ctrl.start_recording()
        ctrl.stop_recording()
        ctrl.start_recording()           # starts rec 2 immediately
        ctrl.stop_recording()            # release queues rec 2 for finalize
        ctrl._test_unblock_finalize.set()
        _wait_state(ctrl, ControllerState.IDLE)
        assert ctrl.state == ControllerState.IDLE
        assert ctx["source"].started == 2

    def test_release_after_finalized_start_stops_fresh_recording(self) -> None:
        """Release immediately after automatic start stops the new recording.

        Previously, a release in the handoff window failed due to state==IDLE
        (or the start and release raced each other).
        """
        ctrl, ctx = make_controller(blocking_transcriber=True)
        ctrl.start_recording()
        ctrl.stop_recording()            # -> FINALIZING
        ctrl.start_recording()           # key held
        ctrl._test_unblock_finalize.set()
        ctrl._wait_state(ControllerState.RECORDING)  # automatic start has occurred
        ctrl.stop_recording()           # release afterward
        ctrl._test_unblock_finalize.set()  # release the second finalization
        _wait_state(ctrl, ControllerState.IDLE)
        assert ctrl._key_down is False
        assert ctx["source"].started == 2
        assert ctx["source"].stopped == 2

    def test_press_during_recording_is_idempotent(self) -> None:
        """Repeated presses during RECORDING do not start anything new."""
        ctrl, ctx = make_controller(blocking_transcriber=True)
        ctrl.start_recording()
        ctrl.start_recording()           # press again
        assert ctrl._key_down is True
        assert ctrl.state == ControllerState.RECORDING
        assert ctx["source"].started == 1
        ctrl.stop_recording()
        ctrl._test_unblock_finalize.set()
        _wait_state(ctrl, ControllerState.IDLE)
        assert ctx["source"].started == 1

    def test_keydown_during_finalize_starts_recording_immediately(self) -> None:
        ctrl, ctx = make_controller(blocking_transcriber=True)
        ctrl.start_recording()
        ctrl.stop_recording()            # job 1 blocks in finalize()
        assert ctrl.state == ControllerState.FINALIZING
        ctrl.start_recording()           # press during FINALIZING
        assert ctrl.state == ControllerState.RECORDING
        assert ctx["source"].started == 2
        ctrl.stop_recording()            # job 2 queued
        ctrl._test_unblock_finalize.set()
        _wait_state(ctrl, ControllerState.IDLE)
        assert ctx["source"].stopped == 2
        assert len(ctrl._finalize_queue) == 0
        assert ctrl._finalizer_running is False

    def test_capture_loop_binds_own_queue(self) -> None:
        ctrl, ctx = make_controller(blocking_transcriber=True)
        ctrl.start_recording()
        first_queue = ctrl._audio_queue
        first_thread = ctrl._capture_thread
        first_stop = ctrl._capture_stop
        ctrl.stop_recording()
        ctrl.start_recording()           # immediate restart
        assert first_stop.is_set()
        assert not ctrl._capture_stop.is_set()
        assert ctrl._audio_queue is not first_queue
        first_thread.join(timeout=2.0)
        assert not first_thread.is_alive(), "old capture thread still running"
        assert ctrl._capture_thread.is_alive()
        ctrl.stop_recording()
        ctrl._test_unblock_finalize.set()
        _wait_state(ctrl, ControllerState.IDLE)


    def test_stop_without_start_is_noop(self) -> None:
        ctrl, ctx = make_controller()
        ctrl.stop_recording()
        assert ctx["source"].stopped == 0


class TestStreaming:
    def test_run_once_feeds_and_transcribes(self) -> None:
        ctrl, ctx = make_controller()
        ctrl.start_recording()
        # Two run_once calls = 2 chunks of 0.5s = 1.0s >= min_audio_s (0.8s)
        ctrl.run_once()
        ctrl.run_once()
        assert ctx["engine"].calls >= 1

    def test_text_reaches_typer(self) -> None:
        ctrl, ctx = make_controller()
        ctrl.start_recording()
        for _ in range(3):
            ctrl.run_once()
        assert any(c[0] == "type" for c in ctx["typer"].calls)

    def test_release_finalizes_and_appends_space(self) -> None:
        ctrl, ctx = make_controller()
        ctrl.start_recording()
        for _ in range(3):
            ctrl.run_once()
        ctrl.stop_recording()
        # Finalization intentionally runs in a background thread; wait (with a bound)
        # for completion instead of relying on thread scheduling.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and ctrl.state != ControllerState.IDLE:
            time.sleep(0.005)
        assert any(c[0] == "key" and c[1] == "space" for c in ctx["typer"].calls)


class TestTimeouts:
    def test_no_audio_timeout_aborts(self) -> None:
        source = FakeAudioSource([])  # no audio data
        typer = FakeTyper()
        ctrl = PushToTalkController(
            config=None,
            source_factory=lambda: source,
            engine=FakeEngine(),
            delta_typer=DeltaTyper(typer, append_space=True),
            no_audio_timeout_s=0.01,
        )
        ctrl.start_recording()
        time.sleep(0.02)  # exceed the timeout
        ctrl.run_once()
        assert ctrl.state == ControllerState.IDLE

    def test_max_record_auto_finalizes(self) -> None:
        """Maximum duration finalizes; no automatic restart without a held key.

        If the key is released before finalization ends (as in a real release),
        the controller remains IDLE. If the user keeps holding it, the next
        recording starts automatically; this is covered by
        test_key_held_through_finalize_restarts_recording.
        """
        ctrl, ctx = make_controller(blocking_transcriber=True)
        ctrl._max_record_s = 0.01
        ctrl.start_recording()
        time.sleep(0.02)
        ctrl._enforce_max_duration()
        assert ctrl.state == ControllerState.FINALIZING
        ctrl.stop_recording()          # release the key
        assert ctrl._key_down is False
        ctrl._test_unblock_finalize.set()
        _wait_state(ctrl, ControllerState.IDLE)
        assert ctrl.state == ControllerState.IDLE
        assert ctx["source"].stopped == 1


class TestErrors:
    def test_engine_error_aborts(self) -> None:
        class Boom:
            def transcribe(self, data, initial_prompt=""):
                raise RuntimeError("kaputt")

        source = FakeAudioSource([b"\x00" * 16000] * 3)
        typer = FakeTyper()
        ctrl = PushToTalkController(
            config=None,
            source_factory=lambda: source,
            engine=Boom(),
            delta_typer=DeltaTyper(typer, append_space=True),
        )
        ctrl.start_recording()
        ctrl.run_once()
        ctrl.run_once()  # enough audio for a transcription
        assert ctrl.state == ControllerState.IDLE

    def test_abort_resets_state(self) -> None:
        ctrl, ctx = make_controller()
        ctrl.start_recording()
        ctrl.abort()
        assert ctrl.state == ControllerState.IDLE
        assert ctx["source"].stopped == 1


class TestShutdown:
    def test_shutdown_stops_source(self) -> None:
        ctrl, ctx = make_controller()
        ctrl.start_recording()
        ctrl.shutdown()
        assert ctx["source"].stopped == 1


class TestCaptureThread:
    def test_audio_not_lost_during_blocking_inference(self) -> None:
        """The capture thread keeps reading even when the engine blocks.

        Reproduces the 'key held longer than speech' bug: the engine blocks
        (ASR inference), but the source provides more chunks. Without a capture
        thread, these chunks were lost.
        """
        # The source continuously provides chunks (more than the main thread can read).
        source = FakeAudioSource([b"\x00" * 16000] * 50)
        typer = FakeTyper()
        ctrl = PushToTalkController(
            config=None,
            source_factory=lambda: source,
            engine=FakeEngine(),
            delta_typer=DeltaTyper(typer, append_space=True),
        )
        ctrl.start_recording()
        # Give the capture thread time to read chunks into the queue.
        time.sleep(0.3)
        # All 50 chunks must have reached the queue (no loss while the thread runs,
        # regardless of whether the engine blocks).
        assert ctrl._audio_queue.qsize() == 50
        ctrl.stop_recording()

class TestFinalizePasteLockHierarchy:
    """Characterization: a FinalPaster hang does NOT block the key thread.

    The controller lock hierarchy is intentionally designed as follows:

    * ``finalize``/``paste`` run IN THE FINALIZER THREAD and do NOT access
      the controller lock (controller.py: _finish holds the lock only for the
      brief state transition).
    * ``start_recording``/``stop_recording`` acquire the lock only briefly
      and return immediately even if the finalizer is still stuck in
      final_paste.

    This test characterizes that behavior: even if FinalPaster blocks forever,
    the key thread remains responsive and key edges are processed correctly.
    """

    def test_blocking_final_paster_does_not_block_key_thread(self) -> None:
        """Hanging final_paste: start/stop return without blocking."""
        unblock_paste = threading.Event()
        paste_entered = threading.Event()

        class BlockingFinalPaster:
            """Fake FinalPaster that waits for an event never set (immediately)."""

            def paste(self, text: str) -> None:
                paste_entered.set()
                unblock_paste.wait(10.0)

        source = FakeAudioSource([b"\x00" * 16000] * 6)
        typer = FakeTyper()
        delta = DeltaTyper(typer, append_space=True, final_paster=BlockingFinalPaster())
        ctrl = PushToTalkController(
            config=None,
            source_factory=lambda: source,
            engine=FakeEngine(),
            delta_typer=delta,
            transcriber_factory=lambda *a, **kw: InstantTranscriber(),
        )

        ctrl.start_recording()
        for _ in range(3):
            ctrl.run_once()
        # Release: the finalizer starts in the background and hangs in paste.
        ctrl.stop_recording()
        assert ctrl.state == ControllerState.FINALIZING
        assert paste_entered.wait(timeout=2.0)

        # Core characterization: the key thread (simulated by this thread) returns
        # immediately despite the hanging paste; start/stop hold the lock only
        # briefly, while paste() runs lock-free in the finalizer.
        t0 = time.monotonic()
        ctrl.start_recording()  # press during FINALIZING
        assert ctrl._key_down is True
        ctrl.stop_recording()  # release
        assert ctrl._key_down is False
        assert time.monotonic() - t0 < 0.5, (
            "start/stop_recording blockieren am haengenden final_paste - "
            "Lock-Hierarchie verletzt"
        )
        assert ctrl.state == ControllerState.FINALIZING

        # Now unblock paste: the finalizer completes.
        unblock_paste.set()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if ctrl.state == ControllerState.IDLE:
                break
            time.sleep(0.005)
        assert ctrl.state == ControllerState.IDLE
        # NO ghost recording: the key was released.
        assert ctrl._key_down is False


class RestAudioSource:
    """Source that additionally returns pipe remainder ``rest`` from stop().

    The capture thread reads only ``chunks``; the final pipe bytes (``rest``)
    appear only when drained in ``stop()``, as with pw-record.
    """

    def __init__(self, chunks: list[bytes], rest: bytes) -> None:
        self.chunks = list(chunks)
        self.rest = rest
        self.started = 0
        self.stopped = 0

    def start(self) -> None:
        self.started += 1

    def stop(self) -> bytes:
        self.stopped += 1
        out, self.chunks = b"".join(self.chunks), []
        return out + self.rest

    def read(self, block: bool = True) -> bytes:
        if self.chunks:
            return self.chunks.pop(0)
        return b""


class TestLatencyLockDecoupling:
    """STAGE 1: release must not wait for ongoing live inference.

    Controller lock ``_lock`` now protects only brief state transitions; the
    live/final chain runs under ``_exec_lock``. Therefore, release stops the
    audio source immediately even during multi-second live inference.
    """

    def test_release_during_live_asr_stops_audio_before_inference_returns(
        self,
    ) -> None:
        """Release during blocking live inference: audio stops in < 0.3s."""
        engine = BlockingEngine()
        source = FakeAudioSource([b"\x00" * 16000] * 8)
        typer = FakeTyper()
        ctrl = PushToTalkController(
            config=None,
            source_factory=lambda: source,
            engine=engine,
            delta_typer=DeltaTyper(typer, append_space=True),
            min_audio_s=0.1,
            asr_interval_s=0.1,
        )
        ctrl.start_recording()
        ticker = threading.Thread(target=ctrl.run_once, daemon=True)
        ticker.start()
        # Live inference is running (engine blocks for up to 2s in transcribe()).
        assert engine.entered.wait(timeout=5.0), "Live-Inferenz started nicht"

        t0 = time.monotonic()
        ctrl.stop_recording()
        elapsed = time.monotonic() - t0

        # Core of STAGE 1: the source stopped even though the engine is still busy,
        # and stop_recording returned immediately.
        assert not engine.release.is_set(), "Engine wurde vor Release freigegeben"
        assert source.stopped == 1, "Audioquelle beim Release nicht gestoppt"
        assert elapsed < 0.3, (
            f"stop_recording blockierte {elapsed:.2f}s an der Live-Inferenz"
        )

        engine.release.set()
        ticker.join(timeout=5.0)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and ctrl.state != ControllerState.IDLE:
            time.sleep(0.005)
        assert ctrl.state == ControllerState.IDLE

    def test_live_tick_and_finalize_are_serialized(self) -> None:
        """Live tick and finalize never call the engine concurrently."""
        engine = ConcurrencyCountingEngine()
        source = FakeAudioSource([b"\x00" * 16000] * 8)
        typer = FakeTyper()
        delta = DeltaTyper(typer, append_space=True)
        ctrl = PushToTalkController(
            config=None,
            source_factory=lambda: source,
            engine=engine,
            delta_typer=delta,
            min_audio_s=0.1,
            asr_interval_s=0.1,
        )
        ctrl.start_recording()
        ticker = threading.Thread(
            target=lambda: [
                ctrl.run_once()
                for _ in iter(lambda: ctrl.state == ControllerState.RECORDING, False)
            ],
            daemon=True,
        )
        ticker.start()
        time.sleep(0.3)  # multiple live ticks
        ctrl.stop_recording()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and ctrl.state != ControllerState.IDLE:
            time.sleep(0.005)
        ticker.join(timeout=5.0)

        assert engine.max_active == 1, (
            f"Engine {engine.max_active}x gleichzeitig aufgerufen - "
            "_exec_lock serialisiert Live/Final nicht"
        )
        # Final output comes from the LAST engine call (finalize), not from a live
        # tick that completed later.
        assert delta.last_text == engine.last_text

    def test_release_feeds_exactly_capture_queue_then_source_rest(self) -> None:
        """RC order: all queue chunks, then the stop() remainder, exactly once."""
        recorder = RecordingTranscriber()
        chunks = [bytes([i]) * 16000 for i in range(6)]
        source = RestAudioSource(chunks, rest=b"REST")
        ctrl = PushToTalkController(
            config=None,
            source_factory=lambda: source,
            engine=FakeEngine(),
            delta_typer=DeltaTyper(FakeTyper(), append_space=True),
            transcriber_factory=lambda *a, **kw: recorder,
        )
        ctrl.start_recording()
        time.sleep(0.3)  # Capture thread fills the queue; NO run_once
        ctrl.stop_recording()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and ctrl.state != ControllerState.IDLE:
            time.sleep(0.005)

        assert b"".join(recorder.feeds) == b"".join(chunks) + b"REST", (
            "Queue-Chunks und stop()-Rest nicht exakt einmal in richtiger "
            "Reihenfolge gefuettert"
        )

    def test_source_stopped_before_finalizer_starts(self) -> None:
        """Hard rule: source.stop() runs before the finalizer feeds/finalizes."""
        order: list[str] = []

        class OrderSource(FakeAudioSource):
            def stop(self) -> bytes:
                order.append("stop")
                return super().stop()

        class OrderTranscriber(RecordingTranscriber):
            def feed(self, chunk: bytes) -> None:
                order.append("feed")
                super().feed(chunk)

            def finalize(self):
                order.append("finalize")
                return super().finalize()

        source = OrderSource([b"\x00" * 16000] * 4)
        ctrl = PushToTalkController(
            config=None,
            source_factory=lambda: source,
            engine=FakeEngine(),
            delta_typer=DeltaTyper(FakeTyper(), append_space=True),
            transcriber_factory=lambda *a, **kw: OrderTranscriber(),
        )
        ctrl.start_recording()
        time.sleep(0.2)  # capture thread fills the queue; NO run_once
        ctrl.stop_recording()
        _wait_state(ctrl, ControllerState.IDLE)

        assert order, "nothing recorded"
        assert order[0] == "stop", f"finalizer ran before source.stop(): {order}"
        assert order.count("stop") == 1, order
        assert order[-1] == "finalize", order


class BlockingEngine:
    """Engine whose transcribe() waits for an event (live inference fake).

    The wait is intentionally bounded (2s) so a failing test (previously:
    stop_recording blocked on the lock) does not hang indefinitely.
    """

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def transcribe(self, data: bytes, initial_prompt: str = "") -> TranscriptionResult:
        self.calls += 1
        self.entered.set()
        self.release.wait(timeout=2.0)
        words = max(1, len(data) // 16000)
        return TranscriptionResult(text=" ".join(f"w{i}" for i in range(words)))


class ConcurrencyCountingEngine:
    """Engine that counts concurrent calls."""

    def __init__(self) -> None:
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self.last_text = ""

    def transcribe(self, data: bytes, initial_prompt: str = "") -> TranscriptionResult:
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        time.sleep(0.05)
        text = f"call{self.calls}"
        self.active -= 1
        self.last_text = text
        return TranscriptionResult(text=text)


class RecordingTranscriber:
    """Fake StreamTranscriber that records every feed() chunk."""

    def __init__(self) -> None:
        self.feeds: list[bytes] = []
        self._buffer = bytearray()
        self.sample_rate = 16000

    def feed(self, chunk: bytes) -> None:
        self.feeds.append(bytes(chunk))
        self._buffer.extend(chunk)

    def maybe_transcribe(self):
        return None

    def finalize(self):
        return TranscriptionResult(text="fertig")


class InstantTranscriber:
    """Fake StreamTranscriber with immediate, deterministic finalization."""

    def __init__(self) -> None:
        self._buffer = b"\x00" * 16000
        self.sample_rate = 16000

    def feed(self, data: bytes) -> None:
        pass

    def maybe_transcribe(self):
        return None

    def finalize(self):
        return TranscriptionResult(text="fertig")


class TestLiveAsrUntilController:
    """STAGE 3: live_asr_until_s in the controller (config passed through).

    After the limit, run_once() ticks return no more text; the final text
    comes entirely from finalize().
    """

    def test_ticks_stop_after_limit_final_text_complete(self) -> None:
        from stt_button.config import Config

        typer = FakeTyper()
        # FakeEngine: 0.5s -> 1 word; text grows with the buffer.
        class GrowingEngine:
            def __init__(self) -> None:
                self.calls = 0

            def transcribe(self, data, initial_prompt=""):
                self.calls += 1
                words = max(1, len(data) // 16000)
                return TranscriptionResult(
                    text=" ".join(f"w{i}" for i in range(words))
                )

        engine = GrowingEngine()
        ctrl = PushToTalkController(
            config=Config(),
            source_factory=lambda: FakeAudioSource([b"\x00" * 16000] * 12),
            engine=engine,
            delta_typer=DeltaTyper(typer, append_space=True),
            min_audio_s=0.1,
            asr_interval_s=0.1,
        )
        # Limit 1.5s: live ticks stop after 3 chunks (1.5s).
        ctrl.start_recording()
        ctrl._transcriber.live_asr_until_s = 1.5  # tighten it afterward
        # Drain queue + force a live tick: 6 chunks = 3s buffered.
        for _ in range(6):
            ctrl.run_once()
        calls_before = engine.calls
        # Further ticks with a larger buffer -> NO engine call.
        for _ in range(3):
            ctrl.run_once()
        assert engine.calls == calls_before, (
            "Live-Ticks liefen trotz live_asr_until_s weiter"
        )
        ctrl.stop_recording()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and ctrl.state != ControllerState.IDLE:
            time.sleep(0.005)

        typed = "".join(c[1] for c in typer.calls if c[0] == "type")
        # The final text includes ALL 12 chunks (6s, w0..w11); feed() kept collecting
        # regardless of the live limit, and finalize() saw everything.
        assert typed.endswith("w0 w1 w2 w3 w4 w5 w6 w7 w8 w9 w10 w11"), (
            typer.calls
        )


class TestPauseIndicatorController:
    """STAGE 4: pause indicator in the controller (live_asr_until_s > 0).

    When live ASR reaches the limit, the controller types the indicator
    EXACTLY ONCE through DeltaTyper; on release, finalize() removes it via
    the normal diff, and the final full text contains NO indicator. With
    live_asr_until_s=0.0 (default), an indicator NEVER appears.
    """

    def _make(self, live_asr_until_s: float, *, mode: str = "full"):
        from stt_button.asr import StreamTranscriber

        engine = FakeEngine()
        typer = FakeTyper()
        delta = DeltaTyper(typer, append_space=True, live_typing_mode=mode)

        def make_transcriber(engine_, interval_s, min_audio_s):
            return StreamTranscriber(
                engine_,
                interval_s=interval_s,
                min_audio_s=min_audio_s,
                live_asr_until_s=live_asr_until_s,
            )

        ctrl = PushToTalkController(
            config=None,
            source_factory=lambda: FakeAudioSource([b"\x00" * 16000] * 8),
            engine=engine,
            delta_typer=delta,
            min_audio_s=0.1,
            asr_interval_s=0.1,
            transcriber_factory=make_transcriber,
        )
        return ctrl, typer

    def _wait_idle(self, ctrl: PushToTalkController, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and ctrl.state != ControllerState.IDLE:
            time.sleep(0.005)
        assert ctrl.state == ControllerState.IDLE, (
            f"IDLE nicht erreicht (ist {ctrl.state})"
        )

    def _indicator_calls(self, typer: FakeTyper) -> list[tuple[str, ...]]:
        from stt_button.typing import PAUSE_INDICATOR

        return [c for c in typer.calls if c == ("type", PAUSE_INDICATOR)]

    def test_limit_types_indicator_once_and_final_without_it(self) -> None:
        """(6) One indicator after the limit; release -> full text without '...'."""
        from stt_button.typing import PAUSE_INDICATOR

        ctrl, typer = self._make(live_asr_until_s=1.5)
        ctrl.start_recording()
        # Run live ticks until the limit (1.5s buffer) applies:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            ctrl.run_once()
            if self._indicator_calls(typer):
                break
            time.sleep(0.02)
        assert self._indicator_calls(typer), (
            "Pausen-Indikator wurde nach dem Live-Limit nicht getippt"
        )
        # Further ticks do NOT type the indicator again:
        for _ in range(5):
            time.sleep(0.02)
            ctrl.run_once()
        assert len(self._indicator_calls(typer)) == 1, typer.calls

        # Release: finalize() removes the indicator via backspace (3 characters,
        # '...') and returns the COMPLETE text without the indicator.
        ctrl.stop_recording()
        self._wait_idle(ctrl)
        assert ("backspace", str(len(PAUSE_INDICATOR))) in typer.calls, typer.calls
        typed = "".join(
            c[1] for c in typer.calls
            if c[0] == "type" and c != ("type", PAUSE_INDICATOR)
        )
        assert PAUSE_INDICATOR not in typed
        assert typed == "wort0 wort1 wort2 wort3 wort4 wort5 wort6 wort7", (
            typer.calls
        )

    def test_committed_mode_shows_indicator_too(self) -> None:
        """(6b) live_typing_mode='committed': indicator after the last live commit."""
        from stt_button.typing import PAUSE_INDICATOR

        ctrl, typer = self._make(live_asr_until_s=1.5, mode="committed")
        ctrl.start_recording()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            ctrl.run_once()
            if self._indicator_calls(typer):
                break
            time.sleep(0.02)
        assert len(self._indicator_calls(typer)) == 1, typer.calls
        ctrl.stop_recording()
        self._wait_idle(ctrl)
        assert ("backspace", str(len(PAUSE_INDICATOR))) in typer.calls, typer.calls
        typed = "".join(
            c[1] for c in typer.calls
            if c[0] == "type" and c != ("type", PAUSE_INDICATOR)
        )
        assert typed == "wort0 wort1 wort2 wort3 wort4 wort5 wort6 wort7", (
            typer.calls
        )

    def test_default_limit_zero_never_shows_indicator(self) -> None:
        """(7) Default live_asr_until_s=0: NEVER an indicator (previous behavior)."""
        from stt_button.typing import PAUSE_INDICATOR

        ctrl, typer = self._make(live_asr_until_s=0.0)
        ctrl.start_recording()
        for _ in range(10):
            time.sleep(0.02)
            ctrl.run_once()
        assert not self._indicator_calls(typer), typer.calls
        ctrl.stop_recording()
        self._wait_idle(ctrl)
        assert not self._indicator_calls(typer), typer.calls
        typed = "".join(c[1] for c in typer.calls if c[0] == "type")
        assert PAUSE_INDICATOR not in typed
        assert typed == "wort0 wort1 wort2 wort3 wort4 wort5 wort6 wort7", (
            typer.calls
        )

    def _make_short_session(self, live_asr_until_s: float):
        """Short session (11:56 regression): last tick BELOW the limit.

        Real wiring (real StreamTranscriber, FakeEngine): 7 chunks (3.5s)
        enter the queue live, so NO live tick reaches the 4s limit. The 8th
        chunk (1s) appears only as the stop() remainder in the finalizer.
        Its queue/remainder feed grows the buffer to 4.5s, EXCEEDING the
        limit—exactly the real-test situation (audio continued during the
        last inference; release came before the next tick). Before the fix,
        the indicator NEVER appeared here.
        """
        from stt_button.asr import StreamTranscriber

        engine = FakeEngine()
        typer = FakeTyper()
        delta = DeltaTyper(typer, append_space=True)

        def make_transcriber(engine_, interval_s, min_audio_s):
            return StreamTranscriber(
                engine_,
                interval_s=interval_s,
                min_audio_s=min_audio_s,
                live_asr_until_s=live_asr_until_s,
            )

        source = RestAudioSource(
            [b"\x00" * 16000] * 7, rest=b"\x00" * 16000
        )
        ctrl = PushToTalkController(
            config=None,
            source_factory=lambda: source,
            engine=engine,
            delta_typer=delta,
            min_audio_s=0.1,
            asr_interval_s=0.1,
            no_audio_timeout_s=60.0,
            transcriber_factory=make_transcriber,
        )
        return ctrl, typer

    def test_short_session_indicator_shown_in_finalizer(self) -> None:
        """(BUG1/3) Last tick below limit: indicator in the FINALIZER.

        The live path never reached the limit; only the queue feed in
        _finalize_worker grows the buffer beyond it. Its take_live_paused()
        check types the indicator EXACTLY ONCE before final output; the final
        diff removes it again (3 backspaces, no '...' in the final result).
        """
        from stt_button.typing import PAUSE_INDICATOR

        ctrl, typer = self._make_short_session(live_asr_until_s=4.0)
        ctrl.start_recording()
        # Run ticks until the queue is empty: all 7 chunks (3.5s) are fed, but NO
        # live tick saw >= 4s (the limit was never reached live—the real-test
        # situation).
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            ctrl.run_once()
            with ctrl._lock:
                if ctrl._total_bytes >= 7 * 16000:
                    break
            time.sleep(0.01)
        assert not self._indicator_calls(typer), (
            "Indikator im Live-Pfad getippt - Setup deckt den Bug nicht ab"
        )
        ctrl.stop_recording()
        self._wait_idle(ctrl)
        # EXACTLY ONE PAUSE_INDICATOR, before the final suffix:
        indicators = [
            (i, c) for i, c in enumerate(typer.calls)
            if c == ("type", PAUSE_INDICATOR)
        ]
        assert len(indicators) == 1, (
            f"Pausen-Indikator fehlt im Finalizer (Anzahl: {len(indicators)})",
            typer.calls,
        )
        # The final diff removes it: 3 backspaces present, final result without a
        # '...' remainder (8 chunks = 4.0s -> 8 words). Without FinalPaster,
        # the space is a separate key('space') call (FakeTyper does not count
        # key calls as cursor text); the call itself is the space expectation.
        assert ("backspace", str(len(PAUSE_INDICATOR))) in typer.calls, (
            typer.calls
        )
        assert typer.typed_text_final == (
            "wort0 wort1 wort2 wort3 wort4 wort5 wort6 wort7"
        ), typer.calls
        assert ("key", "space") in typer.calls, typer.calls

    def test_indicator_live_shown_not_repeated_in_finalizer(self) -> None:
        """(BUG1/4) Indicator shown live: NO second one in the finalizer.

        The take_live_paused() notification is consumed: the finalizer check
        must not type it again (DeltaTyper.show_paused() would be idempotent
        anyway, but the call itself is allowed only for a pending notification).
        """
        ctrl, typer = self._make_short_session(live_asr_until_s=1.5)
        ctrl.start_recording()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            ctrl.run_once()
            if self._indicator_calls(typer):
                break
            time.sleep(0.02)
        assert len(self._indicator_calls(typer)) == 1, (
            "Live-Pfad zeigte den Indikator nicht"
        )
        ctrl.stop_recording()
        self._wait_idle(ctrl)
        assert len(self._indicator_calls(typer)) == 1, (
            "Indikator im Finalizer ein zweites Mal getippt", typer.calls
        )

    def test_short_session_limit_zero_never_indicator(self) -> None:
        """(BUG1/5) live_asr_until_s=0: NEVER an indicator in the finalizer either."""
        ctrl, typer = self._make_short_session(live_asr_until_s=0.0)
        ctrl.start_recording()
        for _ in range(10):
            time.sleep(0.02)
            ctrl.run_once()
        assert not self._indicator_calls(typer)
        ctrl.stop_recording()
        self._wait_idle(ctrl)
        assert not self._indicator_calls(typer), typer.calls


class TestAudioDumpModes:
    """STAGE 1: audio_dump_mode controls the WAV dump (sync|async|off)."""

    def _ctrl(self, tmp_path, mode: str):
        from stt_button.config import Config

        recorder = RecordingTranscriber()
        source = RestAudioSource([b"\x00" * 16000] * 4, rest=b"")
        dump = tmp_path / "last.wav"
        ctrl = PushToTalkController(
            config=Config(audio_dump_mode=mode),
            source_factory=lambda: source,
            engine=FakeEngine(),
            delta_typer=DeltaTyper(FakeTyper(), append_space=True),
            transcriber_factory=lambda *a, **kw: recorder,
            dump_path=str(dump),
        )
        return ctrl, dump

    def _run(self, ctrl) -> None:
        ctrl.start_recording()
        time.sleep(0.2)  # Capture thread fills the queue
        ctrl.stop_recording()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and ctrl.state != ControllerState.IDLE:
            time.sleep(0.005)

    def test_sync_default_writes_wav_before_worker_done(self, tmp_path) -> None:
        """sync (default): dump completes before the finalizer closes the state."""
        ctrl, dump = self._ctrl(tmp_path, "sync")
        self._run(ctrl)
        assert dump.exists(), "sync-Dump fehlt nach Finalisierung"
        assert dump.read_bytes().startswith(b"RIFF")

    def test_off_writes_no_wav(self, tmp_path) -> None:
        ctrl, dump = self._ctrl(tmp_path, "off")
        self._run(ctrl)
        assert not dump.exists(), "off darf keine WAV-Datei schreiben"

    def test_async_writes_wav_from_snapshot(self, tmp_path) -> None:
        """async: dump runs in its own thread and writes the snapshot."""
        ctrl, dump = self._ctrl(tmp_path, "async")
        self._run(ctrl)
        # The async thread must not block the process, but the file must be written
        # by test end. Deadline 5s: under nice -n 19/load, a scheduler slot may
        # take longer (previously 2s -> sporadic load flakiness).
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not dump.exists():
            time.sleep(0.01)
        assert dump.exists(), "async-Dump fehlt"
        assert dump.read_bytes().startswith(b"RIFF")


class TestLatencyTrace:
    """latency_trace=True logs debug measurement points (default: off)."""

    def test_measurement_points_logged(self, tmp_path, caplog) -> None:
        import logging

        from stt_button.config import Config

        with caplog.at_level(logging.DEBUG, logger="stt_button.controller"):
            recorder = RecordingTranscriber()
            source = RestAudioSource([b"\x00" * 16000] * 4, rest=b"")
            ctrl = PushToTalkController(
                config=Config(latency_trace=True),
                source_factory=lambda: source,
                engine=FakeEngine(),
                delta_typer=DeltaTyper(FakeTyper(), append_space=True),
                transcriber_factory=lambda *a, **kw: recorder,
                dump_path=str(tmp_path / "last.wav"),
            )
            ctrl.start_recording()
            time.sleep(0.2)
            ctrl.stop_recording()
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and ctrl.state != ControllerState.IDLE:
                time.sleep(0.005)

        logged = [r.getMessage() for r in caplog.records]
        for point in ("stop entered", "source stopped", "dump start",
                      "dump end", "finalizer exec acquired",
                      "finalize start", "finalize end", "paste"):
            assert any(f"latency[{point}]" in m for m in logged), (
                f"Messpunkt {point!r} fehlt - geloggt: {logged}"
            )

    def test_disabled_by_default(self, tmp_path, caplog) -> None:
        import logging

        from stt_button.config import Config

        with caplog.at_level(logging.DEBUG, logger="stt_button.controller"):
            recorder = RecordingTranscriber()
            source = RestAudioSource([b"\x00" * 16000] * 4, rest=b"")
            ctrl = PushToTalkController(
                config=Config(),
                source_factory=lambda: source,
                engine=FakeEngine(),
                delta_typer=DeltaTyper(FakeTyper(), append_space=True),
                transcriber_factory=lambda *a, **kw: recorder,
                dump_path=str(tmp_path / "last.wav"),
            )
            ctrl.start_recording()
            time.sleep(0.2)
            ctrl.stop_recording()
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and ctrl.state != ControllerState.IDLE:
                time.sleep(0.005)

        assert not [r for r in caplog.records if "latency[" in r.getMessage()]


class BlockingLiveTickTranscriber:
    """Fake StreamTranscriber: maybe_transcribe() blocks controllably.

    The live tick holds ``_exec_lock`` (like real engine inference). Releasing
    the result simulates inference ending AFTER a release in the key thread.
    In exactly that case, the live result must no longer be typed (STAGE 2:
    post-release discard).
    """

    def __init__(self, release: threading.Event) -> None:
        self._release = release
        self.entered = threading.Event()
        self._buffer = b"\x00" * 16000
        self.sample_rate = 16000
        self.finalized = threading.Event()

    def feed(self, data: bytes) -> None:
        pass

    def maybe_transcribe(self):
        self.entered.set()
        self._release.wait(timeout=5.0)
        return TranscriptionResult(text="live wort0 wort1 wort2")

    def finalize(self):
        self.finalized.set()
        return TranscriptionResult(text="finale wort0 wort1 wort2")


class TwoPhaseLiveTranscriber:
    """Two live ticks + finalize: committed-prefix scenario.

    Tick 1 returns "live wort0 wort1" (committed_len=6) and is typed BEFORE
    release. Tick 2 blocks across release and then returns different text;
    exactly this result must be discarded. finalize() returns the complete text.
    """

    def __init__(self) -> None:
        self._tick = 0
        self.tick1_release = threading.Event()
        self.tick2_release = threading.Event()
        self.tick2_entered = threading.Event()
        self._buffer = b"\x00" * 16000
        self.sample_rate = 16000

    def feed(self, data: bytes) -> None:
        pass

    def maybe_transcribe(self):
        self._tick += 1
        if self._tick == 1:
            self.tick1_release.wait(timeout=5.0)
            return TranscriptionResult(text="live wort0 wort1", committed_len=6)
        self.tick2_entered.set()
        self.tick2_release.wait(timeout=5.0)
        # If this text were typed AFTER release, it would create garbage; the
        # finalizer would have to correct it via backspace.
        return TranscriptionResult(text="live ganz anderer Muell", committed_len=6)

    def finalize(self):
        return TranscriptionResult(text="live wort0 wort1 wort2")


class TestPostReleaseDiscard:
    """STAGE 2: discard live results that would be typed after release.

    Scenario: the user releases the key while live inference (under
    ``_exec_lock``) is still running. Its result must NEVER be typed; it would
    otherwise preempt the final text, forcing the finalizer to retract it via
    backspace (noticeable latency).
    """

    def _make_ctrl(
        self, tmp_path
    ) -> tuple[PushToTalkController, BlockingLiveTickTranscriber,
               FakeTyper, threading.Event]:
        live_release = threading.Event()
        transcriber = BlockingLiveTickTranscriber(live_release)
        source = FakeAudioSource([b"\x00" * 16000] * 6)
        typer = FakeTyper()
        ctrl = PushToTalkController(
            config=None,
            source_factory=lambda: source,
            engine=FakeEngine(),
            delta_typer=DeltaTyper(typer, append_space=True),
            transcriber_factory=lambda *a, **kw: transcriber,
            dump_path=str(tmp_path / "last.wav"),
        )
        return ctrl, transcriber, typer, live_release

    def _start_live_tick(
        self, ctrl: PushToTalkController, transcriber: BlockingLiveTickTranscriber
    ) -> threading.Thread:
        """Start a live tick and wait until it blocks in inference."""
        ticker = threading.Thread(target=ctrl.run_once, daemon=True)
        ticker.start()
        assert transcriber.entered.wait(timeout=5.0), (
            "Live-Tick erreichte maybe_transcribe() nicht"
        )
        return ticker

    def _wait_idle(self, ctrl: PushToTalkController, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and ctrl.state != ControllerState.IDLE:
            time.sleep(0.005)
        assert ctrl.state == ControllerState.IDLE, (
            f"IDLE nicht erreicht (ist {ctrl.state})"
        )

    def test_live_result_after_release_is_not_typed(self, tmp_path) -> None:
        """Release during a blocking live tick: no type/backspace afterward."""
        ctrl, transcriber, typer, live_release = self._make_ctrl(tmp_path)
        ctrl.start_recording()
        ticker = self._start_live_tick(ctrl, transcriber)

        ctrl.stop_recording()  # Release: FINALIZING, source stops immediately
        calls_at_release = list(typer.calls)

        # Live inference "returns"; its result must be discarded.
        live_release.set()
        self._wait_idle(ctrl)
        ticker.join(timeout=5.0)

        # NO live result after release: the only type call is the final text from the
        # finalizer; no backspaces, no "live ...".
        typed = [c for c in typer.calls if c[0] == "type"]
        assert typed == [("type", "finale wort0 wort1 wort2")], (
            f"Nach dem Release wurde getippt: {typer.calls}"
        )
        assert not any(c[0] == "backspace" for c in typer.calls)
        assert ("key", "space") in typer.calls

    def test_final_text_is_typed_exactly_once_by_finalizer(self, tmp_path) -> None:
        """The final text arrives completely and exactly once (from finalize())."""
        ctrl, transcriber, typer, live_release = self._make_ctrl(tmp_path)
        ctrl.start_recording()
        ticker = self._start_live_tick(ctrl, transcriber)
        ctrl.stop_recording()
        live_release.set()
        self._wait_idle(ctrl)
        ticker.join(timeout=5.0)

        typed = "".join(c[1] for c in typer.calls if c[0] == "type")
        assert typed == "finale wort0 wort1 wort2", (
            f"Finaltext unvollstaendig: {typer.calls}"
        )
        # No backspace: the live tick never typed anything.
        assert not any(c[0] == "backspace" for c in typer.calls)

    def test_final_text_with_committed_prefix_is_complete(self, tmp_path) -> None:
        """Advanced committed prefix: final text remains complete.

        Text was already typed live before release ("live wort0 wort1" with
        committed prefix "live "); a second live tick blocks across release.
        Its differing result must be discarded; the finalizer computes the
        final delta against the effective state and appends only " wort2".
        No backspace, no garbage at the cursor.
        """
        transcriber = TwoPhaseLiveTranscriber()
        source = FakeAudioSource([b"\x00" * 16000] * 6)
        typer = FakeTyper()
        ctrl = PushToTalkController(
            config=None,
            source_factory=lambda: source,
            engine=FakeEngine(),
            delta_typer=DeltaTyper(typer, append_space=True),
            transcriber_factory=lambda *a, **kw: transcriber,
            dump_path=str(tmp_path / "last.wav"),
        )
        # Tick 1: result is typed BEFORE release (committed "live ").
        ctrl.start_recording()
        tick1 = threading.Thread(target=ctrl.run_once, daemon=True)
        tick1.start()
        transcriber.tick1_release.set()  # unblock tick 1 immediately
        tick1.join(timeout=5.0)
        assert "".join(c[1] for c in typer.calls if c[0] == "type") == (
            "live wort0 wort1"
        )

        # Tick 2 blocks in inference; release now.
        tick2 = threading.Thread(target=ctrl.run_once, daemon=True)
        tick2.start()
        assert transcriber.tick2_entered.wait(timeout=5.0)
        ctrl.stop_recording()

        # Tick 2 returns AFTER release -> discard the result.
        transcriber.tick2_release.set()
        self._wait_idle(ctrl)
        tick2.join(timeout=5.0)

        typed = "".join(c[1] for c in typer.calls if c[0] == "type")
        assert typed == "live wort0 wort1 wort2", (
            f"Finaltext am Cursor unvollstaendig: {typer.calls}"
        )
        assert not any(c[0] == "backspace" for c in typer.calls), typer.calls
        assert not any("Muell" in c[1] for c in typer.calls if c[0] == "type")


class TestLiveLatencyTrace:
    """latency_trace: live tick measurement points (live asr/live typing)."""

    def test_live_tick_points_logged(self, tmp_path, caplog) -> None:
        import logging

        from stt_button.config import Config

        with caplog.at_level(logging.DEBUG, logger="stt_button.controller"):
            source = FakeAudioSource([b"\x00" * 16000] * 6)
            typer = FakeTyper()
            ctrl = PushToTalkController(
                config=Config(latency_trace=True),
                source_factory=lambda: source,
                engine=FakeEngine(),
                delta_typer=DeltaTyper(typer, append_space=True),
                dump_path=str(tmp_path / "last.wav"),
            )
            ctrl.start_recording()
            for _ in range(3):
                ctrl.run_once()
            ctrl.stop_recording()
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and ctrl.state != ControllerState.IDLE:
                time.sleep(0.005)

        logged = [r.getMessage() for r in caplog.records]
        for point in ("live asr start", "live asr end",
                      "live typing start", "live typing end"):
            assert any(f"latency[{point}]" in m for m in logged), (
                f"Messpunkt {point!r} fehlt - geloggt: {logged}"
            )


class WaitProbe:
    """Run wait_for_work() in a daemon thread.

    assert_returned() uses a bounded deadline to verify that the call returns
    cleanly and without errors (RED phase: missing method fails; no vacuous
    green). assert_blocked() verifies the opposite: no return without an event
    (otherwise the worker would poll again instead of blocking).
    """

    def __init__(self, ctrl: PushToTalkController) -> None:
        self._ctrl = ctrl
        self.returned = threading.Event()
        self.error: BaseException | None = None
        self._thread = threading.Thread(
            target=self._run, name="wait-for-work-test", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        try:
            self._ctrl.wait_for_work()
        except BaseException as exc:  # noqa: BLE001 - log errors
            self.error = exc
        self.returned.set()

    def assert_returned(self, timeout: float = 2.0) -> None:
        assert self.returned.wait(timeout=timeout), (
            "wait_for_work() ist nicht zurueckgekehrt (Deadline abgelaufen)"
        )
        assert self.error is None, (
            f"wait_for_work() warf {self.error!r} statt sauber "
            "zurueckzukehren"
        )

    def assert_blocked(self, timeout: float = 0.3) -> None:
        """Verify that wait_for_work() still blocks (no fail-pass).

        Note: call only after sufficient settling time; the daemon thread must
        first enter wait().
        """
        assert not self.returned.wait(timeout=timeout), (
            "wait_for_work() ist ohne Ereignis zurueckgekehrt - der "
            "Worker wuerde wieder pollen statt zu blocken"
        )


class FeedCountingTranscriber:
    """Fake StreamTranscriber: count feed() calls; maybe_transcribe=None.

    For wait tests: deterministically count which chunks a live tick consumes,
    without real ASR interval or VAD logic.
    """

    def __init__(self) -> None:
        self.feed_count = 0
        self._buffer = b"\x00" * 16000
        self.sample_rate = 16000

    def feed(self, data: bytes) -> None:
        self.feed_count += 1

    def maybe_transcribe(self):
        return None

    def finalize(self):
        return TranscriptionResult(text="fertig")


class EmptyAudioSource:
    """Never provides audio (read() -> b""): capture thread stays quiet.

    For wait tests: the queue remains empty except for puts controlled by the
    test itself; otherwise FakeAudioSource's capture thread would fill it
    uncontrollably.
    """

    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0

    def start(self) -> None:
        self.started += 1

    def stop(self) -> bytes:
        self.stopped += 1
        return b""

    def read(self, block: bool = True) -> bytes:
        return b""


def make_wait_controller(
    source: FakeAudioSource | EmptyAudioSource | None = None,
    engine=None,
    **kwargs,
) -> PushToTalkController:
    """Controller for wait tests: quiet source, no stray chunks.

    Without an engine, inject FeedCountingTranscriber (counts consumed chunks).
    VAD remains disabled (StreamTranscriber default: vad=None; an engine is
    provided only in test 7).
    """
    if engine is not None:
        # Real StreamTranscriber: test asr.py interval semantics.
        return PushToTalkController(
            config=None,
            source_factory=lambda: (source or EmptyAudioSource()),
            engine=engine,
            delta_typer=DeltaTyper(FakeTyper(), append_space=True),
            **kwargs,
        )
    return PushToTalkController(
        config=None,
        source_factory=lambda: (source or EmptyAudioSource()),
        engine=FakeEngine(),
        delta_typer=DeltaTyper(FakeTyper(), append_space=True),
        transcriber_factory=lambda *a, **kw: FeedCountingTranscriber(),
        **kwargs,
    )


class TestWaitForWork:
    """Feature 4: event-driven wakeup instead of 20ms polling.

    wait_for_work() replaces time.sleep(0.02) in Application.run_forever().
    In IDLE/FINALIZING it blocks until the next event (notify); in RECORDING
    it blocks at most until the next deadline (new chunk, no-audio timeout,
    maximum duration, release/finalization). All tests use bounded deadlines
    and threading.Events—no polling, no guessing.
    """

    def test_idle_blocks_until_start(self) -> None:
        """Test 1: in IDLE, wait_for_work blocks until start_recording()."""
        ctrl = make_wait_controller()
        probe = WaitProbe(ctrl)
        probe.assert_blocked(timeout=0.5)
        ctrl.start_recording()
        probe.assert_returned(timeout=2.0)
        assert ctrl.state == ControllerState.RECORDING
        ctrl.abort()

    def test_capture_chunk_wakes_worker(self) -> None:
        """Test 2: nonempty queue put wakes the worker (capture path).

        The put is equivalent to _capture_loop; the returning worker consumes
        the chunk via run_once() exactly once (feed_count on
        FeedCountingTranscriber).
        """
        ctrl = make_wait_controller()
        ctrl.start_recording()
        assert ctrl.state == ControllerState.RECORDING
        ctrl._audio_queue.put(b"\x00" * 16000)  # simulate capture put
        probe = WaitProbe(ctrl)
        probe.assert_returned(timeout=2.0)
        ctrl.run_once()
        transcriber = ctrl._transcriber
        assert transcriber is not None
        assert transcriber.feed_count == 1, (
            f"Chunk nicht genau einmal konsumiert "
            f"(feeds={transcriber.feed_count})"
        )
        assert ctrl._audio_queue.qsize() == 0
        ctrl.abort()

    def test_nonempty_queue_returns_without_notify(self) -> None:
        """Test 3: lost-wakeup guard—nonempty queue before waiting.

        The notification from put time has already passed: the nonempty check
        BEFORE wait() still returns immediately. Without this guard, the worker
        would block until the deadline (classic lost wakeup).
        """
        ctrl = make_wait_controller()
        ctrl.start_recording()
        ctrl._audio_queue.put(b"\x00" * 16000)  # notification has passed
        probe = WaitProbe(ctrl)
        probe.assert_returned(timeout=2.0)
        ctrl.abort()

    def test_no_audio_timeout_wakes_and_aborts(self) -> None:
        """Test 4: no-audio deadline wakes; the following tick aborts.

        start_recording sets _last_audio_at = now: the wait must be at least
        _no_audio_timeout_s. A completed timeout ends in IDLE (abort path)
        and then wakes the worker.
        """
        ctrl = make_wait_controller(no_audio_timeout_s=0.2)
        ctrl.start_recording()
        probe = WaitProbe(ctrl)
        probe.assert_returned(timeout=2.0)
        # Not woken before the deadline (50ms tolerance; the old 20ms polling
        # would already have returned after ~20ms).
        woke_at = time.monotonic()
        assert woke_at >= ctrl._last_audio_at + ctrl._no_audio_timeout_s - 0.05, (
            f"wait_for_work() weckte nach {woke_at - ctrl._last_audio_at:.3f}s "
            "- vor der No-Audio-Frist"
        )
        # The next live tick sees the timeout and aborts (edge case: wakeup exactly
        # at the deadline -> a brief follow-up tick is allowed).
        deadline = time.monotonic() + 2.0
        while ctrl.state == ControllerState.RECORDING:
            ctrl.run_once()
            if ctrl.state == ControllerState.RECORDING:
                ctrl.wait_for_work()  # deadline expired -> immediate return
            assert time.monotonic() < deadline, (
                "Abbruch nach No-Audio-Timeout blieb aus"
            )
        assert ctrl.state == ControllerState.IDLE
        # After aborting, a fresh worker blocks again until the next event (abort
        # sends a notification).
        probe2 = WaitProbe(ctrl)
        probe2.assert_blocked(timeout=0.3)

    def test_max_record_deadline_wakes(self) -> None:
        """Test 5: maximum-duration deadline wakes the worker on time."""
        ctrl = make_wait_controller(max_record_s=0.3)
        ctrl.start_recording()
        probe = WaitProbe(ctrl)
        probe.assert_returned(timeout=2.0)
        # Not woken before the deadline (50ms tolerance; the old 20ms polling
        # would already have returned after ~20ms).
        woke_at = time.monotonic()
        assert woke_at >= ctrl._started_at + ctrl._max_record_s - 0.05, (
            f"wait_for_work() weckte nach {woke_at - ctrl._started_at:.3f}s "
            "- vor der Max-Dauer-Frist"
        )
        # Release the key (release after wakeup): finalization completes WITHOUT an
        # automatic restart (no ghost run).
        ctrl.stop_recording()
        _wait_state(ctrl, ControllerState.IDLE, timeout=5.0)
        assert ctrl._key_down is False
        assert ctrl.state == ControllerState.IDLE, "Geisteraufnahme gestartet"
        probe2 = WaitProbe(ctrl)
        probe2.assert_blocked(timeout=0.3)

    def test_stop_recording_wakes_and_no_ghost_restart(self) -> None:
        """Test 6: release wakes; no autostart with a released key."""
        ctrl = make_wait_controller()
        ctrl.start_recording()
        probe = WaitProbe(ctrl)
        probe.assert_blocked(timeout=0.3)
        ctrl.stop_recording()
        probe.assert_returned(timeout=2.0)
        _wait_state(ctrl, ControllerState.IDLE, timeout=5.0)
        assert ctrl._key_down is False
        assert ctrl.state == ControllerState.IDLE, "Geisteraufnahme gestartet"
        # In IDLE without another event, the worker keeps blocking.
        probe2 = WaitProbe(ctrl)
        probe2.assert_blocked(timeout=0.3)

    def test_asr_interval_gating_intact(self) -> None:
        """Test 7: asr.py interval_s gating remains effective.

        The gate counts BUFFER seconds (audio_seconds - _last_transcribed_at
        < interval_s -> no engine call), not wall time. Multiple wakeups with
        < interval_s of NEW audio trigger no additional call; afterward
        (>= interval_s buffered), EXACTLY ONE more. Real StreamTranscriber +
        FakeEngine; 32000 bytes = 1.0s audio.
        """
        engine = FakeEngine()
        ctrl = make_wait_controller(
            engine=engine,
            asr_interval_s=0.15,
            min_audio_s=0.5,
        )
        ctrl.start_recording()
        try:
            chunk = b"\x00" * 32000  # 1.0s audio
            # Phase 1: first transcription fires immediately (min_audio_s is sufficient).
            ctrl._audio_queue.put(chunk)
            WaitProbe(ctrl).assert_returned(timeout=2.0)
            ctrl.run_once()
            assert engine.calls == 1, (
                f"Erste Transkription fehlt (calls={engine.calls})"
            )
            # Phase 2: < interval_s of NEW audio -> wakeup + tick, but NO
            # additional engine call (gate holds).
            ctrl._audio_queue.put(b"\x00" * 3200)  # 0.1 s < 0.15 s
            WaitProbe(ctrl).assert_returned(timeout=2.0)
            ctrl.run_once()
            assert engine.calls == 1, (
                f"Intervall-Gating verletzt: {engine.calls} statt 1 "
                "Calls bei < interval_s neuem Audio"
            )
            # Phase 3: >= interval_s of new audio -> EXACTLY ONE additional
            # call (even repeated wakeups must not over-transcribe).
            ctrl._audio_queue.put(b"\x00" * 8000)  # 0.25 s > 0.15 s
            WaitProbe(ctrl).assert_returned(timeout=2.0)
            ctrl.run_once()
            assert engine.calls == 2, (
                f"Nach Intervall-Ablauf {engine.calls} statt genau 2 "
                "Calls"
            )
        finally:
            ctrl.abort()

    def test_release_responsive_during_live_inference(self) -> None:
        """Test 8: release remains responsive during blocking live inference.

        wait_for_work() must never run under _exec_lock: otherwise the waiting
        worker traps live inference (and therefore every release). Use
        BlockingEngine in a live tick; stop_recording() must return without
        waiting for inference and stop the source.
        """
        engine = BlockingEngine()
        ctrl = make_wait_controller(
            engine=engine,
            min_audio_s=0.5,
        )
        ctrl.start_recording()
        ctrl._audio_queue.put(b"\x00" * 16000)
        worker = threading.Thread(
            target=ctrl.run_once, name="live-tick-worker", daemon=True
        )
        worker.start()
        assert engine.entered.wait(timeout=2.0), (
            "Live-Inferenz nicht gestartet"
        )
        # Release in the key thread: without waiting for inference (STAGE 1).
        t0 = time.monotonic()
        ctrl.stop_recording()
        elapsed = time.monotonic() - t0
        assert elapsed < 0.5, (
            f"stop_recording() wartete {elapsed:.3f}s - Release blockiert "
            "an der laufenden Inferenz"
        )
        # The source stopped immediately (even though inference is still running).
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and ctrl._source is not None:
            time.sleep(0.005)
        assert ctrl._source is None, "Quelle nicht gestoppt"
        # Unblock inference: the live result is discarded (post-release discard), and
        # the finalizer completes (feeds remainder + finalize).
        engine.release.set()
        _wait_state(ctrl, ControllerState.IDLE, timeout=5.0)
        assert engine.calls >= 1
        worker.join(timeout=2.0)
        # After completion, a fresh worker blocks cleanly again until the next event
        # (finalizer completion sends a notification).
        probe = WaitProbe(ctrl)
        probe.assert_blocked(timeout=0.3)


# ---------------------------------------------------------------------------
# Completion signal (operator, October 31; Kimi follow-up review): after
# finalization, the controller plays a sound so the user knows the text is ready
# in the clipboard and can be pasted.
# Semantics (Kimi): sound AFTER paste is triggered = 'paste triggered'; no sound
# on exceptions (honest feedback, no 'done' on failure).
# ---------------------------------------------------------------------------


class TestDoneSound:
    def _play_calls(self, monkeypatch):
        import stt_button.controller as ctrl_mod

        calls = []

        def fake_popen(cmd, *a, **kw):
            calls.append(list(cmd))

            class _P:
                def poll(self):
                    return 0

            return _P()

        monkeypatch.setattr(ctrl_mod.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(ctrl_mod.shutil, "which", lambda b: f"/usr/bin/{b}")
        monkeypatch.setattr(
            ctrl_mod.os.path, "exists", lambda p: p.endswith(".oga")
        )
        monkeypatch.setattr(ctrl_mod.focus, "terminal_focused", lambda: None)
        return calls

    def test_sound_after_successful_finalize(self, monkeypatch, tmp_path) -> None:
        from stt_button.config import Config

        calls = self._play_calls(monkeypatch)
        recorder = RecordingTranscriber()
        source = RestAudioSource([b"\x00" * 16000] * 4, rest=b"")
        ctrl = PushToTalkController(
            config=Config(done_sound="complete"),
            source_factory=lambda: source,
            engine=FakeEngine(),
            delta_typer=DeltaTyper(FakeTyper(), append_space=True),
            transcriber_factory=lambda *a, **kw: recorder,
            dump_path=str(tmp_path / "last.wav"),
        )
        ctrl.start_recording()
        time.sleep(0.2)
        ctrl.stop_recording()
        _wait_state(ctrl, ControllerState.IDLE)
        assert len(calls) == 1, "genau ein Fertig-Signal nach erfolgreichem Finalize"
        assert calls[0][0].endswith("paplay") and any(
            a.endswith("complete.oga") for a in calls[0]
        )

    def test_no_sound_when_off(self, monkeypatch, tmp_path) -> None:
        from stt_button.config import Config

        calls = self._play_calls(monkeypatch)
        recorder = RecordingTranscriber()
        source = RestAudioSource([b"\x00" * 16000] * 4, rest=b"")
        ctrl = PushToTalkController(
            config=Config(),  # done_sound default '' = off
            source_factory=lambda: source,
            engine=FakeEngine(),
            delta_typer=DeltaTyper(FakeTyper(), append_space=True),
            transcriber_factory=lambda *a, **kw: recorder,
            dump_path=str(tmp_path / "last.wav"),
        )
        ctrl.start_recording()
        time.sleep(0.2)
        ctrl.stop_recording()
        _wait_state(ctrl, ControllerState.IDLE)
        assert calls == []

    def test_failing_paplay_never_breaks_finalize(self, monkeypatch, tmp_path) -> None:
        import stt_button.controller as ctrl_mod

        from stt_button.config import Config

        def boom(cmd, *a, **kw):
            raise OSError("kein Audiosink")

        monkeypatch.setattr(ctrl_mod.subprocess, "Popen", boom)
        monkeypatch.setattr(ctrl_mod.shutil, "which", lambda b: f"/usr/bin/{b}")
        monkeypatch.setattr(
            ctrl_mod.os.path, "exists", lambda p: p.endswith(".oga")
        )
        recorder = RecordingTranscriber()
        source = RestAudioSource([b"\x00" * 16000] * 4, rest=b"")
        ctrl = PushToTalkController(
            config=Config(done_sound="bell"),
            source_factory=lambda: source,
            engine=FakeEngine(),
            delta_typer=DeltaTyper(FakeTyper(), append_space=True),
            transcriber_factory=lambda *a, **kw: recorder,
            dump_path=str(tmp_path / "last.wav"),
        )
        ctrl.start_recording()
        time.sleep(0.2)
        ctrl.stop_recording()
        _wait_state(ctrl, ControllerState.IDLE)
        assert ctrl.state == ControllerState.IDLE, "Tonfehler darf nie abbrechen"


class TestDoneSoundAbsolutePath:
    """done_sound may be an absolute file path (custom sound;
    operator, October 31: freedesktop 'complete' is too high-pitched—a custom
    generated lower-pitched sound resides outside the sound directory)."""

    def test_absolute_path_used_verbatim(self, monkeypatch, tmp_path) -> None:
        import stt_button.controller as ctrl_mod

        from stt_button.config import Config

        wav = tmp_path / "tief.wav"
        wav.write_bytes(b"RIFF....WAVEfmt ")
        calls = []

        def fake_popen(cmd, *a, **kw):
            calls.append(list(cmd))

            class _P:
                def poll(self):
                    return 0

            return _P()

        monkeypatch.setattr(ctrl_mod.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(ctrl_mod.shutil, "which", lambda b: f"/usr/bin/{b}")
        monkeypatch.setattr(ctrl_mod.focus, "terminal_focused", lambda: None)
        recorder = RecordingTranscriber()
        source = RestAudioSource([b"\x00" * 16000] * 4, rest=b"")
        ctrl = PushToTalkController(
            config=Config(done_sound=str(wav)),
            source_factory=lambda: source,
            engine=FakeEngine(),
            delta_typer=DeltaTyper(FakeTyper(), append_space=True),
            transcriber_factory=lambda *a, **kw: recorder,
            dump_path=str(tmp_path / "last.wav"),
        )
        ctrl.start_recording()
        time.sleep(0.2)
        ctrl.stop_recording()
        _wait_state(ctrl, ControllerState.IDLE)
        assert len(calls) == 1
        assert calls[0][-1] == str(wav), "absoluter Pfad unverwendet verwenden"


class TestFinalizeQueue:
    """Dictation queue: recordings finalize FIFO while the next one records."""

    @staticmethod
    def _two_jobs(**kwargs):
        ctrl, ctx = make_controller(blocking_transcriber=True, **kwargs)
        ctrl.start_recording()
        ctrl.stop_recording()            # job 1 (blocked)
        ctrl.start_recording()           # rec 2 immediately
        return ctrl, ctx

    def test_live_typing_waits_for_pending_finalize(self) -> None:
        ctrl, ctx = self._two_jobs()
        ctrl._audio_queue.put(b"\x00" * 32000)
        # Job 1's finalizer holds _exec_lock while blocked in finalize(), so
        # run_once() would deadlock here; call the tick body directly. The
        # busy guard returns before any DeltaTyper access.
        ctrl._run_once_exec()            # live tick of rec 2
        assert ctx["typer"].calls == [], "rec 2 typed before job 1 finished"
        assert ctrl._typed_epoch != ctrl._typing_epoch
        ctrl.stop_recording()
        ctrl._test_unblock_finalize.set()
        _wait_state(ctrl, ControllerState.IDLE)
        assert ctx["typer"].typed_text_final == "fertigfertig"

    def test_abort_keeps_pending_job(self) -> None:
        ctrl, ctx = self._two_jobs()
        ctrl.abort()                     # aborts rec 2 only
        assert ctrl._finalizer_running is True
        # Accepted: state is already IDLE while job 1 still runs.
        assert ctrl.state == ControllerState.IDLE
        ctrl._test_unblock_finalize.set()
        deadline = time.monotonic() + 5.0
        while ctrl._finalizer_running and time.monotonic() < deadline:
            time.sleep(0.005)
        assert ctrl._finalizer_running is False
        assert ctrl.state == ControllerState.IDLE
        assert ctx["typer"].typed_text_final == "fertig"

    def test_shutdown_drops_queued_jobs(self) -> None:
        ctrl, ctx = self._two_jobs()
        ctrl.stop_recording()            # job 2 queued behind job 1
        ctrl.shutdown()
        assert len(ctrl._finalize_queue) == 0
        ctrl._test_unblock_finalize.set()
        deadline = time.monotonic() + 5.0
        while ctrl._finalizer_running and time.monotonic() < deadline:
            time.sleep(0.005)
        assert ctx["typer"].typed_text_final == "fertig"   # only job 1


class TestQueueDepth:
    def test_start_refused_when_queue_full(self) -> None:
        ctrl, ctx = make_controller(blocking_transcriber=True)
        ctrl._max_queue_depth = 1
        ctrl.start_recording(); ctrl.stop_recording()   # 1 job pending
        ctrl.start_recording()
        assert ctrl.state == ControllerState.FINALIZING
        assert ctx["source"].started == 1
        ctrl.stop_recording()
        ctrl._test_unblock_finalize.set()
        _wait_state(ctrl, ControllerState.IDLE)


class TestFailingJob:
    def test_failing_job_does_not_block_next(self) -> None:
        unblock = threading.Event()
        made = []

        class Boom(BlockingTranscriber):
            def finalize(self):
                self._unblock.wait()
                raise RuntimeError("asr crash")

        def factory(*a, **kw):
            t = Boom(unblock) if not made else BlockingTranscriber(unblock)
            made.append(t)
            return t

        typer = FakeTyper()
        ctrl = PushToTalkController(
            config=None,
            source_factory=FakeSourceFactory([b"\x00" * 16000] * 2),
            engine=FakeEngine(),
            delta_typer=DeltaTyper(typer, append_space=True),
            transcriber_factory=factory,
        )
        ctrl.start_recording(); ctrl.stop_recording()
        ctrl.start_recording(); ctrl.stop_recording()
        unblock.set()
        _wait_state(ctrl, ControllerState.IDLE)
        assert typer.typed_text_final == "fertig"
        assert ctrl._finalizer_running is False


class TestDoneSoundPerJob(TestDoneSound):
    def test_two_jobs_two_sounds(self, monkeypatch, tmp_path) -> None:
        from stt_button.config import Config
        calls = self._play_calls(monkeypatch)
        unblock = threading.Event()
        ctrl = PushToTalkController(
            config=Config(done_sound="complete"),
            source_factory=FakeSourceFactory([b"\x00" * 16000] * 2),
            engine=FakeEngine(),
            delta_typer=DeltaTyper(FakeTyper(), append_space=True),
            transcriber_factory=lambda *a, **kw: BlockingTranscriber(unblock),
            dump_path=str(tmp_path / "last.wav"),
        )
        ctrl.start_recording(); ctrl.stop_recording()
        ctrl.start_recording(); ctrl.stop_recording()
        unblock.set()
        _wait_state(ctrl, ControllerState.IDLE)
        assert len(calls) == 2


class TextBlockingTranscriber(BlockingTranscriber):
    """BlockingTranscriber returning a per-instance text."""

    def __init__(self, unblock: threading.Event, text: str) -> None:
        super().__init__(unblock)
        self._text = text

    def finalize(self):
        self._unblock.wait()
        return TranscriptionResult(text=self._text)


def _make_text_queue_controller(texts: list[str], **kwargs):
    """Controller whose n-th recording finalizes to texts[n]; collects finalize texts."""
    unblock = threading.Event()
    pending = list(texts)
    factory = FakeSourceFactory([b"\x00" * 16000] * 6)
    typer = FakeTyper()
    delta = DeltaTyper(typer, append_space=True)
    finalized: list[str] = []
    orig_finalize = delta.finalize

    def recording_finalize(text, *a, **kw):
        finalized.append(text)
        return orig_finalize(text, *a, **kw)

    delta.finalize = recording_finalize
    ctrl = PushToTalkController(
        config=None,
        source_factory=factory,
        engine=FakeEngine(),
        delta_typer=delta,
        transcriber_factory=lambda *a, **kw: TextBlockingTranscriber(
            unblock, pending.pop(0)),
        **kwargs,
    )
    ctrl._test_unblock_finalize = unblock
    return ctrl, {"source": factory, "typer": typer, "finalized": finalized}


def _wait_finalizer_idle(ctrl: PushToTalkController, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while ctrl._finalizer_running and time.monotonic() < deadline:
        time.sleep(0.005)
    assert ctrl._finalizer_running is False


class TestQueueFifoThreeJobs:
    def test_three_dictations_finalize_in_fifo_order(self) -> None:
        ctrl, ctx = _make_text_queue_controller(["alpha", "beta", "gamma"])
        for _ in range(3):
            ctrl.start_recording()
            assert ctrl.state == ControllerState.RECORDING
            ctrl.stop_recording()
        # All three are queued/in flight while job 1 blocks in finalize().
        assert ctx["finalized"] == []
        ctrl._test_unblock_finalize.set()
        _wait_finalizer_idle(ctrl)
        _wait_state(ctrl, ControllerState.IDLE)
        assert ctx["finalized"] == ["alpha", "beta", "gamma"]
        assert ctx["source"].started == 3


class TestQueuedRecordingTimeouts:
    def test_max_record_on_queued_recording(self) -> None:
        """Rec N+1 hits max_record_s while job N still finalizes."""
        ctrl, ctx = _make_text_queue_controller(["first", "second"])
        ctrl.start_recording()
        ctrl.stop_recording()                 # job N blocked in finalize()
        ctrl.start_recording()                # rec N+1
        assert ctrl.state == ControllerState.RECORDING
        ctrl._max_record_s = 0.01
        time.sleep(0.02)
        ctrl._enforce_max_duration()          # timeout on the ACTIVE recording
        assert ctrl.state == ControllerState.FINALIZING
        assert ctx["source"].stopped == 2
        assert ctx["finalized"] == []         # queue untouched, N still pending
        ctrl.stop_recording()                 # key release, no restart
        ctrl._test_unblock_finalize.set()
        _wait_finalizer_idle(ctrl)
        _wait_state(ctrl, ControllerState.IDLE)
        assert ctx["finalized"] == ["first", "second"]

    def test_no_audio_timeout_on_queued_recording(self) -> None:
        """Rec N+1 aborts on no_audio_timeout; job N still completes."""
        ctrl, ctx = _make_text_queue_controller(
            ["first", "unused"], no_audio_timeout_s=0.01)
        ctrl.start_recording()
        ctrl.stop_recording()                 # job N blocked in finalize()
        ctx["source"]._chunks = []            # rec N+1 delivers no audio
        ctrl.start_recording()                # rec N+1
        assert ctrl.state == ControllerState.RECORDING
        ctrl._last_audio_at = time.monotonic() - 1.0
        # run_once() would block on _exec_lock held by job N; call the body.
        ctrl._run_once_exec()
        assert ctrl.state != ControllerState.RECORDING
        assert ctrl._finalizer_running is True
        ctrl._test_unblock_finalize.set()
        _wait_finalizer_idle(ctrl)
        _wait_state(ctrl, ControllerState.IDLE)
        assert ctx["finalized"] == ["first"]


class CancellableEngine:
    """Engine whose FIRST transcribe() blocks until cancel (or 2s timeout).

    Mimics ParakeetEngine's cooperative cancel API (Phase 2, A).
    """

    def __init__(self) -> None:
        self.entered = threading.Event()
        self._cancel = threading.Event()
        self.cancel_requests = 0
        self.calls = 0

    def request_cancel(self) -> None:
        self.cancel_requests += 1
        self._cancel.set()

    def clear_cancel(self) -> None:
        self._cancel.clear()

    def transcribe(self, data: bytes, initial_prompt: str = "") -> TranscriptionResult:
        from stt_button.asr import InferenceCancelled

        self.calls += 1
        if self.calls == 1:
            self.entered.set()
            if self._cancel.wait(timeout=2.0):
                raise InferenceCancelled("cancelled")
        if self._cancel.is_set():
            raise InferenceCancelled("cancelled (stray)")
        return TranscriptionResult(text="final text")


class TestReleaseCancelsLiveTick:
    """Phase 2 (A): release must not queue the finalizer behind a live tick."""

    @staticmethod
    def _start_ticker(ctrl, engine) -> threading.Thread:
        """Tick until the engine is entered (capture may lag the first tick)."""
        def loop() -> None:
            deadline = time.monotonic() + 5.0
            while not engine.entered.is_set() and time.monotonic() < deadline:
                ctrl.run_once()
                time.sleep(0.01)
        t = threading.Thread(target=loop, daemon=True)
        t.start()
        return t

    def _ctrl(self, engine, typer):
        return PushToTalkController(
            config=None,
            source_factory=lambda: FakeAudioSource([b"\x00" * 16000] * 8),
            engine=engine,
            delta_typer=DeltaTyper(typer, append_space=True),
            min_audio_s=0.1,
            asr_interval_s=0.1,
        )

    def test_release_cancels_running_live_tick_and_final_is_fast(self) -> None:
        engine = CancellableEngine()
        typer = FakeTyper()
        ctrl = self._ctrl(engine, typer)
        ctrl.start_recording()
        ticker = self._start_ticker(ctrl, engine)
        assert engine.entered.wait(timeout=5.0)

        t0 = time.monotonic()
        ctrl.stop_recording()
        _wait_state(ctrl, ControllerState.IDLE, timeout=5.0)
        elapsed = time.monotonic() - t0
        ticker.join(timeout=5.0)

        assert engine.cancel_requests == 1
        # Without cancel the finalizer waits for the 2s live timeout.
        assert elapsed < 1.0, f"finalizer queued behind live tick ({elapsed:.2f}s)"
        assert "final text" in typer.typed_text_final

    def test_cancelled_live_tick_does_not_abort(self, caplog) -> None:
        engine = CancellableEngine()
        typer = FakeTyper()
        ctrl = self._ctrl(engine, typer)
        ctrl.start_recording()
        ticker = self._start_ticker(ctrl, engine)
        assert engine.entered.wait(timeout=5.0)
        with caplog.at_level("INFO", logger="stt_button.controller"):
            ctrl.stop_recording()
            _wait_state(ctrl, ControllerState.IDLE, timeout=5.0)
            ticker.join(timeout=5.0)
        assert "Transcription failed" not in caplog.text
        assert "Live tick cancelled" in caplog.text

    def test_no_cancel_without_running_live_tick(self) -> None:
        engine = CancellableEngine()
        engine.calls = 1  # never block: no live tick in flight
        typer = FakeTyper()
        ctrl = self._ctrl(engine, typer)
        ctrl.start_recording()
        time.sleep(0.1)
        ctrl.stop_recording()
        _wait_state(ctrl, ControllerState.IDLE, timeout=5.0)
        assert engine.cancel_requests == 0

    def test_stray_cancel_is_cleared_before_finalize(self) -> None:
        engine = CancellableEngine()
        engine.calls = 1
        engine.request_cancel()  # stray flag left over (tick ended just before)
        typer = FakeTyper()
        ctrl = self._ctrl(engine, typer)
        ctrl.start_recording()
        time.sleep(0.1)
        ctrl.stop_recording()
        _wait_state(ctrl, ControllerState.IDLE, timeout=5.0)
        assert "final text" in typer.typed_text_final

    def test_engine_without_cancel_api_still_works(self) -> None:
        typer = FakeTyper()
        ctrl = self._ctrl(FakeEngine(), typer)
        ctrl.start_recording()
        ctrl.run_once()
        ctrl.stop_recording()
        _wait_state(ctrl, ControllerState.IDLE, timeout=5.0)
        assert ctrl.state == ControllerState.IDLE
