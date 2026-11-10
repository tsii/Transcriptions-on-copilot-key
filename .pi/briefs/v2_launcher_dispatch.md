# Dispatch V2 — Launcher ohne Bootstrap (Kleinpaket: EINE Datei + EINE neue Testdatei)

Repo `/home/salmon/pi-web-ui/STT-Button`. Plan `.pi/plans/plan_no_venv_B.md` — **nur Zeile 63
(V2-Zeile)** lesen, sonst nichts. NIEMALS den ganzen Plan oder `src/` ganz.

## Vorgabe des Planers (V2-Zeile, inhaltlich woertlich)
`systemd/stt-button-launcher.sh` (111 Z.):
* **Bootstrap-Block (`:23-31`) und venv-Teil (`:32-38`) raus.**
* Stattdessen: Import-Check der Kernmodule mit klarem pip-Hint, dann
  `exec /usr/bin/python3 -m stt_button "$@"`.
* `PYTHONPATH` **nur** `$ROOT/src` (das enthaelt ausschliesslich `stt_button`, kein Fremdmodul).
* Der Launcher ruft **niemals** `run_bootstrap` und baut **kein** venv mehr (Weg B:
  Ubuntu-System-python3, Drittbibliotheken im User-Site `~/.local/lib/python3.12/site-packages`).

Warum das sicher ist (gemessen): `site.ENABLE_USER_SITE=True`, `USER_SITE=~/.local/lib/
python3.12/site-packages`, und die geladene Unit setzt kein `PYTHONNOUSERSITE` — der
systemd-User-Dienst sieht das User-Site also.

## Testpflicht — NEUE Datei `tests/test_launcher.py`
* `sh -n systemd/stt-button-launcher.sh`_exit 0 (Syntax),
* Datei enthaelt **kein** `run_bootstrap` und **kein** `venv` (Case-insensitive auf den
  Launcher-Text, Kommentar- erwhaehne: auch keine Bootstrap-Reste in Auskommentierung),
* sie enthaelt `exec /usr/bin/python3 -m stt_button`,
* die `PYTHONPATH`-Zeile nennt ausschliesslich `$ROOT/src`,
* Import-Check der Kernmodule + pip-Hint sind vorhanden (zwei Muster).
Die Tests lesen nur die Datei und fuehren `sh -n` aus — **kein** Start des Dienstes, kein
`stt-button setup`, kein Network, kein echtes `/usr/bin/python3 -m stt_button`-Exec (der
Launcher kann auf diesem Rechner mangels pip noch nicht laufen — das ist erwartet und wird
nicht getestet).

## TDD + Abschluss
Testdatei ZUERST (ROT: `run_bootstrap` ist noch drin), dann Launcher (GRUEN).
VolSuite **hoechstens einmal**: `nice -n 19 .venv/bin/python -m pytest -q -p no:randomly -o addopts=""`.
Baseline **826 passed / 2 skipped**; FL1 `TestLiveAsrUntilController::test_ticks_stop_after_limit_final_text_complete`
und FL2 `test_async_writes_wav_from_snapshot` = praedatierte Timing-Flakes (Signatur G26:
literales `'...'` im typed + `('backspace','3')`) — **nur melden**, nicht ungeprüft buchen:
im Zweifelsfall dreimal einzeln laufen lassen und Signatur vergleichen.
Commit: `refactor(deb): launcher runs system python without bootstrap`

## Erlaubte Dateien
`systemd/stt-button-launcher.sh`, NEU `tests/test_launcher.py`
Verboten: `src/**` (insb. `bootstrap.py` — es bleibt unangetastet als dev-Werkzeug),
`systemd/stt-button.service` (das ist V3), `__main__.py` (V6!), `packaging/**` (V4),
`tools/build-deb.sh` (V5), Doku (V7), `pyproject.toml`; kein sudo/apt/dpkg/pip, kein
Netzwerk, keine Tastatur-/Clipboard-/Ton-Emission, **kein git push**, kein Neustart des
Dienstes, kein Schreiben unter `~/.config/systemd/user/`.

## Stall-Prophylaxe (verbindlich)
* Launcher nur in Stuecken <= 80 Z. lesen; `edit`/`patch` statt einem großen `write`.
* KEINE `while pgrep`-Wartesprleifen, KEINE 20x-Testschleifen, KEIN conftest.py-Gesuche.
* Wenn dein letzter Schritt ein Werkzeug war und dir nichts mehr bleibt: melde das in EINER
  Zeile, statt schweigend zu enden.
* Nach zwei erfolglosen Schleifen: abbrechen, Fehlerausgabe in <=4 Zeilen zitieren, Baum
  liegen lassen. Schlussmeldung max 5 Zeilen (Sha, N passed, tatsaechlich gelaufenes Modell).
