# PLAN: Focus-gated final paste (STT-Button)

Repo: /home/salmon/pi-web-ui/STT-Button. TDD (P3): write tests first and record RED, then implement until GREEN.
Allowed files: `src/stt_button/typing.py`, `src/stt_button/app.py`, `tests/test_typing.py`, `tests/test_app_wiring.py`.
(Intended location was /tmp/plan_clipboard.md; the session policy blocks writes outside the workspace, so the plan is stored here. Not committed.)

## DO NOT TOUCH
- `src/stt_button/controller.py`, `tests/test_controller.py` (parallel chain, incl. `max_queue_depth`; both are already modified in the working tree)
- `src/stt_button/config.py` (no new config fields)
- `src/stt_button/focus.py`
- the done_sound logic (controller.py:~509-520)
- the signature `DeltaTyper.finalize(final_text, committed_len=0)`

## Design decisions
1. **Split the paster without a breaking change.** `FinalPaster` (only `paste`) stays as it is. `ClipboardPaster` gets a new method `copy(text) -> bool`: wl-copy only, no chord. It returns True when the copy worked. On a missing or failing binary it falls back to `typer.type_text` and returns False, which is the same fallback `paste()` uses. `paste()` is refactored internally to call `copy()` and then press the chord, so its behaviour stays identical. DeltaTyper finds `copy` via `getattr`; a paster without `copy` gets the legacy path.
2. **Space handling.** Terminal path: the space stays inside the copied text (`to_add + " "`), so there is no separate space key. Browser path: exactly like the current non-paster path, i.e. `type_text(to_add)` followed by `key("space")`. Confirmed.
3. **Backspaces.** Browser path = exactly the current non-paster path (`typer.backspace(n)` + `type_text`). Terminal path: `typer.backspace(n)` (dotool), then `copy`. Without a chord the OPT1 combined path (`paste_combined`) is pointless, so it is **only used in legacy mode** (probe=None).
4. **Mode decision.** Called once per non-empty finalize, and only if a `final_paster` is set:
   - `focus_probe is None` → **legacy**: today's behaviour exactly, chord and combined path included.
   - probe → `True`/`None` → **copy**.
   - probe → `False` → **type** (0× wl-copy).
   - probe raises an exception → treat like None (copy). finalize must never crash.
   - paster without a `copy` method → legacy.
   Tests: probe = lambda True/False/None/raise, `_capture` (subprocess fake) counts wl-copy calls, `FakeTyper.calls` counts chords and typed text.

Note: the controller calls `focus.terminal_focused()` again for the done sound when it is in mode "terminal", so up to 2 DBus calls per finalize (each ≤1 s budget). Acceptable; dedup only later inside controller.py (outside this chain).

---

## Step 1 (RED): append to `tests/test_typing.py` (end of file)
Uses the existing `FakeTyper` (line 26), `_capture` (line 796), `ClipboardPaster`/`DeltaTyper` (already imported).

