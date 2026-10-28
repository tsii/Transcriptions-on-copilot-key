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
        # Die Finalisierung laeuft bewusst im Hintergrund-Thread; auf deren
        # Abschluss warten (begrenzt), statt aufThread-Scheduling zu wetten.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and ctrl.state != ControllerState.IDLE:
            time.sleep(0.005)
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

class TestFinalizePasteLockHierarchy:
    """Charakterisierung: FinalPaster-Hang blockiert den Key-Thread NICHT.

    Die Lock-Hierarchie im Controller ist bewusst so gebaut:

    * ``finalize``/``paste`` laufen IM FINALIZER-THREAD und greifen NICHT
      auf das Controller-Lock zu (controller.py: _finish setzt nur den
      kurzen Zustandswechsel unter Lock).
    * ``start_recording``/``stop_recording`` holen das Lock nur kurz und
      kehren auch dann sofort zurueck, wenn der Finalizer (noch) im
      final_paste haengt.

    Dieser Test charakterisiert dieses Verhalten: selbst wenn der
    FinalPaster fuer immer blockiert, bleibt der Key-Thread ansprechbar
    und die Key-Edges werden korrekt verarbeitet.
    """

    def test_blocking_final_paster_does_not_block_key_thread(self) -> None:
        """Haengendes final_paste: start/stop kehren ohne Blockade zurueck."""
        unblock_paste = threading.Event()
        paste_entered = threading.Event()

        class BlockingFinalPaster:
            """FinalPaster-Fake, der auf ein nie (sofort) gesetztes Event wartet."""

            def paste(self, text: str) -> None:
                paste_entered.set()
                unblock_paste.wait(10.0)

        source = FakeAudioSource([b"\x00" * 16000] * 6)
        typer = FakeTyper()
        delta = DeltaTyper(typer, append_space=True, final_paster=BlockingFinalPaster())
        ctrl = PushToTalkController(
            config=None,
            source_factory=lambda: source,
            engine=FakeEngine(),
            delta_typer=delta,
            transcriber_factory=lambda *a, **kw: InstantTranscriber(),
        )

        ctrl.start_recording()
        for _ in range(3):
            ctrl.run_once()
        # Loslassen: Finalizer startet im Hintergrund und haengt im Paste.
        ctrl.stop_recording()
        assert ctrl.state == ControllerState.FINALIZING
        assert paste_entered.wait(timeout=2.0)

        # Kern-Charakterisierung: Der Key-Thread (dieser Thread simuliert
        # ihn) kehrt trotz haengendem Paste sofort zurueck - start/stop
        # nehmen nur kurz das Lock, paste() laeuft lock-frei im Finalizer.
        t0 = time.monotonic()
        ctrl.start_recording()  # press waehrend FINALIZING
        assert ctrl._key_down is True
        ctrl.stop_recording()  # release
        assert ctrl._key_down is False
        assert time.monotonic() - t0 < 0.5, (
            "start/stop_recording blockieren am haengenden final_paste - "
            "Lock-Hierarchie verletzt"
        )
        assert ctrl.state == ControllerState.FINALIZING

        # Jetzt das Paste freigeben: Finalizer laeuft zu Ende.
        unblock_paste.set()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if ctrl.state == ControllerState.IDLE:
                break
            time.sleep(0.005)
        assert ctrl.state == ControllerState.IDLE
        # KEINE Geisteraufnahme: die Taste wurde losgelassen.
        assert ctrl._key_down is False


class RestAudioSource:
    """Quelle, die beim stop() zusaetzlich den Pipe-Rest ``rest`` liefert.

    Der Capture-Thread liest nur ``chunks``; die letzten Bytes des Pipes
    (``rest``) erscheinen erst beim Drain in ``stop()`` - wie bei pw-record.
    """

    def __init__(self, chunks: list[bytes], rest: bytes) -> None:
        self.chunks = list(chunks)
        self.rest = rest
        self.started = 0
        self.stopped = 0

    def start(self) -> None:
        self.started += 1

    def stop(self) -> bytes:
        self.stopped += 1
        out, self.chunks = b"".join(self.chunks), []
        return out + self.rest

    def read(self, block: bool = True) -> bytes:
        if self.chunks:
            return self.chunks.pop(0)
        return b""


