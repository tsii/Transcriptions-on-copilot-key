"""Audio-Aufnahme ueber PipeWire.

``pw-record`` schreibt rohe PCM-Daten (s16le, mono, 16 kHz) nach stdout; der
``PwRecordSource`` kapselt den Subprozess und liefert Chunks an die
Anwendung. Damit das Ende sauber aufgenommen wird, wird beim Stoppen der
Prozess beendet und die Restdaten aus stdout gelesen.

Fuer Tests gibt es ``FileAudioSource``, das eine WAV-Datei abspielt.
"""
from __future__ import annotations

import logging
import os
import subprocess
import time
import wave
from pathlib import Path
from typing import Protocol, runtime_checkable

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
CHANNELS = 1
FORMAT = "s16"
BYTES_PER_SAMPLE = 2


class AudioSourceError(RuntimeError):
    """Die Audioquelle konnte nicht geoeffnet werden."""


@runtime_checkable
class AudioSource(Protocol):
    """Schnittstelle einer Audioquelle."""

    def start(self) -> None:
        """Startet die Aufnahme."""

    def stop(self) -> bytes:
        """Stoppt die Aufnahme und liefert verbliebene Daten."""

    def read(self, block: bool = True) -> bytes:
        """Liest einen Chunk roher PCM-Daten (kann ``b""`` liefern)."""


def _resolve_source(source: str) -> str:
    """Loest ``auto`` in den Standardquellennamen auf."""
    if source != "auto":
        return source
    try:
        proc = subprocess.run(
            ["pactl", "get-default-source"], capture_output=True, text=True
        )
        name = proc.stdout.strip()
        if name:
            return name
    except (OSError, subprocess.SubprocessError):
        pass
    raise AudioSourceError("Konnte die Standard-Audioquelle nicht ermitteln")


class PwRecordSource:
    """Nimmt Audio ueber ``pw-record`` auf.

    Der Prozess schreibt PCM-Daten (s16le, mono, 16 kHz) nach stdout.
    """

    def __init__(
        self,
        source: str,
        *,
        rate: int = SAMPLE_RATE,
        channels: int = CHANNELS,
        audio_format: str = FORMAT,
        popen_factory=None,
    ) -> None:
        self.source = source
        self.rate = rate
        self.channels = channels
        self.audio_format = audio_format
        self._popen_factory = popen_factory or subprocess.Popen
        self._proc = None

    def start(self) -> None:
        """Startet den ``pw-record``-Prozess."""
        name = _resolve_source(self.source)
        cmd = [
            "pw-record",
            "--rate", str(self.rate),
            "--channels", str(self.channels),
            "--format", self.audio_format,
            "--target", name,
            "-",
        ]
        log.debug("Starte: %s", " ".join(cmd))
        try:
            self._proc = self._popen_factory(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
        except OSError as exc:
            raise AudioSourceError(f"pw-record nicht startbar: {exc}") from exc

        # Den stdout-Pipe auf non-blocking schalten, damit read() nie haengt.
        try:
            if self._proc.stdout is not None:
                os.set_blocking(self._proc.stdout.fileno(), False)
        except OSError:
            pass

    def read(self, block: bool = True) -> bytes:
        """Liest einen Chunk roher PCM-Daten (non-blocking)."""
        if self._proc is None or self._proc.stdout is None:
            return b""
        try:
            return self._proc.stdout.read(4096)
        except BlockingIOError:
            return b""
        except OSError:
            return b""

    def stop(self) -> bytes:
        """Beendet die Aufnahme und liefert die verbliebenen Daten.

        Liest zunaechst alle bereits gepufferten Daten (Drain), beendet dann
        den Prozess und liest den Rest mit einem Timeout, damit kein
        Aufruf unbegrenzt haengen bleibt.
        """
        if self._proc is None:
            return b""
        proc = self._proc
        self._proc = None

        # 1) Drain der bereits anliegenden Daten (ohne auf EOF zu warten).
        rest = b""
        if proc.stdout is not None:
            try:
                while True:
                    chunk = proc.stdout.read()
                    if not chunk:
                        break
                    rest += chunk
            except BlockingIOError:
                pass
            except OSError:
                pass

        # 2) Prozess beenden und den Rest mit Timeout sammeln.
        try:
            proc.terminate()
        except OSError:
            pass

        deadline = time.monotonic() + 2.0
        if proc.stdout is not None:
            try:
                while time.monotonic() < deadline:
                    try:
                        chunk = os.read(proc.stdout.fileno(), 4096)
                    except BlockingIOError:
                        # Keine Daten mehr - kurz warten und erneut prüfen.
                        if proc.poll() is not None:
                            break
                        time.sleep(0.02)
                        continue
                    except OSError:
                        break
                    if not chunk:
                        break
                    rest += chunk
            except OSError:
                pass

        try:
            proc.wait(timeout=2.0)
        except (subprocess.TimeoutExpired, OSError):
            try:
                proc.kill()
                proc.wait(timeout=1.0)
            except OSError:
                pass
        return rest


class FileAudioSource:
    """Liest Audio aus einer WAV-Datei (fuer Tests und Diagnose)."""

    def __init__(self, path: str | Path, *, chunk_s: float = 0.5) -> None:
        self.path = Path(path)
        self.chunk_s = chunk_s
        self._wf = None
        self._chunk_bytes = 0

    def start(self) -> None:
        try:
            self._wf = wave.open(str(self.path), "rb")
        except (OSError, wave.Error) as exc:
            raise AudioSourceError(f"{self.path}: nicht lesbar ({exc})") from exc
        if self._wf.getsampwidth() != 2 or self._wf.getnchannels() != 1:
            # Fuer Tests reicht Mono/s16; sterero wird unten nicht gemischt.
            pass
        self._chunk_bytes = int(
            self._wf.getframerate() * self.chunk_s * self._wf.getsampwidth()
        )

    def read(self, block: bool = True) -> bytes:
        if self._wf is None:
            return b""
        return self._wf.readframes(self._chunk_bytes // self._wf.getsampwidth())

    def stop(self) -> bytes:
        if self._wf is None:
            return b""
        rest = self._wf.readframes(self._wf.getnframes())
        self._wf.close()
        self._wf = None
        return rest