"""Fokus-Fenster-Abfrage ueber die GNOME-Shell-Extension (Kimi 31.10.).

Die Extension stt-button-focus@salmon.io exportiert das aktuell
fokussierte Fenster (app-id + Titel) per DBus-Method GetFocus() auf
dem Session-Bus. Diese Modul fragt es synchron mit engem Zeitbudget ab
(Fertig-Ton-Pfad: der Finalizer darf nie auf Fensterfragen warten).

Bei fehlgeschlagener/nicht installierter Extension liefert
``terminal_focused()`` None (unentschieden) — der Caller entscheidet,
z.B. Ton trotzdem spielen (Betreiber-Modus) oder nicht.
"""
from __future__ import annotations

import logging
import subprocess

log = logging.getLogger(__name__)

#: DBus-Zielpunkt der GNOME-Shell-Extension (siehe extension.js).
DBUS_NAME = "com.salmon.sttbutton"
OBJECT_PATH = "/com/salmon/sttbutton"
IFACE = "com.salmon.sttbutton.Focus"

#: app-id-Untermengen, die als "Terminal" gelten (Org.gnome.Terminal,
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

#: Maximaler busctl-Budget: Fokusfrage ist Komfort, kein Muss.
_TIMEOUT_S = 1.0


def _call_focus() -> tuple[str, str] | None:
    """Ruft GetFocus() per busctl ab; None bei jedem Fehler."""
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
        log.debug("Fokus-Abfrage fehlgeschlagen (%s)", exc)
        return None
    if proc.returncode != 0:
        return None
    try:
        import json

        doc = json.loads(proc.stdout or "{}")
        ret = (doc.get("result") or [None, None, None])[2]
        if not ret or len(ret) < 2:
            return None
        app_id, title = str(ret[0]), str(ret[1])
        return app_id, title
    except (ValueError, TypeError, IndexError) as exc:
        log.debug("Fokus-Antwort unlesbar (%s)", exc)
        return None


def terminal_focused() -> bool | None:
    """Ist ein TERMINAL-FENSTER fokussiert?

    Returns:
        True  = Fokus ist ein bekanntes Terminal (app-id-Zuordnung).
        False = Fokus ist eindeutig KEIN Terminal (anderes Fenster).
        None  = unentschieden (Extension fehlt, Aufruf fehlgeschlagen,
        kein Fenster fokussiert) — Caller entscheidet.
    """
    focus = _call_focus()
    if focus is None:
        return None
    app_id, _title = focus
    if app_id in _TERMINAL_IDS:
        return True
    if app_id == "none":
        return None
    return False
