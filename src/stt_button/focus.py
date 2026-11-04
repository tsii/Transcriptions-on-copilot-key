"""Focused-window query via the GNOME Shell extension (Kimi, 31.10.).

The extension stt-button-focus@salmon.io exports the currently focused
window (app-id + title) via the DBus method GetFocus() on the session
bus. This module queries it synchronously with a tight time budget
(done-sound path: the finalizer must never wait on window queries).

If the extension is missing or not answering, ``terminal_focused()``
returns None (undecided) -- the caller decides, e.g. play the sound
anyway (operator mode) or not.
"""
from __future__ import annotations

import json
import logging
import subprocess
from typing import Literal

log = logging.getLogger(__name__)

#: DBus endpoint of the GNOME Shell extension (see extension.js).
DBUS_NAME = "com.salmon.sttbutton"
OBJECT_PATH = "/com/salmon/sttbutton"
IFACE = "com.salmon.sttbutton.Focus"

#: App-id subsets that count as a "terminal" (org.gnome.Terminal,
#: kitty, alacritty, foot, konsole, xterm, wezterm, terminator, ...).
_TERMINAL_IDS = (
    "org.gnome.Terminal",
    "kitty",
    "org.kitty",
    "org.alacritty.Alacritty",
    "org.alacritty",
    "org.materia",
    "org.wezfurlong.wezterm",
    "org.xfce.Terminal",
    "konsole",
    "org.kde.konsole",
    "xterm",
    "gnome-terminal",
)

#: Maximum busctl budget: the focus query is convenience, not critical.
_TIMEOUT_S = 1.0


def _call_focus() -> tuple[str, str] | None:
    """Query GetFocus() via busctl; None on any error."""
    try:
        proc = subprocess.run(
            [
                "busctl", "--user", "--json=pretty", "call",
                DBUS_NAME, OBJECT_PATH, IFACE, "GetFocus",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("focus query failed (%s)", exc)
        return None
    if proc.returncode != 0:
        return None
    try:
        doc = json.loads(proc.stdout or "{}")
        # Real busctl call output: {"type":"ss","data":[app_id,title]}.
        data = doc.get("data")
        if not isinstance(data, list) or len(data) < 2:
            return None
        app_id, title = str(data[0]), str(data[1])
        return app_id, title
    except (ValueError, TypeError) as exc:
        log.debug("focus reply unreadable (%s)", exc)
        return None


FocusState = Literal["terminal", "other", "no_window", "unreachable"]


def focus_state() -> FocusState:
    """Classify the current focus.

    Returns:
        "terminal"    = a known terminal is focused.
        "other"       = some other window is focused.
        "no_window"   = extension answered, but no window is focused.
        "unreachable" = extension dead/missing, call failed or timed out.
    """
    focus = _call_focus()
    if focus is None:
        return "unreachable"
    app_id, _title = focus
    if app_id in _TERMINAL_IDS:
        return "terminal"
    if app_id == "none":
        return "no_window"
    return "other"


def terminal_focused() -> bool | None:
    """Is a TERMINAL window focused? (stable wrapper over focus_state())

    Returns:
        True  = focus is a known terminal (app-id match).
        False = focus is definitely NOT a terminal (other window).
        None  = undecided (extension missing, call failed, no window
        focused) -- caller decides.
    """
    return {"terminal": True, "other": False}.get(focus_state())
