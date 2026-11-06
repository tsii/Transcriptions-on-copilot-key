"""Session buffer for summary mode (D2, block B4).

Privacy rule D5: the buffer NEVER logs the text itself, only its character
length and a truncated sha1 hash. Focus/DBus wiring arrives in a later block;
this module is pure in-memory state with an injectable clock.
"""
from __future__ import annotations

import hashlib
import logging
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
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


def play_error_sound(name: str, volume: int, *,
                      asset_dir: Path | None = None,
                      popen=subprocess.Popen) -> bool:
    """Fire-and-forget playback of an error sound asset via paplay.

    Returns True if the process was started, False otherwise (paplay
    missing, asset missing, or any error). Never raises.
    """
    try:
        if shutil.which("paplay") is None:
            return False
        directory = asset_dir or Path(__file__).resolve().parent / "sounds"
        path = (directory / f"{name}.wav").resolve()
        if not path.is_file():
            return False
        popen(["paplay", "--volume", str(volume), str(path)],
              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


#: Coordinator status when the controller never went idle (B9a).
BUSY = "busy"

#: Abort status when the focused window is not an editable field (B9b).
FOCUS_NOT_EDITABLE = "focus_not_editable"

#: Abort reason when neither selection nor field read yields work (D4).
NOTHING_TO_DO = "nothing_to_do"

#: Focus classifications that block every emission (plan lines 66-72):
#: in a terminal ctrl+shift+Home is a terminal keybinding, and without a
#: reachable window there is nothing editable to act on at all.
FOCUS_BLOCKING_STATES = frozenset({"terminal", "unreachable", "no_window"})

#: Idle-wait poll interval (seconds) before keystroke output (B9a).
IDLE_POLL_S = 0.1


def _default_focus_state() -> str:
    """Production focus classifier (lazy import, never raises)."""
    try:
        from stt_button import focus
        return focus.focus_state()
    except Exception:  # noqa: BLE001 - classify as unreachable
        return "unreachable"


class WlClipboard:
    """Production clipboard collaborator: wl-paste / wl-copy (D4).

    Reads return ``""`` on any failure instead of raising, so a dead
    Wayland clipboard degrades to the abort paths instead of crashing
    the coordinator thread.
    """

    _TIMEOUT_S = 2.0

    def _paste(self, primary: bool) -> str:
        cmd = ["wl-paste"]
        if primary:
            cmd.append("-p")
        cmd += ["-t", "text/plain"]
        try:
            out = subprocess.run(cmd, capture_output=True, check=True,
                                 timeout=self._TIMEOUT_S)
            return out.stdout.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return ""

    def read_clipboard(self) -> str:
        return self._paste(primary=False)

    def read_primary(self) -> str:
        return self._paste(primary=True)

    def _copy(self, text: str, primary: bool) -> None:
        cmd = ["wl-copy"]
        if primary:
            cmd.append("-p")
        try:
            subprocess.run(cmd, input=text.encode("utf-8"), check=True,
                           timeout=self._TIMEOUT_S)
        except Exception:  # noqa: BLE001 - restore is best effort
            log.debug("wl-copy restore failed", exc_info=True)

    def copy_clipboard(self, text: str) -> None:
        self._copy(text, primary=False)

    def copy_primary(self, text: str) -> None:
        self._copy(text, primary=True)


class SummaryCoordinator:
    """State machine + idle wait for summary apply/undo (B9a).

    Owns exactly ONE worker thread. Before every keystroke-producing
    operation it polls ``controller.has_pending_work()`` every
    ``IDLE_POLL_S`` seconds until it returns ``False``; the deadline is
    ``cfg.summary_wait_idle_s``. On timeout the job is aborted with
    status :data:`BUSY`, the error sound is played, and not a single
    key is emitted. Decisions and undo bookkeeping come solely from the
    :class:`ApplyResult` returned by the submitted operation.

    Queueing multiple jobs, focus gating and the enabled switch are
    later blocks (B9b/B9c) and intentionally absent here: one job at a
    time, second submissions are rejected with ``False``.
    """

    IDLE = "idle"
    WORKING = "working"
    ACTIVE_UNDO = "active_undo"

    def __init__(self, controller, cfg, *, clock=None, sleep=None,
                 play_error=None, focus_state=None, clipboard=None,
                 typer=None, paster=None):
        self._controller = controller
        self._cfg = cfg
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep
        self._play_error = play_error or play_error_sound
        # B9b collaborators (D8): injected in tests, production defaults
        # are additive keyword arguments.
        self._classify_focus = focus_state or _default_focus_state
        self._clipboard = clipboard or WlClipboard()
        self._typer = typer
        self._paster = paster
        self._lock = threading.Lock()
        self._job = None  # (op, undo) - exactly one slot (queue = B9c)
        self._wake = threading.Event()
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="summary-coordinator")
        self._state = self.IDLE
        self._result = None
        self._submitted = 0
        self._completed = 0
        self._shutdown = False
        self._thread.start()

    # -- public API --------------------------------------------------

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def result(self) -> Optional[ApplyResult]:
        with self._lock:
            return self._result

    def submit(self, op, *, undo: bool = False) -> bool:
        """Submit one operation (callable -> ApplyResult).

        Returns ``False`` if a job is running or the coordinator is
        shut down; the job is never queued (B9c).
        """
        with self._lock:
            if self._shutdown or self._job is not None:
                return False
            self._job = (op, bool(undo))
            self._submitted += 1
            self._done.clear()
        self._wake.set()
        return True

    def submit_apply(self, replacement_text: str, chunks) -> bool:
        """Submit the gated apply flow (B9b).

        Runs the full decision chain on the worker thread: focus gate,
        primary-selection check (D4), then either the paste-chord
        selection path or the D3 block roundtrip. ``chunks`` are the
        anchor chunks (D2) for :func:`locate_block`.
        """
        anchor_chunks = list(chunks)

        def op() -> ApplyResult:
            return self._apply_flow(replacement_text, anchor_chunks)

        return self.submit(op)

    def wait_for_result(self, timeout: Optional[float] = None) -> Optional[ApplyResult]:
        """Block the caller until the worker produced a result.

        Returns the :class:`ApplyResult` of the most recently submitted
        job, or ``None`` if ``timeout`` elapses (or no job was ever
        submitted). Never runs work inline. Must not be called from the
        worker thread.
        """
        if threading.current_thread() is self._thread:
            raise RuntimeError(
                "wait_for_result() must not be called from the worker thread")
        with self._lock:
            target = self._submitted
            if self._completed >= target:
                return self._result
        deadline = (None if timeout is None
                    else time.monotonic() + float(timeout))
        while True:
            with self._lock:
                if self._completed >= target:
                    return self._result
            if deadline is not None and time.monotonic() >= deadline:
                return None
            self._done.wait(0.05)

    def shutdown(self) -> None:
        """Stop the worker thread; rejects further submissions."""
        with self._lock:
            self._shutdown = True
        self._wake.set()
        self._thread.join(timeout=5)

    # -- internals ---------------------------------------------------

    def _run(self) -> None:
        while True:
            self._wake.wait()
            self._wake.clear()
            with self._lock:
                if self._shutdown:
                    return
                job = self._job
                if job is None:
                    continue
            op, undo = job
            if undo:
                with self._lock:
                    self._state = self.ACTIVE_UNDO
            else:
                with self._lock:
                    self._state = self.WORKING
            try:
                if self._wait_idle():
                    result = self._safe_call(op)
                else:
                    self._play_busy_sound()
                    result = ApplyResult(
                        ok=False, status=BUSY, mode="",
                        removed_text="", inserted_text="",
                        reason="controller_busy")
            finally:
                with self._lock:
                    self._result = result
                    self._state = self.IDLE
                    self._job = None
                    self._completed += 1
                self._done.set()

    def _wait_idle(self) -> bool:
        """Poll has_pending_work() until False or the deadline passes."""
        timeout = float(getattr(self._cfg, "summary_wait_idle_s", 20))
        deadline = self._clock() + timeout
        while self._controller.has_pending_work() is not False:
            if self._clock() >= deadline:
                return False
            self._sleep(IDLE_POLL_S)
        return True

    def _play_busy_sound(self) -> None:
        name = getattr(self._cfg, "summary_error_sound", "")
        volume = int(getattr(self._cfg, "summary_error_sound_volume", 0))
        if not name:
            return
        try:
            self._play_error(name, volume)
        except Exception:  # noqa: BLE001 - sound must never break the FSM
            log.debug("summary error sound failed", exc_info=True)

    @staticmethod
    def _safe_call(op) -> ApplyResult:
        try:
            return op()
        except Exception:  # noqa: BLE001 - contract: never raise outward
            log.exception("summary operation crashed")
            return ApplyResult(ok=False, status=FAILED, mode="",
                              removed_text="", inserted_text="",
                              reason="coordinator_error")

    # -- B9b apply flow -------------------------------------------------

    def _apply_flow(self, replacement_text: str, chunks) -> ApplyResult:
        """Gated decision chain; never raises, emits nothing on gate."""
        try:
            return self._apply_flow_inner(replacement_text, chunks)
        except Exception:  # noqa: BLE001 - contract: never raise outward
            log.exception("summary apply flow crashed")
            return ApplyResult(ok=False, status=FAILED, mode="",
                               removed_text="", inserted_text="",
                               reason="coordinator_error")

    def _apply_flow_inner(self, replacement_text: str, chunks) -> ApplyResult:
        focus = self._classify_focus_safe()
        if focus in FOCUS_BLOCKING_STATES:
            # Gate BEFORE any emission: no key, no read, no paste (D7).
            self._play_busy_sound()
            return ApplyResult(ok=False, status=FOCUS_NOT_EDITABLE,
                               mode="", removed_text="", inserted_text="",
                               reason=FOCUS_NOT_EDITABLE)
        if self._typer is None or self._paster is None:
            return ApplyResult(ok=False, status=FAILED, mode="",
                               removed_text="", inserted_text="",
                               reason="no_collaborators")
        clip = self._clipboard
        primary = clip.read_primary()
        if primary:
            # D4: a non-empty primary selection means a live selection -
            # its content is the job; replace it with the paste chord.
            # No roundtrip, no ctrl+shift+Home (second read forbidden).
            return self._selection_path(replacement_text, primary)
        return self._block_path(replacement_text, chunks)

    def _classify_focus_safe(self) -> str:
        try:
            return self._classify_focus()
        except Exception:  # noqa: BLE001 - classify as unreachable
            return "unreachable"

    def _selection_path(self, replacement_text: str,
                        selection_text: str) -> ApplyResult:
        clip = self._clipboard
        saved_clipboard = clip.read_clipboard()
        try:
            combined = self._paster.paste_combined(replacement_text,
                                                  backspaces=0)
        finally:
            # D4: both selections restored in every branch, clipboard first.
            clip.copy_clipboard(saved_clipboard)
            clip.copy_primary(selection_text)
        if not combined:
            return ApplyResult(ok=False, status=FAILED, mode="",
                              removed_text="", inserted_text="",
                              reason="paster_error")
        return ApplyResult(ok=True, status=APPLY_PASTED, mode="pasted",
                           removed_text=selection_text,
                           inserted_text=replacement_text, reason=None)

    def _block_path(self, replacement_text: str, chunks) -> ApplyResult:
        clip, typer = self._clipboard, self._typer
        saved_clipboard = clip.read_clipboard()

        def restore() -> None:
            clip.copy_clipboard(saved_clipboard)
            clip.copy_primary("")

        # D3 roundtrip: select to field start, read, then clean up with
        # `key right` so no selection state remains in the field.
        typer.key("ctrl+shift+Home")
        read = clip.read_clipboard()
        typer.key("right")
        if not read:
            restore()
            return ApplyResult(ok=False, status=ABORTED, mode="",
                               removed_text="", inserted_text="",
                               reason=NOTHING_TO_DO)
        location = locate_block(
            chunks, read,
            summary_max_gap_chars=int(getattr(
                self._cfg, "summary_max_gap_chars",
                DEFAULT_MAX_GAP_CHARS)),
            summary_max_read_chars=int(getattr(
                self._cfg, "summary_max_read_chars",
                DEFAULT_MAX_READ_CHARS)),
        )
        if location.status != CARET_AT_BLOCK_END:
            restore()
            return ApplyResult(ok=False, status=ABORTED, mode="",
                               removed_text="", inserted_text="",
                               reason=location.status)
        n = location.end - location.start
        return apply_summary(
            replacement_text,
            original_text=read[location.start:location.end],
            n=n,
            typer=typer,
            paster=self._paster,
            restore_clipboard=lambda: clip.copy_clipboard(saved_clipboard),
            restore_primary=lambda: clip.copy_primary(""),
            paste_min_chars=int(getattr(
                self._cfg, "summary_paste_min_chars",
                DEFAULT_PASTE_MIN_CHARS)),
        )
