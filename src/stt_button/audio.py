"""Audio capture via PipeWire.

``pw-record`` writes raw PCM data (s16le, mono, 16 kHz) to stdout;
``PwRecordSource`` wraps the subprocess and delivers chunks to the
application. To capture the ending cleanly, stopping terminates the
process and reads the remaining data from stdout.

``FileAudioSource`` plays a WAV file for tests.
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
    """The audio source could not be opened."""


@runtime_checkable
class AudioSource(Protocol):
    """Interface for an audio source."""

    def start(self) -> None:
        """Start recording."""

    def stop(self) -> bytes:
        """Stop recording and return remaining data."""

    def read(self, block: bool = True) -> bytes:
        """Read a chunk of raw PCM data (may return ``b""``)."""


def _resolve_source(source: str) -> str:
    """Resolve ``auto`` to the default source name."""
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
    raise AudioSourceError("Could not determine the default audio source")


class PwRecordSource:
    """Capture audio via ``pw-record``.

    The process writes PCM data (s16le, mono, 16 kHz) to stdout.
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
        """Start the ``pw-record`` process."""
        name = _resolve_source(self.source)
        cmd = [
            "pw-record",
            "--rate", str(self.rate),
            "--channels", str(self.channels),
            "--format", self.audio_format,
            "--target", name,
            "-",
        ]
        log.debug("Starting: %s", " ".join(cmd))
        try:
            self._proc = self._popen_factory(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
        except OSError as exc:
            raise AudioSourceError(f"Could not start pw-record: {exc}") from exc

        # Set the stdout pipe to non-blocking so read() never hangs.
        try:
            if self._proc.stdout is not None:
                os.set_blocking(self._proc.stdout.fileno(), False)
        except OSError:
            pass

    def read(self, block: bool = True) -> bytes:
        """Read a chunk of raw PCM data (non-blocking)."""
        if self._proc is None or self._proc.stdout is None:
            return b""
        try:
            return self._proc.stdout.read(4096)
        except BlockingIOError:
            return b""
        except OSError:
            return b""

    def stop(self) -> bytes:
        """Stop recording and return the remaining data.

        First read all already buffered data (drain), then terminate the
        process and read the remainder with a timeout so that no call can
        hang indefinitely.
        """
        if self._proc is None:
            return b""
        proc = self._proc
        self._proc = None

        # 1) Drain data that is already available (without waiting for EOF).
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

        # 2) Terminate the process and collect the remainder with a timeout.
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
                        # No more data - wait briefly and check again.
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
    """Read audio from a WAV file (for tests and diagnostics)."""

    def __init__(self, path: str | Path, *, chunk_s: float = 0.5) -> None:
        self.path = Path(path)
        self.chunk_s = chunk_s
        self._wf = None
        self._chunk_bytes = 0

    def start(self) -> None:
        try:
            self._wf = wave.open(str(self.path), "rb")
        except (OSError, wave.Error) as exc:
            raise AudioSourceError(f"{self.path}: unreadable ({exc})") from exc
        if self._wf.getsampwidth() != 2 or self._wf.getnchannels() != 1:
            # Mono/s16 is sufficient for tests; stereo is not mixed below.
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