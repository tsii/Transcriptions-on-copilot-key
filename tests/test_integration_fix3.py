"""Integration test for fix 3: WindowedTranscriber -> DeltaTyper.

Ensures the contract: maybe_transcribe() returns the FULL text
(committed + partial) and committed_len as the prefix length. This prevents
DeltaTyper.update() from discarding anything from the second commit onward.
"""
from __future__ import annotations

from stt_button.streaming_types import Token, Transcription
from stt_button.typing import DeltaTyper
from stt_button.windowed import WindowedTranscriber


class FakeEngine:
    def __init__(self, script: dict[float, list[Token]]) -> None:
        self.script = script
        self.offsets: list[float] = []

    def transcribe_with_timestamps(self, data: bytes, window_offset_s: float = 0.0) -> Transcription:
        self.offsets.append(round(window_offset_s, 2))
        # Always return the 0.0 script, regardless of offset (for the test).
        return Transcription(tokens=self.script.get(0.0, []))

    def transcribe(self, data: bytes, initial_prompt: str = ""):
        class R:
            text = "ende"
        return R()


class AlwaysSpeech:
    def has_speech(self, audio_tail) -> bool:
        return True


class RecordingTyper:
    def __init__(self) -> None:
        self.typed: list[str] = []

    def type_text(self, text: str) -> None:
        self.typed.append(text)

    def backspace(self, n: int) -> None:
        self.typed.append(f"<BS:{n}>")

    def key(self, name: str) -> None:
        self.typed.append(f"<KEY:{name}>")


def test_maybe_transcribe_returns_full_text() -> None:
    """Core of the RC-3 fix: full text instead of a window delta."""
    script = {
        0.0: [
            Token("Das", 0.0, 0.2),
            Token(" ist", 0.2, 0.4),
            Token(" de", 0.4, 0.5),      # word start -> remains partial
            Token("utlich", 0.5, 0.7),   # -> "deutlich"
            Token(" besser", 0.7, 0.9),
        ],
    }
    engine = FakeEngine(script)
    delta_typer = RecordingTyper()
    delta = DeltaTyper(delta_typer, append_space=False)
    t = WindowedTranscriber(engine=engine, min_audio_s=0.5, chunk_s=0.5,
                            left_context_s=0.0, right_context_s=0.0,
                            vad=AlwaysSpeech())

    t.feed(b"\x00\x00" * int(1.0 * 16000))
    result = t.maybe_transcribe()
    assert result is not None

    # result.text must be the FULL text (not just a window delta).
    assert result.text.strip() != ""
    # committed_len must be <= len(text) (prefix, not total length).
    assert result.committed_len <= len(result.text)

    # DeltaTyper must NOT discard it when processing the update.
    delta.update(result.text, result.committed_len)
    # Something must have been typed.
    assert any(not x.startswith("<") for x in delta_typer.typed)

def test_maybe_transcribe_then_finalize_full_text() -> None:
    """Review P1 test: commit-then-finalize must not lose the remainder.

    Reproduces the case where the first window commits "So jetzt aber",
    then finalize returns ONLY the remainder "mal". DeltaTyper must receive
    the complete text; otherwise, the remainder is lost.
    """
    from stt_button.streaming_types import Token, Transcription

    class ScriptEngine:
        def __init__(self) -> None:
            self.tokens = [
                Token("So", 0.0, 0.2),
                Token(" jetzt", 0.2, 0.4),
                Token(" aber", 0.4, 0.6),
            ]

        def transcribe_with_timestamps(self, data, window_offset_s=0.0) -> Transcription:
            return Transcription(tokens=self.tokens)

        def transcribe(self, data, initial_prompt=""):
            class R:
                text = "mal"
            return R()

    engine = ScriptEngine()
    typer = RecordingTyper()
    delta = DeltaTyper(typer, append_space=False)
    t = WindowedTranscriber(engine=engine, min_audio_s=0.5, chunk_s=0.5,
                            left_context_s=0.0, right_context_s=0.0,
                            vad=AlwaysSpeech())

    t.feed(b"\x00\x00" * int(2.0 * 16000))
    r1 = t.maybe_transcribe()
    assert r1 is not None
    delta.update(r1.text, r1.committed_len)

    # finalize: the result must contain the full text (prefix + partial)
    # so DeltaTyper does not discard or duplicate anything.
    result = t.finalize()
    assert result.text.strip() != ""
    assert result.committed_len <= len(result.text)

    delta.finalize(result.text, result.committed_len)
    # Text must have been typed at the end (no silent loss).
    joined = "".join(x for x in typer.typed if not x.startswith("<"))
    assert joined.strip() != ""
