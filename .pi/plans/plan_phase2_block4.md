# Phase 2 – Block 4 (B): Adaptive tick interval

Independent of A and C. It can run in parallel or before them. It is ordered after A because A changes the same controller line region (`_run_once_exec`), and doing them in sequence avoids merge conflicts.

## Goal
- `StreamTranscriber` learns its effective interval: `effective = clamp(k × last_tick_duration, interval_s, interval_max_s)`.
- TalkType uses `DecodePace`: k=2, base 2 s, clamp 2–6 s.
- Adaptation for STT-Button:
  - min = `asr_interval_s` (production 1.0 s config / 1.5 s live)
  - k = `asr_interval_adaptive_k`, **default 0.0 = off = current behavior**, recommended for the live test: 2.0
  - max = `asr_interval_max_s`, default 6.0
- "Tick duration" is wall-clock time of the engine call in `_transcribe` (asr.py:901), measured with `time.monotonic()`.
- Rationale: at FP32 RTF ~0.2, a 15 s buffer gives a ~3 s tick. With a 1 s interval, the live path saturates and blocks the CPU for the final. With k=2, the interval becomes 6 s and the CPU share drops to ≤ 50 %.
- Also gate the VAD throttle `_vad_due` (asr.py:890) with the same effective interval. Otherwise the VAD checks every 1 s, which is wasted work.

## Test first – append to `tests/test_asr.py`
```python
class TestAdaptiveInterval:
    """Phase 2 (B): interval = clamp(k * last tick, interval_s, interval_max_s)."""

    def _t(self, durations, **kw):
        import itertools

        engine = FakeEngine(["a b c"])
        t = StreamTranscriber(engine, interval_s=1.0, min_audio_s=0.5, **kw)
        clock = itertools.count()
        dur = iter(durations)
        state = {"now": 0.0}

        def fake_monotonic():
            return state["now"]

        orig = engine.transcribe

        def timed(data, initial_prompt=""):
            state["now"] += next(dur)
            return orig(data, initial_prompt)

        engine.transcribe = timed
        t._clock = fake_monotonic
        del clock
        return t, engine

    def test_default_k_zero_keeps_fixed_interval(self) -> None:
        t, engine = self._t([5.0] * 10)
        assert t.asr_interval_adaptive_k == 0.0
        t.feed(audio(1.0)); t.maybe_transcribe()
        t.feed(audio(1.0)); t.maybe_transcribe()
        assert len(engine.calls) == 2  # 1s interval despite a 5s tick

    def test_slow_tick_stretches_interval(self) -> None:
        t, engine = self._t([2.0] * 10, asr_interval_adaptive_k=2.0,
                            asr_interval_max_s=6.0)
        t.feed(audio(1.0)); t.maybe_transcribe()          # tick 1: 2s -> 4s
        assert t.effective_interval_s == pytest.approx(4.0)
        t.feed(audio(3.0)); assert t.maybe_transcribe() is None
        t.feed(audio(1.0)); assert t.maybe_transcribe() is not None
        assert len(engine.calls) == 2

    def test_clamped_to_max(self) -> None:
        t, _ = self._t([10.0], asr_interval_adaptive_k=2.0, asr_interval_max_s=6.0)
        t.feed(audio(1.0)); t.maybe_transcribe()
        assert t.effective_interval_s == pytest.approx(6.0)

    def test_clamped_to_min(self) -> None:
        t, _ = self._t([0.1], asr_interval_adaptive_k=2.0, asr_interval_max_s=6.0)
        t.feed(audio(1.0)); t.maybe_transcribe()
        assert t.effective_interval_s == pytest.approx(1.0)

    def test_reset_restores_base_interval(self) -> None:
        t, _ = self._t([3.0], asr_interval_adaptive_k=2.0)
        t.feed(audio(1.0)); t.maybe_transcribe()
        t.reset()
        assert t.effective_interval_s == pytest.approx(1.0)
```
`tests/test_config.py`, appended:
```python
class TestAdaptiveIntervalConfig:
    def test_defaults_keep_behavior(self) -> None:
        cfg = Config()
        assert cfg.asr_interval_adaptive_k == 0.0
        assert cfg.asr_interval_max_s == 6.0

    def test_negative_k_rejected(self, tmp_path) -> None:
        p = write_config(tmp_path, "asr_interval_adaptive_k = -1.0\n")
        with pytest.raises(ConfigError):
            load_config(p)

    def test_max_below_interval_rejected(self, tmp_path) -> None:
        p = write_config(tmp_path, "asr_interval_s = 2.0\nasr_interval_max_s = 1.0\n")
        with pytest.raises(ConfigError):
            load_config(p)
```
`tests/test_app_wiring.py`, appended in `TestTranscriberWiring`:
```python
    def test_adaptive_interval_wired(self, monkeypatch) -> None:
        _app, controller = _application(monkeypatch, asr_interval_adaptive_k=2.0,
                                        asr_interval_max_s=5.0)
        t = controller.kwargs["transcriber_factory"](
            engine=object(), interval_s=1.0, min_audio_s=0.8)
        assert t.asr_interval_adaptive_k == 2.0
        assert t.asr_interval_max_s == 5.0
```

