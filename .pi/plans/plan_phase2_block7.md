# Phase 2 – Block 7 (C3): Config, validation, wiring, and controller seams (dump)

Depends on blocks 5 and 6.

## Goal
- New config keys, with defaults that keep current behavior:
  - `segment_pause_s = 0.0` (off)
  - `segment_min_s = 3.0`
  - `segment_max_s = 0.0`
- Validation:
  - no negative values
  - `segment_pause_s > 0` requires `streaming_mode="whole_buffer"`
  - `segment_pause_s > 0` excludes `finalize_mode="cached_mid_tail"`, because both rebuild the prefix
  - `segment_pause_s > 0` requires `segment_max_s > 0`
  - when `live_asr_until_s > 0`, also `segment_max_s ≤ live_asr_until_s`
- `app.py` passes the keys to `StreamTranscriber`.
- Controller seams:
  - **`_dump_audio`** (controller.py:651): uses `transcriber.full_audio()` when present. Otherwise only the last segment would be saved, which breaks the corpus/gate.
  - **Live typing:** unchanged. The cut tick returns a result with `committed_len=len(closed)`, and DeltaTyper types the closed prefix in committed mode.
  - **`finalize_mode`:**
    - `full` and `cached` both work via `_finalize_compose` (block 5).
    - `cached_mid_tail` is excluded by validation.
  - **Dictation queue / FIFO:** unchanged. The segment state lives in the transcriber of the respective `FinalizeJob`.
  - **Done sound:** unchanged. It plays only after `_delta_typer.finalize`, not on a mini-final.
  - **`take_live_paused`:** works per segment (block 6). It never fires when cap ≤ limit.

## Test first
`tests/test_config.py`, appended:
```python
class TestSegmentConfig:
    def test_defaults_off(self) -> None:
        cfg = Config()
        assert (cfg.segment_pause_s, cfg.segment_min_s, cfg.segment_max_s) == (0.0, 3.0, 0.0)

    @pytest.mark.parametrize("body", [
        "segment_pause_s = -1.0\n",
        "segment_pause_s = 1.2\n",                                  # no cap
        "segment_pause_s = 1.2\nsegment_max_s = 20.0\n",            # cap > live limit 15
        "segment_pause_s = 1.2\nsegment_max_s = 15.0\nfinalize_mode = \"cached_mid_tail\"\n",
        "segment_pause_s = 1.2\nsegment_max_s = 15.0\nstreaming_mode = \"windowed\"\n",
    ])
    def test_invalid_combinations_rejected(self, tmp_path, body) -> None:
        with pytest.raises(ConfigError):
            load_config(write_config(tmp_path, body))

    def test_valid_segment_config(self, tmp_path) -> None:
        cfg = load_config(write_config(
            tmp_path, "segment_pause_s = 1.2\nsegment_max_s = 15.0\n"))
        assert cfg.segment_max_s == 15.0
```
`tests/test_app_wiring.py` → `TestTranscriberWiring`:
```python
    def test_segment_keys_wired(self, monkeypatch) -> None:
        _app, controller = _application(monkeypatch, segment_pause_s=1.2,
                                        segment_min_s=2.0, segment_max_s=15.0)
        t = controller.kwargs["transcriber_factory"](
            engine=object(), interval_s=1.0, min_audio_s=0.8)
        assert (t.segment_pause_s, t.segment_min_s, t.segment_max_s) == (1.2, 2.0, 15.0)
```
`tests/test_controller.py`, appended:
```python
class TestSegmentDump:
    def test_dump_contains_all_segments(self, tmp_path) -> None:
        import wave

        class SegTranscriber(RecordingTranscriber):
            def full_audio(self) -> bytes:
                return b"\x01\x00" * 16000 + bytes(self._buffer)

        dump = tmp_path / "last.wav"
        rec = SegTranscriber()
        ctrl = PushToTalkController(
            config=None,
            source_factory=lambda: FakeAudioSource([b"\x00" * 16000] * 2),
            engine=FakeEngine(),
            delta_typer=DeltaTyper(FakeTyper(), append_space=True),
            transcriber_factory=lambda *a, **kw: rec,
            dump_path=str(dump),
        )
        ctrl.start_recording()
        time.sleep(0.1)
        ctrl.stop_recording()
        _wait_state(ctrl, ControllerState.IDLE)
        with wave.open(str(dump), "rb") as w:
            assert w.getnframes() == 16000 + len(rec._buffer) // 2
```

