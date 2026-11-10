# Dispatch V3 — Unit-VORLAGE ohne Bootstrap-Timeout (Kleinstpaket: EINE Datei + EINE neue Testdatei)

Repo `/home/salmon/pi-web-ui/STT-Button`. Plan `.pi/plans/plan_no_venv_B.md` — **nur die
V3-Zeile** lesen (Tabelle §3), sonst nichts.

## Vorgabe des Planers (V3-Zeile, inhaltlich woertlich)
`systemd/stt-button.service` (die **Vorlage** im Repo, 25 Z.):
* `TimeoutStartSec=1800` **und** den zugehoerigen venv-Kommentar (`:13-15`) raus,
* `@EXEC@`-Kommentar auf Weg B umformulieren (System-python3 + User-Site, kein venv-Bau),
* `Environment=HF_HOME=%h/.cache/huggingface` **bleibt** erhalten,
* `ExecStart=@EXEC@` bleibt unveraendert (die Substitution uebernimmt `build-deb.sh`),
* **Reine Vorlagenänderung — die lebende Unit unter `~/.config/systemd/user/` wird NICHT
  angefasst** (die Produktion laeuft weiter aus dem Repo).

## Testpflicht — NEUE Datei `tests/test_unit_template.py`
* kein `1800` in der Vorlage,
* das Wort `venv` kommt nirgends vor (auch nicht in Kommentaren),
* `Environment=HF_HOME` ist vorhanden,
* `ExecStart=@EXEC@` unveraendert enthalten.
Tests lesen nur die Datei — **kein** `systemctl`, kein `daemon-reload`, kein `stt-button setup`,
kein `systemd-analyze verify` gegen die geladene Unit, kein Schreiben unter `~/.config/`.

## TDD + Abschluss
Testdatei ZUERST (ROT: `1800` und `venv` sind noch drin), dann Vorlage (GRUEN).
VolSuite **hoechstens einmal**: `nice -n 19 .venv/bin/python -m pytest -q -p no:randomly -o addopts=""`.
Baseline **832 passed / 2 skipped**; FL1 `TestLiveAsrUntilController::test_ticks_stop_after_limit_final_text_complete`
und FL2 `test_async_writes_wav_from_snapshot` = praedatierte Timing-Flakes (Signatur G26:
literales `'...'` im typed + `('backspace','3')`) — nur melden, bei Zweifel dreimal einzeln
laufen lassen und Signatur vergleichen.
Commit: `refactor(systemd): drop one-time venv build timeout`

## Erlaubte Dateien
`systemd/stt-button.service`, NEU `tests/test_unit_template.py`
Verboten: `~/.config/systemd/user/**` (lebende Unit!), `src/**`, `systemd/stt-button-launcher.sh`
(fertig, V2), `__main__.py` (V4), `packaging/**` (V5), `tools/build-deb.sh` (V6), Doku (V7),
`pyproject.toml`; kein sudo/systemctl/daemon-reload, kein Netzwerk, keine
Tastatur-/Clipboard-/Ton-Emission, **kein git push**, kein Dienst-Neustart.

## Stall-Prophylaxe (verbindlich)
* Dateien nur in Stuecken <= 80 Z. lesen; `edit` statt großem `write`.
* KEINE `while pgrep`-Wartesprleifen, KEINE 20x-Testschleifen, KEIN conftest.py-Gesuche.
* Wenn dein letzter Schritt ein Werkzeug war und dir nichts mehr bleibt: melde das in EINER
  Zeile, statt schweigend zu enden.
* Schlussmeldung max 5 Zeilen (Sha, N passed, tatsaechlich gelaufenes Modell).
