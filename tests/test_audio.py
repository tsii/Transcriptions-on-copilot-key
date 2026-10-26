"""Tests fuer die Audio-Aufnahme (TDD-Kette 5).

Die ``PwRecordSource`` kapselt einen ``pw-record``-Subprozess. Getestet wird
gegen einen Fake-Prozess - es laeuft kein echtes Mikrofon.
"""
from __future__ import annotations



import pytest

from stt_button.audio import (
    AudioSource,
    AudioSourceError,
    FileAudioSource,
    PwRecordSource,
    _resolve_source,
)


class FakeProc:
    """Simuliert einen pw-record-Subprozess."""

    def __init__(self, chunks: list[bytes] | None = None) -> None:
        self.chunks = list(chunks or [])
        self.terminated = False
        self.waited = []
        self.returncode = None

    @property
    def stdout(self):
        return self

    def fileno(self) -> int:
        return -1  # non-blocking wird per Mock ignoriert

    def read(self, size: int = -1) -> bytes:
        if not self.chunks:
            return b""
        return self.chunks.pop(0)

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        self.waited.append(timeout)
        return 0

    def poll(self) -> int | None:
        return 0 if self.terminated else None

    def kill(self) -> None:
        self.terminated = True


class FakePopenFactory:
    """Liefert vordefinierte FakeProc-Instanzen."""

    def __init__(self, procs: list[FakeProc]) -> None:
        self.procs = procs
        self.commands: list[list[str]] = []
        self.idx = 0

    def __call__(self, cmd: list[str], **kwargs) -> FakeProc:
        self.commands.append(cmd)
        proc = self.procs[self.idx]
        self.idx += 1
        return proc


class TestResolveSource:
    def test_auto_resolves_default_source(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "stt_button.audio.subprocess.run",
            lambda *a, **k: _Result("deepfilter_source\n"),
        )
        assert _resolve_source("auto") == "deepfilter_source"

    def test_explicit_source_is_passthrough(self) -> None:
        assert _resolve_source("meine-quelle") == "meine-quelle"


class _Result:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout


class TestPwRecordSourceArgv:
    def test_builds_correct_argv(self, monkeypatch: pytest.MonkeyPatch) -> None:
        factory = FakePopenFactory([FakeProc([b"\x00\x00"])])
        monkeypatch.setattr("stt_button.audio.subprocess.Popen", factory)
        src = PwRecordSource("meine-quelle", popen_factory=factory)
        src.start()
        cmd = factory.commands[0]
        assert cmd[0] == "pw-record"
        assert "--rate" in cmd and "16000" in cmd
        assert "--channels" in cmd and "1" in cmd
        assert "--format" in cmd and "s16" in cmd
        # Quelle als --target
        i = cmd.index("--target")
        assert cmd[i + 1] == "meine-quelle"
        # stdout als Ziel (steht am Ende als "-")
        assert cmd[-1] == "-"

    def test_start_failure_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(cmd, **kwargs):
            raise OSError("pw-record nicht da")

        monkeypatch.setattr("stt_button.audio.subprocess.Popen", boom)
        src = PwRecordSource("q", popen_factory=boom)
        with pytest.raises(AudioSourceError):
            src.start()


class TestPwRecordSourceRead:
    def test_read_returns_chunks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        data = b"\x01\x02" * 100
        factory = FakePopenFactory([FakeProc([data])])
        monkeypatch.setattr("stt_button.audio.subprocess.Popen", factory)
        src = PwRecordSource("q", popen_factory=factory)
        src.start()
        assert src.read(block=False) == data

    def test_read_after_eof_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        factory = FakePopenFactory([FakeProc([b"\x01\x02"])])
        monkeypatch.setattr("stt_button.audio.subprocess.Popen", factory)
        src = PwRecordSource("q", popen_factory=factory)
        src.start()
        src.read(block=False)
        assert src.read(block=False) == b""

    def test_stop_terminates_and_drains(self, monkeypatch: pytest.MonkeyPatch) -> None:
        tail = b"\x03\x04" * 10
        factory = FakePopenFactory([FakeProc([b"\x01\x02", tail])])
        monkeypatch.setattr("stt_button.audio.subprocess.Popen", factory)
        src = PwRecordSource("q", popen_factory=factory)
        src.start()
        src.read(block=False)
        rest = src.stop()
        assert factory.procs[0].terminated
        assert rest == tail

    def test_stop_without_start_is_noop(self) -> None:
        src = PwRecordSource("q")
        assert src.stop() == b""


class TestFileAudioSource:
    def _wav(self, tmp_path, seconds: float, rate: int = 16000) -> str:
        # Erzeugt eine kleine WAV-Datei (s16le mono).
        import struct
        import wave

        path = tmp_path / "test.wav"
        n = int(seconds * rate)
        frames = b"\x00\x00" * n
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(frames)
        return str(path)

    def test_reads_wav_chunks(self, tmp_path) -> None:
        path = self._wav(tmp_path, 0.5)
        src = FileAudioSource(path, chunk_s=0.25)
        src.start()
        data = src.read(block=False)
        # 0.25s * 16000 Hz * 2 bytes = 8000 bytes
        assert len(data) == 8000
        src.stop()

    def test_missing_file_raises_on_start(self, tmp_path) -> None:
        src = FileAudioSource(str(tmp_path / "nope.wav"))
        with pytest.raises(AudioSourceError):
            src.start()

    def test_stop_returns_remaining(self, tmp_path) -> None:
        path = self._wav(tmp_path, 0.5)
        src = FileAudioSource(path, chunk_s=0.25)
        src.start()
        src.read(block=False)  # konsumiert einen Chunk (0.25s = 8000 bytes)
        rest = src.stop()
        assert len(rest) == 8000  # der zweite Chunk


class TestProtocol:
    def test_audio_source_is_a_protocol(self) -> None:
        assert isinstance(PwRecordSource("q"), AudioSource)
        assert isinstance(FileAudioSource("x.wav"), AudioSource)