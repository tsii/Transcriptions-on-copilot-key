# Phase 2 – Block 2 (A2): Release aborts a running live tick (controller)

Depends on block 1 (`InferenceCancelled`, `request_cancel`/`clear_cancel`).

## Goal
- On release (`_finish`, controller.py:445), the controller calls `engine.request_cancel()` **only when a live tick is currently inside the engine**. A running finalizer from an earlier job must never be hit.
- The live tick catches `InferenceCancelled` **without** calling `abort()`, unlike the ASR error path at controller.py:361.
- The finalizer clears the cancel flag under `_exec_lock` before its own inference.

Race analysis:
- A live tick can only hold `_exec_lock` while the flag `_live_tick_active` is set, and that flag is read and written under `_lock`.
- The finalizer calls `clear_cancel()` under `_exec_lock`, where no live tick can run. Any stray cancel (one where the tick finished right before `request_cancel`) therefore gets cleared before the final inference.
- The next cancel needs `_live_tick_active=True`, which can only happen once the finalizer has released `_exec_lock`.
- This holds for every finalizer job, not just the one belonging to the release.

StreamTranscriber needs no change:
- `_transcribe` (asr.py:892) invalidates the cache before the engine call (asr.py:899). When the cancel exception propagates, `_history` and the commit stay unchanged and the cache is invalid, so `finalize()` transcribes the full buffer. That is correct for both `full` and `cached`.
- `_mid_tail_step` (asr.py:811) catches every `Exception` and sets `_mid_tail_failed=True`, which falls back to the full buffer. That is correct.

Lock hierarchy: `request_cancel()` only sets an `Event`. It may be called under `_lock` and takes no other lock. `_exec_lock → _lock` stays unchanged.

## Test first – append to `tests/test_controller.py` (end of file)
```python
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
        ticker = threading.Thread(target=ctrl.run_once, daemon=True)
        ticker.start()
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
        ticker = threading.Thread(target=ctrl.run_once, daemon=True)
        ticker.start()
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
```
Before writing the tests, check that `FakeTyper.typed_text_final` exists (helper around test_controller.py:58). If the attribute is named differently, adjust the asserts to that name. Do not change the helper.

## Expected RED
- `test_release_cancels_running_live_tick_and_final_is_fast`: `assert engine.cancel_requests == 1` → `0 == 1`.
- `test_cancelled_live_tick_does_not_abort`: likewise RED. No cancel happens, so "Live tick cancelled" never appears in the log.
- `test_stray_cancel_is_cleared_before_finalize`: RED. The finalizer raises `InferenceCancelled (stray)`, which leads to "Final transcription failed", and no "final text" is typed.
- `test_no_cancel_without_running_live_tick` and `test_engine_without_cancel_api_still_works`: already green (guard tests).

## Implementation (before → after)

**1. Import, controller.py:29**
```python
# before
from .asr import StreamTranscriber, TranscriptionResult
# after
from .asr import InferenceCancelled, StreamTranscriber, TranscriptionResult
```

**2. State, controller.py:142, after `self._typed_epoch = 0 ...`:**
```python
        # Phase 2 (A): True while a live tick is inside the engine; under _lock.
        self._live_tick_active = False
```

**3. `_run_once_exec`, controller.py:357-364**
```python
# before
        try:
            self._trace("live asr start")
            result = transcriber.maybe_transcribe()
            self._trace("live asr end")
        except Exception as exc:  # noqa: BLE001 - ASR error -> clean abort
            log.error("Transcription failed: %s", exc)
            self.abort()
            return
# after
        with self._lock:
            if self.state != ControllerState.RECORDING:
                return
            self._live_tick_active = True
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
```
The existing state check at 352-355 stays. The new one, in the same lock section, sets the flag atomically with the check. Alternatively, merge the two blocks into one `with self._lock:` (the flag set right after `if ... return`). That is preferred, since it avoids a double check.

**4. `_finish`, controller.py:464**, inside the `with self._lock:` block after `self._capture_stop.set()`:
```python
# before
            self._capture_stop.set()
            self._capture_thread = None
# after
            self._capture_stop.set()
            # Phase 2 (A): a live tick still inside the engine would only be
            # discarded (post-release discard); stop it so the finalizer can
            # take _exec_lock at once. Event.set() only - no lock taken.
            if self._live_tick_active:
                cancel = getattr(self._engine, "request_cancel", None)
                if cancel is not None:
                    cancel()
            self._capture_thread = None
```

**5. `_run_finalize_job`, controller.py:580-581**
```python
# before
            with self._exec_lock:
                self._trace("finalizer exec acquired")
# after
            with self._exec_lock:
                self._trace("finalizer exec acquired")
                # Phase 2 (A): no live tick can run while we hold _exec_lock;
                # drop any cancel request aimed at a (finished) live tick.
                clear_cancel = getattr(self._engine, "clear_cancel", None)
                if clear_cancel is not None:
                    clear_cancel()
```

**6. `abort()` (controller.py:267)** stays **unchanged**, so abort remains isolated. It does not cancel a live tick: the tick finishes and gets discarded by the stale check at 379.

**7. Next recording (live start):** `_run_once_exec` must not start with a set flag. The finalizer clears it, but a stray cancel can also arrive with no job, when `transcriber is None` at controller.py:496. Also call `clear_cancel` before `maybe_transcribe` in the same lock section where `_live_tick_active = True` is set: `getattr(self._engine, "clear_cancel", lambda: None)()`. This is safe because cancel only fires when the flag is True.

## GREEN
```bash
nice -n 19 .venv/bin/python -m pytest tests/test_controller.py -q -k "CancelsLiveTick or serialized or release_during_live"
nice -n 19 .venv/bin/python -m pytest -q
```
The existing tests `test_live_tick_and_finalize_are_serialized` and `test_release_during_live_asr_stops_audio_before_inference_returns` must stay green. `BlockingEngine` has no cancel API, so the guard path applies.

## Abort criterion
- A green test only passes with `sleep` tuning. Treat that as a signal of a race and redesign the flag logic first.
- Or the live test in block 3 shows "Final transcription failed: live inference cancelled" in the journal, even once.

Then revert the block (`git revert`) and keep block 1 as dead code. Alternatively, revert it too.