class TestLatencyLockDecoupling:
    """STUFE 1: Release darf nicht auf laufender Live-Inferenz warten.

    Der Controller-Lock ``_lock`` schuetzt nur noch kurze Zustandswechsel;
    die Live-/Final-Kette laeuft unter ``_exec_lock``. Dadurch stoppt ein
    Release die Audioquelle sofort, auch wenn gerade eine mehrsekundige
    Live-Inferenz laeuft.
    """

    def test_release_during_live_asr_stops_audio_before_inference_returns(
        self,
    ) -> None:
        """Release bei blockierender Live-Inferenz: Audio stoppt < 0,3s."""
        engine = BlockingEngine()
        source = FakeAudioSource([b"\x00" * 16000] * 8)
        typer = FakeTyper()
        ctrl = PushToTalkController(
            config=None,
            source_factory=lambda: source,
            engine=engine,
            delta_typer=DeltaTyper(typer, append_space=True),
            min_audio_s=0.1,
            asr_interval_s=0.1,
        )
        ctrl.start_recording()
        ticker = threading.Thread(target=ctrl.run_once, daemon=True)
        ticker.start()
        # Live-Inferenz laeuft (Engine blockiert bis zu 2s im transcribe()).
        assert engine.entered.wait(timeout=5.0), "Live-Inferenz started nicht"

        t0 = time.monotonic()
        ctrl.stop_recording()
        elapsed = time.monotonic() - t0

        # Kern der STUFE 1: Die Quelle ist gestoppt, obwohl die Engine noch
        # belegt ist, und stop_recording kehrte sofort zurueck.
        assert not engine.release.is_set(), "Engine wurde vor Release freigegeben"
        assert source.stopped == 1, "Audioquelle beim Release nicht gestoppt"
        assert elapsed < 0.3, (
            f"stop_recording blockierte {elapsed:.2f}s an der Live-Inferenz"
        )

        engine.release.set()
        ticker.join(timeout=5.0)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and ctrl.state != ControllerState.IDLE:
            time.sleep(0.005)
        assert ctrl.state == ControllerState.IDLE

    def test_live_tick_and_finalize_are_serialized(self) -> None:
        """Live-Tick und Finalize rufen die Engine nie gleichzeitig auf."""
        engine = ConcurrencyCountingEngine()
        source = FakeAudioSource([b"\x00" * 16000] * 8)
        typer = FakeTyper()
        delta = DeltaTyper(typer, append_space=True)
        ctrl = PushToTalkController(
            config=None,
            source_factory=lambda: source,
            engine=engine,
            delta_typer=delta,
            min_audio_s=0.1,
            asr_interval_s=0.1,
        )
        ctrl.start_recording()
        ticker = threading.Thread(
            target=lambda: [
                ctrl.run_once()
                for _ in iter(lambda: ctrl.state == ControllerState.RECORDING, False)
            ],
            daemon=True,
        )
        ticker.start()
        time.sleep(0.3)  # mehrere Live-Ticks
        ctrl.stop_recording()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and ctrl.state != ControllerState.IDLE:
            time.sleep(0.005)
        ticker.join(timeout=5.0)

        assert engine.max_active == 1, (
            f"Engine {engine.max_active}x gleichzeitig aufgerufen - "
            "_exec_lock serialisiert Live/Final nicht"
        )
        # Die finale Ausgabe stammt aus dem LETZTEN Engine-Aufruf (finalize),
        # nicht aus einem spaeter fertig gewordenen Live-Tick.
        assert delta.last_text == engine.last_text

    def test_release_feeds_exactly_capture_queue_then_source_rest(self) -> None:
        """RC-Reihenfolge: alle Queue-Chunks, dann der stop()-Rest - exakt einmal."""
        recorder = RecordingTranscriber()
        chunks = [bytes([i]) * 16000 for i in range(6)]
        source = RestAudioSource(chunks, rest=b"REST")
        ctrl = PushToTalkController(
            config=None,
            source_factory=lambda: source,
            engine=FakeEngine(),
            delta_typer=DeltaTyper(FakeTyper(), append_space=True),
            transcriber_factory=lambda *a, **kw: recorder,
        )
        ctrl.start_recording()
        time.sleep(0.3)  # Capture-Thread fuellt die Queue, KEIN run_once
        ctrl.stop_recording()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and ctrl.state != ControllerState.IDLE:
            time.sleep(0.005)

        assert b"".join(recorder.feeds) == b"".join(chunks) + b"REST", (
            "Queue-Chunks und stop()-Rest nicht exakt einmal in richtiger "
            "Reihenfolge gefuettert"
        )


