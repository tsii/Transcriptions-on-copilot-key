"""Verdrahtung: baut die Anwendung aus den Einzelteilen zusammen.

Trennt die Komposition von der eigentlichen Logik, damit der Hauptlauf
testbar bleibt und die CLI schlank ist.
"""
from __future__ import annotations

import logging
import signal
import threading

from .audio import PwRecordSource
from .asr import ParakeetEngine, QwenEngine, StreamTranscriber, WhisperEngine
from .buffered import BufferedTranscriber
from .config import Config
from .controller import PushToTalkController
from .keys import KeyListener, resolve_device
from .typing import (
    ClipboardPaster,
    ClipboardTyper,
    DeltaTyper,
    DotoolTyper,
    TyperError,
    WtypeTyper,
)
from .vad import SileroVadGate
from .windowed import WindowedTranscriber

log = logging.getLogger(__name__)


class Application:
    """Haelt den laufenden Dienst zusammen."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._listener: KeyListener | None = None
        self._controller: PushToTalkController | None = None
        self._listener_thread: threading.Thread | None = None
        self._engine = self._build_engine()

    def _build_engine(self):
        """Baut die ASR-Engine anhand der Konfiguration."""
        cfg = self.config
        backend = getattr(cfg, "backend", "whisper")
        if backend == "parakeet":
            return ParakeetEngine(
                cfg.model,
                language=cfg.language,
                compute_type=cfg.compute_type,
                cpu_threads=cfg.cpu_threads,
            )
        if backend == "qwen":
            return QwenEngine(
                cfg.model,
                language=cfg.language,
                compute_type=cfg.compute_type,
                cpu_threads=cfg.cpu_threads,
            )
        return WhisperEngine(
            cfg.model,
            language=cfg.language,
            compute_type=cfg.compute_type,
            cpu_threads=cfg.cpu_threads,
        )

    def _build_typer(self):
        if self.config.typer == "clipboard":
            return ClipboardTyper()
        if self.config.typer == "dotool":
            try:
                return DotoolTyper(
                    xkb_layout=self.config.xkb_layout,
                    xkb_variant=self.config.xkb_variant,
                    typedelay_ms=self.config.typedelay_ms,
                    typehold_ms=self.config.typehold_ms,
                    capslock_autocorrect=self.config.capslock_autocorrect,
                    capslock_led_inverted=self.config.capslock_led_inverted,
                )
            except TyperError as exc:
                log.warning("dotool nicht verfuegbar (%s) - Fallback auf wtype", exc)
                try:
                    return WtypeTyper()
                except TyperError:
                    return ClipboardTyper()
        try:
            return WtypeTyper()
        except TyperError:
            log.warning("wtype nicht verfuegbar - nutze Clipboard-Fallback")
            return ClipboardTyper()

    def _build_controller(self) -> PushToTalkController:
        typer = self._build_typer()
        cfg = self.config

        # Final-Delta-Paste nur bei explizitem Flag: dann wird das finale
        # Delta per Zwischenablage eingefuegt statt zeichenweise getippt.
        # Ohne Flag bleibt es beim bisherigen Verhalten (final_paster=None).
        final_paster = None
        if cfg.final_paste:
            final_paster = ClipboardPaster(
                typer,
                copy_binary=cfg.paste_binary,
                paste_key=cfg.paste_key,
            )

        delta = DeltaTyper(
            typer,
            append_space=cfg.append_space,
            max_revision_chars=cfg.max_revision_chars,
            final_paster=final_paster,
            latency_trace=cfg.latency_trace,
            live_typing_mode=cfg.live_typing_mode,
        )

        def make_source() -> PwRecordSource:
            return PwRecordSource(
                cfg.audio_source,
                rate=cfg.sample_rate,
            )

        def make_transcriber(engine, interval_s, min_audio_s):
            if cfg.streaming_mode == "windowed":
                return WindowedTranscriber(
                    engine,
                    interval_s=interval_s,
                    min_audio_s=min_audio_s,
                    sample_rate=cfg.sample_rate,
                    commit_agreements=cfg.commit_agreements,
                    vad=SileroVadGate(),
                )
            if cfg.streaming_mode == "buffered":
                # STUFE 3: Hybrid Volllpuffer (<= whole_buffer_until_s) ->
                # bounded Fenster. NUR bei explizitem Flag aktiv.
                return BufferedTranscriber(
                    engine,
                    interval_s=interval_s,
                    min_audio_s=min_audio_s,
                    whole_buffer_until_s=cfg.buffered_whole_buffer_until_s,
                    left_context_s=cfg.buffered_left_context_s,
                    chunk_s=cfg.buffered_chunk_s,
                    right_context_s=cfg.buffered_right_context_s,
                    commit_agreements=cfg.commit_agreements,
                    max_final_window_s=cfg.buffered_max_final_window_s,
                    sample_rate=cfg.sample_rate,
                    vad=SileroVadGate(),
                )
            return StreamTranscriber(
                engine,
                interval_s=interval_s,
                min_audio_s=min_audio_s,
                sample_rate=cfg.sample_rate,
                commit_agreements=cfg.commit_agreements,
                vad=SileroVadGate(),
                # STUFE 1: "cached" ueberspringt den finalen Engine-Call bei
                # unveraendertem Puffer; "full" = bisheriges Verhalten.
                finalize_mode=cfg.finalize_mode,
                # STUFE 3: Live-Ticks nach dieser Pufferlaenge einstellen
                # (0.0 = immer aktiv, bisheriges Verhalten).
                live_asr_until_s=cfg.live_asr_until_s,
            )

        return PushToTalkController(
            config=self.config,
            source_factory=make_source,
            engine=self._engine,
            delta_typer=delta,
            min_audio_s=cfg.min_audio_s,
            asr_interval_s=cfg.asr_interval_s,
            max_record_s=cfg.max_record_s,
            transcriber_factory=make_transcriber,
        )

    def start(self) -> None:
        """Startet Key-Listener und Controller und laedt das Modell warm."""
        device = resolve_device(self.config.input_device_name)
        log.info("Ueberwache %s (%s) auf Tasten %s",
                 device.path, device.name, self.config.key_codes)

        self._controller = self._build_controller()

        self._listener = KeyListener(
            [device],
            self.config.key_codes,
            on_press=self._on_press,
            on_release=self._on_release,
        )
        self._listener.open()

        # Key-Monitoring in einen eigenen Thread auslagern: Die ASR-Inferenz
        # blockiert die Hauptschleife, aber Press/Release muessen trotzdem
        # sofort verarbeitet werden, sonst geht das Loslassen der Taste
        # waehrend der Transkription verloren.
        self._listener_thread = threading.Thread(
            target=self._listener.run,
            name="key-listener",
            daemon=True,
        )
        self._listener_thread.start()

        # Modell bereits jetzt warm laden, damit der erste Tastendruck
        # ohne Ladeverzoegerung sofort transkribieren kann.
        log.info("Lade Modell warm ...")
        self._engine.load()
        log.info("Bereit. Bitte Copilot-Taste halten und sprechen.")

    def _on_press(self, _event) -> None:
        assert self._controller is not None
        self._controller.start_recording()

    def _on_release(self, _event) -> None:
        assert self._controller is not None
        self._controller.stop_recording()

    def run_forever(self) -> None:
        """Hauptschleife: Audio lesen und transkribieren.

        Der Key-Listener laeuft in einem eigenen Thread, damit das Loslassen
        auch waehrend einer langen ASR-Inferenz sofort verarbeitet wird.
        """
        self.start()
        assert self._controller is not None

        try:
            while True:
                self._controller.run_once()
                self._controller.wait_for_work()
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        """Beendet alles sauber (idempotent)."""
        if self._listener is not None:
            self._listener.close()
            self._listener = None
        if self._controller is not None:
            self._controller.shutdown()
            self._controller = None


def run(config: Config) -> int:
    """Startet den Dienst und blockiert bis SIGINT/SIGTERM."""
    app = Application(config)

    # Sauberer Shutdown bei Signalen.
    def _handle_signal(signum, _frame):
        log.info("Signal %s erhalten - beende", signum)
        app.shutdown()
        raise SystemExit(0)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    app.run_forever()
    return 0