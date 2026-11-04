"""Output the transcript at the cursor position.

:class:`DeltaTyper` compares consecutive transcriptions and types only the
difference. If an already typed portion is corrected, it is removed with
backspaces and typed again.

``wtype`` is used (Wayland, no X toolkit required). A clipboard mode is
available as a fallback; it inserts text via ``wl-copy`` and Ctrl+V and also
works in windows where ``wtype`` has no effect.
"""
from __future__ import annotations

import glob
import logging
import shutil
import subprocess
import time
from typing import Callable, Protocol, runtime_checkable

log = logging.getLogger(__name__)

#: Maximum number of backspaces per wtype call (keeps the command line short).
BACKSPACE_BATCH = 20

#: Default timeout for _run() commands in seconds. Protection against
#: daemon pipe hangs: ``wl-copy`` forks a resident daemon process that
#: inherits the pipe FDs (opened by capture_output). subprocess.run then
#: waits in communicate() for an EOF that never arrives. DEVNULL FDs and
#: a timeout close both attack paths.
COMMAND_TIMEOUT_S = 2.0

#: Pause indicator (STAGE 4): typed EXACTLY ONCE when live ASR shuts off
#: due to ``live_asr_until_s`` ("still listening, but not writing right
#: now"). Deliberately three ASCII dots instead of the SINGLE Unicode
#: character U+2026: a Ground-Truth experiment (11:36 live test) showed
#: that dotool CANNOT type U+2026 in the active de/nodeadkeys layout
#: ('dotool: WARNING: impossible character for layout: …', rc=0, character
#: SILENTLY DISCARDED), so the indicator never appeared in the target
#: application. '...' can be typed in every layout.
#: Collision safety: the finalize() difference is prefix-based (common
#: prefix). If the final text itself ends in dots, the common prefix simply
#: extends across the dots (up to the first difference); only
#: old_tail[common:] is removed and new_tail[common:] is output, so actual
#: dots can neither be swallowed nor diluted.
PAUSE_INDICATOR = "..."


def _capslock_leds() -> dict[str, str]:
    """Read the CapsLock LED state per device (diagnostics).

    Background: An inverted CapsLock state in the input pipeline corrupts
    typed text character by character ("Das" -> "dAS"). This is NOT the ASR
    text. We therefore log the lock state before and after each typing
    operation to identify the source unambiguously.

    Returns:
        Mapping device name -> "an" (=1), "aus" (=0), or "?" (unreadable).
        The device name is the basename of the LED directory name, e.g.,
        "input3::capslock".
    """
    result: dict[str, str] = {}
    try:
        leds = glob.glob("/sys/class/leds/*capslock*/brightness")
    except Exception:  # noqa: BLE001 - diagnostics must never disrupt operation
        return result
    for path in leds:
        import os
        # Basename of the LED directory name, e.g.,
        # "/sys/class/leds/input3::capslock" -> "input3::capslock".
        device = os.path.basename(os.path.dirname(path))
        try:
            with open(path) as fh:
                raw = fh.read().strip()
            if raw == "1":
                result[device] = "an"
            elif raw == "0":
                result[device] = "aus"
            else:
                result[device] = "?"
        except OSError:
            result[device] = "?"
    return result


def _capslock_state() -> str:
    """Read the aggregate CapsLock LED state (diagnostics).

    Builds on :func:`_capslock_leds`: "an" if any device reports "an",
    otherwise "aus"; "?" if there is no LED.

    Returns:
        "an", "aus", or "?" (no LED found).
    """
    try:
        leds = _capslock_leds()
    except Exception:  # noqa: BLE001 - diagnostics must never disrupt operation
        return "?"
    if not leds:
        return "?"
    return "an" if any(v == "an" for v in leds.values()) else "aus"


class TyperError(RuntimeError):
    """Output to the focused window failed."""


@runtime_checkable
class Typer(Protocol):
    """Interface for all output adapters."""

    def type_text(self, text: str) -> None:
        """Type the text literally into the focused window."""

    def backspace(self, n: int) -> None:
        """Delete ``n`` characters before the cursor."""

    def key(self, name: str) -> None:
        """Press a key, e.g., ``"space"`` or ``"ctrl+v"``."""


@runtime_checkable
class FinalPaster(Protocol):
    """Interface for inserting the final delta via the clipboard.

    Used during finalization instead of character-by-character typing to
    output long dictations without ~35 ms per character. Receives only the
    calculated delta, never the full text.
    """

    def paste(self, text: str) -> None:
        """Insert ``text`` at the cursor position."""


