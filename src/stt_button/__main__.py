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
import subprocess
import sys

from .config import ConfigError, load_config
from .typing import _capslock_leds, _capslock_state


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
    from .typing import ClipboardPaster, DeltaTyper

    cfg = load_config(args.config)
    typer = ClipboardTyper() if cfg.typer == "clipboard" else WtypeTyper()
    if getattr(args, "final_paste", False):
        # Testet denselben Finalize-Pfad wie im Live-Betrieb: DeltaTyper
        # mit ClipboardPaster, Ausgabe ausschliesslich ueber finalize().
        paster = ClipboardPaster(
            typer,
            copy_binary=cfg.paste_binary,
            paste_key=cfg.paste_key,
        )
        delta = DeltaTyper(
            typer,
            append_space=cfg.append_space,
            final_paster=paster,
        )
        delta.finalize(args.text)
        print(
            f"'{args.text}' wurde per {cfg.paste_binary} + {cfg.paste_key} "
            f"eingefuegt."
        )
        return 0
    typer.type_text(args.text)
    if cfg.append_space:
        typer.key("space")
    print(f"'{args.text}' wurde in das fokussierte Fenster getippt.")
    return 0


def _cmd_reset_capslock(args: argparse.Namespace) -> int:
    """Setzt die CapsLock-LED auf den Zielzustand aus der Config.

    Das Ziel ist konfigurierbar ueber ``capslock_led_inverted``. Ground Truth
    (empirisch belegt, ThinkPad + keyd + GNOME/Wayland): LED **aus** =
    CapsLock aktiv = invertierter Text; LED **an** = normale Schreibung.
    Daher ist das Default-Ziel hier "an".

    ``key capslock`` ist ein TOGGLE, der Lock-State ist nicht zuverlaessig
    verifizierbar: deshalb hoechstens drei Versuche.
    """
    import time as _t

    from .config import ConfigError, load_config
    from .typing import DotoolTyper

    target = "an"  # Default invertiert (belegt)
    try:
        cfg = load_config(getattr(args, "config", None))
        target = "an" if cfg.capslock_led_inverted else "aus"
    except ConfigError as exc:
        print(f"Hinweis: Konfiguration wird ignoriert ({exc})")

    def _show_leds(label: str) -> None:
        leds = _capslock_leds()
        if leds:
            for device, val in sorted(leds.items()):
                print(f"  {device}: {val}")
        else:
            print(f"  (keine LED gefunden)  {label}")

    print(f"CapsLock-Ziel: {target}")
    print("LEDs pro Device:")
    _show_leds("vorher")

    typer = DotoolTyper()
    for attempt in (1, 2, 3):
        if _capslock_state() == target:
            break
        print(f"  Toggle {attempt}/3: CapsLock auf {target!r} ...")
        typer._send_raw("key capslock\n")
        _t.sleep(0.12)

    final = _capslock_state()
    print(f"CapsLock-LED jetzt: {final}")
    if final == target:
        # Semantik erklaeren, abhaengig vom Ziel.
        if target == "an":
            print("OK - LED an = normale Schreibung")
        else:
            print("OK - LED aus = normale Schreibung")
        print(
            "Falls Text weiter invertiert: einmal physische CapsLock-Taste "
            "druecken oder ab-/anmelden."
        )
        return 0
    print("WARNUNG: Zielzustand nicht erreicht.")
    print(
        "  Der Lock sitzt vermutlich im XKB-State des Compositors.\n"
        "  Abhilfe: keyd neu starten (sudo systemctl restart keyd) "
        "oder ab-/anmelden."
    )
    return 1


