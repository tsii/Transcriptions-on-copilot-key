#!/usr/bin/env python3
"""Probe the configured model primaries/fallbacks and report availability flips.

Reads the fallback map from ``.pi/model-fallback.json`` (next to this tool's
parent repo), probes every primary and fallback with a minimal completion
request, writes the state to ``.pi/state/model-availability.json`` and prints
one line per model plus ``CHANGED`` lines for state flips since the last run.

Policy (operator, 2026-11-05, AGENTS.md "Model fallback"): a delegation uses
the primary; if the primary is DOWN it uses the mapped fallback and says so;
as soon as a probe sees the primary UP again, it switches BACK.

API keys are read from ``~/.pi/agent/auth.json`` at runtime and are NEVER
printed, logged or written to the state file.

Usage:
    nice -n 19 .venv/bin/python tools/model_fallback_probe.py            # probe all
    nice -n 19 .venv/bin/python tools/model_fallback_probe.py --quiet    # only CHANGED lines
    nice -n 19 .venv/bin/python tools/model_fallback_probe.py --json     # machine-readable

Exit code: 0 = no flips, 10 = at least one flip (so a scheduler can branch),
2 = the map/state file could not be read.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
MAP_PATH = ROOT / ".pi" / "model-fallback.json"
STATE_PATH = ROOT / ".pi" / "state" / "model-availability.json"
AUTH_PATH = Path.home() / ".pi" / "agent" / "auth.json"
PROBE_PROMPT = "Reply with exactly: OK"
DEFAULT_TIMEOUT = 25.0


def _api_key(provider: str) -> str | None:
    """Returns the key for ``provider`` from auth.json (never printed)."""
    try:
        data = json.loads(AUTH_PATH.read_text(encoding="utf-8"))
        entry = data.get(provider) or {}
        key = entry.get("key")
        if key:
            return str(key)
    except (OSError, ValueError):
        pass
    env = os.environ.get(f"{provider.upper()}_API_KEY")
    return env or None


def _request(endpoint: dict, model: str, key: str, timeout: float) -> tuple[bool, int, float, str]:
    """Sends one minimal completion. Returns (ok, http_status, seconds, note)."""
    base = str(endpoint.get("baseUrl", "")).rstrip("/")
    if endpoint.get("api") == "responses":
        url, payload = f"{base}/responses", {"model": model, "input": PROBE_PROMPT,
                                             "max_output_tokens": 8}
    else:
        url, payload = f"{base}/chat/completions", {
            "model": model, "messages": [{"role": "user", "content": PROBE_PROMPT}],
            "max_tokens": 8,
        }
    t0 = time.monotonic()
    try:
        resp = requests.post(url, json=payload, timeout=timeout,
                             headers={"Authorization": f"Bearer {key}",
                                      "Content-Type": "application/json"})
    except requests.exceptions.Timeout:
        return False, 0, time.monotonic() - t0, "timeout"
    except requests.exceptions.RequestException as exc:
        return False, 0, time.monotonic() - t0, type(exc).__name__
    dt = time.monotonic() - t0
    note = ""
    if resp.status_code >= 400:
        note = (resp.text or "")[:120].replace("\n", " ")
    return resp.status_code == 200, resp.status_code, dt, note


def probe(map_path: Path = MAP_PATH, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Probes every model in the map; returns {model_id: state dict}."""
    cfg = json.loads(map_path.read_text(encoding="utf-8"))
    endpoints = cfg["endpoints"]
    seen: dict[str, dict] = {}
    for entry in cfg["map"]:
        for role in ("primary", "fallback"):
            mid = entry[role]
            if mid in seen:
                continue
            provider = mid.split("/", 1)[0]
            model = mid.split("/", 1)[1]
            endpoint = endpoints.get(provider)
            key = _api_key(provider)
            if endpoint is None or key is None:
                seen[mid] = {"ok": False, "http": 0, "seconds": 0.0,
                             "note": "no endpoint or no api key configured"}
                continue
            ok, http, seconds, note = _request(endpoint, model, key, timeout)
            seen[mid] = {"ok": ok, "http": http, "seconds": round(seconds, 2),
                         "note": note, "probe": endpoint.get("api", "chat")}
    return seen


def diff(prev: dict, now: dict) -> list[tuple[str, str, str]]:
    """Returns [(model, 'UP->DOWN'|'DOWN->UP', role_hint)] for changed models."""
    out = []
    for mid, st in now.items():
        old = prev.get(mid)
        if old is None or "ok" not in old:
            continue
        if bool(old["ok"]) != bool(st["ok"]):
            out.append((mid,
                        ("UP->DOWN" if old["ok"] else "DOWN->UP"),
                        "primary" if st.get("is_primary") else ""))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--quiet", action="store_true", help="print CHANGED lines only")
    ap.add_argument("--json", action="store_true", help="print machine-readable JSON")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    args = ap.parse_args(argv)

    if not MAP_PATH.is_file():
        print(f"missing map file: {MAP_PATH}", file=sys.stderr)
        return 2

    prev: dict = {}
    if STATE_PATH.is_file():
        try:
            prev = json.loads(STATE_PATH.read_text(encoding="utf-8")).get("models", {})
        except (OSError, ValueError):
            prev = {}

    cfg = json.loads(MAP_PATH.read_text(encoding="utf-8"))
    primaries = {e["primary"] for e in cfg["map"]}
    now = probe(MAP_PATH, args.timeout)
    for mid, st in now.items():
        st["is_primary"] = mid in primaries

    flips = diff(prev, now)
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps({"ts": time.time(), "models": now}, indent=2),
                          encoding="utf-8")

    if args.json:
        print(json.dumps({"models": now, "flips": flips}, indent=2))
    else:
        for mid, st in now.items():
            tag = "primary " if st["is_primary"] else "fallback"
            mark = "UP  " if st["ok"] else "DOWN"
            if args.quiet:
                continue
            extra = "" if st["ok"] else f"  http={st['http']} {st['note'][:70]}"
            print(f"{mark} {tag:8s} {mid:36s} {st['seconds']:>5.2f}s{extra}")
        for mid, kind, _ in flips:
            print(f"CHANGED {mid} {kind}")

    return 10 if flips else 0


if __name__ == "__main__":
    sys.exit(main())
