"""Domain types and windowing policy for Parakeet streaming.

Based on the Kimi K3 research: Parakeet has no native streaming,
so we use "Buffered Chunked Inference" with sliding windows
[left_context | chunk | right_context]. Only tokens from the stable
center of the window are committed.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Token:
    """A single transcription token with an absolute timestamp in the stream."""

    text: str          # raw token text (including a leading space)
    start: float       # seconds, absolute within the stream
    end: float

    @property
    def is_word_start(self) -> bool:
        """Does this token begin a new word?"""
        return self.text.startswith(" ") or self.text.startswith("▁")


@dataclass(frozen=True)
class Transcription:
    """A transcription represented as a list of timestamped tokens."""

    tokens: list[Token]

    @property
    def text(self) -> str:
        return "".join(t.text for t in self.tokens)


@dataclass(frozen=True)
class StreamWindow:
    """An audio window to be transcribed."""

    start: float
    end: float
    commit_horizon: float


@dataclass(frozen=True)
class StreamWindowPolicy:
    """Determines which window to transcribe next.

    Parameters (tuned for CPU latency, more aggressive than NeMo 10-2-2):
        left_context_s  Context before the commit point (quality anchor).
        chunk_s         Length of the new stable chunk per tick.
        right_context_s Lookahead beyond the commit horizon.
        min_audio_s     Transcribe only once this much audio is available.
    """

    left_context_s: float = 4.0
    chunk_s: float = 2.0
    right_context_s: float = 1.0
    min_audio_s: float = 2.5

    def next_window(self, committed_s: float, audio_len_s: float) -> StreamWindow | None:
        """Return the next window, or None if there is nothing to do yet."""
        if audio_len_s < self.min_audio_s:
            return None

        # The new window starts at the commit point minus left context.
        start = max(0.0, committed_s - self.left_context_s)
        commit_end = committed_s + self.chunk_s

        # Fix (deadlock): The window must NOT be hard-capped at
        # committed+chunk+right. Otherwise, transcription never sees audio
        # beyond that point when the commit point stalls -> deadlock.
        # Extend the window through the available audio, capped at twice the
        # nominal size (keeps inference costs bounded).
        max_window_s = (self.left_context_s + self.chunk_s
                        + self.right_context_s) * 2
        end = min(audio_len_s, start + max_window_s)

        # If the commit point is already at or beyond the end of the audio,
        # there is nothing new to commit.
        if committed_s >= audio_len_s:
            return None

        # RC-A3: Commit only if right context remains beyond the horizon.
        # Otherwise, the horizon falls at the audio edge (TDT words unreliable).
        if audio_len_s < commit_end + self.right_context_s:
            return None

        commit_horizon = min(commit_end, audio_len_s)
        if commit_horizon <= committed_s:
            return None

        return StreamWindow(start=start, end=end, commit_horizon=commit_horizon)