def _run(cmd: list[str], *, timeout_s: float = COMMAND_TIMEOUT_S) -> None:
    """Run a command and convert errors to :class:`TyperError`.

    Hardening against daemon pipe hangs (final_paste freeze): all three
    standard FDs point to DEVNULL instead of pipes. ``wl-copy`` forks a
    resident daemon and passes open pipe FDs to child processes;
    communicate() would otherwise wait for an EOF that never arrives.
    The timeout is the second line of defense.
    """
    try:
        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise TyperError(f"{cmd[0]}: timeout after {timeout_s:.1f}s") from exc
    except FileNotFoundError as exc:
        raise TyperError(f"{cmd[0]} is not installed") from exc
    except OSError as exc:
        raise TyperError(f"{cmd[0]} could not be started: {exc}") from exc
    if proc.returncode != 0:
        raise TyperError(f"{cmd[0]} failed (rc={proc.returncode})")


class DotoolTyper:
    """Type via ``dotool`` (uinput, also works under GNOME Wayland).

    ``dotool`` reads commands from stdin: ``type TEXT`` for text and
    ``key NAME`` for individual keys. It requires the udev rule that grants
    users in the ``input`` group access to /dev/uinput.
    """

    binary = "dotool"

    def __init__(
        self,
        binary: str | None = None,
        *,
        xkb_layout: str = "de",
        xkb_variant: str = "nodeadkeys",
        typedelay_ms: int = 20,
        typehold_ms: int = 15,
        capslock_autocorrect: bool = False,
        capslock_led_inverted: bool = True,
    ) -> None:
        if binary:
            self.binary = binary
        if shutil.which(self.binary) is None:
            raise TyperError(f"{self.binary} was not found")
        # dotool types characters via the XKB layout. Without the German
        # layout, errors such as swapped z<->y or ? vs _ (US layout) occur.
        self._xkb_layout = xkb_layout
        self._xkb_variant = xkb_variant
        # Increase the key-press delay: with the dotool defaults (2 ms),
        # Shift remains active for the following letter -> "So" becomes "SO".
        self._typedelay_ms = typedelay_ms
        self._typehold_ms = typehold_ms
        # Default: DO NOT intervene. The CapsLock lock state cannot be read
        # reliably (LED != State, xset != State), so the app may only detect
        # it and warn.
        self._capslock_autocorrect = capslock_autocorrect
        # LED semantics of this system (empirically verified): True = LED off
        # means CapsLock is active (= inverted). False = conventional mapping
        # (LED on = CapsLock active).
        self._capslock_led_inverted = capslock_led_inverted

    def _capslock_leds_target(self) -> str:
        """Target LED state for normal typing.

        With inverted semantics, LED "an" is normal; otherwise "aus".
        """
        return "an" if self._capslock_led_inverted else "aus"

    def _ensure_capslock_normal(self) -> None:
        """Warn if CapsLock inverts text; never toggle by default.

        Ground Truth (empirically verified): LED **off** = CapsLock active =
        inverted text; LED **on** = normal typing. The target is configurable
        via ``capslock_led_inverted``.

        ``key capslock`` is a TOGGLE, and the lock state cannot be verified
        reliably. Therefore, only a warning is issued by default. Toggling
        occurs only with explicit ``capslock_autocorrect=True``, with at
        most two attempts.
        """
        target = self._capslock_leds_target()
        state = _capslock_state()
        if state == "?":
            # No LED found - do not toggle blindly (desynchronization risk).
            log.debug("CapsLock state unknown (no LED) - no intervention")
            return
        if state == target:
            return

        if not self._capslock_autocorrect:
            log.warning(
                "CapsLock LED is %r, target %r - typed text may appear "
                "inverted. It will NOT be toggled "
                "(capslock_autocorrect = false). Remedy: press the physical "
                "CapsLock key or log out and back in.",
                state, target,
            )
            return

        log.warning(
            "CapsLock LED is %r, target %r -> toggling",
            state, target,
        )
        for attempt in (1, 2):
            self._send_raw("key capslock\n")
            time.sleep(0.08)
            if _capslock_state() == target:
                log.info("CapsLock set to target %r (attempt %d)", target, attempt)
                return
        log.error(
            "Could not set CapsLock to target %r - text may be inverted. "
            "Manual fix: stt-button reset-capslock",
            target,
        )

    def type_text(self, text: str) -> None:
        if not text:
            return
        # CapsLock must not invert the dictation.
        self._ensure_capslock_normal()
        # Set the key-press delay so Shift does not remain active ("So"
        # instead of "SO").
        pre = f"typedelay {self._typedelay_ms}\ntypehold {self._typehold_ms}\n"
        # Literal text via ``type``. Newlines become ``enter``.
        parts = [pre]
        for i, line in enumerate(text.split("\n")):
            if i:
                parts.append("key enter\n")
            if line:
                parts.append(f"type {line}\n")
        if not parts:
            return
        self._send("".join(parts))

    def backspace(self, n: int) -> None:
        if n <= 0:
            return
        lines = "\n".join("key backspace" for _ in range(n)) + "\n"
        self._send(lines)

    def backspace_and_key(self, n: int, name: str) -> None:
        """Backspaces and chord in ONE dotool process (OPT1).

        The ~0.7 s cost in the finalize path comes from starting the dotool
        process, not from the keys: n backspaces and the paste chord (e.g.,
        'ctrl+v') run in a single stdin stream. Ordering remains guaranteed:
        backspaces first, then the chord.
        """
        if n <= 0:
            self.key(name)
            return
        payload = (
            "".join("key backspace\n" for _ in range(n)) + f"key {name}\n"
        )
        self._send(payload)

    def backspace_and_type(
        self, n: int, text: str, trailing_key: str | None = None
    ) -> None:
        """Backspaces, text and an optional trailing key in ONE dotool process (M2a).

        Each dotool process costs ~0.7 s startup, so the final output (and
        live corrections) send everything in a single stdin payload. The
        trailing key (e.g. 'space') is a separate ``key`` line because dotool
        drops trailing spaces inside a ``type`` line. The timeout budget of
        :meth:`_send_raw` counts only ``type`` lines, as before.
        """
        parts = ["key backspace\n" for _ in range(max(0, n))]
        if text:
            self._ensure_capslock_normal()
            parts.append(
                f"typedelay {self._typedelay_ms}\ntypehold {self._typehold_ms}\n"
            )
            for i, line in enumerate(text.split("\n")):
                if i:
                    parts.append("key enter\n")
                if line:
                    parts.append(f"type {line}\n")
        if trailing_key:
            parts.append(f"key {trailing_key}\n")
        if parts:
            self._send("".join(parts))

    def key(self, name: str) -> None:
        if not name:
            return
        # dotool only knows individual names without +; ctrl+v -> separate keys
        keys = name.split("+")
        if len(keys) == 1:
            self._send(f"key {keys[0]}\n")
        else:
            # Modifier + key: dotool expects "mod+key" as a CHORD.
            chord = "+".join(keys)
            self._send(f"key {chord}\n")

    def _send_raw(self, payload: str) -> None:
        """Send a payload to dotool without CapsLock handling.

        Also used by :meth:`_ensure_capslock_normal`, so it deliberately
        does not repeat the CapsLock check (no recursion risk).

        Unlike :func:`_run`, ``capture_output`` remains active: dotool does
        not fork a daemon process, so pipes close cleanly. However, dotool
        can also hang, hence a payload-dependent timeout budget: each
        character to type costs typedelay+typehold milliseconds, plus a 5 s
        buffer for process startup and layout initialization.
        """
        import os

        env = os.environ.copy()
        env["DOTOOL_XKB_LAYOUT"] = self._xkb_layout
        env["DOTOOL_XKB_VARIANT"] = self._xkb_variant
        # Only 'type ' lines cost time: key/typedelay/typehold are fixed.
        typed_chars = sum(
            len(line) for line in payload.split("\n") if line.startswith("type ")
        )
        type_s = typed_chars * (self._typedelay_ms + self._typehold_ms) / 1000
        timeout_s = max(5.0, type_s + 5.0)
        try:
            proc = subprocess.run(
                [self.binary],
                input=payload,
                capture_output=True,
                text=True,
                env=env,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            raise TyperError(
                f"{self.binary}: timeout after {timeout_s:.1f}s "
                f"({typed_chars} characters)"
            ) from exc
        except FileNotFoundError as exc:
            raise TyperError(f"{self.binary} is not installed") from exc
        except OSError as exc:
            raise TyperError(f"{self.binary} could not start: {exc}") from exc
        if proc.returncode != 0:
            raise TyperError(
                f"{self.binary} failed (rc={proc.returncode}): "
                f"{(proc.stderr or '').strip()}"
            )

    def _send(self, payload: str) -> None:
        # Diagnostics: capture the lock state before typing. The visible text
        # is corrupted by an inverted CapsLock level ("Das" -> "dAS"); this
        # does NOT happen in ASR, but through the modifier/lock state of the
        # input pipeline.
        led_before = _capslock_state()
        leds_before = _capslock_leds()
        self._send_raw(payload)
        led_after = _capslock_state()

        target = self._capslock_leds_target()
        if led_before != target or led_after != target:
            log.warning(
                "CAPSLOCK diagnostics: LED before=%s after=%s - CapsLock is "
                "probably active (LED is not a reliable state); text may "
                "appear inverted. payload=%r",
                led_before, led_after, payload[:80],
            )
        else:
            log.debug("capslock ok (%s) payload=%r", target, payload[:60])

        # Per-device diagnostics: the LED <-> state mapping is unreliable;
        # multiple devices can disagree.
        if leds_before:
            log.debug(
                "CAPSLOCK LEDs per device: %s (LED != State, diagnostics only)",
                leds_before,
            )


class WtypeTyper:
    """Type directly into the focused window via ``wtype``."""

    binary = "wtype"

    def __init__(self, binary: str | None = None) -> None:
        if binary:
            self.binary = binary
        if shutil.which(self.binary) is None:
            raise TyperError(f"{self.binary} was not found (please install it)")

    def type_text(self, text: str) -> None:
        if not text:
            return
        cmd = [self.binary]
        for i, line in enumerate(text.split("\n")):
            if i:
                cmd += ["-k", "Return"]
            if line:
                cmd += ["-", line]
        _run(cmd)

    def backspace(self, n: int) -> None:
        if n <= 0:
            return
        while n > 0:
            batch = min(n, BACKSPACE_BATCH)
            cmd = [self.binary]
            for _ in range(batch):
                cmd += ["-k", "BackSpace"]
            _run(cmd)
            n -= batch

    def key(self, name: str) -> None:
        if not name:
            return
        cmd = [self.binary]
        for part in name.split("+"):
            cmd += ["-k", part]
        _run(cmd)


class ClipboardTyper:
    """Place text on the clipboard and insert it with Ctrl+V.

    Backspace is not supported: clipboard mode is intended as a fallback
    for the fallback where ``wtype`` has no effect. It is therefore suitable
    only for final output.
    """

    def __init__(self, copy_binary: str = "wl-copy", paste_binary: str = "wtype") -> None:
        self.copy_binary = copy_binary
        self.paste_binary = paste_binary
        if shutil.which(self.copy_binary) is None:
            raise TyperError(f"{self.copy_binary} was not found")

    def type_text(self, text: str) -> None:
        if not text:
            return
        _run([self.copy_binary, text])
        self.key("ctrl+v")

    def backspace(self, n: int) -> None:
        raise TyperError("Clipboard typing cannot remove text")

    def key(self, name: str) -> None:
        if not name:
            return
        cmd = [self.paste_binary]
        for part in name.split("+"):
            cmd += ["-k", part]
        _run(cmd)


class ClipboardPaster:
    """Insert the final delta via ``wl-copy`` + paste chord.

    The copy step follows the :class:`ClipboardTyper` pattern: the binary
    (e.g., ``wl-copy``) receives the text as an argument, and the chord is
    pressed through the normal typer (``typer.key``).

    Fallback rules:

    * If the copy binary is missing or copying fails, type the delta
      character by character (``type_text``); the old path remains usable.
    * If the paste chord fails AFTER a successful copy, do NOT repeat with
      ``type_text``: the delta may already have been inserted (risk of
      duplicate insertion). Log the error and leave the state unchanged.
    * The previous clipboard contents are NOT restored (race with clipboard
      managers, no ``wl-paste``).
    """

    def __init__(
        self,
        typer: Typer,
        *,
        copy_binary: str = "wl-copy",
        paste_key: str = "ctrl+v",
    ) -> None:
        self._typer = typer
        self.copy_binary = copy_binary
        self.paste_key = paste_key

    def copy(self, text: str) -> bool:
        """Copy ``text`` to the clipboard WITHOUT pressing the paste chord.

        Used when a terminal is focused: the chord is ineffective there
        (and ctrl+v = quoted-insert may swallow the next keystroke), so
        the user pastes manually after the done sound.

        Returns:
            True if the copy succeeded. False for empty text, or if the
            copy binary is missing/failed; in that case the text was typed
            character by character instead (same fallback as paste()).
        """
        if not text:
            return False
        if shutil.which(self.copy_binary) is None:
            log.warning(
                "%s unavailable - typing the delta character by character",
                self.copy_binary,
            )
            self._typer.type_text(text)
            return False
        try:
            log.debug("final paste: copy start (%d characters)", len(text))
            _run([self.copy_binary, text])
            log.debug("final paste: copy done")
        except TyperError as exc:
            log.warning(
                "Copy via %s failed (%s) - typing the delta character by character",
                self.copy_binary, exc,
            )
            self._typer.type_text(text)
            return False
        return True

    def paste(self, text: str) -> None:
        """Copy ``text`` to the clipboard and press the paste chord."""
        if not self.copy(text):
            return
        try:
            log.debug("final paste: chord %s", self.paste_key)
            self._typer.key(self.paste_key)
        except TyperError as exc:
            # Deliberately NO retry with type_text: the chord may have had a
            # partial effect, and the delta could already be inserted.
            log.error(
                "Paste chord %r failed (%s) - no retry "
                "(risk of duplicate insertion)",
                self.paste_key, exc,
            )

    def paste_combined(self, text: str, *, backspaces: int = 0) -> bool:
        """OPT1: Copy + (backspaces & chord) in one dotool process.

        Like :meth:`paste`, but the ``backspaces`` deletions and the paste
        chord run in ONE dotool call if the typer supports
        ``backspace_and_key`` (DotoolTyper). Saves the second process start
        (~0.7 s in the finalize path).

        Returns:
            True  = combined path completed.
            False = NOT attempted (empty text, typer without support,
            missing copy binary, or failed copy). The caller can safely fall
            back to the existing path (backspace + paste); nothing was typed
            or deleted.

        Raises:
            TyperError: Copy succeeded, but the chord failed. The state is
            unknown (backspaces/chord may have had a partial effect); the
            caller must NOT follow up with the old path (risk of duplicate
            insertion) and must leave the state unchanged.
        """
        if not text:
            return False
        combined = getattr(self._typer, "backspace_and_key", None)
        if combined is None:
            return False
        if shutil.which(self.copy_binary) is None:
            log.warning(
                "%s unavailable - combined path not possible",
                self.copy_binary,
            )
            return False
        try:
            log.debug("final paste combined: copy start (%d characters, %d bs)",
                      len(text), backspaces)
            _run([self.copy_binary, text])
        except TyperError as exc:
            log.warning(
                "Copy via %s failed (%s) - combined path canceled",
                self.copy_binary, exc,
            )
            return False
        try:
            log.debug("final paste combined: %d bs + chord %s",
                      backspaces, self.paste_key)
            combined(backspaces, self.paste_key)
            return True
        except TyperError as exc:
            log.error(
                "Combined chord %r failed (%s) - state unknown, "
                "no retry (risk of duplicate insertion)",
                self.paste_key, exc,
            )
            raise


class DeltaTyper:
    """Type only the difference between two transcriptions.

    ``committed_len`` is the length of the prefix that is already final and
    therefore never removed again.
    """

    def __init__(
        self,
        typer: Typer,
        *,
        append_space: bool = True,
        max_revision_chars: int = 60,
        final_paster: FinalPaster | None = None,
        latency_trace: bool = False,
        live_typing_mode: str = "full",
        focus_probe: Callable[[], bool | str | None] | None = None,
        focus_unknown_fallback: str = "copy",
        on_focus_unknown: Callable[[], None] | None = None,
        final_paste_min_chars: int = 0,
    ) -> None:
        self._typer = typer
        # M1: paste final deltas of at least this many characters when the
        # focus is CONFIRMED non-terminal (0 = disabled, always type).
        self._final_paste_min_chars = max(0, int(final_paste_min_chars))
        # Set by _final_mode(): True only for a confirmed non-terminal focus
        # (never for unknown/no_window, even with fallback 'type').
        self._focus_confirmed_other = False
        if focus_unknown_fallback not in ("copy", "type"):
            raise ValueError(
                "focus_unknown_fallback must be 'copy' or 'type', "
                f"is {focus_unknown_fallback!r}"
            )
        # F2: what to do when focus is unknown (None): "copy" (default,
        # terminal-safe) or "type". Never silent: the first None of an
        # outage logs a WARNING and calls on_focus_unknown (notify).
        self._focus_unknown_fallback = focus_unknown_fallback
        self._on_focus_unknown = on_focus_unknown
        self._focus_unknown_warned = False
        # Focus gating for the final delta (only with final_paster):
        # None = legacy (copy + chord). Otherwise probe() -> False (no
        # terminal) types the delta via the typer and leaves the clipboard
        # untouched; True/None (terminal/unknown) copies WITHOUT chord.
        self._focus_probe = focus_probe
        self._append_space = append_space
        self._max_revision_chars = max_revision_chars
        # Optional: paste the final delta via the clipboard instead of typing
        # it. Live updates always use the normal typer (dotool).
        self._final_paster = final_paster
        # Latency trace (config.latency_trace): logs the domino sizes
        # (rev/backspace/paste/space) at debug level in finalize().
        self._latency_trace = latency_trace
        # Live preview mode: "full" (default) also types unstable partials
        # (with backspace corrections); "committed" types only the stable
        # (committed) addition. The partial is tracked internally but never
        # typed or removed (STAGE 2).
        self._live_typing_mode = live_typing_mode
        self._last_text = ""
        self._last_committed = 0
        # STAGE 4: pause indicator already typed? Set only in show_paused()
        # and reset only in reset().
        self._pause_shown = False

    def _trace(self, point: str) -> None:
        """Debug measurement point for finalization (only with latency_trace)."""
        if not self._latency_trace:
            return
        log.debug("latency[%s]", point)

    def _final_mode(self) -> str:
        """Decide the final output path: 'legacy' | 'copy' | 'type'.

        Side effect: ``_focus_confirmed_other`` is True only when the probe
        positively reported a non-terminal focus (M1 paste eligibility).
        """
        self._focus_confirmed_other = False
        if self._final_paster is None:
            return "type"
        if self._focus_probe is None:
            return "legacy"
        try:
            state = self._focus_probe()
        except Exception as exc:  # noqa: BLE001 - finalize must never crash
            log.debug("focus probe failed (%s) - treating as terminal", exc)
            state = None
        # F2b: the probe may return focus.focus_state() strings. Only a
        # dead extension ("unreachable", or a legacy bool-probe None)
        # warns; "no_window" is a normal transient state (debug only).
        label = state if isinstance(state, str) else repr(state)
        if isinstance(state, str):
            reason = state
            state = {"terminal": True, "other": False}.get(state)
        else:
            reason = "unreachable" if state is None else "known"
        if reason == "unreachable":
            self._warn_focus_unknown()
        elif reason == "no_window":
            log.debug("focus: no window focused")
        else:
            self._focus_unknown_warned = False
        self._focus_confirmed_other = state is False
        if state is False or (
            state is None and self._focus_unknown_fallback == "type"
        ):
            mode = "type"
        elif callable(getattr(self._final_paster, "copy", None)):
            mode = "copy"
        else:
            mode = "legacy"
        self._trace(f"focus {label} -> {mode}")
        return mode

    def _warn_focus_unknown(self) -> None:
        """Warn once per outage (re-armed by the next known focus)."""
        if self._focus_unknown_warned:
            return
        self._focus_unknown_warned = True
        log.warning(
            "focus extension unreachable - run tools/focus_check.py; "
            "final text -> %s",
            "typed" if self._focus_unknown_fallback == "type" else "clipboard",
        )
        if self._on_focus_unknown is not None:
            try:
                self._on_focus_unknown()
            except Exception as exc:  # noqa: BLE001 - finalize must never crash
                log.debug("on_focus_unknown failed (%s)", exc)

    @property
    def last_text(self) -> str:
        """The most recently output text."""
        return self._last_text

    def reset(self) -> None:
        """Forget the previous state (e.g., after a new recording).

        Also reset the pause indicator flag: the next recording starts clean
        (the previous recording's indicator may remain physically at the
        cursor; abort() deliberately types nothing).
        """
        self._last_text = ""
        self._last_committed = 0
        self._pause_shown = False

    def show_paused(self) -> None:
        """Type the pause indicator EXACTLY ONCE ("still listening").

        Called by the controller when live ASR shuts off due to the
        ``live_asr_until_s`` limit. The indicator is tracked as ACTUAL typed
        text (``_last_text`` is extended) so that the existing finalize()
        difference automatically removes it again
        (to_remove == len(PAUSE_INDICATOR) == 3 backspaces): backspace
        deletes exactly that many code points in the target app.

        If the final text is empty (model failure), the indicator deliberately
        remains (an honest state). If recording is ABORTED after the indicator
        was typed, it likewise remains at the cursor; abort() fundamentally
        types nothing (no special case).

        Repeated calls are no-ops: the indicator is typed exactly once per
        recording (a single type_text call, no sub-letter timing).
        """
        if self._pause_shown:
            return
        self._typer.type_text(PAUSE_INDICATOR)
        self._last_text += PAUSE_INDICATOR
        self._pause_shown = True

    def update(self, text: str, committed_len: int = 0, *, force_revision: bool = False) -> None:
        """Output the difference between the previous and new text.

        Only the *non*-committed part may be removed. The committed part is
        never touched.

        ``force_revision`` disables the ``max_revision_chars`` safety limit.
        This is necessary for the *final* transcription because the cursor is
        known to be after our own text, and the final text should replace (not
        duplicate) the text already typed.
        """
        old = self._last_text
        committed_len = max(0, committed_len)

        # If the committed region would be lost, ignore the new version
        # entirely; the text already typed remains in place.
        if len(text) < committed_len:
            return

        # STAGE 2 (live_typing_mode='committed'): Only the stable addition is
        # typed. The internal state tracks only the text actually typed; the
        # unstable partial is never typed or removed (no live backspaces, no
        # flickering). finalize() therefore calculates against the effective
        # cursor state: to_remove==0, and the suffix arrives as one paste.
        if self._live_typing_mode == "committed":
            stable = text[:committed_len]
            if stable.startswith(old):
                to_add = stable[len(old):]
                if to_add:
                    self._typer.type_text(to_add)
                self._last_text = stable
                self._last_committed = len(stable)
            else:
                # The stable region contradicts the typed text (practically
                # impossible with LocalAgreement): do not change anything live;
                # finalize() applies the final correction.
                log.debug(
                    "committed live: prefix conflict - update discarded"
                )
            return

        committed = min(committed_len, len(old), len(text))

        # If the model differs in the committed region, retain the old
        # version there.
        if old[:committed] != text[:committed]:
            text = old[:committed] + text[committed:]

        old_tail = old[committed:]
        new_tail = text[committed:]

        if old_tail == new_tail:
            self._last_text = old[:committed] + new_tail
            self._last_committed = committed
            return

        common = _common_prefix_len(old_tail, new_tail)
        to_remove = len(old_tail) - common
        to_add = new_tail[common:]

        if to_remove and not force_revision and to_remove > self._max_revision_chars:
            # An overly long removal often means that the cursor is no longer
            # after our text. In that case, only append.
            to_add = new_tail
            to_remove = 0

        # M2b: backspace + type in ONE dotool process when supported.
        merged = getattr(self._typer, "backspace_and_type", None)
        if to_add and merged is not None:
            merged(to_remove, to_add)
        else:
            if to_remove:
                self._typer.backspace(to_remove)
            if to_add:
                self._typer.type_text(to_add)

        self._last_text = old[:committed] + new_tail
        self._last_committed = committed

    def finalize(self, final_text: str, committed_len: int = 0) -> None:
        """Output the final text and finish with a space.

        With a configured ``final_paster``, the calculated delta (``to_add``)
        is inserted exactly once via the clipboard; the committed prefix and
        text already typed are never included. Without ``final_paster``, the
        existing path applies: backspace and delta via the normal typer.
        With a ``focus_probe``, the paster path is focus-gated: terminal or
        unknown focus copies to the clipboard without a chord; other focus
        types the delta via the typer and leaves the clipboard untouched,
        EXCEPT (M1) when ``final_paste_min_chars`` > 0, the focus is
        CONFIRMED non-terminal and the delta has at least that many
        characters: then the delta (plus trailing space) is pasted once via
        ``final_paster.paste()`` (clipboard overwritten, no retry). Terminal
        and unknown focus never paste.

        Empty final output must not delete text already typed (e.g., if the
        model failed during the final transcription).

        Bracketed paste markers (operator, 31.10.): manual pasting with
        bracketed paste enabled inserts '^[[200~' ... '~' into the text.
        Removed AT THE INPUT BOUNDARY so delta/backspace/paste see clean
        values (no data loss: only verified markers).
        """
        final_text = strip_paste_markers(final_text)
        if not final_text.strip():
            return

        old = self._last_text
        committed_len = max(0, committed_len)
        # Committed protection: the higher state wins so that a smaller
        # committed_len at completion does not reopen anything already stable.
        effective_committed = max(committed_len, self._last_committed)

        # Space micro-fix: with a final_paster and a non-empty delta, append
        # the trailing space directly to the paste; a separate key("space")
        # is omitted (no second space, one less key press). With an empty
        # to_add there is no paste, so the separate space key remains. Without
        # a paster and with append_space=False, nothing changes.
        space_in_paste = False
        mode = self._final_mode()
        log.debug("final output mode: %s", mode)

        if len(final_text) >= effective_committed:
            committed = min(effective_committed, len(old), len(final_text))

            # If the model differs in the committed region, retain the old
            # version there.
            if old[:committed] != final_text[:committed]:
                final_text = old[:committed] + final_text[committed:]

            old_tail = old[committed:]
            new_tail = final_text[committed:]

            common = _common_prefix_len(old_tail, new_tail)
            to_remove = len(old_tail) - common
            to_add = new_tail[common:]

            # Backspace always uses the normal typer (dotool), even in
            # final_paste mode. The final correction is fully corrective:
            # no max_revision_chars safety limit (force_revision in the old
            # path).
            self._trace(f"rev {to_remove}/{len(to_add)}")
            # OPT1: Backspace + paste chord in ONE dotool process when a
            # paste_combined paster is available and deletions are needed.
            # combined_pasted=True prohibits the old backspace()/paste()
            # follow-up.
            combined_pasted = False
            if to_remove and to_add and mode == "legacy":
                paste_combined = getattr(
                    self._final_paster, "paste_combined", None
                )
                if paste_combined is not None:
                    combined_text = (
                        to_add + " " if self._append_space else to_add
                    )
                    t0 = time.monotonic()
                    try:
                        combined_pasted = bool(
                            paste_combined(combined_text, backspaces=to_remove)
                        )
                    except TyperError:
                        # State unknown (backspaces may have taken effect):
                        # do not follow up; leave the text state unchanged.
                        combined_pasted = True
                    if combined_pasted:
                        space_in_paste = self._append_space
                        to_remove = 0
                        to_add = ""
                        self._trace(
                            f"combined backspace+paste "
                            f"{time.monotonic() - t0:.3f}s"
                        )
            # M1: long delta on confirmed non-terminal focus -> paste once.
            if (
                to_add
                and mode == "type"
                and self._focus_confirmed_other
                and self._final_paste_min_chars > 0
                and len(to_add) >= self._final_paste_min_chars
                and self._final_paster is not None
            ):
                if to_remove:
                    t0 = time.monotonic()
                    self._typer.backspace(to_remove)
                    self._trace(f"backspace {time.monotonic() - t0:.3f}s")
                    to_remove = 0
                t0 = time.monotonic()
                space_in_paste = self._append_space
                self._final_paster.paste(
                    to_add + " " if space_in_paste else to_add
                )
                self._trace("paste (min_chars)")
                self._trace(f"output {time.monotonic() - t0:.3f}s")
                to_add = ""
            # M2a: typer path -> backspaces, delta and trailing space in ONE
            # dotool process (saves ~0.7 s per avoided process start).
            merged = getattr(self._typer, "backspace_and_type", None)
            if to_add and mode == "type" and merged is not None:
                t0 = time.monotonic()
                merged(
                    to_remove,
                    to_add,
                    trailing_key="space" if self._append_space else None,
                )
                space_in_paste = self._append_space
                self._trace("type")
                self._trace(f"output {time.monotonic() - t0:.3f}s")
                if space_in_paste:
                    self._trace("space 0.000s (in type)")
                to_remove = 0
                to_add = ""
            if to_remove:
                t0 = time.monotonic()
                self._typer.backspace(to_remove)
                self._trace(f"backspace {time.monotonic() - t0:.3f}s")
            if to_add:
                t0 = time.monotonic()
                if mode == "legacy":
                    # Appended space instead of a separate space key
                    # (no duplicate space; see above).
                    space_in_paste = self._append_space
                    self._final_paster.paste(
                        to_add + " " if space_in_paste else to_add
                    )
                    self._trace("paste")
                    if space_in_paste:
                        self._trace("space 0.000s (in paste)")
                elif mode == "copy":
                    # Terminal focus: clipboard only, NO chord. The space
                    # rides along in the clipboard (manual paste). On copy
                    # failure the fallback already typed text + space.
                    copied = self._final_paster.copy(
                        to_add + " " if self._append_space else to_add
                    )
                    space_in_paste = self._append_space
                    self._trace(f"copy (no chord) ok={copied}")
                else:
                    # Browser focus or no paster: typer path, clipboard
                    # untouched.
                    self._typer.type_text(to_add)
                    self._trace("type")
                self._trace(f"output {time.monotonic() - t0:.3f}s")

            self._last_text = old[:committed] + new_tail
            self._last_committed = committed

        if self._append_space and not space_in_paste:
            t0 = time.monotonic()
            self._typer.key("space")
            self._trace(f"space {time.monotonic() - t0:.3f}s")
        self._last_text = final_text


# Bracketed paste markers in the three relevant representations:
# raw with an actual ESC character (0x1b), ^ notation (terminal view), and
# hexadecimal text form "\x1b[200~" (as dumps may show).
_RAW_200 = "\x1b[200~"
_RAW_201 = "\x1b[201~"
_LIT_200 = "\\x1b[200~"
_LIT_201 = "\\x1b[201~"
_MARKER_200 = ("^[[200~", _RAW_200, _LIT_200)
_MARKER_201 = ("^[[201~", _RAW_201, _LIT_201)


def strip_paste_markers(text: str) -> str:
    """Remove bracketed paste markers from ``text`` (operator, 31.10.).

    Pasting with bracketed paste enabled (in a shell, e.g., after previous
    Ctrl+V in quoted-insert) literally inserts ESC[200~/ESC[201~ into the
    text: visible as '^[[200~' at the beginning and (after ESC is consumed)
    a leftover '~' at the end.

    Rules (deliberately conservative):
      * only the MARKERS (^ notation AND raw) are removed;
      * a bare '~' is removed at the end ONLY if a start marker was found
        beforehand (otherwise every legitimate '~', e.g., in '~/Documents',
        remains intact);
      * everything else remains unchanged.

    Applied in finalize() AT THE INPUT BOUNDARY so delta calculation,
    backspace count, and paste see clean values.
    """
    had_start = any(m in text for m in _MARKER_200)
    for m in _MARKER_200 + _MARKER_201:
        text = text.replace(m, "")
    if had_start and text.endswith("~"):
        text = text[:-1]
    return text


def _common_prefix_len(a: str, b: str) -> int:
    """Length of the common prefix of ``a`` and ``b``."""
    limit = min(len(a), len(b))
    i = 0
    while i < limit and a[i] == b[i]:
        i += 1
    return i
