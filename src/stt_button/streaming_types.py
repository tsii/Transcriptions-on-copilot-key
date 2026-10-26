"""Domänen-Typen und Windowing-Policy für das Parakeet-Streaming.

Basierend auf der Kimi-K3-Recherche: Parakeet hat kein natives Streaming,
also verwenden wir "Buffered Chunked Inference" mit gleitenden Fenstern
[links_context | chunk | right_context]. Nur Tokens aus der stabilen
Fenstermitte werden committet.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Token:
    """Ein einzelnes Transkriptions-Token mit absoluter Zeit im Stream."""

    text: str          # roher Token-Text (inkl. führendem Leerzeichen)
    start: float       # Sekunden, absolut im Stream
    end: float

    @property
    def is_word_start(self) -> bool:
        """Ist dieses Token der Beginn eines neuen Wortes?"""
        return self.text.startswith(" ") or self.text.startswith("▁")


@dataclass(frozen=True)
class Transcription:
    """Eine Transkription als Liste von zeitgestempelten Tokens."""

    tokens: list[Token]

    @property
    def text(self) -> str:
        return "".join(t.text for t in self.tokens)


@dataclass(frozen=True)
class StreamWindow:
    """Ein zu transkribierendes Audio-Fenster."""

    start: float
    end: float
    commit_horizon: float


@dataclass(frozen=True)
class StreamWindowPolicy:
    """Entscheidet, welches Fenster als nächstes transkribiert wird.

    Parameter (an CPU-Latenz angepasst, aggressiver als NeMo 10-2-2):
        left_context_s  Context vor dem Commit-Punkt (Qualitätsanker).
        chunk_s         Länge des neuen stabilen Chunks pro Tick.
        right_context_s Lookahead nach dem Commit-Horizont.
        min_audio_s     Erst ab dieser Audio-Länge überhaupt transkribieren.
    """

    left_context_s: float = 4.0
    chunk_s: float = 2.0
    right_context_s: float = 1.0
    min_audio_s: float = 2.5

    def next_window(self, committed_s: float, audio_len_s: float) -> StreamWindow | None:
        """Liefert das nächste Fenster oder None, wenn noch nichts zu tun ist."""
        if audio_len_s < self.min_audio_s:
            return None

        # Neues Fenster beginnt beim committed-Punkt minus Links-Kontext.
        start = max(0.0, committed_s - self.left_context_s)
        # Ende = committed + chunk + right_context, gedeckelt auf audio_len.
        commit_end = committed_s + self.chunk_s
        end = min(audio_len_s, commit_end + self.right_context_s)

        # Wenn der Commit-Punkt schon am (oder über) dem Audioende liegt,
        # gibt es nichts neues zu committen.
        if committed_s >= audio_len_s:
            return None

        # RC-A3: Nur committen, wenn danach noch Rechts-Kontext vorhanden ist.
        # Sonst landet der Horizont an der Audio-Kante (TDT-Wörter unsicher).
        if audio_len_s < commit_end + self.right_context_s:
            return None

        commit_horizon = min(commit_end, audio_len_s)
        if commit_horizon <= committed_s:
            return None

        return StreamWindow(start=start, end=end, commit_horizon=commit_horizon)