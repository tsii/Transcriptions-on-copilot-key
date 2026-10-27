"""Tests fuer den Controller / Integration (TDD-Kette 6).

Der PushToTalkController verbindet Key-Trigger, Audioquelle, StreamTranscriber
und DeltaTyper. Getestet wird end-to-end mit Fakes - kein Mikrofon, kein Modell,
kein Wayland-Tool.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

import pytest

from stt_button.audio import AudioSource
from stt_button.asr import TranscriptionResult
from stt_button.controller import ControllerState, PushToTalkController
from stt_button.typing import DeltaTyper


class FakeAudioSource:
    """Schiebt vordefinierte Chunks."""

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = list(chunks)
        self.started = 0
        self.stopped = 0

    def start(self) -> None:
        self.started += 1

    def stop(self) -> bytes:
        self.stopped += 1
        out = b"".join(self.chunks)
        self.chunks = []
        return out

    def read(self, block: bool = True) -> bytes:
        if self.chunks:
            return self.chunks.pop(0)
        return b""


class FakeEngine:
    """Transkribiert Audio deterministisch anhand der Pufferlaenge."""

    def __init__(self) -> None:
        self.calls = 0

    def transcribe(self, data: bytes, initial_prompt: str = "") -> TranscriptionResult:
        self.calls += 1
        # 0.5s = 16000 bytes -> ein "Wort" pro 0.5s
        words = len(data) // 16000
        text = " ".join(f"wort{i}" for i in range(words))
        return TranscriptionResult(text=text)


class FakeTyper:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def type_text(self, text: str) -> None:
        self.calls.append(("type", text))

    def backspace(self, n: int) -> None:
        self.calls.append(("backspace", str(n)))

    def key(self, name: str) -> None:
        self.calls.append(("key", name))


class BlockingTranscriber:
    """StreamTranscriber-Fake, dessen finalize() auf ein Event wartiert.

    Macht das Ende der Finalisierung deterministisch steuerbar: Ohne
    threading.Events muessten die Tests sleeps raten (flaky).
    """
    def __init__(self, unblock: threading.Event) -> None:
        self._unblock = unblock
        self._buffer = b"\x00" * 16000  # fuer evtl. Audio-Dump
        self.sample_rate = 16000
        self.finalized = threading.Event()

    def feed(self, data: bytes) -> None:
        pass

    def maybe_transcribe(self):
        return None

    def finalize(self):
        self._unblock.wait()
        return TranscriptionResult(text="fertig")


class FakeSourceFactory:
    """Zaehlt die erzeugten Quellen (je Aufnahme eine neue)."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)
        self.created: list[FakeAudioSource] = []

    def __call__(self) -> FakeAudioSource:
        source = FakeAudioSource(self._chunks)
        self.created.append(source)
        return source

    @property
    def started(self) -> int:
        return sum(1 for s in self.created if s.started > 0)

    @property
    def stopped(self) -> int:
        return sum(1 for s in self.created if s.stopped > 0)


def make_controller(**kwargs) -> tuple[PushToTalkController, dict]:
    blocking = kwargs.pop("blocking_transcriber", False)
    if blocking:
        unblock = threading.Event()
        factory = FakeSourceFactory([b"\x00" * 16000] * 6)
        engine = FakeEngine()
        typer = FakeTyper()
        delta = DeltaTyper(typer, append_space=True)
        ctrl = PushToTalkController(
            config=None,
            source_factory=factory,
            engine=engine,
            delta_typer=delta,
            transcriber_factory=lambda *a, **kw: BlockingTranscriber(unblock),
            **kwargs,
        )
        ctrl._test_unblock_finalize = unblock
        ctrl._wait_state = lambda target, timeout=5.0: _wait_state(
            ctrl, target, timeout
        )
        return ctrl, {"source": factory, "engine": engine, "typer": typer,
                      "delta": delta}
    source = FakeAudioSource([b"\x00" * 16000] * 6)
    engine = FakeEngine()
    typer = FakeTyper()
    delta = DeltaTyper(typer, append_space=True)
    ctrl = PushToTalkController(
        config=None,
        source_factory=lambda: source,
        engine=engine,
        delta_typer=delta,
        **kwargs,
    )
    return ctrl, {
        "source": source,
        "engine": engine,
        "typer": typer,
        "delta": delta,
    }


