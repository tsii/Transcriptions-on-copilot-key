#!/usr/bin/env python3
"""Gate check for summary mode (B11b): reachability, model, roundtrip.

Exit code 0 = all gates green, 1 = a gate was violated (model missing,
timeout, answer == prompt), 2 = unreachable or configuration error.

Public routes only (plan §4): ``SummaryClient.chat`` for the roundtrip,
plain ``GET {summary_base_url}/models`` for reachability — no private
``_`` methods, no own queue.

Real gateway runs require ``STT_SUMMARY_REAL=1``; without the flag the
suite exercises this tool against monkeypatched HTTP only.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import requests  # noqa: E402

from stt_button.config import Config, load_config  # noqa: E402
from stt_button.summary import ModelUnavailable, SummaryClient  # noqa: E402

MODELS_TIMEOUT_S = 3.0
ROUNDTRIP_PROMPT = (
    "Fasse kurz zusammen: Der Kater sitzt auf der Matte und schlaeft."
)
EXIT_OK = 0
EXIT_GATE = 1
EXIT_FATAL = 2


def _headers(token: str) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_models(cfg: Config, session=None):
    """GET {summary_base_url}/models (timeout 3 s); returns model ids.

    Raises requests exceptions on network/HTTP failure.
    """
    s = session if session is not None else requests
    resp = s.get(
        f"{cfg.summary_base_url.rstrip('/')}/models",
        headers=_headers(cfg.summary_token),
        timeout=MODELS_TIMEOUT_S,
    )
    resp.raise_for_status()
    payload = resp.json()
    return [m.get("id", "") for m in payload.get("data", [])]


def list_models(cfg: Config, session=None) -> list[str]:
    """Report lines for ``--list-models``; marks the active model."""
    ids = fetch_models(cfg, session=session)
    lines = [f"{'* ' if m == cfg.summary_model else '  '}{m}" for m in ids]
    return lines


def check(cfg: Config, session=None) -> tuple[int, list[str]]:
    """Run all gates; returns (exit_code, report_lines)."""
    lines: list[str] = []
    # Gate 1: reachability.
    try:
        ids = fetch_models(cfg, session=session)
    except requests.exceptions.RequestException as exc:
        lines.append(f"gate 1 reachability: FAIL ({exc.__class__.__name__})")
        return EXIT_FATAL, lines
    lines.append(f"gate 1 reachability: ok ({len(ids)} models)")

    # Gate 2: configured model in the list.
    if cfg.summary_model not in ids:
        lines.append(f"gate 2 model: FAIL "
                     f"'{cfg.summary_model}' not in model list")
        return EXIT_GATE, lines
    lines.append(f"gate 2 model: ok '{cfg.summary_model}' active")

    # Gate 3: roundtrip via the public route SummaryClient.chat.
    client = SummaryClient(
        base_url=cfg.summary_base_url,
        token=cfg.summary_token,
        model=cfg.summary_model,
        timeout_s=cfg.summary_timeout_s,
    )
    start = time.monotonic()
    try:
        answer = client.chat(ROUNDTRIP_PROMPT)
    except ModelUnavailable as exc:
        msg = str(exc)
        fatal = "connection" in msg
        lines.append(f"gate 3 roundtrip: FAIL ({msg})")
        return (EXIT_FATAL if fatal else EXIT_GATE), lines
    elapsed = time.monotonic() - start
    if answer == ROUNDTRIP_PROMPT:
        lines.append("gate 3 roundtrip: FAIL (answer equals prompt)")
        return EXIT_GATE, lines
    if elapsed >= cfg.summary_timeout_s:
        lines.append(f"gate 3 roundtrip: FAIL "
                     f"(elapsed {elapsed:.1f}s >= {cfg.summary_timeout_s}s)")
        return EXIT_GATE, lines
    lines.append(f"gate 3 roundtrip: ok (elapsed {elapsed:.1f}s, "
                 f"answer_len={len(answer)})")
    return EXIT_OK, lines


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="path to config file")
    parser.add_argument("--list-models", action="store_true",
                        help="print the model list, marking the active one")
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except Exception as exc:  # config errors are fatal, not gate failures
        print(f"config error: {exc}")
        raise SystemExit(EXIT_FATAL)

    if args.list_models:
        try:
            for line in list_models(cfg):
                print(line)
        except requests.exceptions.RequestException as exc:
            print(f"unreachable: {exc.__class__.__name__}")
            raise SystemExit(EXIT_FATAL)
        raise SystemExit(EXIT_OK)

    if os.environ.get("STT_SUMMARY_REAL") != "1":
        print("note: STT_SUMMARY_REAL not set; "
              "real gateway runs require STT_SUMMARY_REAL=1")
    code, lines = check(cfg)
    for line in lines:
        print(line)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
