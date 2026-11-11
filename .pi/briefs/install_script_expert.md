# Experten-Auftrag — Installationsskript fuer die venv-freie Umstellung (Weg B, Release 0.2.1)

Repo `/home/salmon/pi-web-ui/STT-Button` (branch `master`). Du schreibst **EIN** Shell-Skript
plus **EINE** Testdatei. Kein Produktcode, keine Umstellung selbst, kein sudo-Aufruf von dir.

## Waehrum (Kontext, entschieden — nicht neu verhandeln)
Die Runtime soll **ohne venv** laufen: `/usr/bin/python3` (3.12) + Drittbibliotheken im
User-Site via `pip install --user --break-system-packages`. Das Paket `0.2.1` ist gebaut und
geprueft (`dist/stt-button_0.2.1_amd64.deb`, 133 kB). Die bisherige Produktion laeuft aus dem
Repo und **darf zu keinem Zeitpunkt sterben**. `sudo` braucht auf diesem Rechner ein Passwort,
deshalb fuehrt der **Betreiber** das Skript aus, kein Agent.

## Gemessene Fakten (verlasse dich darauf, pruefe aber im Skript trotzdem)
* Repo: `/home/salmon/pi-web-ui/STT-Button`; venv der Produktion: `.venv` (bleibt als
  Rueckweg erhalten, **niemals loeschen**).
* Paket-Zustand jetzt: `dpkg -l stt-button` = `iU 0.2.0` (unpacked, nicht konfiguriert) — muss
  vor der Neuinstallation **restlos entfernt** werden (`dpkg --purge` loescht `/opt/stt-button`
  und `/usr/bin/stt-button`).
* `/usr/bin/python3` ist **3.12.3**, PEP-668-Marker `/usr/lib/python3.12/EXTERNALLY-MANAGED`
  ist vorhanden, `site.ENABLE_USER_SITE=True`, User-Site `~/.local/lib/python3.12/site-packages`.
  Die geladene User-Unit setzt **kein** `PYTHONNOUSERSITE` — der Dienst sieht das User-Site.
* `python3-pip` ist **nicht** installiert (unser Gate meldet Level 1 `pip_missing`).
* Requirements (pinned): **`packaging/runtime-requirements.txt`** im Repo (1006 B; enthaelt
  u. a. `ctranslate2==4.8.4`, `faster-whisper==1.2.1`, `numpy`/`scipy`-Pins). **torch steht
  NICHT darin** und ist auf PyPI nur als CUDA-Build.
* torch vom CPU-Index, exakt so (Quelle `src/stt_button/bootstrap.py:23-28`):
  Index `https://download.pytorch.org/whl/cpu`, Pakete `torch==2.14.1+cpu`,
  `torchaudio==2.11.0+cpu`. **Reihenfolge: erst torch/torchaudio vom Index, dann der Rest** —
  sonst zieht der Index keine Wirkung.
* Gate-Werkzeug: `.venv/bin/python tools/deps_gate.py` (Optionen `--python`, `--src`,
  `--requirements`, `--report`). Exitlevel: **0** ok, **1** `pip_missing`/`deps_missing`,
  **2** `requirements_missing`, **3** `shadow_risk` (fremde Pakete im User-Site ueberschreiben).
* Gebautes Paket: `dist/stt-button_0.2.1_amd64.deb`. Inhalt geprueft: `Depends: python3,
  python3-pip, pipewire-bin, pulseaudio-utils, wtype`; `/usr/bin/stt-button` (Schlank-Starter,
  `exec /usr/bin/python3 -m stt_button`); `/usr/lib/systemd/user/stt-button.service` mit
  **eingesetztem** `ExecStart=/usr/bin/stt-button run`; `/opt/stt-button/systemd/stt-button.service`
  als Template mit `@EXEC@`; `deps_gate.py` ist im Paket.
* Lebende User-Unit `~/.config/systemd/user/stt-button.service` (336 B, vom 2026-10-28) enthält
  `ExecStart=/home/salmon/pi-web-ui/STT-Button/.venv/bin/stt-button -v run`,
  `Restart=on-failure`, `RestartSec=3`, `Environment=HF_HOME=%h/.cache/huggingface`,
  `WantedBy=default.target`. **Diese Datei ist das Einzige, was den Dienst umschaltet.**
* Der CLI-Befehl `stt-button setup` schreibt die User-Unit **nur** mit `--install-unit` und
  bricht bei abweichendem `ExecStart` mit Level 1 ab (Block V4, Commit `b65907d`). Trotzdem:
  **das Skript ruft `stt-button setup` nicht auf** — es kopiert die Unit explizit selbst,
  damit der Vorgang sichtbar und umkehrbar bleibt.

## Was das Skript tun soll (Schrittfolge ist beschlossen)
`bash .pi/tools/install_wayb_0.2.1.sh [--dry-run|--apply] [--only=SCHRITT] [--rollback]`

1. **Pruefen** (ohne Schreibgriff): Repo vorhanden, Deb vorhanden, Python-Version,
   PEP-668-Marker, `dpkg -l stt-button`-Zustand, Unit-Datei vorhanden + ihr `ExecStart`,
  当前 `systemctl --user is-active`. Bei Abweichung: **abbrechen mit Grund**, nichts aendern.
   `--dry-run` ist die **Voreinstellung**, wenn kein Flag gegeben ist.