```python
class TestFocusGatedFinalPaste:
    """Final delta: terminal focus -> clipboard only (no chord);
    browser focus -> typed via typer, clipboard untouched."""

    @staticmethod
    def _delta(monkeypatch, probe, **kw):
        calls = _capture(monkeypatch)
        monkeypatch.setattr("stt_button.typing.shutil.which", lambda b: "/usr/bin/" + b)
        t = FakeTyper()
        p = ClipboardPaster(t, copy_binary="wl-copy", paste_key="ctrl+v")
        d = DeltaTyper(t, final_paster=p, focus_probe=probe, **kw)
        return d, t, calls

    @staticmethod
    def _wl_copies(calls):
        return [c for c in calls if c and c[0] == "wl-copy"]

    def test_terminal_focus_copies_delta_with_space(self, monkeypatch) -> None:
        d, t, calls = self._delta(monkeypatch, lambda: True, append_space=True)
        d.update("Hallo", committed_len=5)
        t.calls.clear()
        d.finalize("Hallo Welt", committed_len=5)
        assert self._wl_copies(calls) == [["wl-copy", " Welt "]]
        assert ("key", "space") not in t.calls

    def test_terminal_focus_sends_no_chord(self, monkeypatch) -> None:
        d, t, calls = self._delta(monkeypatch, lambda: True)
        d.update("abcd", committed_len=0)
        t.calls.clear()
        d.finalize("abXY")
        assert not any(c[0] == "key" and "+" in c[1] for c in t.calls)
        assert ("backspace", "2") in t.calls
        assert t.typed == ""
        assert self._wl_copies(calls) == [["wl-copy", "XY "]]

    def test_unknown_focus_behaves_like_terminal(self, monkeypatch) -> None:
        d, t, calls = self._delta(monkeypatch, lambda: None)
        d.finalize("Hallo")
        assert self._wl_copies(calls) == [["wl-copy", "Hallo "]]
        assert ("key", "ctrl+v") not in t.calls

    def test_probe_exception_behaves_like_terminal(self, monkeypatch) -> None:
        def boom():
            raise RuntimeError("dbus down")
        d, t, calls = self._delta(monkeypatch, boom)
        d.finalize("Hallo")
        assert self._wl_copies(calls) == [["wl-copy", "Hallo "]]
        assert ("key", "ctrl+v") not in t.calls

    def test_browser_focus_types_and_never_touches_clipboard(self, monkeypatch) -> None:
        d, t, calls = self._delta(monkeypatch, lambda: False, append_space=True)
        d.update("abcd", committed_len=0)
        t.calls.clear()
        d.finalize("abXY")
        assert self._wl_copies(calls) == []
        assert t.calls == [("backspace", "2"), ("type", "XY"), ("key", "space")]

    def test_browser_focus_without_space(self, monkeypatch) -> None:
        d, t, calls = self._delta(monkeypatch, lambda: False, append_space=False)
        d.finalize("Hallo")
        assert self._wl_copies(calls) == []
        assert t.calls == [("type", "Hallo")]

    def test_no_probe_keeps_legacy_chord(self, monkeypatch) -> None:
        d, t, calls = self._delta(monkeypatch, None, append_space=True)
        d.finalize("Hallo")
        assert self._wl_copies(calls) == [["wl-copy", "Hallo "]]
        assert ("key", "ctrl+v") in t.calls
        assert ("key", "space") not in t.calls

    def test_no_paster_ignores_probe_and_types(self, monkeypatch) -> None:
        """final_paste=false: typer path unchanged, probe never consulted."""
        calls = _capture(monkeypatch)
        t = FakeTyper()
        asked: list[int] = []
        d = DeltaTyper(t, focus_probe=lambda: asked.append(1) or True)
        d.finalize("Hallo")
        assert t.calls == [("type", "Hallo"), ("key", "space")]
        assert self._wl_copies(calls) == []
        assert asked == []

    def test_probe_called_once_per_finalize(self, monkeypatch) -> None:
        asked: list[int] = []
        d, t, calls = self._delta(monkeypatch, lambda: asked.append(1) or False)
        d.update("abcd", committed_len=0)
        assert asked == []
        d.finalize("abXY")
        assert asked == [1]


class TestClipboardPasterCopy:
    def test_copy_runs_wl_copy_without_chord(self, monkeypatch) -> None:
        calls = _capture(monkeypatch)
        monkeypatch.setattr("stt_button.typing.shutil.which", lambda b: "/usr/bin/" + b)
        t = FakeTyper()
        p = ClipboardPaster(t, copy_binary="wl-copy")
        assert p.copy("abc") is True
        assert calls == [["wl-copy", "abc"]]
        assert t.calls == []

    def test_copy_missing_binary_falls_back_to_typing(self, monkeypatch) -> None:
        _capture(monkeypatch)
        monkeypatch.setattr("stt_button.typing.shutil.which", lambda b: None)
        t = FakeTyper()
        assert ClipboardPaster(t, copy_binary="wl-copy").copy("abc") is False
        assert t.calls == [("type", "abc")]

    def test_copy_empty_is_noop(self, monkeypatch) -> None:
        calls = _capture(monkeypatch)
        t = FakeTyper()
        assert ClipboardPaster(t).copy("") is False
        assert calls == [] and t.calls == []
```

