"""Kommandozeile fuer STT-Button.

Subcommands:
    run          Startet den Dienst (Push-to-Talk).
    calibrate    Interaktive Kalibrierung der Trigger-Taste.
    list-keys    Listet Keycodes und erkannte Devices.
    test-asr     Transkribiert eine Test-WAV/live-Aufnahme.
    test-typer   Tippt "test" ins fokussierte Fenster.
"""
from __future__ import annotations

import argparse
import logging
import sys

from .config import Config, ConfigError, load_config


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _cmd_run(args: argparse.Namespace) -> int:
    from .app import run

    cfg = load_config(args.config, override=vars(args) if hasattr(args, "model") else None)
    return run(cfg)


def _cmd_list_keys(args: argparse.Namespace) -> int:
    from .keys import KEY_CODES, list_input_devices

    print("Verfuegbare input-Devices:")
    for d in list_input_devices():
        print(f"  {d.path}  {d.name}")

    if args.codes:
        print("\nBekannte Keycodes:")
        for name in sorted(KEY_CODES):
            print(f"  {name} = {KEY_CODES[name]}")
    return 0


def _cmd_calibrate(args: argparse.Namespace) -> int:
    from .calibrate import calibrate

    return calibrate(args)


def _cmd_test_asr(args: argparse.Namespace) -> int:
    import wave

    from .asr import WhisperEngine

    cfg = load_config(args.config)
    engine = WhisperEngine(
        cfg.model, language=cfg.language, compute_type=cfg.compute_type,
        cpu_threads=cfg.cpu_threads,
    )
    engine.load()

    if args.file:
        with wave.open(args.file, "rb") as wf:
            data = wf.readframes(wf.getnframes())
    else:
        from .audio import PwRecordSource

        src = PwRecordSource(cfg.audio_source)
        src.start()
        import time

        time.sleep(args.seconds)
        data = b""
        while True:
            chunk = src.read(block=False)
            if not chunk:
                break
            data += chunk
        data += src.stop()

    result = engine.transcribe(data)
    print(f"Erkannt: {result.text!r}")
    return 0


def _cmd_test_typer(args: argparse.Namespace) -> int:
    from .typing import ClipboardTyper, WtypeTyper

    cfg = load_config(args.config)
    typer = ClipboardTyper() if cfg.typer == "clipboard" else WtypeTyper()
    typer.type_text(args.text)
    if cfg.append_space:
        typer.key("space")
    print(f"'{args.text}' wurde in das fokussierte Fenster getippt.")
    return 0


def _cmd_reset_capslock(args: argparse.Namespace) -> int:
    """Schaltet CapsLock aus (behebt invertierten Diktat-Text).

    Nuetzlich, wenn der Text invertiert erscheint ("Das" -> "dAS"), weil
    CapsLock an ist - auch wenn die Tastatur-LED etwas anderes anzeigt.
    """
    from .typing import DotoolTyper, _capslock_state

    state = _capslock_state()
    print(f"CapsLock laut LED: {state}")

    typer = DotoolTyper()
    # Immer einmal toggeln, wenn der Zustand an ODER unbekannt ist - bei
    # bekanntem 'aus' nur, wenn der Nutzer es explizit will.
    if state == "an":
        typer._send_raw("key capslock\n")
        import time as _t
        _t.sleep(0.1)
        print(f"Nach Reset: {_capslock_state()}")
        return 0

    # Zustand 'aus' oder '?': einmal toggeln und zurueck, um einen
    # etwaigen Desync zu beheben.
    typer._send_raw("key capslock\n")
    import time as _t
    _t.sleep(0.1)
    typer._send_raw("key capslock\n")
    _t.sleep(0.1)
    print(f"Nach Doppel-Toggle (Desync-Reset): {_capslock_state()}")
    print("Falls der Text weiter invertiert kommt: CapsLock-Taste manuell pruefen.")
    return 0


def _cmd_compare_stream(args: argparse.Namespace) -> int:
    """Vergleicht Streaming- vs. Ganz-Audio-Transkription.

    Nutzt die zuletzt aufgenommene Datei /tmp/stt_last.wav.
    """
    import wave

    from .asr import ParakeetEngine, QwenEngine, StreamTranscriber, WhisperEngine
    from .config import load_config

    cfg = load_config(args.config)
    path = args.file or "/tmp/stt_last.wav"

    # Engine anhand des Backends bauen
    if cfg.backend == "qwen":
        engine = QwenEngine(cfg.model, language=cfg.language)
    elif cfg.backend == "parakeet":
        engine = ParakeetEngine(cfg.model, language=cfg.language)
    else:
        engine = WhisperEngine(
            cfg.model, language=cfg.language, compute_type=cfg.compute_type
        )

    with wave.open(path, "rb") as wf:
        data = wf.readframes(wf.getnframes())

    # 1) Streaming-Variante (1s-Chunks, wie im Live-Betrieb)
    st = StreamTranscriber(engine, interval_s=1.0, min_audio_s=1.0,
                           sample_rate=16000, commit_agreements=2)
    CH = 32000
    streaming_steps = []
    for i in range(0, len(data), CH):
        st.feed(data[i:i + CH])
        r = st.maybe_transcribe()
        if r is not None and r.text:
            streaming_steps.append(r.text)
    fin = st.finalize()

    # 2) Ganzes Audio am Stueck
    whole = engine.transcribe(data)

    print(f"=== {cfg.backend} auf {path} ===")
    print()
    print("[STREAMING]", fin.text)
    print()
    print("[GANZES AUDIO]", whole.text)
    print()
    print(f"(Streaming-Schritte: {len(streaming_steps)})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stt-button",
                                      description="Push-to-Talk Diktat (Whisper)")
    parser.add_argument("--config", help="Pfad zur config.toml")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Debug-Ausgabe")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="Dienst starten")
    p_run.set_defaults(func=_cmd_run)

    p_list = sub.add_parser("list-keys", help="Devices und Keycodes auflisten")
    p_list.add_argument("--codes", action="store_true", help="auch Keycodes zeigen")
    p_list.set_defaults(func=_cmd_list_keys)

    p_cal = sub.add_parser("calibrate", help="Trigger-Taste kalibrieren")
    p_cal.set_defaults(func=_cmd_calibrate)

    p_asr = sub.add_parser("test-asr", help="ASR testen")
    p_asr.add_argument("--file", help="WAV-Datei (sonst Mikrofon)")
    p_asr.add_argument("--seconds", type=float, default=5.0,
                       help="Aufnahmedauer (nur Mikrofon)")
    p_asr.set_defaults(func=_cmd_test_asr)

    p_typer = sub.add_parser("test-typer", help="Textausgabe testen")
    p_typer.add_argument("--text", default="test", help="Text zum Tippen")
    p_typer.set_defaults(func=_cmd_test_typer)

    p_cmp = sub.add_parser("compare-stream",
                            help="Streaming vs. Ganz-Audio-Transkription")
    p_cmp.add_argument("--file", help="Audiodatei (default /tmp/stt_last.wav)")
    p_cmp.set_defaults(func=_cmd_compare_stream)

    p_caps = sub.add_parser("reset-capslock",
                            help="CapsLock ausschalten (behebt invertierten Text)")
    p_caps.set_defaults(func=_cmd_reset_capslock)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"Konfigurationsfehler: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())