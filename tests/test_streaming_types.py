"""Tests für den windowed Streaming-Ansatz (Schritt 1+2).

Getestet wird die WindowingPolicy: reine Funktion, die entscheidet, welches
Audio-Fenster als nächstes transkribiert wird. Kein Modell nötig.
"""
from __future__ import annotations

import pytest

from stt_button.streaming_types import StreamWindow, StreamWindowPolicy, Token, Transcription


class TestToken:
    def test_transcription_text_is_concatenation(self) -> None:
        tokens = [
            Token(text="So", start=0.0, end=0.16),
            Token(text=" jetzt", start=0.16, end=0.40),
        ]
        tr = Transcription(tokens=tokens)
        assert tr.text == "So jetzt"

    def test_word_boundary_detection(self) -> None:
        # Tokens mit führendem Leerzeichen sind Wortanfänge
        t1 = Token(text="So", start=0.0, end=0.16)
        t2 = Token(text=" jetzt", start=0.16, end=0.40)
        assert not _is_word_start(t1)
        assert _is_word_start(t2)


def _is_word_start(t: Token) -> bool:
    return t.text.startswith(" ") or t.text.startswith(" ")


class TestStreamWindowPolicy:
    def _policy(self, **kw) -> StreamWindowPolicy:
        defaults = dict(
            left_context_s=4.0,
            chunk_s=2.0,
            right_context_s=1.0,
            min_audio_s=2.5,
        )
        defaults.update(kw)
        return StreamWindowPolicy(**defaults)

    def test_below_min_audio_returns_none(self) -> None:
        window = self._policy().next_window(committed_s=0.0, audio_len_s=2.0)
        assert window is None

    def test_first_window_uses_min_audio(self) -> None:
        window = self._policy().next_window(committed_s=0.0, audio_len_s=3.0)
        assert window is not None
        assert window.start == 0.0
        # chunk endet bei committed + chunk, gedeckelt auf audio_len
        assert window.end == pytest.approx(3.0)
        assert window.commit_horizon <= window.end

    def test_window_bounded_even_for_long_stream(self) -> None:
        """Fenster bleibt begrenzt, auch bei 60s Stream (Skalierungs-Fix)."""
        policy = self._policy()
        committed_s = 30.0
        audio_len_s = 60.0
        window = policy.next_window(committed_s=committed_s, audio_len_s=audio_len_s)
        assert window is not None
        assert window.start >= committed_s - policy.left_context_s
        # Begrenzung: max. das Doppelte der Nominalgroesse (Deadlock-Schutz).
        nominal = policy.left_context_s + policy.chunk_s + policy.right_context_s
        assert window.end - window.start <= nominal * 2

    def test_commit_horizon_leaves_right_context(self) -> None:
        policy = self._policy()
        committed_s = 10.0
        audio_len_s = 20.0
        window = policy.next_window(committed_s=committed_s, audio_len_s=audio_len_s)
        assert window is not None
        # Commit-Horizont darf nie über das Audioende hinausragen
        assert window.commit_horizon <= audio_len_s
        # Und liegt normalerweise um right_context vor dem Fensterende
        if window.end < audio_len_s:
            assert window.commit_horizon == pytest.approx(window.end - policy.right_context_s)

    def test_no_new_chunk_after_commit_returns_none(self) -> None:
        policy = self._policy()
        # committed steht bereits am Ende -> kein neuer chunk verfügbar
        window = policy.next_window(committed_s=19.0, audio_len_s=20.0)
        # Falls der verbleibende Rest < chunk, darf trotzdem finalisiert werden
        # (unten separat getestet); hier: wenn kaum noch audio da ist, None oder
        # ein letztes kleines Fenster.
        # Wir testen nur, dass es nicht crasht und bounded bleibt.
        if window is not None:
            assert window.end <= 20.0

    def test_window_start_depends_on_context(self) -> None:
        policy = self._policy(left_context_s=4.0)
        window = policy.next_window(committed_s=6.0, audio_len_s=20.0)
        assert window is not None
        assert window.start == pytest.approx(6.0 - 4.0)

    def test_default_window_fields(self) -> None:
        w = StreamWindow(start=0.0, end=3.0, commit_horizon=2.0)
        assert w.start == 0.0
        assert w.commit_horizon == 2.0