## Expected RED
- config: `AttributeError: 'Config' object has no attribute 'segment_pause_s'`
- wiring: `AttributeError` / `assert (0.0, 3.0, 0.0) == ...`
- dump: `assert 16000 == 32000`. Only `_buffer` gets dumped.

## Implementation (before → after)

**1. `config.py:164`, after `live_asr_until_s: float = 15.0`:**
```python
    #: Phase 2 (C): sentence-pause segmentation. >0 = VAD pause (seconds)
    #: after a hypothesis ending in .?! closes the segment (mini-final,
    #: permanent commit, buffer reset). 0.0 = off (default). Requires
    #: segment_max_s > 0; live_asr_until_s then applies PER SEGMENT.
    segment_pause_s: float = 0.0
    #: Phase 2 (C): minimum segment length (seconds) before a pause cut.
    segment_min_s: float = 3.0
    #: Phase 2 (C): hard cap per segment (seconds); 0.0 = none. Must be
    #: <= live_asr_until_s so the live limit never pauses a segment.
    segment_max_s: float = 0.0
```
**2. `validate()`, after `live_asr_until_s < 0` (config.py:228-229):**
```python
        for key in ("segment_pause_s", "segment_min_s", "segment_max_s"):
            if getattr(self, key) < 0:
                raise ConfigError(f"{key} must not be negative")
        if self.segment_pause_s > 0:
            if self.streaming_mode != "whole_buffer":
                raise ConfigError("segment_pause_s requires streaming_mode='whole_buffer'")
            if self.finalize_mode == "cached_mid_tail":
                raise ConfigError("segment_pause_s cannot be combined with cached_mid_tail")
            if self.segment_max_s <= 0:
                raise ConfigError("segment_pause_s requires segment_max_s > 0")
            if 0 < self.live_asr_until_s < self.segment_max_s:
                raise ConfigError("segment_max_s must be <= live_asr_until_s")
```
Check the name of the default streaming mode: `VALID_STREAMING_MODES` in config.py, and `config.example.toml:38` uses `"whole_buffer"`.

**3. `app.py:166-167`**, inside `StreamTranscriber(...)` after `live_asr_until_s=cfg.live_asr_until_s,`:
```python
                # Phase 2 (C): sentence-pause segmentation (0.0 = off).
                segment_pause_s=cfg.segment_pause_s,
                segment_min_s=cfg.segment_min_s,
                segment_max_s=cfg.segment_max_s,
```
**4. `controller.py:651-653` (`_dump_audio`)**
```python
# before
            if not transcriber._buffer:
                return
            snapshot = bytes(transcriber._buffer)
# after
            full_audio = getattr(transcriber, "full_audio", None)
            # Phase 2 (C): dump ALL segments, not only the current buffer.
            snapshot = full_audio() if full_audio is not None else bytes(transcriber._buffer)
            if not snapshot:
                return
```
**5. `config.example.toml`**, after `live_asr_until_s = 15.0` (line 76):
```toml
# Phase 2 (C): sentence-pause segmentation (opt-in). A VAD pause after a
# hypothesis ending in .?! closes the segment; live_asr_until_s then
# applies per segment. Recommended when enabling: 1.2 / 3.0 / 15.0.
segment_pause_s = 0.0
segment_min_s = 3.0
segment_max_s = 0.0
```
**6. `docs/KONZEPT.md`:** add a short "Segmentation (Phase 2 C)" section. Afterwards check `tests/test_docs.py`.

## GREEN
```bash
nice -n 19 .venv/bin/python -m pytest tests/test_config.py tests/test_app_wiring.py tests/test_controller.py tests/test_docs.py -q
nice -n 19 .venv/bin/python -m pytest -q
```

## Abort criterion
An existing config/wiring test breaks because of the validation order. In that case move the segment check to the end of `validate()`. If the break is not fixable without semantic changes, stop and escalate to the reviewer.
