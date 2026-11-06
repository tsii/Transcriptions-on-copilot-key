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

import requests

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


#: Default request timeout for the summary gateway (seconds).
DEFAULT_TIMEOUT_S = 30.0

#: German system prompt for the summarizing model (plan, D6).
SUMMARY_SYSTEM_PROMPT = (
    "knapp, sachlich, sauber gegliedert; Stil der Vorlage erhalten "
    "(Du/Sie, lässig/formell); nichts hinzufügen, keine Zahlen "
    "erfinden; bei Zweifel Vorlage zurückgeben"
)


class ModelUnavailable(Exception):
    """The summary model is not usable (network, HTTP, empty or no-op)."""


@dataclass
class SummaryClient:
    """Minimal OpenAI-compatible chat client for summary mode (B7).

    Privacy rule D5: prompt, answer and token never reach the log -
    only character lengths and truncated sha1 hashes.
    """

    base_url: str
    token: str
    model: str
    timeout_s: float = DEFAULT_TIMEOUT_S
    max_chars: int = DEFAULT_MAX_READ_CHARS

    def chat(self, text: str) -> str:
        """Send ``text`` to the gateway and return the summary.

        Raises :class:`ModelUnavailable` for connection errors, timeouts,
        HTTP 4xx/5xx, empty answers and no-op answers (answer == prompt).
        """
        prompt = text[: self.max_chars]
        if len(prompt) != len(text):
            log.info("summary prompt truncated: len=%d trunc_len=%d sha1=%s",
                     len(text), len(prompt),
                     hashlib.sha1(prompt.encode()).hexdigest()[:8])
        model = self.model
        if model.startswith("my/"):
            model = model[len("my/"):]
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        payload = {
            "model": model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        }
        try:
            resp = requests.post(
                f"{self.base_url.rstrip('/')}/chat/completions",
                json=payload, timeout=self.timeout_s, headers=headers)
        except requests.exceptions.Timeout as exc:
            raise ModelUnavailable("timeout") from exc
        except requests.exceptions.RequestException as exc:
            raise ModelUnavailable("connection error") from exc
        if resp.status_code >= 400:
            raise ModelUnavailable(f"http {resp.status_code}")
        try:
            answer = resp.json()["choices"][0]["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ModelUnavailable("bad response body") from exc
        if not answer:
            raise ModelUnavailable("empty answer")
        if answer == prompt:
            raise ModelUnavailable("answer equals prompt")
        log.info("summary answer received: prompt_len=%d answer_len=%d sha1=%s",
                 len(prompt), len(answer),
                 hashlib.sha1(answer.encode()).hexdigest()[:8])
        return answer


#: Statuses returned by :func:`apply_summary` / :func:`undo_summary`.
APPLY_TYPED = "typed"
APPLY_PASTED = "pasted"
APPLY_PASTED_FALLBACK = "pasted_fallback"
UNDO_NOT_FOUND = "undo_not_found"
UNDO_DONE = "undone"
ABORTED = "aborted"
FAILED = "failed"

#: Paste threshold (mirrors config.summary_paste_min_chars).
DEFAULT_PASTE_MIN_CHARS = 40


@dataclass(frozen=True)
class ApplyResult:
    """Frozen result of :func:`apply_summary` / :func:`undo_summary` (B6).

    One object carries both channels: the outcome for the caller
    (``ok``/``status``/``reason``) and the exact undo bookkeeping
    (``removed_text``/``inserted_text``/``mode``). Privacy rule D5: the
    texts themselves never reach the journal, so ``__repr__`` only
    reports character lengths and a truncated sha1 over both texts.
    """

    ok: bool
    status: str
    mode: str
    removed_text: str
    inserted_text: str
    reason: Optional[str]

    def __repr__(self) -> str:
        digest = hashlib.sha1(
            (self.removed_text + "\x00" + self.inserted_text).encode()
        ).hexdigest()[:8]
        return (
            "ApplyResult(ok=%r, status=%r, mode=%r, reason=%r, "
            "removed_len=%d, inserted_len=%d, sha1=%s)"
            % (self.ok, self.status, self.mode, self.reason,
               len(self.removed_text), len(self.inserted_text), digest)
        )


def _restore(restore_clipboard: Callable[[], None],
             restore_primary: Callable[[], None]) -> None:
    """Restore clipboard first, then primary selection (both, in order)."""
    restore_clipboard()
    restore_primary()


def _put(text: str, n: int, *, typer, paster,
         paste_min_chars: int) -> tuple[str, str, Optional[str]]:
    """Replace ``n`` characters with ``text``: type or paste (B6).

    Short output goes through :meth:`backspace_and_type` with a separate
    trailing space key - the ``type`` payload itself never carries a
    synthetic trailing space (G24). Long output goes through
    ``paste_combined``; if that is not possible (``False``), the fallback
    is to type the text via ``backspace_and_type`` (plan decision (c):
    ``pasted_fallback`` carries ``mode="typed"``).

    Returns ``(status, mode, reason)``; ``status`` is ``None`` on error
    and ``reason`` is then ``"typer_error"`` or ``"paster_error"``.
    Never raises.
    """
    if len(text) < paste_min_chars:
        try:
            typer.backspace_and_type(n, text, trailing_key="space")
        except Exception:  # noqa: BLE001 - contract: never raise outward
            return "", "", "typer_error"
        return APPLY_TYPED, "typed", None
    try:
        combined = paster.paste_combined(text, backspaces=n)
    except Exception:  # noqa: BLE001
        return "", "", "paster_error"
    if combined:
        return APPLY_PASTED, "pasted", None
    try:
        typer.backspace_and_type(n, text, trailing_key="space")
    except Exception:  # noqa: BLE001
        return "", "", "typer_error"
    return APPLY_PASTED_FALLBACK, "typed", None


def apply_summary(
    replacement_text: str,
    *,
    original_text: str,
    n: int,
    typer,
    paster,
    restore_clipboard: Callable[[], None],
    restore_primary: Callable[[], None],
    paste_min_chars: int = DEFAULT_PASTE_MIN_CHARS,
) -> ApplyResult:
    """Replace the located block (``n`` chars) with the summary text.

    Returns an :class:`ApplyResult` whose ``removed_text``/
    ``inserted_text`` carry the exact undo bookkeeping for a later
    :func:`undo_summary`. Never raises and never reports ``ok=True``
    after an error (contract D5). Clipboard and primary selection are
    restored in EVERY branch, clipboard first.
    """
    try:
        status, mode, reason = _put(replacement_text, n, typer=typer,
                                    paster=paster,
                                    paste_min_chars=paste_min_chars)
    finally:
        _restore(restore_clipboard, restore_primary)
    if reason is not None:  # typer_error / paster_error
        return ApplyResult(ok=False, status=FAILED, mode="",
                           removed_text="", inserted_text="", reason=reason)
    digest = hashlib.sha1(replacement_text.encode()).hexdigest()[:8]
    log.info("summary applied: replaced=%d repl_len=%d sha1=%s",
             n, len(replacement_text), digest)
    return ApplyResult(ok=True, status=status, mode=mode,
                       removed_text=original_text,
                       inserted_text=replacement_text, reason=None)


def undo_summary(
    result: ApplyResult,
    *,
    read_text: str,
    typer,
    paster,
    restore_clipboard: Callable[[], None],
    restore_primary: Callable[[], None],
    paste_min_chars: int = DEFAULT_PASTE_MIN_CHARS,
) -> ApplyResult:
    """Undo an applied summary by re-typing the original block.

    Takes the :class:`ApplyResult` returned by :func:`apply_summary` and
    swaps ``removed_text``/``inserted_text`` back. The undo only fires
    when the current field content (``read_text``) ends with the inserted
    text (endsuffix proof). Without that proof nothing is typed or
    deleted (``ok=False``, ``status="aborted"``, ``reason="undo_not_found"``);
    only the clipboard/primary restore runs (in every branch, clipboard
    first). Never raises.
    """
    if not read_text.endswith(result.inserted_text):
        log.info("summary undo: replacement not at end - aborted")
        try:
            _restore(restore_clipboard, restore_primary)
        finally:
            pass
        return ApplyResult(ok=False, status=ABORTED, mode="",
                           removed_text="", inserted_text="",
                           reason=UNDO_NOT_FOUND)
    try:
        status, mode, reason = _put(result.removed_text,
                                   len(result.inserted_text),
                                   typer=typer, paster=paster,
                                   paste_min_chars=paste_min_chars)
    finally:
        _restore(restore_clipboard, restore_primary)
    if reason is not None:  # typer_error / paster_error
        return ApplyResult(ok=False, status=FAILED, mode="",
                           removed_text="", inserted_text="", reason=reason)
    digest = hashlib.sha1(result.removed_text.encode()).hexdigest()[:8]
    log.info("summary undone: repl=%d orig_len=%d sha1=%s status=%s",
             len(result.inserted_text), len(result.removed_text),
             digest, status)
    return ApplyResult(ok=True, status=UNDO_DONE, mode=mode,
                        removed_text=result.inserted_text,
                        inserted_text=result.removed_text, reason=None)