class BlockingEngine:
    """Engine, deren transcribe() auf ein Event wartet (Live-Inferenz-Fake).

    Die Wartezeit ist bewusst begrenzt (2s), damit ein fehlschlagender
    Test (alt: stop_recording blockiert am Lock) nicht endlos haengt.
    """

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def transcribe(self, data: bytes, initial_prompt: str = "") -> TranscriptionResult:
        self.calls += 1
        self.entered.set()
        self.release.wait(timeout=2.0)
        words = max(1, len(data) // 16000)
        return TranscriptionResult(text=" ".join(f"w{i}" for i in range(words)))


class ConcurrencyCountingEngine:
    """Engine, die Gleichzeitigkeit der Aufruefe zaehlt."""

    def __init__(self) -> None:
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self.last_text = ""

    def transcribe(self, data: bytes, initial_prompt: str = "") -> TranscriptionResult:
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        time.sleep(0.05)
        text = f"call{self.calls}"
        self.active -= 1
        self.last_text = text
        return TranscriptionResult(text=text)


class RecordingTranscriber:
    """StreamTranscriber-Fake, der jedes feed()-Chunk protokolliert."""

    def __init__(self) -> None:
        self.feeds: list[bytes] = []
        self._buffer = bytearray()
        self.sample_rate = 16000

    def feed(self, chunk: bytes) -> None:
        self.feeds.append(bytes(chunk))
        self._buffer.extend(chunk)

    def maybe_transcribe(self):
        return None

    def finalize(self):
        return TranscriptionResult(text="fertig")


class InstantTranscriber:
    """StreamTranscriber-Fake mit sofortiger, deterministischer Finalisierung."""

    def __init__(self) -> None:
        self._buffer = b"\x00" * 16000
        self.sample_rate = 16000

    def feed(self, data: bytes) -> None:
        pass

    def maybe_transcribe(self):
        return None

    def finalize(self):
        return TranscriptionResult(text="fertig")


class TestAudioDumpModes:
    """STUFE 1: audio_dump_mode steuert den WAV-Dump (sync|async|off)."""

    def _ctrl(self, tmp_path, mode: str):
        from stt_button.config import Config

        recorder = RecordingTranscriber()
        source = RestAudioSource([b"\x00" * 16000] * 4, rest=b"")
        dump = tmp_path / "last.wav"
        ctrl = PushToTalkController(
            config=Config(audio_dump_mode=mode),
            source_factory=lambda: source,
            engine=FakeEngine(),
            delta_typer=DeltaTyper(FakeTyper(), append_space=True),
            transcriber_factory=lambda *a, **kw: recorder,
            dump_path=str(dump),
        )
        return ctrl, dump

    def _run(self, ctrl) -> None:
        ctrl.start_recording()
        time.sleep(0.2)  # Capture-Thread fuellt die Queue
        ctrl.stop_recording()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and ctrl.state != ControllerState.IDLE:
            time.sleep(0.005)

    def test_sync_default_writes_wav_before_worker_done(self, tmp_path) -> None:
        """sync (Default): Dump ist fertig, bevor der Finalizer den Zustand schliesst."""
        ctrl, dump = self._ctrl(tmp_path, "sync")
        self._run(ctrl)
        assert dump.exists(), "sync-Dump fehlt nach Finalisierung"
        assert dump.read_bytes().startswith(b"RIFF")

    def test_off_writes_no_wav(self, tmp_path) -> None:
        ctrl, dump = self._ctrl(tmp_path, "off")
        self._run(ctrl)
        assert not dump.exists(), "off darf keine WAV-Datei schreiben"

    def test_async_writes_wav_from_snapshot(self, tmp_path) -> None:
        """async: Dump laeuft im eigenen Thread und schreibt den Snapshot."""
        ctrl, dump = self._ctrl(tmp_path, "async")
        self._run(ctrl)
        # Der async-Thread darf den Prozess nicht blockieren; bis zum
        # Testende muss die Datei aber geschrieben sein (knapp bemessen).
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not dump.exists():
            time.sleep(0.01)
        assert dump.exists(), "async-Dump fehlt"
        assert dump.read_bytes().startswith(b"RIFF")


class TestLatencyTrace:
    """latency_trace=True loggt die 6 Debug-Messpunkte (Default: aus)."""

    def test_six_measurement_points_logged(self, tmp_path, caplog) -> None:
        import logging

        from stt_button.config import Config

        with caplog.at_level(logging.DEBUG, logger="stt_button.controller"):
            recorder = RecordingTranscriber()
            source = RestAudioSource([b"\x00" * 16000] * 4, rest=b"")
            ctrl = PushToTalkController(
                config=Config(latency_trace=True),
                source_factory=lambda: source,
                engine=FakeEngine(),
                delta_typer=DeltaTyper(FakeTyper(), append_space=True),
                transcriber_factory=lambda *a, **kw: recorder,
                dump_path=str(tmp_path / "last.wav"),
            )
            ctrl.start_recording()
            time.sleep(0.2)
            ctrl.stop_recording()
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and ctrl.state != ControllerState.IDLE:
                time.sleep(0.005)

        logged = [r.getMessage() for r in caplog.records]
        for point in ("stop entered", "source stopped", "dump",
                      "finalize start", "finalize end", "paste"):
            assert any(f"latency[{point}]" in m for m in logged), (
                f"Messpunkt {point!r} fehlt - geloggt: {logged}"
            )

    def test_disabled_by_default(self, tmp_path, caplog) -> None:
        import logging

        from stt_button.config import Config

        with caplog.at_level(logging.DEBUG, logger="stt_button.controller"):
            recorder = RecordingTranscriber()
            source = RestAudioSource([b"\x00" * 16000] * 4, rest=b"")
            ctrl = PushToTalkController(
                config=Config(),
                source_factory=lambda: source,
                engine=FakeEngine(),
                delta_typer=DeltaTyper(FakeTyper(), append_space=True),
                transcriber_factory=lambda *a, **kw: recorder,
                dump_path=str(tmp_path / "last.wav"),
            )
            ctrl.start_recording()
            time.sleep(0.2)
            ctrl.stop_recording()
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and ctrl.state != ControllerState.IDLE:
                time.sleep(0.005)

        assert not [r for r in caplog.records if "latency[" in r.getMessage()]
