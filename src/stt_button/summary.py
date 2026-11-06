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

#: Default maximum interior gap between two anchors (B5).
DEFAULT_MAX_GAP_CHARS = 2000

#: Default maximum read-text length (B5).
DEFAULT_MAX_READ_CHARS = 20000

#: Statuses returned by :func:`locate_block`.
CARET_AT_BLOCK_END = "caret_at_block_end"
CARET_NOT_AT_END = "caret_not_at_end"
AMBIGUOUS = "ambiguous"
NO_MATCH = "no_match"
GAP_TOO_LARGE = "gap_too_large"
TOO_LONG = "too_long"
EMPTY_READ = "empty_read"


@dataclass
class BlockLocation:
    """Result of locating the anchor block inside the read text.

    ``start``/``end`` are character offsets into the read text and are
    only set when mutation is allowed (``caret_at_block_end``). The
    conservative rule applies: without proof, nothing is deletable, so
    every abort status carries ``start=None``.
    """

    status: str
    start: Optional[int] = None
    end: Optional[int] = None


def locate_block(
    chunks: list[Chunk],
    read_text: str,
    *,
    summary_max_gap_chars: int = DEFAULT_MAX_GAP_CHARS,
    summary_max_read_chars: int = DEFAULT_MAX_READ_CHARS,
) -> BlockLocation:
    """Locate the anchor block (chunk texts) inside the read text.

    Pure function, no I/O. The read text is what a roundtrip reads from
    field start up to the caret; the chunk texts serve as anchors that
    must appear in order. The block spans from the first anchor's start
    to the end of the read text - typed text between anchors is part of
    the block (M3a). Mutation is only allowed when the read text ends
    with the last anchor (``caret_at_block_end``); every other situation
    aborts without a deletable span (conservative rule, plan line 124ff).
    """
    if len(read_text) > summary_max_read_chars:
        return BlockLocation(status=TOO_LONG)
    if not read_text:
        return BlockLocation(status=EMPTY_READ)
    anchors = [c.text for c in chunks if c.text]
    if not anchors:
        return BlockLocation(status=NO_MATCH)
    if any(read_text.count(a) > 1 for a in anchors):
        return BlockLocation(status=AMBIGUOUS)
    pos = 0
    start: Optional[int] = None
    gap_chars = 0
    for anchor in anchors:
        idx = read_text.find(anchor, pos)
        if idx < 0:
            return BlockLocation(status=NO_MATCH)
        if start is None:
            start = idx
        else:
            gap_chars += idx - pos
        pos = idx + len(anchor)
    if gap_chars > summary_max_gap_chars:
        return BlockLocation(status=GAP_TOO_LARGE)
    if pos != len(read_text):
        return BlockLocation(status=CARET_NOT_AT_END)
    return BlockLocation(status=CARET_AT_BLOCK_END, start=start, end=pos)


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
