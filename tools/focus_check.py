#!/usr/bin/env python3
"""One-shot diagnostic for the stt-button focus extension (F3).

Checks, in order:
  1. GNOME extension state (``gnome-extensions info``).
  2. DBus name owner of com.salmon.sttbutton (``busctl --user status``).
  3. GetFocus() round trip via ``stt_button.focus`` (focus_state()).
  4. On ERROR: extension errors from org.gnome.Shell.Extensions.

Exit codes: 0 = OK, 1 = degraded (name/call missing), 2 = extension ERROR.
Read-only: never toggles, reloads or repairs anything.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from stt_button import focus  # noqa: E402

UUID = "stt-button-focus@salmon.io"


def _run(cmd: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, timeout=3)
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, f"{cmd[0]}: {exc}"
    return p.returncode, (p.stdout or "")


def extension_state() -> str:
    rc, out = _run(["gnome-extensions", "info", UUID])
    if rc != 0:
        return "UNKNOWN"
    for line in out.splitlines():
        key, _, val = line.strip().partition(":")
        if key.strip().lower() == "state":
            return val.strip().upper()
    return "UNKNOWN"


def name_owned() -> bool:
    rc, _ = _run(["busctl", "--user", "status", focus.DBUS_NAME])
    return rc == 0


def extension_errors() -> str:
    _rc, out = _run([
        "gdbus", "call", "--session", "--dest", "org.gnome.Shell.Extensions",
        "--object-path", "/org/gnome/Shell/Extensions",
        "--method", "org.gnome.Shell.Extensions.GetExtensionErrors", UUID,
    ])
    return out.strip()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--quiet", action="store_true",
                    help="print only on failure")
    args = ap.parse_args(argv)

    lines: list[str] = []
    state = extension_state()
    lines.append(f"extension state: {state}")
    owned = name_owned()
    lines.append(f"DBus name {focus.DBUS_NAME}: "
                 f"{'owned' if owned else 'not on the bus'}")
    fstate = focus.focus_state()
    lines.append(f"focus_state(): {fstate} "
                 f"(terminal_focused={ {'terminal': True, 'other': False}.get(fstate)})")

    if state == "ERROR":
        errors = extension_errors()
        lines.append(f"extension errors: {errors or '(none reported)'}")
        if "already exported" in errors:
            lines.append("DIAGNOSIS: ERROR + 'already exported' -> Zombie "
                         "registration on the shell bus; only Logout/Login helps.")
        else:
            lines.append("DIAGNOSIS: extension in ERROR - check journalctl "
                         "--user -b | grep stt-button-focus; try "
                         "disable/enable, else Logout/Login.")
        rc = 2
    elif not owned or fstate == "unreachable":
        lines.append("DIAGNOSIS: extension not answering (state "
                     f"{state}) - final text goes to clipboard only.")
        rc = 1
    else:
        lines.append("DIAGNOSIS: OK")
        rc = 0

    if rc != 0 or not args.quiet:
        print("\n".join(lines))
    return rc


if __name__ == "__main__":
    sys.exit(main())
