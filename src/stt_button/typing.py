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
import time
from typing import Protocol, runtime_checkable

log = logging.getLogger(__name__)

#: Maximale Anzahl Backspaces pro wtype-Aufruf (haelt die Kommandozeile kurz).
BACKSPACE_BATCH = 20

#: Standard-Timeout fuer _run()-Befehle in Sekunden. Schutz gegen
#: Daemon-Pipe-Hangs: ``wl-copy`` forked einen residenten Daemon-Prozess,
#: der die (bei capture_output geoeffneten) Pipe-FDs erbt. subprocess.run
#: wartet dann in communicate() auf ein nie kommendes EOF. DEVNULL-FDs +
#: Timeout schliessen beide Angriffspfade.
COMMAND_TIMEOUT_S = 2.0


def _capslock_leds() -> dict[str, str]:
    """Liest den CapsLock-LED-Zustand pro Device (Diagnose).

    Hintergrund: Ein invertierter CapsLock-Zustand in der Input-Pipeline
    verfaelscht den getippten Text zeichengenau ("Das" -> "dAS"). Das ist
    NICHT der ASR-Text. Deshalb loggen wir den Lock-Zustand vor/nach jedem
    Tippvorgang, um die Quelle eindeutig zu identifizieren.

    Returns:
        Mapping Device-Name -> "an" (=1), "aus" (=0) oder "?" (nicht lesbar).
        Der Device-Name ist der Basename des LED-Dirnames, z.B.
        "input3::capslock".
    """
    result: dict[str, str] = {}
    try:
        leds = glob.glob("/sys/class/leds/*capslock*/brightness")
    except Exception:  # noqa: BLE001 - Diagnose darf nie den Betrieb stoeren
        return result
    for path in leds:
        import os
        # Basename des LED-Dirnames, z.B. "/sys/class/leds/input3::capslock"
        # -> "input3::capslock".
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
    """Liest den aggregierten CapsLock-LED-Zustand (Diagnose).

    Baut auf :func:`_capslock_leds` auf: "an", wenn irgendein Device "an"
    meldet, sonst "aus"; bei keiner LED "?".

    Returns:
        "an", "aus" oder "?" (kein LED gefunden).
    """
    try:
        leds = _capslock_leds()
    except Exception:  # noqa: BLE001 - Diagnose darf nie den Betrieb stoeren
        return "?"
    if not leds:
        return "?"
    return "an" if any(v == "an" for v in leds.values()) else "aus"


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


@runtime_checkable
class FinalPaster(Protocol):
    """Schnittstelle fuer das Einfuegen des finalen Deltas per Clipboard.

    Wird beim Abschluss (finalize) statt des zeichenweisen Tippens genutzt,
    um lange Diktate ohne ~35 ms pro Zeichen auszugeben. Bekommt nur das
    berechnete Delta - niemals den Volltext.
    """

    def paste(self, text: str) -> None:
        """Fuegt ``text`` an der Cursor-Position ein."""