2. **R1 (root)** `sudo dpkg --purge stt-button` — nur wenn das Paket ueberhaupt vorhanden ist;
   danach `dpkg -l` erneut pruefen (darf `deinstall not-installed`/kein Eintrag sein).
3. **R2 (root)** `sudo apt-get install -y python3-pip`; danach `/usr/bin/python3 -m pip --version`.
4. **R3 (User, kein root)** torch zuerst:
   `/usr/bin/python3 -m pip install --user --break-system-packages --index-url
   https://download.pytorch.org/whl/cpu torch==2.14.1+cpu torchaudio==2.11.0+cpu`,
   dann der Rest aus **dem Repo-Pfad** `packaging/runtime-requirements.txt` (nicht `/opt`,
   das existiert zu diesem Zeitpunkt nicht).
5. **R4 Gate-Pflicht**: `.venv/bin/python tools/deps_gate.py` muss **Level 0** melden, sonst
   **Abbruch vor jeder Installation** und Ausdruck des Gate-Reports. Level 3 (`shadow_risk`)
   bekommt zusaetzlich den Isolations-Hinweis: `Environment=PYTHONUSERBASE=%h/.local/stt` in
   der Unit **und** identische Variable beim Installieren (README-Abschnitt „Shadow-Isolation").
6. **R5 (root)** `sudo apt-get install -y ./dist/stt-button_0.2.1_amd64.deb`; danach
   `dpkg -s` (Status install installed), `dpkg -L | grep -E "bin/stt-button$|systemd/user"`.
7. **R6 Umschaltung, nur auf ausdruecklichen Wunsch (`--switch-unit`)**:
   * Unit-Quelle: `/usr/lib/systemd/user/stt-button.service` (die mit eingesetztem ExecStart),
   * Sicherungskopie `~/.config/systemd/user/stt-button.service.<Zeitstempel>.bak` **vor** jedem
     Schreibgriff, Kopie nach `~/.config/systemd/user/stt-button.service`,
   * `systemctl --user daemon-reload`, **genau ein** `systemctl --user restart stt-button`,
   *的健康-Check: `is-active` = active, `NRestarts` lesen, `journalctl --user -u stt-button -n 80`
     auf `ERROR`/`Traceback` pruefen und `Ready.`-Zeile verlangen; Fehler => **automatisch die
     .bak zurueckspielen**, reload, erneut genau ein restart, und den Betreiber-Befund ausgeben.
8. **`--rollback`**: letzte `.bak` zurueckspielen, `daemon-reload`, genau ein restart,
   Aktiv-Zustand melden. Das deinstalliert **kein** Paket und loescht **kein** User-Site
   (explizit: kein `rm -rf`, kein `pip uninstall` auf fremde Pakete).

Zusaetze, die das Skript braucht: `set -euo pipefail`; Log mit Zeitstempel nach
`.pi/logs/install_wayb_<Zeitstempel>.log`; jede root-Zeile wird **vor** der Ausfuehrung
ausgedruckt; idempotent (zweites Ausfuehren ist ein No-Op, keine Doppel-Sicherung die eine
gute Kopie ueberschreibt — `.bak` nur wenn keine gleichinhaltige exists); **niemals**
Tastatur-/Clipboard-/Ton-Aktionen, kein `stt-button run`, kein `setup`, kein `rm -rf`,
kein Loeschen von `~/.local/lib/python3.12/site-packages`, kein Anrühren von `.venv`,
kein `git`-Kommando.

## Testpflicht — NEUE Datei `tests/test_install_wayb_script.py`
* `bash -n` auf dem Skript (Syntax),
* `--dry-run` fuehrt **keine** sudo-Zeile aus (Test setzt ein `PATH`-Fake-Sudo, das eine
  Markerdatei schreibt, und verlangt: Marker existiert nicht),
* Schluessel-Wachtungen im Text: `set -euo pipefail`, `--break-system-packages`,
  `https://download.pytorch.org/whl/cpu`, `--purge`, `.bak`, `daemon-reload`, `Level`-Prüfung
  vor R5, und dass `rm -rf` **nicht** vorkommt,
* Rollback-Zweig existiert und enthaelt kein `dpkg --purge`.
Der Test fuehrt **keine** Installation, **kein** sudo, **kein** apt, **kein** pip und **keinen**
systemctl aus — nur `bash -n` und `--dry-run` gegen das PATH-Fake-Sudo in einem tmp-Verzeichnis.

## Erlaubte / verbotene Dateien
Erlaubt: NEU `.pi/tools/install_wayb_0.2.1.sh`, NEU `tests/test_install_wayb_script.py`.
Verboten: `src/**`, `systemd/**`, `packaging/**`, `tools/**`, `README.md`, `CHANGELOG.md`,
`pyproject.toml`, `dist/**`, andere `tests/*`, `packaging/runtime-requirements.txt`;
kein `git commit`/`push`, kein sudo/apt/pip/systemctl/dpkg-Aufruf (auch nicht "nur zum Testen"),
kein Netzwerk, kein Schreiben unter `~/.config/`, keine Tastatur-/Clipboard-/Ton-Emission.

## Abschluss
Schlussmeldung max 8 Zeilen: Skriptpfad, was `--dry-run` zeigt (die gedruckten Schritte),
N passed, und das **tatsaechlich gelaufene Modell**.
