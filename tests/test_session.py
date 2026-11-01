"""Tests for StreamingAsrSession (step 6).

End-to-end with a fake engine and fake typer; no real model.
"""
from __future__ import annotations

import numpy as np
import pytest

from stt_button.session import StreamingAsrSession
from stt_button.streaming_types import StreamWindowPolicy, Token, Transcription
from stt_button.vad import VadGate


def audio(seconds: float) -> bytes:
    return b"\x00\x00" * int(seconds * 16000)


class FakeEngine:
    """Return scripted transcriptions for each window."""

    def __init__(self, results: dict[float, list[Token]] | None = None) -> None:
        self.results = results or {}
        self.calls: list[bytes] = []

    def transcribe_with_timestamps(self, data: bytes, window_offset_s: float = 0.0) -> Transcription:
        self.calls.append(data)
        tokens = self.results.get(window_offset_s, [])
        return Transcription(tokens=tokens)

    def transcribe(self, data: bytes, initial_prompt: str = "") -> object:
        class R:
            text = "Resttext"
        return R()


class FakeTyper:
    def __init__(self) -> None:
        self.commits: list[str] = []
        self.revisions: list[tuple[str, str]] = []

    def commit(self, text: str) -> None:
        self.commits.append(text)

    def revise_partial(self, old: str, new: str) -> None:
        self.revisions.append((old, new))


class AlwaysSpeech(VadGate):
    def has_speech(self, audio_tail) -> bool:
        return True


class TestSession:
    def _session(self, engine, typer, **kw) -> StreamingAsrSession:
        return StreamingAsrSession(
            engine, typer, vad=AlwaysSpeech(),
            policy=StreamWindowPolicy(left_context_s=2.0, chunk_s=1.0,
                                      right_context_s=0.5, min_audio_s=1.5),
            **kw,
        )

    def test_tick_below_min_audio_does_nothing(self) -> None:
        e = FakeEngine()
        t = FakeTyper()
        s = self._session(e, t)
        s.on_audio(audio(1.0))
        s.on_tick()
        assert e.calls == []
        assert t.commits == []

    def test_tick_transcribes_window_and_commits(self) -> None:
        # Window at committed=0 -> offset 0, commit_horizon = chunk=1.0
        tokens = [
            Token("So", 0.0, 0.3),
            Token(" jetzt", 0.3, 0.6),
            Token(" weit", 0.6, 1.4),  # crosses horizon=1.0
        ]
        e = FakeEngine({0.0: tokens})
        t = FakeTyper()
        s = self._session(e, t)
        s.on_audio(audio(3.0))
        s.on_tick()
        assert e.calls
        # " weit" crosses the horizon and is a word start -> it is
        # conservatively held back rather than committed.
        assert not any("weit" in c for c in t.commits)

    def test_release_finalizes(self) -> None:
        e = FakeEngine()
        t = FakeTyper()
        s = self._session(e, t)
        s.on_audio(audio(4.0))
        s.on_release()
        # Final transcription was triggered
        assert t.revisions or t.commits

    def test_reset_clears_state(self) -> None:
        e = FakeEngine()
        t = FakeTyper()
        s = self._session(e, t)
        s.on_audio(audio(2.0))
        s.reset()
        assert s.audio_seconds == 0.0
        assert s.committer.committed_s == 0.0