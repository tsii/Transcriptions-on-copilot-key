"""Ausgabe des Transkripts an die Cursor-Position.

Der :class:`DeltaTyper` vergleicht aufeinanderfolgende Transkriptionen und
tippt nur die Differenz. Wird ein bereits getippter Teil korrigiert, wird er
per Backspace zurueckgenommen und neu getippt.

Genutzt wird ``wtype`` (Wayland, kein X-Toolkit noetig). Als Fallback gibt es
den Clipboard-Modus, der den Text ueber ``wl-copy`` und Ctrl+V einfuegt --
der funktioniert auch in Fenstern, in denen ``wtype`` nicht wirkt.
"""
from __future__ import annotations

import glob
import logging
import shutil
import subprocess
from typing import Protocol, runtime_checkable

log = logging.getLogger(__name__)

#: Maximale Anzahl Backspaces pro wtype-Aufruf (haelt die Kommandozeile kurz).
BACKSPACE_BATCH = 20


def _capslock_state() -> str:
    """Liest den CapsLock-LED-Zustand (Diagnose).

    Hintergrund: Ein invertierter CapsLock-Zustand in der Input-Pipeline
    verfaelscht den getippten Text zeichengenau ("Das" -> "dAS"). Das ist
    NICHT der ASR-Text. Deshalb loggen wir den Lock-Zustand vor/nach jedem
    Tippvorgang, um die Quelle eindeutig zu identifizieren.

    Returns:
        "an", "aus" oder "?" (kein LED gefunden).
    """
    try:
        leds = glob.glob("/sys/class/leds/*capslock*/brightness")
        if not leds:
            return "?"
        # Bei mehreren Devices: "an", wenn irgendeine LED leuchtet.
        states = []
        for path in leds:
            try:
                with open(path) as fh:
                    states.append(fh.read().strip())
            except OSError:
                pass
        if not states:
            return "?"
        return "an" if any(s == "1" for s in states) else "aus"
    except Exception:  # noqa: BLE001 - Diagnose darf nie den Betrieb stoeren
        return "?"


class TyperError(RuntimeError):
    """Die Ausgabe in das fokussierte Fenster ist fehlgeschlagen."""


@runtime_checkable
class Typer(Protocol):
    """Schnittstelle aller Ausgabe-Adapter."""

    def type_text(self, text: str) -> None:
        """Tippt den Text literal in das fokussierte Fenster."""

    def backspace(self, n: int) -> None:
        """Loescht ``n`` Zeichen vor dem Cursor."""

    def key(self, name: str) -> None:
        """Drueckt eine Taste, z.B. ``"space"`` oder ``"ctrl+v"``."""


def _run(cmd: list[str]) -> None:
    """Fuehrt einen Befehl aus und wandelt Fehler in :class:`TyperError` um."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise TyperError(f"{cmd[0]} ist nicht installiert") from exc
    except OSError as exc:
        raise TyperError(f"{cmd[0]} konnte nicht gestartet werden: {exc}") from exc
    if proc.returncode != 0:
        raise TyperError(
            f"{' '.join(cmd[:3])} fehlgeschlagen (rc={proc.returncode}): "
            f"{(proc.stderr or '').strip()}"
        )


class DotoolTyper:
    """Tippt ueber ``dotool`` (uinput, funktioniert auch unter GNOME-Wayland).

    ``dotool`` liest Befehle von stdin: ``type TEXT`` fuer Text, ``key NAME``
    fuer einzelne Tasten. Es braucht die udev-Regel, die User aus Gruppe
    ``input`` Zugriff auf /dev/uinput gewaehrt.
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
    ) -> None:
        if binary:
            self.binary = binary
        if shutil.which(self.binary) is None:
            raise TyperError(f"{self.binary} wurde nicht gefunden")
        # dotool tippt Zeichen ueber das XKB-Layout. Ohne deutsches Layout
        # entstehen Fehler wie z<->y vertauscht oder ? vs _ (US-Layout).
        self._xkb_layout = xkb_layout
        self._xkb_variant = xkb_variant
        # Antipp-Verzoegerung erhoehen: bei den dotool-Defaults (2ms) haengt
        # Shift beim Folgebuchstaben nach -> "So" wird zu "SO".
        self._typedelay_ms = typedelay_ms
        self._typehold_ms = typehold_ms

    def type_text(self, text: str) -> None:
        if not text:
            return
        # Antipp-Verzoegerung setzen, damit Shift nicht nachhaengt ("So"
        # statt "SO").
        pre = f"typedelay {self._typedelay_ms}\ntypehold {self._typehold_ms}\n"
        # Text literal via ``type``. Newlines werden zu ``enter``.
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

    def key(self, name: str) -> None:
        if not name:
            return
        # dotool kennt nur Einzelnamen ohne +; ctrl+v -> separate keys
        keys = name.split("+")
        if len(keys) == 1:
            self._send(f"key {keys[0]}\n")
        else:
            # Modifier + Taste: dotool erwartet "mod+taste" als CHORD.
            chord = "+".join(keys)
            self._send(f"key {chord}\n")

    def _send(self, payload: str) -> None:
        import os

        env = os.environ.copy()
        env["DOTOOL_XKB_LAYOUT"] = self._xkb_layout
        env["DOTOOL_XKB_VARIANT"] = self._xkb_variant

        # Diagnose: Lock-Zustand vor dem Tippen erfassen. Der sichtbare Text
        # wird naemlich von einer invertierten CapsLock-Ebene verfaelscht
        # ("Das" -> "dAS") - das passiert NICHT im ASR, sondern durch den
        # Modifier-/Lock-Zustand der Input-Pipeline.
        led_before = _capslock_state()
        try:
            proc = subprocess.run(
                [self.binary], input=payload, capture_output=True, text=True, env=env
            )
        except FileNotFoundError as exc:
            raise TyperError(f"{self.binary} ist nicht installiert") from exc
        except OSError as exc:
            raise TyperError(f"{self.binary} konnte nicht starten: {exc}") from exc
        led_after = _capslock_state()

        if led_before or led_after or led_before != led_after:
            log.warning(
                "CAPSLOCK-Diagnose: vor=%s nach=%s payload=%r",
                led_before, led_after, payload[:80],
            )
        else:
            log.debug("capslock ok (aus) payload=%r", payload[:60])

        if proc.returncode != 0:
            raise TyperError(
                f"{self.binary} fehlgeschlagen (rc={proc.returncode}): "
                f"{(proc.stderr or '').strip()}"
            )