def _wait_state(ctrl: PushToTalkController, target: ControllerState,
                timeout: float = 5.0) -> None:
    """Wartet (mit Deadline, ohne blindes Sleep) bis der Zustand erreicht ist."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ctrl.state == target:
            return
        time.sleep(0.005)
    raise AssertionError(
        f"Zustand {target} nicht erreicht (ist {ctrl.state})"
    )


class TestStateLifecycle:
    def test_initial_state_is_idle(self) -> None:
        ctrl, _ = make_controller()
        assert ctrl.state == ControllerState.IDLE

    def test_start_recording_changes_state(self) -> None:
        ctrl, ctx = make_controller()
        ctrl.start_recording()
        assert ctrl.state == ControllerState.RECORDING
        assert ctx["source"].started == 1

    def test_double_start_is_ignored(self) -> None:
        ctrl, ctx = make_controller()
        ctrl.start_recording()
        ctrl.start_recording()
        assert ctx["source"].started == 1

    def test_stop_recording_changes_state(self) -> None:
        ctrl, ctx = make_controller()
        ctrl.start_recording()
        ctrl.stop_recording()
        # Finalisierung laeuft im Hintergrund -> kurz warten.
        for _ in range(100):
            if ctrl.state == ControllerState.IDLE:
                break
            time.sleep(0.02)
        assert ctrl.state == ControllerState.IDLE
        assert ctx["source"].stopped == 1

    def test_release_during_finalizing_updates_key_state(self) -> None:
        """Fix: Release waehrend FINALIZING aktualisiert _key_down immer.

        Vorher kehrte stop_recording() bei state != RECORDING kommentarlos
        zurueck - der Release-Edge ging verloren und die Geisteraufnahme
        startete spaeter trotzdem.
        """
        ctrl, ctx = make_controller(blocking_transcriber=True)
        ctrl.start_recording()
        ctrl.stop_recording()            # -> FINALIZING
        assert ctrl.state == ControllerState.FINALIZING
        ctrl.start_recording()           # press waehrend FINALIZING
        assert ctrl._key_down is True
        ctrl.stop_recording()            # release waehrend FINALIZING
        # Der Release-Edge darf NIE verloren gehen:
        assert ctrl._key_down is False
        # Jetzt die Finalisierung abschliessen lassen.
        ctrl._test_unblock_finalize.set()
        ctrl._wait_state(ControllerState.IDLE)
        # KEINE Geisteraufnahme: die Taste ist nicht gehalten.
        assert ctrl.state == ControllerState.IDLE
        assert ctx["source"].started == 1

    def test_key_held_through_finalize_restarts_recording(self) -> None:
        """Taste bleibt gehalten: nach Finalisierung startet die Aufnahme."""
        ctrl, ctx = make_controller(blocking_transcriber=True)
        ctrl.start_recording()
        ctrl.stop_recording()            # -> FINALIZING
        ctrl.start_recording()           # press (halten) waehrend FINALIZING
        ctrl._test_unblock_finalize.set()
        ctrl._wait_state(ControllerState.RECORDING)
        # Start nur, weil _key_down noch True ist.
        assert ctrl.state == ControllerState.RECORDING
        assert ctrl._key_down is True
        assert ctx["source"].started == 2

    def test_start_after_finalizing_only_when_key_held(self) -> None:
        """Alternierender Druck: press+release vor Finalisierungsende.

        Die Taste wurde vor dem Ende der Finalisierung bereits wieder
        losgelassen - es darf KEINE Aufnahme gestartet werden.
        """
        ctrl, ctx = make_controller(blocking_transcriber=True)
        ctrl.start_recording()
        ctrl.stop_recording()
        ctrl.start_recording()           # vorgemerkt
        ctrl.stop_recording()            # ... aber schon losgelassen
        ctrl._test_unblock_finalize.set()
        ctrl._wait_state(ControllerState.IDLE)
        assert ctrl.state == ControllerState.IDLE
        assert ctx["source"].started == 1

    def test_release_after_finalized_start_stops_fresh_recording(self) -> None:
        """Release direkt nach dem automatischen Start stoppt die neue Aufnahme.

        Frueher scheiterte ein Release im Handoff-Fenster an state==IDLE
        (bzw. der Start und das Release race-ten gegeneinander).
        """
        ctrl, ctx = make_controller(blocking_transcriber=True)
        ctrl.start_recording()
        ctrl.stop_recording()            # -> FINALIZING
        ctrl.start_recording()           # Taste gehalten
        ctrl._test_unblock_finalize.set()
        ctrl._wait_state(ControllerState.RECORDING)  # Auto-Start ist da
        ctrl.stop_recording()           # Release danach
        ctrl._test_unblock_finalize.set()  # 2. Finalisierung freigeben
        ctrl._wait_state(ControllerState.IDLE)
        assert ctrl._key_down is False
        assert ctx["source"].started == 2
        assert ctx["source"].stopped == 2

    def test_press_during_recording_is_idempotent(self) -> None:
        """Mehrfaches Druecken waehrend RECORDING startet nichts Neues."""
        ctrl, ctx = make_controller(blocking_transcriber=True)
        ctrl.start_recording()
        ctrl.start_recording()           # nochmal druecken
        assert ctrl._key_down is True
        assert ctrl.state == ControllerState.RECORDING
        assert ctx["source"].started == 1
        ctrl.stop_recording()
        ctrl._test_unblock_finalize.set()
        ctrl._wait_state(ControllerState.IDLE)
        assert ctx["source"].started == 1


    def test_stop_without_start_is_noop(self) -> None:
        ctrl, ctx = make_controller()
        ctrl.stop_recording()
        assert ctx["source"].stopped == 0


class TestStreaming:
    def test_run_once_feeds_and_transcribes(self) -> None:
        ctrl, ctx = make_controller()
        ctrl.start_recording()
        # Zwei run_once = 2 Chunks a 0.5s = 1.0s >= min_audio_s (0.8s)
        ctrl.run_once()
        ctrl.run_once()
        assert ctx["engine"].calls >= 1

    def test_text_reaches_typer(self) -> None:
        ctrl, ctx = make_controller()
        ctrl.start_recording()
        for _ in range(3):
            ctrl.run_once()
        assert any(c[0] == "type" for c in ctx["typer"].calls)

    def test_release_finalizes_and_appends_space(self) -> None:
        ctrl, ctx = make_controller()
        ctrl.start_recording()
        for _ in range(3):
            ctrl.run_once()
        ctrl.stop_recording()
        assert any(c[0] == "key" and c[1] == "space" for c in ctx["typer"].calls)


class TestTimeouts:
    def test_no_audio_timeout_aborts(self) -> None:
        source = FakeAudioSource([])  # keine Audio-Daten
        typer = FakeTyper()
        ctrl = PushToTalkController(
            config=None,
            source_factory=lambda: source,
            engine=FakeEngine(),
            delta_typer=DeltaTyper(typer, append_space=True),
            no_audio_timeout_s=0.01,
        )
        ctrl.start_recording()
        time.sleep(0.02)  # Timeout ueberschreiten
        ctrl.run_once()
        assert ctrl.state == ControllerState.IDLE

    def test_max_record_auto_finalizes(self) -> None:
        """Max-Dauer finalisiert; ohne gehaltene Taste KEIN Auto-Neustart.

        Wird die Taste (wie beim echten Loslassen) vor Finalisierungsende
        losgelassen, bleibt der Controller in IDLE. Hielte der Nutzer die
        Taste weiter, startete die naechste Aufnahme automatisch - das
        testet test_key_held_through_finalize_restarts_recording.
        """
        ctrl, ctx = make_controller(blocking_transcriber=True)
        ctrl._max_record_s = 0.01
        ctrl.start_recording()
        time.sleep(0.02)
        ctrl._enforce_max_duration()
        assert ctrl.state == ControllerState.FINALIZING
        ctrl.stop_recording()          # Taste loslassen
        assert ctrl._key_down is False
        ctrl._test_unblock_finalize.set()
        ctrl._wait_state(ControllerState.IDLE)
        assert ctrl.state == ControllerState.IDLE
        assert ctx["source"].stopped == 1


class TestErrors:
    def test_engine_error_aborts(self) -> None:
        class Boom:
            def transcribe(self, data, initial_prompt=""):
                raise RuntimeError("kaputt")

        source = FakeAudioSource([b"\x00" * 16000] * 3)
        typer = FakeTyper()
        ctrl = PushToTalkController(
            config=None,
            source_factory=lambda: source,
            engine=Boom(),
            delta_typer=DeltaTyper(typer, append_space=True),
        )
        ctrl.start_recording()
        ctrl.run_once()
        ctrl.run_once()  # genug Audio fuer eine Transkription
        assert ctrl.state == ControllerState.IDLE

    def test_abort_resets_state(self) -> None:
        ctrl, ctx = make_controller()
        ctrl.start_recording()
        ctrl.abort()
        assert ctrl.state == ControllerState.IDLE
        assert ctx["source"].stopped == 1


class TestShutdown:
    def test_shutdown_stops_source(self) -> None:
        ctrl, ctx = make_controller()
        ctrl.start_recording()
        ctrl.shutdown()
        assert ctx["source"].stopped == 1


class TestCaptureThread:
    def test_audio_not_lost_during_blocking_inference(self) -> None:
        """Der Capture-Thread liest weiter, auch wenn die Engine blockiert.

        Reproduziert den Bug 'Taste laenger gehalten als gesprochen': die
        Engine blockiert (ASR-Inferenz), aber die Quelle liefert weitere
        Chunks. Ohne Capture-Thread gingen diese verloren.
        """
        # Quelle liefert laufend Chunks (mehr als der Hauptthread lesen kann).
        source = FakeAudioSource([b"\x00" * 16000] * 50)
        typer = FakeTyper()
        ctrl = PushToTalkController(
            config=None,
            source_factory=lambda: source,
            engine=FakeEngine(),
            delta_typer=DeltaTyper(typer, append_space=True),
        )
        ctrl.start_recording()
        # Der Capture-Thread hat Zeit, Chunks in die Queue zu lesen.
        time.sleep(0.3)
        # Alle 50 Chunks muessen in der Queue gelandet sein (kein Verlust
        # waehrend der Thread laeuft, egal ob die Engine blockiert).
        assert ctrl._audio_queue.qsize() == 50
        ctrl.stop_recording()