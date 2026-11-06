# Verifikation Zusammenfassungs-Modus (M8) — 2026-11-06

Ausführende Stelle: Orchestrator (Abnahme nach R4 — niemals über Kinderberichte).
Plan: `.pi/plans/plan_summary_mode_v2.md` (Planer `conv-f5bc04ce`), §6 „VERIFIKATION".

## 1. Suite offline (Abnahme-Schritt 1)

```
nice -n 19 .venv/bin/python -m pytest -q -p no:randomly -o addopts=""
821 passed, 2 skipped in 91.22s (0:01:31)
```

0 failed. Die beiden bekannten Realzeit-Flakes **FL1**
(`test_ticks_stop_after_limit_final_text_complete`) und **FL2**
(`test_async_writes_wav_from_snapshot`, G22/G26) traten in dieser Abnahme **nicht** auf —
die Zahl ist also ohne Flake-Nachrechnung erreicht.

## 2. Blocks und Abnahme (je `git show --stat` + eigene VolSuite)

| Block | Commit | Inhalt |
|---|---|---|
| T1-B1/B2/B4 | `cd580e1` `18944bf` `b487046` | Leerzeichen-Fehler im Einfügepfad: ROT → GRÜN → Doku |
| B1 | `6872b25` | 15 `summary_*`-Schlüssel + `validate()` + Beispielkonfig |
| B2 / B3 | `ced07f8` / `9d8a180` | Doppel-druck-Erkennung auf der zweiten Loslass-Kante + `PressRouter` |
| B4 | `c6647d7` | `SummaryBuffer`, additative Haken `on_output` / `has_pending_work()` |
| B5 | `2423115` | `locate_block()` — sieben Fälle, konservative Abbrüche |
| B6 | `0fe8032` | `apply_summary()` / `undo_summary()`, `ApplyResult` als einziger Kanal |
| B7 | `4cd1011` | `SummaryClient.chat()` mit `ModelUnavailable`-Abbildung |
| B8a / B8b | `41ac5c2` / `cd09fc6` | `you_lose.wav` + `play_error_sound(...)` |
| B9a(-key) | `8c4483b` `46b1121` | Coordinator (Thread, IDLE/WORKING/ACTIVE_UNDO) + `wait_for_result()` |
| B9b (+fix) | `ac091cf` `8c85aee` | Fokus-Gate, Markierungsweg, Roundtrip über **PRIMARY**, `empty_read` mit Ton |
| B9c | `d5e00c3` | Schlange = 1, genau EIN Ton pro verworfenem Auftrag, Enabled-Gate ⇒ null `Popen` |
| B10a / B10b | `0be57eb` / `87bad6b` | `focus.window_identity()`; App-Verdrahtung mit 1:1-Aus-Zustand |
| B10r | `488f19a` | **Planlücken-Beseitigung:** öffentliches `submit_summary(text, chunks)`; `coord._`-Zähler in `app.py` = 0 |
| B11a / B11b | `ed11491` / `68c4141` | CLI `summary-models`/`summary-test`; `tools/summary_check.py` (Exitlevel 0/1/2) |
| B12 | `7e439b0` | README-Abschnitt, CHANGELOG, `build-deb.sh` mit `sounds/`-Asset |

## 3. Echt-Lauf gegen das Gateway (Abnahme-Schritt 4, reines HTTP, keine Tastatur)

```
STT_SUMMARY_REAL=1 .venv/bin/python tools/summary_check.py
gate 1 reachability: ok (57 models)
gate 2 model: ok 'my-qwen38-flash-next' active
gate 3 roundtrip: ok (elapsed 2.0s, answer_len=42)
exit=0
```

Damit erfüllt: HTTP 200, Antwort ≠ Vorlage, Laufzeit 2,0 s < `summary_timeout_s` (12 s).
`my/qwen38-flash-ext` wird **nicht** angeboten (G21: 400 „Invalid model name").

## 4. Dienst-Neustart (P6-Freigabe Q2 = B, genau einer)

`systemctl --user restart stt-button` um **11:28:12**, danach:

```
Active: active (running) since Fri 2026-11-06 11:28:12 UTC; 4s ago
INFO stt_button.app: Watching /dev/input/event9 (keyd virtual keyboard) for keys ['KEY_F24']
INFO stt_button.asr: Parakeet model loaded.
INFO stt_button.app: Ready. Hold the Copilot key and speak.
```

`journalctl --user -u stt-button --since 11:28:10 | grep -cE " ERROR | Traceback"` ⇒ **0**.
(Einzige 404-Meldungen sind optionale HuggingFace-Metadatenpfade
`additional_chat_templates` — Debug-Stufe, vorbestehend, kein Fehler dieser Kette.)

**Produktions neutral:** `~/.config/stt-button/config.toml` enthält weiterhin nur
`backend = "parakeet"` und **keinen** `summary_*`-Schlüssel ⇒ `summary_enabled` bleibt Default
`false`; es wird kein Coordinator konstruiert ( regressionsgesteuert in
`tests/test_summary_wiring.py`).

## 5. Was der Betreiber selbst tun muss (M9)

1. `summary_enabled = true` in `~/.config/stt-button/config.toml` setzen und den Dienst neu
   starten (Agenten setzen die Funktion laut Auftrag nicht).
2. **Handtest** (Doppeldruck, Undo, Markierungs-Weg, `you_lose`-Ton), insbesondere
   §6.7 g: fremder Block zwischen den Diktaten ⇒ `ambiguous`-Abbruch **mit** Schutzwirkung
   (nichts gelöscht) — das bewusste False-Positiv-Verhalten von B5.
3. **T1-B3 Feldtyp-Matrix** (nur Betreiber; Agenten dürften hier nicht tippen): je ein Durchlauf
   in GNOME-Textfeld, Browser, Terminal, LibreOffice — erwartet außer im erlaubten Feld
   `caret_not_at_end` bzw. `focus_not_editable` mit null Tastenereignissen.
4. Echter CLI-Round: `STT_SUMMARY_REAL=1 .venv/bin/python -m stt_button summary-test "…"` und
   `summary_check.py --list-models` (57 Einträge, aktives Modell markiert).
5. XPU-Pfade (Q1): `sudo apt install -y intel-opencl-icd libze-intel-gpu1 libze1`, danach
   `.venv-xpu`-Prüfung (`tools/setup_qwen_xpu.sh`, `tools/model_switch_check.py --device xpu`).
6. `git push origin master` (23 lokale Commits `6872b25..68c4141` + Housekeeping) braucht Deine
   ausdrückliche Freigabe (P7) — bisher **nicht** gepusht.

## 6. Backlog / bekannte Grenzen

* **B5b** (kettenweite Verortungs-Verbesserung gegen `ambiguous`-False-Positives) bleibt Backlog.
* `summary_picker` / `zenity` bewusst nicht in Runde 1 (Fokus-Diebstahl aus dem Headless-Dienst).
* Feldtyp-Deckung ist messtechnisch offen, bis die Matrix (M9.3) gefüllt ist.