## Step 1b (RED): `tests/test_app_wiring.py`, inside `class TestFinalPasteWiring` (line ~71)
`TyperStub`, `VadStub`, `ControllerStub`, `Application`, `Config` are already defined/imported in the module.

```python
    def test_final_paste_injects_terminal_focus_probe(self, monkeypatch) -> None:
        from stt_button import focus

        captured: dict[str, object] = {}

        class DeltaStub:
            def __init__(self, typer, **kwargs):
                captured.update(kwargs)

        class PasterStub:
            def __init__(self, typer, **kwargs):
                pass

        monkeypatch.setattr("stt_button.app.DeltaTyper", DeltaStub)
        monkeypatch.setattr("stt_button.app.ClipboardPaster", PasterStub)
        monkeypatch.setattr("stt_button.app.DotoolTyper", TyperStub)
        monkeypatch.setattr("stt_button.app.SileroVadGate", VadStub)
        monkeypatch.setattr("stt_button.app.PushToTalkController", ControllerStub)

        Application(Config(final_paste=True))._build_controller()
        assert captured["focus_probe"] is focus.terminal_focused

        captured.clear()
        Application(Config())._build_controller()
        assert captured.get("focus_probe") is None
```
Expected RED: TypeError `focus_probe` / AttributeError `copy` / KeyError `focus_probe`.

---

## Step 2 (GREEN): `src/stt_button/typing.py`
Line references from `grep -n`: 18 import, 463 `class ClipboardPaster`, 492-524 `def paste`, 589-598 `DeltaTyper.__init__`, 619 `_trace`, 770 `space_in_paste = False`, 797 combined guard, 826-840 output block.

**2a (line 18)**: `from typing import Protocol, runtime_checkable` → `from typing import Callable, Protocol, runtime_checkable`

**2b (lines 492-524)**: replace `def paste` completely with:
```python
    def copy(self, text: str) -> bool:
        """Copy ``text`` to the clipboard WITHOUT pressing the paste chord.

        Used when a terminal is focused: the chord is ineffective there
        (and ctrl+v = quoted-insert may swallow the next keystroke), so
        the user pastes manually after the done sound.

        Returns:
            True if the copy succeeded. False for empty text, or if the
            copy binary is missing/failed; in that case the text was typed
            character by character instead (same fallback as paste()).
        """
        if not text:
            return False
        if shutil.which(self.copy_binary) is None:
            log.warning(
                "%s unavailable - typing the delta character by character",
                self.copy_binary,
            )
            self._typer.type_text(text)
            return False
        try:
            log.debug("final paste: copy start (%d characters)", len(text))
            _run([self.copy_binary, text])
            log.debug("final paste: copy done")
        except TyperError as exc:
            log.warning(
                "Copy via %s failed (%s) - typing the delta character by character",
                self.copy_binary, exc,
            )
            self._typer.type_text(text)
            return False
        return True

    def paste(self, text: str) -> None:
        """Copy ``text`` to the clipboard and press the paste chord."""
        if not self.copy(text):
            return
        try:
            log.debug("final paste: chord %s", self.paste_key)
            self._typer.key(self.paste_key)
        except TyperError as exc:
            # Deliberately NO retry with type_text: the chord may have had a
            # partial effect, and the delta could already be inserted.
            log.error(
                "Paste chord %r failed (%s) - no retry "
                "(risk of duplicate insertion)",
                self.paste_key, exc,
            )
```

**2c (`__init__`, lines 597-599)**, before:
```python
        live_typing_mode: str = "full",
    ) -> None:
        self._typer = typer
```
after:
```python
        live_typing_mode: str = "full",
        focus_probe: Callable[[], bool | None] | None = None,
    ) -> None:
        self._typer = typer
        # Focus gating for the final delta (only with final_paster):
        # None = legacy (copy + chord). Otherwise probe() -> False (no
        # terminal) types the delta via the typer and leaves the clipboard
        # untouched; True/None (terminal/unknown) copies WITHOUT chord.
        self._focus_probe = focus_probe
```
New method after `_trace` (after line 623):
```python
    def _final_mode(self) -> str:
        """Decide the final output path: 'legacy' | 'copy' | 'type'."""
        if self._final_paster is None:
            return "type"
        if self._focus_probe is None:
            return "legacy"
        try:
            state = self._focus_probe()
        except Exception as exc:  # noqa: BLE001 - finalize must never crash
            log.debug("focus probe failed (%s) - treating as terminal", exc)
            state = None
        if state is False:
            mode = "type"
        elif callable(getattr(self._final_paster, "copy", None)):
            mode = "copy"
        else:
            mode = "legacy"
        self._trace(f"focus {state!r} -> {mode}")
        return mode
```

