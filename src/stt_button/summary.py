"""Session buffer for summary mode (D2, block B4).

Privacy rule D5: the buffer NEVER logs the text itself, only its character
length and a truncated sha1 hash. Focus/DBus wiring arrives in a later block;
this module is pure in-memory state with an injectable clock.
"""
from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Callable, Hashable, Optional

log = logging.getLogger(__name__)

#: Fallback idle window (seconds) when no idle value is given.
DEFAULT_IDLE_S = 300.0


@dataclass
class Chunk:
    """One appended output: text plus its typing epoch and timestamp."""

    text: str
    epoch: int
    ts: float


class SummaryBuffer:
    """Accumulates per-output chunks, resetting on session boundaries.

    A session ends (buffer resets) when either

    * the time gap between two outputs exceeds ``idle_s`` (idle reset), or
    * the ``window_identity`` (e.g. ``(app, title)``) changes between outputs.

    ``window_identity=None`` (unknown window) NEVER resets the session.
    """

    def __init__(
        self,
        *,
        idle_s: float = DEFAULT_IDLE_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._idle_s = float(idle_s)
        self._clock = clock
        self._chunks: list[Chunk] = []
        self._last_ts: Optional[float] = None
        self._last_identity: Optional[Hashable] = None

    @property
    def chunks(self) -> list[Chunk]:
        """Current session chunks (oldest first)."""
        return self._chunks

    def append(
        self,
        text: str,
        *,
        epoch: int,
        window_identity: Optional[Hashable] = None,
    ) -> None:
        """Append one chunk, resetting first if the session boundary passed.

        The session boundary is evaluated BEFORE the append so the new text
        always belongs to the fresh session. Logging follows D5: length and
        sha1 prefix only, never the text.
        """
        now = self._clock()
        reset_reason: Optional[str] = None
        if self._last_identity is not None and window_identity is not None \
                and window_identity != self._last_identity:
            reset_reason = "window identity changed"
        elif self._last_ts is not None and (now - self._last_ts) > self._idle_s:
            reset_reason = "idle gap exceeded"
        if reset_reason:
            self._reset(reset_reason)
        self._chunks.append(Chunk(text=text, epoch=epoch, ts=now))
        self._last_ts = now
        self._last_identity = window_identity
        digest = hashlib.sha1(text.encode()).hexdigest()[:8]
        log.info("summary chunk appended: len=%d sha1=%s", len(text), digest)

    def reset(self) -> None:
        """Explicitly clear the current session (reason unspecified)."""
        self._reset("explicit reset")

    def _reset(self, reason: str) -> None:
        log.info("summary session reset: chunks=%d reason=%s",
                 len(self._chunks), reason)
        self._chunks = []
        self._last_ts = None
        self._last_identity = None