def _run(cmd: list[str], *, timeout_s: float = COMMAND_TIMEOUT_S) -> None:
    """Fuehrt einen Befehl aus und wandelt Fehler in :class:`TyperError` um.

    Haertung gegen Daemon-Pipe-Hangs (final_paste-Freeze): alle drei
    Standard-FDs zeigen auf DEVNULL statt auf Pipes. ``wl-copy`` forked
    naemlich einen residenten Daemon und vererbt offene Pipe-FDs an
    Enkel-Prozesse - communicate() wuerde sonst auf ein nie kommendes EOF
    warten. Das Timeout ist die zweite Verteidigungslinie.
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
        raise TyperError(f"{cmd[0]}: Timeout nach {timeout_s:.1f}s") from exc
    except FileNotFoundError as exc:
        raise TyperError(f"{cmd[0]} ist nicht installiert") from exc
    except OSError as exc:
        raise TyperError(f"{cmd[0]} konnte nicht gestartet werden: {exc}") from exc
    if proc.returncode != 0:
        raise TyperError(f"{cmd[0]} fehlgeschlagen (rc={proc.returncode})")


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
        capslock_autocorrect: bool = False,
        capslock_led_inverted: bool = True,
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
        # Standard: NICHT eingreifen. Der CapsLock-Lock-State ist nicht
        # zuverlaessig lesbar (LED != State, xset != State) - die App darf
        # ihn deshalb nur erkennen und warnen.
        self._capslock_autocorrect = capslock_autocorrect
        # LED-Semantik dieses Systems (empirisch belegt): True = LED aus
        # bedeutet CapsLock aktiv (= invertiert). False = konventionelle
        # Zuordnung (LED an = CapsLock aktiv).
        self._capslock_led_inverted = capslock_led_inverted

    def _capslock_leds_target(self) -> str:
        """Ziel-LED-Zustand fuer normale Schreibung.

        Bei invertierter Semantik ist LED "an" normal, sonst "aus".
        """
        return "an" if self._capslock_led_inverted else "aus"

    def _ensure_capslock_normal(self) -> None:
        """Warnt, wenn CapsLock den Text invertiert - toggelt im Default nie.

        Ground Truth (empirisch belegt): LED **aus** = CapsLock aktiv =
        invertierter Text; LED **an** = normale Schreibung. Das Ziel ist
        ueber ``capslock_led_inverted`` konfigurierbar.

        ``key capslock`` ist ein TOGGLE und der Lock-State nicht verlaesslich
        verifizierbar. Deshalb wird standardmaessig nur gewarnt. Nur bei
        explizitem ``capslock_autocorrect=True`` wird umgeschaltet, mit
        hoechstens zwei Versuchen.
        """
        target = self._capslock_leds_target()
        state = _capslock_state()
        if state == "?":
            # Keine LED gefunden - nicht blind toggeln (Desync-Gefahr).
            log.debug("CapsLock-Zustand unbekannt (keine LED) - kein Eingriff")
            return
        if state == target:
            return

        if not self._capslock_autocorrect:
            log.warning(
                "CapsLock-LED steht auf %r, Ziel %r - getippter Text kann "
                "invertiert erscheinen. Es wird NICHT umgeschaltet "
                "(capslock_autocorrect = false). Abhilfe: physische "
                "CapsLock-Taste druecken oder ab-/anmelden.",
                state, target,
            )
            return

        log.warning(
            "CapsLock-LED steht auf %r, Ziel %r -> wird umgeschaltet",
            state, target,
        )
        for attempt in (1, 2):
            self._send_raw("key capslock\n")
            time.sleep(0.08)
            if _capslock_state() == target:
                log.info("CapsLock auf Ziel %r gesetzt (Versuch %d)", target, attempt)
                return
        log.error(
            "CapsLock liess sich nicht auf Ziel %r setzen - Text kann invertiert "
            "sein. Manuell: stt-button reset-capslock",
            target,
        )

    def type_text(self, text: str) -> None:
        if not text:
            return
        # CapsLock darf das Diktat nicht invertieren.
        self._ensure_capslock_normal()
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

    def _send_raw(self, payload: str) -> None:
        """Schickt einen Payload an dotool - ohne CapsLock-Behandlung.

        Wird auch von :meth:`_ensure_capslock_normal` genutzt, daher bewusst
        ohne erneute CapsLock-Pruefung (kein Rekursionsrisiko).

        Im Gegensatz zu :func:`_run` bleibt ``capture_output`` aktiv: dotool
        forked keinen Daemon-Prozess, Pipes schliessen also sauber. Aber
        auch dotool kann haengen - deshalb ein payload-abhaengiges
        Timeout-Budget: pro zu tippendem Zeichen fallen typedelay+typehold
        Millisekunden an, plus 5 s Puffer fuer Prozessstart und Layout-
        Aufbau.
        """
        import os

        env = os.environ.copy()
        env["DOTOOL_XKB_LAYOUT"] = self._xkb_layout
        env["DOTOOL_XKB_VARIANT"] = self._xkb_variant
        # Nur 'type '-Zeilen kosten Zeit: key/typedelay/typehold sind fix.
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
                f"{self.binary}: Timeout nach {timeout_s:.1f}s "
                f"({typed_chars} Zeichen)"
            ) from exc
        except FileNotFoundError as exc:
            raise TyperError(f"{self.binary} ist nicht installiert") from exc
        except OSError as exc:
            raise TyperError(f"{self.binary} konnte nicht starten: {exc}") from exc
        if proc.returncode != 0:
            raise TyperError(
                f"{self.binary} fehlgeschlagen (rc={proc.returncode}): "
                f"{(proc.stderr or '').strip()}"
            )

    def _send(self, payload: str) -> None:
        # Diagnose: Lock-Zustand vor dem Tippen erfassen. Der sichtbare Text
        # wird naemlich von einer invertierten CapsLock-Ebene verfaelscht
        # ("Das" -> "dAS") - das passiert NICHT im ASR, sondern durch den
        # Modifier-/Lock-Zustand der Input-Pipeline.
        led_before = _capslock_state()
        leds_before = _capslock_leds()
        self._send_raw(payload)
        led_after = _capslock_state()

        target = self._capslock_leds_target()
        if led_before != target or led_after != target:
            log.warning(
                "CAPSLOCK-Diagnose: LED vor=%s nach=%s - CapsLock ist "
                "vermutlich aktiv (LED ist kein verlaesslicher State), der "
                "Text kann invertiert erscheinen. payload=%r",
                led_before, led_after, payload[:80],
            )
        else:
            log.debug("capslock ok (%s) payload=%r", target, payload[:60])

        # Pro-Device-Diagnose: die LED <-> State-Zuordnung ist nicht
        # verlaesslich; mehrere Devices koennen widersprechen.
        if leds_before:
            log.debug(
                "CAPSLOCK-LEDs pro Device: %s (LED != State, nur Diagnose)",
                leds_before,
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


class ClipboardPaster:
    """Fuegt das finale Delta per ``wl-copy`` + Paste-Chord ein.

    Der Copy-Schritt folgt dem Muster von :class:`ClipboardTyper`: das
    Binary (z.B. ``wl-copy``) bekommt den Text als Argument, der Chord wird
    ueber den normalen Typer (``typer.key``) gedrueckt.

    Fallback-Regeln:

    * Fehlt das Copy-Binary oder schlaegt der Copy fehl, wird das Delta
      zeichenweise getippt (``type_text``) - der alte Pfad bleibt nutzbar.
    * Schlaegt der Paste-Chord NACH erfolgreichem Copy, wird NICHT mit
      ``type_text`` wiederholt: das Delta kann bereits eingefuegt worden
      sein (Doppel-Einfuege-Gefahr). Der Fehler wird geloggt und liegen
      gelassen.
    * Der bisherige Clipboard-Inhalt wird NICHT zurueckgespielt (Race mit
      Clipboard-Managern, kein ``wl-paste``).
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

    def paste(self, text: str) -> None:
        """Kopiert ``text`` in die Zwischenablage und drueckt den Paste-Chord."""
        if not text:
            return
        if shutil.which(self.copy_binary) is None:
            log.warning(
                "%s nicht verfuegbar - tippe das Delta zeichenweise",
                self.copy_binary,
            )
            self._typer.type_text(text)
            return
        try:
            log.debug("final paste: copy start (%d Zeichen)", len(text))
            _run([self.copy_binary, text])
            log.debug("final paste: copy done")
        except TyperError as exc:
            log.warning(
                "Copy nach %s fehlgeschlagen (%s) - tippe das Delta zeichenweise",
                self.copy_binary, exc,
            )
            self._typer.type_text(text)
            return
        try:
            log.debug("final paste: chord %s", self.paste_key)
            self._typer.key(self.paste_key)
        except TyperError as exc:
            # Bewusst KEIN Retry mit type_text: der Chord kann teilweise
            # gewirkt haben, das Delta koennte bereits eingefuegt sein.
            log.error(
                "Paste-Chord %r fehlgeschlagen (%s) - kein Retry "
                "(Doppel-Einfuege-Gefahr)",
                self.paste_key, exc,
            )


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
        final_paster: FinalPaster | None = None,
        latency_trace: bool = False,
        live_typing_mode: str = "full",
    ) -> None:
        self._typer = typer
        self._append_space = append_space
        self._max_revision_chars = max_revision_chars
        # Optional: pastet das finale Delta per Zwischenablage statt es zu
        # tippen. Live-Updates bleiben immer beim normalen Typer (dotool).
        self._final_paster = final_paster
        # Latenz-Trace (config.latency_trace): loggt in finalize() auf
        # Debug-Stufe die Domino-Groessen (rev/backspace/paste/space).
        self._latency_trace = latency_trace
        # Live-Vorschau-Modus: "full" (Default) tippt auch unstabile Partials
        # (mit Backspace-Korrekturen); "committed" tippt nur den gesicherten
        # (committeten) Zuwachs - der Partial wird intern weitergefuehrt,
        # aber nie getippt oder zurueckgetippt (STUFE 2).
        self._live_typing_mode = live_typing_mode
        self._last_text = ""
        self._last_committed = 0

    def _trace(self, point: str) -> None:
        """Debug-Messpunkt der Finalisierung (nur bei latency_trace)."""
        if not self._latency_trace:
            return
        log.debug("latency[%s]", point)

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

        # STUFE 2 (live_typing_mode='committed'): Nur der gesicherte Zuwachs
        # wird getippt. Der interne Stand fuehrt ausschliesslich den effektiv
        # getippten Text - der unstabile Partial wird nie getippt und nie
        # zurueckgetippt (keine Live-Backspaces, kein Flackern). finalize()
        # rechnet dadurch gegen den effektiven Cursor-Stand: to_remove==0,
        # das Suffix kommt als ein Paste.
        if self._live_typing_mode == "committed":
            stable = text[:committed_len]
            if stable.startswith(old):
                to_add = stable[len(old):]
                if to_add:
                    self._typer.type_text(to_add)
                self._last_text = stable
                self._last_committed = len(stable)
            else:
                # Gesicherter Bereich widerspricht dem Getippten (per
                # LocalAgreement praktisch ausgeschlossen): Live nichts
                # anfassen, finalize() korrigiert abschliessend.
                log.debug(
                    "committed-Live: Praefix-Widerspruch - update verworfen"
                )
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
        """Gibt den endgueltigen Text aus und schliesst mit einem Leerzeichen ab.

        Mit konfiguriertem ``final_paster`` wird das berechnete Delta
        (``to_add``) genau einmal per Zwischenablage eingefuegt - der
        committete Praefix und bereits getippter Text werden niemals mit-
        gesendet. Ohne ``final_paster`` gilt der bisherige Pfad: Backspace
        und Delta ueber den normalen Typer.

        Eine leere Endausgabe darf den bereits getippten Text nicht loeschen
        (z.B. wenn das Modell bei der Schluss-Transkription gescheitert ist).
        """
        if not final_text.strip():
            return

        old = self._last_text
        committed_len = max(0, committed_len)
        # Committed-Schutz: der hoehere Stand gewinnt, damit ein kleinerer
        # committed_len beim Abschluss nichts bereits Gesichertes oeffnet.
        effective_committed = max(committed_len, self._last_committed)

        if len(final_text) >= effective_committed:
            committed = min(effective_committed, len(old), len(final_text))

            # Weicht das Modell im committeten Bereich ab, uebernehmen wir
            # dort die alte Fassung.
            if old[:committed] != final_text[:committed]:
                final_text = old[:committed] + final_text[committed:]

            old_tail = old[committed:]
            new_tail = final_text[committed:]

            common = _common_prefix_len(old_tail, new_tail)
            to_remove = len(old_tail) - common
            to_add = new_tail[common:]

            # Backspace bleibt immer beim normalen Typer (dotool), auch im
            # final_paste-Modus. Die finale Korrektur ist voll korrigierend:
            # keine max_revision_chars-Schutzgrenze (force_revision im alten
            # Pfad).
            self._trace(f"rev {to_remove}/{len(to_add)}")
            if to_remove:
                t0 = time.monotonic()
                self._typer.backspace(to_remove)
                self._trace(f"backspace {time.monotonic() - t0:.3f}s")
            if to_add:
                t0 = time.monotonic()
                if self._final_paster is not None:
                    self._final_paster.paste(to_add)
                    self._trace("paste")
                else:
                    self._typer.type_text(to_add)
                    self._trace("type")
                self._trace(f"output {time.monotonic() - t0:.3f}s")

            self._last_text = old[:committed] + new_tail
            self._last_committed = committed

        if self._append_space:
            t0 = time.monotonic()
            self._typer.key("space")
            self._trace(f"space {time.monotonic() - t0:.3f}s")
        self._last_text = final_text


def _common_prefix_len(a: str, b: str) -> int:
    """Laenge des gemeinsamen Praefixes von ``a`` und ``b``."""
    limit = min(len(a), len(b))
    i = 0
    while i < limit and a[i] == b[i]:
        i += 1
    return i
