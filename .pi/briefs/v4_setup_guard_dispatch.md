# Dispatch V4 — `setup` ohne venv, mit Unit-Schutz (Kleinpaket: EINE Quelldatei + EINE neue Testdatei)

Repo `/home/salmon/pi-web-ui/STT-Button`. Plan `.pi/plans/plan_no_venv_B.md` — **nur die
V4-Zeile** lesen (Tabelle §3), sonst nichts.

## Was der Block tun muss (V4-Zeile, inhaltlich woertlich)
`_cmd_setup` in `src/stt_button/__main__.py` (heute `:330-405`) wird umgebaut:
* **kein** `run_bootstrap` / `probe_venv` / `mark_ready` mehr (Keine venv-Erzeugung, kein
  `from . import bootstrap` im Rumpf).
* Stattdessen **Gate-Prüfung**: importiere die bereits existente Prüfroutine aus
  `src/stt_button/deps_gate.py` (V4a/V4a-fix, `main()` + Exitlevels 0/1/2/3) — **keine**
  Pfad-Hack-Importe aus `tools/`.
* **Konfig-Seeding wie bisher** (`~/.config/stt-button/config.toml` nur wenn fehlend).
* Unit-Schreibrecht: Unit wird **nur** mit `--install-unit` geschrieben.
  * Existiert bereits eine Unit mit **anderem** `ExecStart` → **Abbruch mit Grund, Exitlevel 1**,
    Datei bleibt unverändert.
  * **Ohne** Flag: Dry-Run (Was wäre geschrieben worden, nichts geschrieben).

## Warum das der sicherheitskritische Block der Serie ist
Heute steht dort (`:388-389`): sobald `/opt/stt-button` existiert, schreibt `setup` die
User-Unit auf `ExecStart=/usr/bin/stt-button run`. Die **lebende** Unit des Betreibers zeigt
aber auf das Repo (`.venv/bin/stt-button -v run`). Ein `stt-button setup` nach dem Deb-Install
würde die Produktion beim nächsten Restart **abschalten**. Der Unit-Schutz ist der Grund,
warum dieser Block existiert — Test (a) ist der Beweis, dass er trägt.

## Testpflicht — NEUE Datei `tests/test_setup_cli.py`
* (a) `setup` **ohne** Flag schreibt **keine** Unit-Datei,
* (b) `setup --install-unit` mit **abweichender** existenter Unit → **Exitlevel 1**, Datei
  inhaltlich unverändert,
* (c) **kein `bootstrap`-Aufruf im Rumpf von `_cmd_setup`** — geprüft über
  `inspect.getsource(_cmd_setup)` (NICHT die ganze Datei: der Unterbefehl `stt-button bootstrap`
  bleibt als dev-Werkzeug bestehen, Entscheidung D2),
* (d) Seeding einer fehlenden Config funktioniert.
**Isolation ist Pflicht**: jede Test die Unit- oder Config-Pfade schreibt, muss `$HOME`
(oder die Pfad-Argumente) per `monkeypatch`/`tmp_path` in ein tmp-Verzeichnis legen. Die
echte `~/.config/systemd/user/stt-button.service` des Betreibers darf in keinem Lauf
gelesen-geschrieben werden — schreibe einen Test, der ihre Unverändertheit explizit prüft,
indem du sie nur auf Existenz+Hash pruefst (lesen ok, schreiben nie).

## TDD + Abschluss
Testdatei ZUERST (ROT: `setup` schreibt noch ohne Flag), dann Umbau (GRUEN).
Blocktests laufen lassen. **Keine VolSuite** (FH1 laeuft parallel und ist Timing-empfindlich).
Commit: `feat(cli): setup verifies runtime, no venv build, guarded unit install`

## Erlaubte Dateien
`src/stt_button/__main__.py`, NEU `tests/test_setup_cli.py`
Verboten: `src/stt_button/bootstrap.py` (bleibt unangetastet), `src/stt_button/deps_gate.py`
(fertig — nur importieren), `tools/**`, `systemd/**`, `packaging/**`, `README.md`,
`CHANGELOG.md`, `tests/test_controller.py` (parallel in Arbeit), andere `tests/*`,
`pyproject.toml`; **kein Ausführen von `stt-button setup` ausserhalb isolierter tmp-HOMEs**,
kein sudo/systemctl/daemon-reload, kein Netzwerk, keine Tastatur-/Clipboard-/Ton-Emission,
**kein git push**, kein Dienst-Neustart.

## Parallelitaet + Stall-Prophylaxe (verbindlich)
* Zwei andere Kinder laufen: FH1 in `tests/test_controller.py`, Doku-Arbeit ist fertig.
  Du fasst `tests/test_controller.py` nicht an.
* `_cmd_setup` nur in Stuecken <= 80 Z. lesen; umbauen mit `edit`/`patch`, nicht mit `write`.
* KEINE `while pgrep`-Wartespruenge, KEINE 20x-Schleifen, KEIN conftest.py-Gesuche.
* Wenn dein letzter Schritt ein Werkzeug war und dir nichts mehr bleibt: melde das in EINER
  Zeile, statt schweigend zu enden.
* Schlussmeldung max 6 Zeilen (Sha, N passed, tatsaechlich gelaufenes Modell).
