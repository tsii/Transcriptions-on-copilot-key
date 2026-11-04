"""Wiring: assembles the application from its individual parts.

Separates composition from the actual logic so that the main loop
stays testable and the CLI stays lean.
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
from . import focus
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
    """Holds the running service together."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._listener: KeyListener | None = None
        self._controller: PushToTalkController | None = None
        self._listener_thread: threading.Thread | None = None
        self._engine = self._build_engine()

    def _build_engine(self):
        """Builds the ASR engine based on the configuration."""
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
                log.warning("dotool not available (%s) - falling back to wtype", exc)
                try:
                    return WtypeTyper()
                except TyperError:
                    return ClipboardTyper()
        try:
            return WtypeTyper()
        except TyperError:
            log.warning("wtype not available - using clipboard fallback")
            return ClipboardTyper()

    def _build_controller(self) -> PushToTalkController:
        typer = self._build_typer()
        cfg = self.config

        # Final-delta paste only with an explicit flag: then the final
        # delta is inserted via the clipboard instead of being typed
        # character by character. Without the flag the previous behavior
        # stays (final_paster=None).
        final_paster = None
        focus_probe = None
        if cfg.final_paste:
            final_paster = ClipboardPaster(
                typer,
                copy_binary=cfg.paste_binary,
                paste_key=cfg.paste_key,
            )
            # Focus gating (default of the paster path, no config option):
            # terminal/unknown -> clipboard only; browser -> typed.
            focus_probe = focus.terminal_focused

        delta = DeltaTyper(
            typer,
            append_space=cfg.append_space,
            max_revision_chars=cfg.max_revision_chars,
            final_paster=final_paster,
            focus_probe=focus_probe,
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
                # STAGE 3: hybrid whole-buffer (<= whole_buffer_until_s) ->
                # bounded window. Only active with an explicit flag.
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
                # STAGE 1: "cached" skips the final engine call on an
                # unchanged buffer; "full" = previous behavior.
                finalize_mode=cfg.finalize_mode,
                # STAGE 3: stop live ticks after this buffer length
                # (0.0 = always active, previous behavior).
                live_asr_until_s=cfg.live_asr_until_s,
                # PHASE 2 (B): adaptive live interval (0.0 = off).
                asr_interval_adaptive_k=cfg.asr_interval_adaptive_k,
                asr_interval_max_s=cfg.asr_interval_max_s,
                # Phase 2 (C): sentence-pause segmentation (0.0 = off).
                segment_pause_s=cfg.segment_pause_s,
                segment_min_s=cfg.segment_min_s,
                segment_max_s=cfg.segment_max_s,
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
        """Starts the key listener and controller and warms up the model."""
        device = resolve_device(self.config.input_device_name)
        log.info("Watching %s (%s) for keys %s",
                 device.path, device.name, self.config.key_codes)

        self._controller = self._build_controller()

        self._listener = KeyListener(
            [device],
            self.config.key_codes,
            on_press=self._on_press,
            on_release=self._on_release,
        )
        self._listener.open()

        # Offload key monitoring to its own thread: ASR inference blocks
        # the main loop, but press/release must still be processed
        # immediately, otherwise the key release during transcription
        # would be lost.
        self._listener_thread = threading.Thread(
            target=self._listener.run,
            name="key-listener",
            daemon=True,
        )
        self._listener_thread.start()

        # Load the model warm already now so that the first key press
        # can transcribe immediately without a loading delay.
        log.info("Loading model warm ...")
        self._engine.load()
        log.info("Ready. Hold the Copilot key and speak.")

    def _on_press(self, _event) -> None:
        assert self._controller is not None
        self._controller.start_recording()

    def _on_release(self, _event) -> None:
        assert self._controller is not None
        self._controller.stop_recording()

    def run_forever(self) -> None:
        """Main loop: read audio and transcribe.

        The key listener runs in its own thread so that the release is
        processed immediately even during a long ASR inference.
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
        """Shuts everything down cleanly (idempotent)."""
        if self._listener is not None:
            self._listener.close()
            self._listener = None
        if self._controller is not None:
            self._controller.shutdown()
            self._controller = None


def run(config: Config) -> int:
    """Starts the service and blocks until SIGINT/SIGTERM."""
    app = Application(config)

    # Clean shutdown on signals.
    def _handle_signal(signum, _frame):
        log.info("Received signal %s - shutting down", signum)
        app.shutdown()
        raise SystemExit(0)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    app.run_forever()
    return 0