## Expected RED
- `TypeError: StreamTranscriber.__init__() got an unexpected keyword argument 'asr_interval_adaptive_k'`, and `AttributeError ... asr_interval_adaptive_k` for the default test.
- `test_config`: `AttributeError: 'Config' object has no attribute 'asr_interval_adaptive_k'`.

## Implementation (before → after)

**1. `asr.py`, StreamTranscriber fields after `mid_tail_anchor_words: int = 2` (asr.py:507):**
```python
    #: PHASE 2 (B): adaptive live interval. 0.0 (default) = fixed interval_s
    #: (previous behavior). >0: next interval = clamp(k * last engine call
    #: duration, interval_s, asr_interval_max_s) - like TalkType DecodePace.
    asr_interval_adaptive_k: float = 0.0
    asr_interval_max_s: float = 6.0
```
After `_mid_tail_failed` (asr.py:546):
```python
    #: Phase 2 (B): duration of the last successful live engine call.
    _last_tick_s: float = field(default=0.0, init=False, repr=False)
    #: Injectable clock (tests).
    _clock: object = field(default=None, init=False, repr=False)
```

**2. New property after `last_text` (asr.py ~566):**
```python
    @property
    def effective_interval_s(self) -> float:
        """Live interval honoring the adaptive pace (Phase 2, B)."""
        if self.asr_interval_adaptive_k <= 0.0 or self._last_tick_s <= 0.0:
            return self.interval_s
        paced = self.asr_interval_adaptive_k * self._last_tick_s
        return max(self.interval_s, min(paced, self.asr_interval_max_s))
```

**3. `reset()` (asr.py:591), after `self._mid_tail_failed = False`:** `self._last_tick_s = 0.0`

**4. asr.py:666 and asr.py:890**: `self.interval_s` → `self.effective_interval_s` (only these two lines).

**5. `_transcribe`, asr.py:901**
```python
# before
        result = self.engine.transcribe(data, initial_prompt=self._committed_text)
# after
        clock = self._clock or time.monotonic
        t0 = clock()
        result = self.engine.transcribe(data, initial_prompt=self._committed_text)
        self._last_tick_s = clock() - t0
```
Also add `import time` to the imports (asr.py:17-20). `finalize()` also calls `_transcribe`, but after finalize the transcriber is discarded, so this is harmless.

**6. `config.py:83`, after `asr_interval_s: float = 1.0`:**
```python
    #: Phase 2 (B): adaptive live interval factor. 0.0 = off (default,
    #: previous behavior); 2.0 = TalkType pacing (interval = 2x last tick).
    asr_interval_adaptive_k: float = 0.0
    #: Phase 2 (B): upper clamp of the adaptive interval (seconds).
    asr_interval_max_s: float = 6.0
```
`validate()` after `asr_interval_s <= 0` (config.py:230-231):
```python
        if self.asr_interval_adaptive_k < 0:
            raise ConfigError("asr_interval_adaptive_k must not be negative")
        if self.asr_interval_max_s < self.asr_interval_s:
            raise ConfigError("asr_interval_max_s must be >= asr_interval_s")
```

**7. `app.py:155-167`, `StreamTranscriber(...)`:** add `asr_interval_adaptive_k=cfg.asr_interval_adaptive_k, asr_interval_max_s=cfg.asr_interval_max_s,`. Do not wire it for windowed/buffered.

**8. `config.example.toml:40`, after `asr_interval_s`:**
```toml
asr_interval_adaptive_k = 0.0  # >0: live interval = k x last tick (2.0 = TalkType)
asr_interval_max_s = 6.0       # upper bound of the adaptive interval
```

## GREEN
```bash
nice -n 19 .venv/bin/python -m pytest tests/test_asr.py tests/test_config.py tests/test_app_wiring.py -q
nice -n 19 .venv/bin/python -m pytest -q
```
Also run `tests/test_docs.py`. If it checks config keys against KONZEPT.md, add a line for both keys to `docs/KONZEPT.md` (Streaming section).

## Abort criterion
In the live test with k=2, p95 release→text does not improve by ≥ 10 % compared with k=0, or the live preview feels choppy. In that case leave the default at 0. The code stays, because it is opt-in and harmless. Do not revert.