class WtypeTyper:
    """Tippt ueber ``wtype`` direkt in das fokussierte Fenster."""

    binary = "wtype"

    def __init__(self, binary: str | None = None) -> None:
        if binary:
            self.binary = binary
        if shutil.which(self.binary) is None:
            raise TyperError(f"{self.binary} wurde nicht gefunden (bitte installieren)")

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
    """Legt den Text in die Zwischenablage und fuegt ihn per Ctrl+V ein.

    Backspace wird nicht unterstuetzt: der Clipboard-Modus ist als Fallback
    fuer den Fallback gedacht, in dem ``wtype`` nicht wirkt. Er eignet sich
    deshalb nur fuer die finale Ausgabe.
    """

    def __init__(self, copy_binary: str = "wl-copy", paste_binary: str = "wtype") -> None:
        self.copy_binary = copy_binary
        self.paste_binary = paste_binary
        if shutil.which(self.copy_binary) is None:
            raise TyperError(f"{self.copy_binary} wurde nicht gefunden")

    def type_text(self, text: str) -> None:
        if not text:
            return
        _run([self.copy_binary, text])
        self.key("ctrl+v")

    def backspace(self, n: int) -> None:
        raise TyperError("Clipboard-Typing kann keinen Text zuruecknehmen")

    def key(self, name: str) -> None:
        if not name:
            return
        cmd = [self.paste_binary]
        for part in name.split("+"):
            cmd += ["-k", part]
        _run(cmd)


class DeltaTyper:
    """Tippt nur die Differenz zwischen zwei Transkriptionen.

    ``committed_len`` bezeichnet die Laenge des Praefixes, der bereits
    endgueltig ist und deshalb nie mehr zurueckgenommen wird.
    """

    def __init__(
        self,
        typer: Typer,
        *,
        append_space: bool = True,
        max_revision_chars: int = 60,
    ) -> None:
        self._typer = typer
        self._append_space = append_space
        self._max_revision_chars = max_revision_chars
        self._last_text = ""
        self._last_committed = 0

    @property
    def last_text(self) -> str:
        """Der zuletzt ausgegebene Text."""
        return self._last_text

    def reset(self) -> None:
        """Vergisst den bisherigen Zustand (z.B. nach einer neuen Aufnahme)."""
        self._last_text = ""
        self._last_committed = 0

    def update(self, text: str, committed_len: int = 0, *, force_revision: bool = False) -> None:
        """Gibt die Differenz zwischen dem letzten und dem neuen Text aus.

        Nur der *nicht* committete Teil darf zurueckgenommen werden. Fuer den
        committeten Teil gilt: er wird nie angefasst.

        ``force_revision`` deaktiviert die ``max_revision_chars``-Schutzgrenze:
        fuer die *Schluss*-Transkription ist das noetig, weil dort der Cursor
        sicher hinter dem eigenen Text steht und der Endtext den bereits
        getippten Text ersetzen soll (nicht duplizieren).
        """
        old = self._last_text
        committed_len = max(0, committed_len)

        # Wuerde der committete Bereich verloren gehen, ignorieren wir die
        # neue Fassung komplett - der bereits getippte Text bleibt stehen.
        if len(text) < committed_len:
            return

        committed = min(committed_len, len(old), len(text))

        # Weicht das Modell im committeten Bereich ab, uebernehmen wir dort
        # die alte Fassung.
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
            # Zu lange Ruecknahme: haeufig bedeutet das, dass der Cursor
            # nicht mehr hinter unserem Text steht. Dann nur anhaengen.
            to_add = new_tail
            to_remove = 0

        if to_remove:
            self._typer.backspace(to_remove)
        if to_add:
            self._typer.type_text(to_add)

        self._last_text = old[:committed] + new_tail
        self._last_committed = committed

    def finalize(self, final_text: str, committed_len: int = 0) -> None:
        """Gibt den endgueltigen Text aus und schliesst mit einem Leerzeichen ab."""
        # Eine leere Endausgabe darf den bereits getippten Text nicht loeschen
        # (z.B. wenn das Modell bei der Schluss-Transkription gescheitert ist).
        if not final_text.strip():
            return
        self.update(final_text, committed_len, force_revision=True)
        if self._append_space:
            self._typer.key("space")
        self._last_text = final_text


def _common_prefix_len(a: str, b: str) -> int:
    """Laenge des gemeinsamen Praefixes von ``a`` und ``b``."""
    limit = min(len(a), len(b))
    i = 0
    while i < limit and a[i] == b[i]:
        i += 1
    return i