def _cmd_caps_diagnose(args: argparse.Namespace) -> int:
    """Diagnose: CapsLock-LED pro Device, xset q, Zielzustand und Marker.

    Liefert alle LEDs samt Zustand, die Caps-Lock-Zeile aus ``xset q``
    (tolerant, DISPLAY=:0), den gueltigen Zielzustand laut Config und tippt
    einen Marker ``[[diagnose-OK]]``, den der Nutzer im Fokusfenster prueft.
    """
    import os

    from .config import ConfigError, load_config
    from .typing import DotoolTyper

    target = "an"
    try:
        cfg = load_config(getattr(args, "config", None))
        target = "an" if cfg.capslock_led_inverted else "aus"
    except ConfigError as exc:
        print(f"Hinweis: Konfiguration wird ignoriert ({exc})")

    print("CapsLock-Diagnose")
    print("-----------------")
    print("LEDs pro Device:")
    leds = _capslock_leds()
    if leds:
        for device, val in sorted(leds.items()):
            print(f"  {device}: {val}")
    else:
        print("  (keine LED gefunden)")

    print("\nxset q (Caps-Lock-Zeile):")
    try:
        env = os.environ.copy()
        env["DISPLAY"] = ":0"
        proc = subprocess.run(["xset", "q"], capture_output=True, text=True, env=env)
        if proc.returncode == 0:
            for line in (proc.stdout or "").splitlines():
                if "Caps Lock" in line or "caps lock" in line.lower():
                    print(f"  {line.strip()}")
        else:
            print(f"  xset q fehlgeschlagen (rc={proc.returncode})")
    except (FileNotFoundError, OSError) as exc:
        print(f"  xset q nicht verfuegbar: {exc}")

    print(f"\nGueltiger Zielzustand laut Config: {target}")
    print(
        f"  (capslock_led_inverted -> LED {target} = normale Schreibung)"
    )

    print("\nTippe Marker [[diagnose-OK]] ...")
    typer = DotoolTyper()
    typer.type_text("[[diagnose-OK]]")
    print(
        "Pruefe im Fokusfenster, ob [[diagnose-OK]] normal angekommen ist."
    )
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


def _cmd_setup(args: argparse.Namespace) -> int:
    """Installiert die Runtime + Konfiguration + User-Service-Unit.

    Ist der Erste-Schritt auf einem System, auf dem das schlanke .deb neu
    installiert wurde: baut die venv samt ML-Stack (via bootstrap), seedet
    ``~/.config/stt-button/config.toml`` (nur, wenn sie fehlt), und
    installiert die systemd-User-Unit. Alles idempotent — erneuter Lauf ist
    ein No-op, solange die venv fertig ist.
    """
    import os
    import shutil
    import sys

    from pathlib import Path

    from . import bootstrap

    # 1) Python-Runtime (venv + torch[cpu] + ML-Stack) — pro-User-venv,
    #    dasselbe Default wie der .deb-Launcher (bootstrap.run_bootstrap).
    venv_dir = Path(os.path.expanduser("~/.local/stt-button/venv"))
    if bootstrap.probe_venv(venv_dir):
        print(f"[1/3] Python-Runtime schon bereit ({venv_dir}) — übersprungen.")
        bootstrap.mark_ready(venv_dir)
    elif not bootstrap.run_bootstrap():
        print(
            "Bootstrap: Python-Runtime NICHT fertiggestellt."
            "\n  Pruefe die o. Zeilen, evtl. fehlende python3-venv",
            "  oder Netzwerk (torch vom PyTorch-CPU-Index).",
            file=sys.stderr,
        )
        return 1
    else:
        print("[1/3] Python-Runtime bereit (venv + ML-Stack).")

    # 2) Konfiguration seeden (nur wenn nicht vorhanden)
    cfg_dir = Path(os.path.expanduser("~/.config/stt-button"))
    cfg_path = cfg_dir / "config.toml"
    if not cfg_path.exists():
        cfg_dir.mkdir(parents=True, exist_ok=True)
        # Vorlage aus dem installierten Paket (falls da), sonst Default.
        source = bootstrap._default_install_root() / "config.example.toml"
        if source.is_file():
            shutil.copyfile(source, cfg_path)
        else:
            cfg_path.write_text("# STT-Button: Defaults gelten (siehe config.example.toml)\n")
        seeded = True
        print(f"[2/3] Konfiguration angelegt: {cfg_path}")
    else:
        print(f"[2/3] Konfiguration vorhanden: {cfg_path} (unverändert)")

    # 3) User-Service-Unit installieren (Deployment: /opt/stt-button)
    unit_src = bootstrap._default_install_root() / "systemd" / "stt-button.service"
    unit_dir = Path(os.path.expanduser("~/.config/systemd/user"))
    unit_dst = unit_dir / "stt-button.service"
    if unit_src.is_file():
        unit_dir.mkdir(parents=True, exist_ok=True)
        # @EXEC@ ersetzen: im .deb-Lauf zeigt die Unit auf den Launcher
        # /usr/bin/stt-button; im Source-Tree auf das Projekt (dev-venv).
        if Path("/opt/stt-button").exists():
            exec_target = "/usr/bin/stt-button run"
        else:
            root = bootstrap._default_install_root()
            exec_target = f"{root}/.venv/bin/stt-button run"
        text = unit_src.read_text().replace("@EXEC@", exec_target)
        unit_dst.write_text(text)
        print(f"[3/3] User-Unit geschrieben: {unit_dst} (ExecStart={exec_target})")
    else:
        print("[3/3] Keine Service-Unit installiert (keine Vorlage).")

    print("")
    print("Fertig. Naechste Schritte:")
    print(f"  1) Config anpassen:      ${cfg_path}")
    print("  2) Einheit laden+starten: systemctl --user daemon-reload")
    print("     systemctl --user start stt-button")
    print("     systemctl --user enable --now stt-button   (optional, Autostart)")
    print("  3) Log:                   journalctl --user -u stt-button -f")
    return 0


