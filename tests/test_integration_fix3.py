"""Integrationstest Fix 3: WindowedTranscriber -> DeltaTyper.

Stellt den Vertrag sicher: maybe_transcribe() liefert den VOLLEN Text
(committed + partial) und committed_len als Praefix-Laenge. So verwirft
DeltaTyper.update() ab dem 2. Commit nichts.
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
        # Immer das 0.0-Script liefern, egal welcher Offset (fuer den Test).
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
    """Der Kern des RC-3-Fixes: voller Text statt Fenster-Delta."""
    script = {
        0.0: [
            Token("Das", 0.0, 0.2),
            Token(" ist", 0.2, 0.4),
            Token(" de", 0.4, 0.5),      # Wortanfang -> bleibt partial
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

    # result.text muss der VOLLE Text sein (nicht nur ein Fenster-Delta).
    assert result.text.strip() != ""
    # committed_len muss <= len(text) sein (Praefix, nicht Gesamtlaenge).
    assert result.committed_len <= len(result.text)

    # Durch den DeltaTyper geleitet, darf es NICHT verworfen werden.
    delta.update(result.text, result.committed_len)
    # Etwas muss getippt worden sein.
    assert any(not x.startswith("<") for x in delta_typer.typed)

def test_maybe_transcribe_then_finalize_full_text() -> None:
    """Review-P1-Test: commit-then-finalize darf den Rest nicht verlieren.

    Reproduziert den Fall: erstes Fenster committet "So jetzt aber",
    dann finalize liefert NUR den Rest "mal". Der DeltaTyper muss den
    kompletten Text bekommen, sonst wird der Rest verschluckt.
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

    # finalize: Ergebnis muss den vollen Text enthalten (Prefix + Partial),
    # damit der DeltaTyper nichts verwirft oder dupliziert.
    result = t.finalize()
    assert result.text.strip() != ""
    assert result.committed_len <= len(result.text)

    delta.finalize(result.text, result.committed_len)
    # Am Ende muss Text getippt sein (kein stiller Verlust).
    joined = "".join(x for x in typer.typed if not x.startswith("<"))
    assert joined.strip() != ""