**2d (`finalize`)**: right after `space_in_paste = False` (line 770):
```python
        mode = self._final_mode()
        log.debug("final output mode: %s", mode)
```
Line 797, before: `if to_remove and to_add and self._final_paster is not None:` → after: `if to_remove and to_add and mode == "legacy":`

Lines 826-840, before:
```python
            if to_add:
                t0 = time.monotonic()
                if self._final_paster is not None:
                    # Appended space instead of a separate space key
                    # (no duplicate space; see above).
                    space_in_paste = self._append_space
                    self._final_paster.paste(
                        to_add + " " if space_in_paste else to_add
                    )
                    self._trace("paste")
                    if space_in_paste:
                        self._trace("space 0.000s (in paste)")
                else:
                    self._typer.type_text(to_add)
                    self._trace("type")
```
after:
```python
            if to_add:
                t0 = time.monotonic()
                if mode == "legacy":
                    # Appended space instead of a separate space key
                    # (no duplicate space; see above).
                    space_in_paste = self._append_space
                    self._final_paster.paste(
                        to_add + " " if space_in_paste else to_add
                    )
                    self._trace("paste")
                    if space_in_paste:
                        self._trace("space 0.000s (in paste)")
                elif mode == "copy":
                    # Terminal focus: clipboard only, NO chord. The space
                    # rides along in the clipboard (manual paste). On copy
                    # failure the fallback already typed text + space.
                    copied = self._final_paster.copy(
                        to_add + " " if self._append_space else to_add
                    )
                    space_in_paste = self._append_space
                    self._trace(f"copy (no chord) ok={copied}")
                else:
                    # Browser focus or no paster: typer path, clipboard
                    # untouched.
                    self._typer.type_text(to_add)
                    self._trace("type")
```
Add one sentence on focus gating to the `finalize` docstring.

## Step 3 (GREEN): `src/stt_button/app.py`
After line 17 (`from .keys import ...`): `from . import focus` (currently not imported; check with `grep -n focus src/stt_button/app.py`).

Lines 100-113, before:
```python
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
```
after:
```python
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
```
The existing `test_final_paste_wiring_respects_flag_and_config` stays green (it only checks `final_paster` and `paster_kwargs`).

## Step 4: tests
```
nice -n 19 .venv/bin/python -m pytest tests/test_typing.py -x
nice -n 19 .venv/bin/python -m pytest tests/test_app_wiring.py -x
nice -n 19 .venv/bin/python -m pytest -x --deselect tests/test_controller.py   # regression; controller = parallel chain
```
Legacy tests (TestFinalPaste with a `Recorder` without probe, OPT1 `paste_combined`, `OldPaster`) must stay green unchanged, because probe=None means legacy.

## Live verification (start/stop the service only with operator approval, P6)
1. `latency_trace = true`, debug log (`journalctl --user -u stt-button -f` or foreground with debug).
2. Browser focus → `latency[focus False -> type]` / `final output mode: type`, **no** `final paste: copy start`. Cross-check: `wl-paste` before and after dictation returns identical content.
3. Terminal focus → `focus True -> copy`, `final paste: copy start`, **no** `final paste: chord`; done sound; ctrl+shift+v pastes "delta ".
4. Extension disabled → `focus None -> copy`.
5. Hard proof: `strace -f -e trace=execve -p <pid> 2>&1 | grep wl-copy` during a browser dictation → 0 hits.

## Commit (P7)
Only the 4 files: `git add src/stt_button/typing.py src/stt_button/app.py tests/test_typing.py tests/test_app_wiring.py` (NO `-A`, because the parallel chain has controller.py/test_controller.py modified), then commit, then `git push origin master`.