def _cmd_bootstrap(args: argparse.Namespace) -> int:
    """Stellt die Python-Runtime (venv + ML-Stack) sicher.

    Ruft ``bootstrap.run_bootstrap`` mit einem expliziten install_root
    (Default: das enthaltende Paket). Wird vom .deb-Launcher vor dem
    Start des eigentlichen Diensts aufgerufen (System-Python, importfrei).
    """
    from pathlib import Path

    from . import bootstrap

    install_root = Path(args.root) if args.root else bootstrap._default_install_root()
    ok = bootstrap.run_bootstrap(install_root=install_root)
    if ok:
        print(f"Python-Runtime bereit ({install_root}/.venv).")
        return 0
    print("Python-Runtime NICHT fertiggestellt.", file=sys.stderr)
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stt-button",
                                      description="Push-to-Talk Diktat (Whisper)")
    parser.add_argument("--config", help="Pfad zur config.toml")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Debug-Ausgabe")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="Dienst starten")
    p_run.set_defaults(func=_cmd_run)

    p_setup = sub.add_parser(
        "setup",
        help="Runtime installieren (venv+ML), Config seeden, User-Unit setzen",
    )
    p_setup.set_defaults(func=_cmd_setup)

    p_boot = sub.add_parser(
        "bootstrap",
        help="Nur die Python-Runtime (venv+ML) sicherstellen (fuer den .deb-Launcher)",
    )
    p_boot.add_argument("--root", help="installiertes Paket-Root (Default: auto)")
    p_boot.set_defaults(func=_cmd_bootstrap)

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
    p_typer.add_argument(
        "--final-paste",
        action="store_true",
        help="Finales Delta per Zwischenablage (wl-copy + Paste-Chord) testen",
    )
    p_typer.set_defaults(func=_cmd_test_typer)

    p_cmp = sub.add_parser("compare-stream",
                            help="Streaming vs. Ganz-Audio-Transkription")
    p_cmp.add_argument("--file", help="Audiodatei (default /tmp/stt_last.wav)")
    p_cmp.set_defaults(func=_cmd_compare_stream)

    p_caps = sub.add_parser("reset-capslock",
                            help="CapsLock ausschalten (behebt invertierten Text)")
    p_caps.set_defaults(func=_cmd_reset_capslock)

    p_diag = sub.add_parser("caps-diagnose",
                            help="CapsLock-LEDs pro Device + Zielzustand diagnostizieren")
    p_diag.set_defaults(func=_cmd_caps_diagnose)

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