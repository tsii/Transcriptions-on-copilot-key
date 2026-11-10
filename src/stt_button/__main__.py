"""Command line for STT-Button.

Subcommands:
    run          Start the service (push-to-talk).
    calibrate    Interactive calibration of the trigger key.
    list-keys    List key codes and detected devices.
    test-asr     Transcribe a test WAV / live recording.
    test-typer   Type "test" into the focused window.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

from .config import ConfigError, load_config
from .typing import _capslock_leds, _capslock_state


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


# B11a: measured fallback list (brief 2026-11-05, G21). The gateway
# answers "400 Invalid model name" for "qwen38-flash-ext" - filtered.
FALLBACK_SUMMARY_MODELS = [
    "my-qwen38-flash-next",
    "work-sipgate-coding-flash",
    "work-zai-org-glm-5.3-flash",
    "work-deepseek-ai-deepseek-v4.1-flash-1017-verda",
]
_BANNED_SUMMARY_MODELS = {"qwen38-flash-ext", "my/qwen38-flash-ext"}


def _fetch_summary_models(base_url: str, token: str) -> list[str]:
    """Live model list from ``GET {base_url}/models`` (timeout 3 s)."""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/models", headers=headers)
    with urllib.request.urlopen(req, timeout=3) as resp:
        data = json.loads(resp.read().decode())
    ids = [m.get("id") for m in data.get("data", [])]
    return [m for m in ids if m]


def _write_summary_model(path, model: str) -> None:
    """Persist ``summary_model`` into the TOML config (line-based)."""
    from pathlib import Path

    target = Path(path)
    text = target.read_text() if target.is_file() else ""
    pattern = re.compile(r"^\s*summary_model\s*=.*$", re.MULTILINE)
    if pattern.search(text):
        text = pattern.sub(f'summary_model = "{model}"', text, count=1)
    else:
        text = text.rstrip("\n") + f'\nsummary_model = "{model}"\n'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)


def _cmd_run(args: argparse.Namespace) -> int:
    from .app import run

    cfg = load_config(args.config, override=vars(args) if hasattr(args, "model") else None)
    return run(cfg)


def _cmd_list_keys(args: argparse.Namespace) -> int:
    from .keys import KEY_CODES, list_input_devices

    print("Available input devices:")
    for d in list_input_devices():
        print(f"  {d.path}  {d.name}")

    if args.codes:
        print("\nKnown key codes:")
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
    print(f"Recognized: {result.text!r}")
    return 0


def _cmd_test_typer(args: argparse.Namespace) -> int:
    from .typing import ClipboardPaster, DeltaTyper

    cfg = load_config(args.config)
    typer = ClipboardTyper() if cfg.typer == "clipboard" else WtypeTyper()
    if getattr(args, "final_paste", False):
        # Tests the same finalize path as in live operation: DeltaTyper
        # with ClipboardPaster, output exclusively via finalize().
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
            f"'{args.text}' was inserted via {cfg.paste_binary} + {cfg.paste_key}."
        )
        return 0
    typer.type_text(args.text)
    if cfg.append_space:
        typer.key("space")
    print(f"'{args.text}' was typed into the focused window.")
    return 0


def _cmd_reset_capslock(args: argparse.Namespace) -> int:
    """Sets the CapsLock LED to the target state from the config.

    The target is configurable via ``capslock_led_inverted``. Ground truth
    (empirically established, ThinkPad + keyd + GNOME/Wayland): LED **off** =
    CapsLock active = inverted text; LED **on** = normal typing.
    Hence the default target here is "an".

    ``key capslock`` is a TOGGLE, the lock state cannot be reliably
    verified: hence at most three attempts.
    """
    import time as _t

    from .config import ConfigError, load_config
    from .typing import DotoolTyper

    target = "an"  # default inverted (established)
    try:
        cfg = load_config(getattr(args, "config", None))
        target = "an" if cfg.capslock_led_inverted else "aus"
    except ConfigError as exc:
        print(f"Note: configuration is ignored ({exc})")

    def _show_leds(label: str) -> None:
        leds = _capslock_leds()
        if leds:
            for device, val in sorted(leds.items()):
                print(f"  {device}: {val}")
        else:
            print(f"  (no LED found)  {label}")

    print(f"CapsLock target: {target}")
    print("LEDs per device:")
    _show_leds("before")

    typer = DotoolTyper()
    for attempt in (1, 2, 3):
        if _capslock_state() == target:
            break
        print(f"  Toggle {attempt}/3: CapsLock to {target!r} ...")
        typer._send_raw("key capslock\n")
        _t.sleep(0.12)

    final = _capslock_state()
    print(f"CapsLock LED now: {final}")
    if final == target:
        # Explain the semantics, depending on the target.
        if target == "an":
            print("OK - LED on = normal typing")
        else:
            print("OK - LED off = normal typing")
        print(
            "If the text is still inverted: press the physical CapsLock key "
            "once, or log out/in."
        )
        return 0
    print("WARNING: target state not reached.")
    print(
        "  The lock probably sits in the compositor's XKB state.\n"
        "  Fix: restart keyd (sudo systemctl restart keyd) or "
        "log out/in."
    )
    return 1


def _cmd_caps_diagnose(args: argparse.Namespace) -> int:
    """Diagnosis: CapsLock LED per device, xset q, target state and marker.

    Prints all LEDs with their state, the Caps-Lock line from ``xset q``
    (tolerant, DISPLAY=:0), the effective target state per config, and types
    a marker ``[[diagnose-OK]]`` that the user checks in the focused window.
    """
    import os

    from .config import ConfigError, load_config
    from .typing import DotoolTyper

    target = "an"
    try:
        cfg = load_config(getattr(args, "config", None))
        target = "an" if cfg.capslock_led_inverted else "aus"
    except ConfigError as exc:
        print(f"Note: configuration is ignored ({exc})")

    print("CapsLock diagnosis")
    print("-----------------")
    print("LEDs per device:")
    leds = _capslock_leds()
    if leds:
        for device, val in sorted(leds.items()):
            print(f"  {device}: {val}")
    else:
        print("  (no LED found)")

    print("\nxset q (Caps Lock line):")
    try:
        env = os.environ.copy()
        env["DISPLAY"] = ":0"
        proc = subprocess.run(["xset", "q"], capture_output=True, text=True, env=env)
        if proc.returncode == 0:
            for line in (proc.stdout or "").splitlines():
                if "Caps Lock" in line or "caps lock" in line.lower():
                    print(f"  {line.strip()}")
        else:
            print(f"  xset q failed (rc={proc.returncode})")
    except (FileNotFoundError, OSError) as exc:
        print(f"  xset q not available: {exc}")

    print(f"\nEffective target state per config: {target}")
    print(
        f"  (capslock_led_inverted -> LED {target} = normale Schreibung)"
    )

    print("\nTyping marker [[diagnose-OK]] ...")
    typer = DotoolTyper()
    typer.type_text("[[diagnose-OK]]")
    print(
        "Check in the focused window whether [[diagnose-OK]] arrived normally."
    )
    return 0


def _cmd_compare_stream(args: argparse.Namespace) -> int:
    """Compares streaming vs. whole-audio transcription.

    Uses the most recently recorded file /tmp/stt_last.wav.
    """
    import wave

    from .app import build_engine
    from .asr import StreamTranscriber
    from .config import load_config

    cfg = load_config(args.config)
    path = args.file or "/tmp/stt_last.wav"

    # Build the engine exactly like the app does (backend + compute_type).
    engine = build_engine(cfg)

    with wave.open(path, "rb") as wf:
        data = wf.readframes(wf.getnframes())

    # 1) Streaming variant (1s chunks, as in live operation)
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

    # 2) The whole audio in one pass
    whole = engine.transcribe(data)

    print(f"=== {cfg.backend} on {path} ===")
    print()
    print("[STREAMING]", fin.text)
    print()
    print("[GANZES AUDIO]", whole.text)
    print()
    print(f"(Streaming steps: {len(streaming_steps)})")
    return 0


def _setup_install_root() -> Path:
    """Package root holding config/unit templates (same rule as the launcher)."""
    here = Path(__file__).resolve().parent
    for base in (here.parent, here.parent.parent, Path("/opt/stt-button")):
        if (base / "pyproject.toml").is_file():
            return base
    return here.parent


def _cmd_setup(args: argparse.Namespace) -> int:
    """Verify the runtime (deps_gate, no venv build), seed config, guarded unit install.

    First step on a system where the slim .deb was freshly installed.
    The Python runtime is only VERIFIED (deps_gate exit levels 0/1/2/3),
    never built here.  ``~/.config/stt-button/config.toml`` is seeded only
    if missing.  The systemd user unit is written ONLY with an explicit
    ``--install-unit``; an existing unit with a different ExecStart is
    never overwritten (abort, exit 1) - protecting the running
    production unit from being silently switched.
    """
    import os
    import sys

    from pathlib import Path

    from . import deps_gate

    home = Path(os.path.expanduser("~"))

    # 1) Runtime gate (no venv build, no network) - deps_gate.main() parses
    #    sys.argv itself, so it gets a clean minimal argv.
    saved_argv = sys.argv
    sys.argv = ["deps_gate"]
    try:
        level = deps_gate.main()
    finally:
        sys.argv = saved_argv
    if level != 0:
        print(f"[1/3] Runtime check FAILED (deps_gate exit {level})."
              "\n  See the gate report above; setup aborted.", file=sys.stderr)
        return level
    print("[1/3] Runtime check passed (deps_gate exit 0).")

    # 2) Seed the configuration (only if not present)
    cfg_dir = home / ".config/stt-button"
    cfg_path = cfg_dir / "config.toml"
    if not cfg_path.exists():
        import shutil

        cfg_dir.mkdir(parents=True, exist_ok=True)
        # Template from the installed package (if present), else default.
        source = _setup_install_root() / "config.example.toml"
        if source.is_file():
            shutil.copyfile(source, cfg_path)
        else:
            cfg_path.write_text(
                "# STT-Button: defaults apply (see config.example.toml)\n")
        print(f"[2/3] Configuration created: {cfg_path}")
    else:
        print(f"[2/3] Configuration present: {cfg_path} (unchanged)")

    # 3) systemd user unit - guarded install, dry-run without the flag.
    unit_src = _setup_install_root() / "systemd" / "stt-button.service"
    unit_dir = home / ".config/systemd/user"
    unit_dst = unit_dir / "stt-button.service"
    if not unit_src.is_file():
        print("[3/3] no service unit template found (nothing to install).")
        return 0
    if Path("/opt/stt-button").exists():
        exec_target = "/usr/bin/stt-button run"
    else:
        exec_target = f"{_setup_install_root()}/.venv/bin/stt-button run"

    if unit_dst.is_file():
        cur_exec = None
        for line in unit_dst.read_text().splitlines():
            if line.strip().startswith("ExecStart="):
                cur_exec = line.split("=", 1)[1].strip()
                break
        if cur_exec is not None and cur_exec != exec_target:
            print(
                f"[3/3] REFUSING to overwrite existing user unit: {unit_dst}"
                f"\n  existing ExecStart={cur_exec}"
                f"\n  intended  ExecStart={exec_target}"
                "\n  (setup never switches an existing unit; edit it manually"
                " if this is really intended)", file=sys.stderr)
            return 1
        print(f"[3/3] user unit already present"
              f" (ExecStart={cur_exec}) - unchanged.")
        return 0

    if not getattr(args, "install_unit", False):
        print("[3/3] DRY-RUN: unit NOT written (pass --install-unit to write it).")
        print(f"  would write: {unit_dst} (ExecStart={exec_target})")
        return 0

    unit_dir.mkdir(parents=True, exist_ok=True)
    text = unit_src.read_text().replace("@EXEC@", exec_target)
    unit_dst.write_text(text)
    print(f"[3/3] user unit written: {unit_dst} (ExecStart={exec_target})")
    return 0


def _cmd_bootstrap(args: argparse.Namespace) -> int:
    """Ensures the Python runtime (venv + ML stack) is present.

    Calls ``bootstrap.run_bootstrap`` with an explicit install_root
    (default: the containing package). Invoked by the .deb launcher
    before starting the actual service (system Python, import-free).
    """
    from pathlib import Path

    from . import bootstrap

    install_root = Path(args.root) if args.root else bootstrap._default_install_root()
    ok = bootstrap.run_bootstrap(install_root=install_root)
    if ok:
        print(f"Python runtime ready ({install_root}/.venv).")
        return 0
    print("Python runtime NOT completed.", file=sys.stderr)
    return 1


def _cmd_summary_models(args: argparse.Namespace) -> int:
    """List summary models (live, with measured fallback) / --set <id>."""
    from .config import DEFAULT_CONFIG_PATH

    cfg = load_config(args.config)
    try:
        fetched = _fetch_summary_models(cfg.summary_base_url, cfg.summary_token)
        source = "live"
    except Exception:
        fetched = FALLBACK_SUMMARY_MODELS
        source = "fallback (network error)"
    models = [m for m in fetched if m not in _BANNED_SUMMARY_MODELS]

    if args.set is not None:
        if args.set not in models:
            print(f"Unknown model: {args.set}", file=sys.stderr)
            print("Choose one of:\n  " + "\n  ".join(models), file=sys.stderr)
            return 1
        path = args.config or DEFAULT_CONFIG_PATH
        _write_summary_model(path, args.set)
        cfg2 = load_config(args.config)
        print(f"summary_model = {cfg2.summary_model} (read back via load_config)")
        return 0

    print(f"Summary models ({source}) - active: {cfg.summary_model}")
    for m in models:
        mark = "  <- aktiv" if m == cfg.summary_model else ""
        print(f"  {m}{mark}")
    return 0


def _cmd_summary_test(args: argparse.Namespace) -> int:
    """Test the summary route - dry-run unless STT_SUMMARY_REAL=1."""
    import os

    cfg = load_config(args.config)
    if os.environ.get("STT_SUMMARY_REAL") != "1":
        print(f"[summary-test] dry-run (gateway only with STT_SUMMARY_REAL=1), "
              f"model={cfg.summary_model}, len={len(args.text)}")
        print(args.text)
        return 0

    from .summary import SummaryClient, SummaryCoordinator

    client = SummaryClient(
        base_url=cfg.summary_base_url,
        token=cfg.summary_token,
        model=cfg.summary_model,
        timeout_s=cfg.summary_timeout_s,
        max_chars=cfg.summary_max_read_chars,
    )
    coord = SummaryCoordinator(None, cfg, client=client)
    try:
        ok = coord.submit_summary(args.text, [])
        result = coord.wait_for_result()
    finally:
        coord.shutdown()
    print(f"submit_summary -> {ok}")
    if result is None:
        print("wait_for_result -> no result (timeout)")
        return 1
    print(f"wait_for_result -> {result}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stt-button",
                                      description="Push-to-talk dictation (Whisper)")
    parser.add_argument("--config", help="path to config.toml")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="debug output")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="start the service")
    p_run.set_defaults(func=_cmd_run)

    p_setup = sub.add_parser(
        "setup",
        help="verify runtime (no venv build), seed config, guarded unit install",
    )
    p_setup.add_argument("--install-unit", action="store_true",
                          help="write the systemd user unit (never over an "
                               "existing unit with a different ExecStart)")
    p_setup.set_defaults(func=_cmd_setup)

    p_boot = sub.add_parser(
        "bootstrap",
        help="only ensure the Python runtime (venv+ML) (for the .deb launcher)",
    )
    p_boot.add_argument("--root", help="installed package root (default: auto)")
    p_boot.set_defaults(func=_cmd_bootstrap)

    p_list = sub.add_parser("list-keys", help="list devices and key codes")
    p_list.add_argument("--codes", action="store_true", help="also show key codes")
    p_list.set_defaults(func=_cmd_list_keys)

    p_cal = sub.add_parser("calibrate", help="calibrate the trigger key")
    p_cal.set_defaults(func=_cmd_calibrate)

    p_asr = sub.add_parser("test-asr", help="test ASR")
    p_asr.add_argument("--file", help="WAV file (otherwise microphone)")
    p_asr.add_argument("--seconds", type=float, default=5.0,
                       help="recording duration (microphone only)")
    p_asr.set_defaults(func=_cmd_test_asr)

    p_typer = sub.add_parser("test-typer", help="test text output")
    p_typer.add_argument("--text", default="test", help="text to type")
    p_typer.add_argument(
        "--final-paste",
        action="store_true",
        help="test final delta via clipboard (wl-copy + paste chord)",
    )
    p_typer.set_defaults(func=_cmd_test_typer)

    p_cmp = sub.add_parser("compare-stream",
                            help="streaming vs. whole-audio transcription")
    p_cmp.add_argument("--file", help="audio file (default /tmp/stt_last.wav)")
    p_cmp.set_defaults(func=_cmd_compare_stream)

    p_caps = sub.add_parser("reset-capslock",
                            help="turn off CapsLock (fixes inverted text)")
    p_caps.set_defaults(func=_cmd_reset_capslock)

    p_diag = sub.add_parser("caps-diagnose",
                            help="diagnose CapsLock LEDs per device + target state")
    p_diag.set_defaults(func=_cmd_caps_diagnose)

    p_sum = sub.add_parser("summary-models",
                           help="list summary models (live/fallback), --set <id>")
    p_sum.add_argument("--set", metavar="ID", default=None,
                       help="set summary_model to ID and read it back")
    p_sum.set_defaults(func=_cmd_summary_models)

    p_stest = sub.add_parser("summary-test", help="test summary route (dry-run)")
    p_stest.add_argument("text", help="text to summarize")
    p_stest.set_defaults(func=_cmd_summary_test)

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