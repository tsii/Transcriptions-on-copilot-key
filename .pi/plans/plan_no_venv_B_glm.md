# Plan Weg B: Runtime ohne venv, Ubuntu-System-python3 (0.2.1)

Status: PLAN (Implementierung delegiert). Baseline Suite: 821 passed / 2 skipped,
Aufruf exklusiv `nice -n 19 .venv/bin/python -m pytest -q -p no:randomly -o addopts=""`.
Produktion läuft bis zum finalen Umstellungsschritt WEITER aus dem Repo (.venv) —
kein Block darf die laufende Unit umbiegen. Repo-.venv bleibt bis auf Weiteres
erhalten (Sicherheitsnetz + Test-Runtime); Abbau ist ein separater späterer Auftrag.

## D1 — numpy-Entscheidung: (a) numpy 2 per Lock ins User-Site, V0 als Messtor
Begründung: Lock pinnt `numpy==2.5.4`; `torch==2.14.1+cpu` (bootstrap.py:41) und
`transformers==5.20.2` sind gegen numpy-2-ABI gebaut — `numpy<2` (b) ist gegen den
Lock vermutlich nicht auflösbar und ohne Netzwerk nicht beweisbar. (c) isolierter
Site-Pfad pro Dienst wäre sicherer, weicht aber vom Betreiber-Beschluss
(`~/.local/lib/python3.12/site-packages`) ab. Also: (a) + Messtor V0. Kriterium:
Nach der numpy-2-Testinstallation (Weg-Pfad s. V0) müssen unsere importierenden
Ketten UND die Distro-Erweiterungen `scipy`, `soundfile`, `pyaudio` für den Nutzer
importierbar bleiben; führt V0 zu relevantem Bruch, greift automatisch Variante (c)
(PYTHONUSERBASE pro Dienst im Launcher, Plan-Addendum) — kein Weitermachen auf (a).
Rückholbarkeit (a): `pip uninstall` bzw. rm aus `~/.local/...` — dpkg unberührt.

## D2 — bootstrap.py: BEHALTEN, aber aus Launcher/setup/Unit-Pfad ENTFERNT
`src/stt_button/bootstrap.py` bleibt unangetastet als Repo-Entwicklungs- und
Notfall-Werkzeug (Repo-.venv der Produktion baut darauf). Neue Rollen:
- Launcher ruft NIE `run_bootstrap` (kein Self-Healing, kein venv).
- `stt-button setup` baut kein venv mehr. Neue Aufgabe: (1) Runtime-CHECK
  (system python3 + user-site Import-Probe, klaren pip-Hint ausgeben, bei
  Fehlschlag Exit 1 — NICHT selbst installieren), (2) Config-Seed (unverändert),
  (3) Unit-Schreiben mit Launcher-Reifegrad-Wächter (V5). Anleiten statt bauen.

## V0 — numpy-Messtor (VOR jeder Umstellung, braucht root-Zeile 2, kein sudo)
Ziel: D1 auf dem Rechner fallen. Dateien: keine in src/**; nutzt Weg-Wurf-Pfad
`PYTHONUSERBASE=$XDG_RUNTIME_DIR/stt-v0` (RAM-tmp, NICHT ~/.local).
Durchführung (Agent, Netz nötig; failsafe: ohne Netz → NO-GO-Meldung an Betreiber):
1. `python3 -m pip install --user --break-system-packages --target ...` nein —
   korrekt: `PYTHONUSERBASE=... python3 -m pip install --user --break-system-packages numpy==2.5.4`
2. `PYTHONUSERBASE=... python3 -c "import numpy; assert numpy.__version__=='2.5.4'"`
3. `PYTHONUSERBASE=... python3 -c "import scipy, soundfile"` (+ pyaudio, wenn apt-pkg da)
4. Danach unsere Stack-Kette: zusätzlich torch==2.14.1+cpu, ctranslate2==4.8.4,
   transformers==5.20.2 in denselben PYTHONUSERBASE, dann Import-Probe von
   `stt_button.asr`-Abhängigkeiten (numpy/ctranslate2/sounddevice) mit
   `PYTHONPATH=src` aus dem Repo.
Test: kein ROT→GRÜN (Messung); Protokoll nach `.pi/reports/v0_numpy_verdict.md`.
Kriterien GO: (2)+(3) sauber, (4) importiert. Sonst → Variante (c), Plan-Addendum.
Aufräumen: `rm -rf $XDG_RUNTIME_DIR/stt-v0`. Rollback: nichts Persistentes.

## V1 — Launcher: System-python3, kein Bootstrap
Ziel: `/usr/bin/stt-button` startet ohne venv/Bootstrap, mit Import-Check +
pip-Hint, Code aus `$ROOT/src` per gezielter PYTHONPATH (NUR der Paket-Pfad;
kein Export vonanything ins Unit-Environment).
Dateien: `systemd/stt-button-launcher.sh` (neu, ~30 Z.),
Testdatei: `tests/test_launcher_no_venv.py` (NEU; parst das Skript).
Tests ROT→GRÜN: (a) enthält kein `bootstrap`/`VENV`/`run_bootstrap`; (b)
enthält `exec`-Zeile `python3 -m stt_button "$@"`; (c) PYTHONPATH nur
`"$ROOT/src"`; (d) Fehlertext enthält `pip install --user --break-system-packages`
+ requirements-Hinweis; (e) Import-Check via `python3 -c "import ..."` mit
PYTHONPATH, NICHT über stt_button.bootstrap.
Commit: "launcher: run on system python3 via user-site, drop bootstrap"
Rollback: `git revert` des Commits; installierter Launcher via
`sudo cp systemd/stt-button-launcher.sh /usr/bin/stt-button` ersetzbar.

## V2 — Unit: Timeout/Kommentare für venv-Ära entfernen
Ziel: `systemd/stt-button.service` ohne venv-Restsemantik.
Dateien: `systemd/stt-button.service`; Testdatei: `tests/test_packaging_no_venv.py`
(NEU; deckt V2–V4 ab, je asserts pro Block, <300 Z.).
Tests ROT→GRÜN: (a) kein `TimeoutStartSec=1800` mehr (Default reicht, Start
ist nur Modelload); (b) kein „venv"-Kommentar; (c) `@EXEC@`-Mechanismus bleibt.
Commit: "service: drop venv-era TimeoutStartSec and comments"
Rollback: `git revert`.

## V3 — debian/control: Depends anpassen (V3)
Ziel: Weg-B-Paketdeklaration.
Dateien: `packaging/debian/control`; Tests in `tests/test_packaging_no_venv.py`.
Tests ROT→GRÜN: (a) `python3-venv` NICHT mehr in Depends; (b) `python3-pip`
IN Depends (Betreiber/Paket braucht pip für --user-Install); (c) restliche
Depends (pipewire-bin, pulseaudio-utils, wtype) unverändert; (d) Description:
venv-Absatz ersetzt durch Weg-B-Satz („Drittbibliotheken per
pip --user --break-system-packages unter ~/.local").
Commit: "control: drop python3-venv, require python3-pip (Weg B)"
Rollback: `git revert`.

## V4 — build-deb.sh: postinst-Hinweis auf Weg B
Ziel: postinst-Text (build-deb.sh:97–109) weist auf `apt python3-pip` +
`pip install --user --break-system-packages` + `stt-button setup` hin statt venv.
Dateien: `tools/build-deb.sh`; Tests: `tests/test_packaging_no_venv.py` ergänzt:
(a) postinst-Text enthält `--break-system-packages`, kein `venv`.
Commit: "build-deb: postinst hint for user-site pip install"
Rollback: `git revert`.

## V5 — _cmd_setup: kein venv-Bau, Check statt Build, Unit-Wächter
Ziel: `stt-button setup` unter System-python3: (1) Runtime-CHECK
(system python3 importiert stt_button-Kernmodule NUR lesend; bei Fehlschlag
klarer pip-Befehl + Exit 1), (2) Config-Seed wie gehabt, (3) Unit-Schreiben
NUR mit Launcher-Reifegrad-Wächter: `exec_target = /usr/bin/stt-button run`
ausschließlich wenn `/usr/bin/stt-button` existiert UND weder `bootstrap` noch
`VENV` im Launcher-Skript vorkommt (V1-Form); sonst unverändert Repo-ExecStart
bzw. Warnung + bestehende Unit NICHT überschreiben. Damit kann setup die
gesunde Repo-Produktion nie mehr auf einen toten Launcher umbiegen.
Dateien: `src/stt_button/__main__.py` (nur `_cmd_setup`), evtl. kleine neue
Helper-Funktion `launcher_is_no_venv()`.
Testdatei: `tests/test_setup_no_venv.py` (NEU; tmp_path-Fakes für Launcher,
/opt-Existenz via monkeypatch, Unit-Schreiben in tmp HOME).
Tests ROT→GRÜN: (a) setup ohne venv-Bau (kein run_bootstrap-Aufruf, mockbar);
(b) Wächter: alter venv-Launcher + /opt vorhanden → Unit wird NICHT auf
/usr/bin umgebogen; (c) V1-Launcher → Unit auf `/usr/bin/stt-button run`;
(d) fehlende Runtime → Exit 1 + pip-Hint, Config/Unit trotzdem idempotent.
Commit: "setup: check runtime instead of building venv; guard unit write"
Rollback: `git revert`; bereits geschriebene Unit: restore aus Backup (s. root-Zeilen).

## V6 — Doku: README-Abschnitt „Installation ohne venv", CHANGELOG 0.2.1
Ziel: Weg B dokumentiert; `config.example.toml` UNVERÄNDERT (keine Runtime-
Relevanz). `bootstrap.py` selbst: keine Codeänderung (D2), README benennt es
ausdrücklich als Repo-/Notfall-Werkzeug, nicht als Installationspfad.
Dateien: `README.md`, `CHANGELOG.md`.
Testdatei: `tests/test_packaging_no_venv.py` ergänzt (asserts: README enthält
`--break-system-packages`-Befehl, CHANGELOG 0.2.1-Eintrag) — bewusst schlank.
Commit: "docs: document venv-less system-python3 installation"
Rollback: `git revert`.

## V7 — Full-Suite + Verifikation ohne Root (Agent)
Ziel: Gesamtbeweis vor dem Betreiber-Umschalten.
Dateien: keine Code-Änderung.
Test/Suite: `nice -n 19 .venv/bin/python -m pytest -q -p no:randomly
-o addopts=""` → 821 passed / 2 skipped (Baseline; neue Tests kommen obendrauf).
Zusätzlich (Agent, ohne sudo):
- `PYTHONPATH=src python3 -c "import stt_button"` (user-site installiert s. R3)
- `python3 -m pip list --user --format=freeze` Diff gegen
  `packaging/runtime-requirements.txt` + torch-Spezifikation
- `systemd-analyze verify systemd/stt-button.service` (nach @EXEC@-Substitution
  durch lokales Ersetzen mit dev-ExecStart in einer Kopie)
Rollback: n/a (nur lesend).

## root-Zeilen in Ausführungsreihenfolge (Betreiber; Vorher/Nachher je Schritt)
R0 (Agent, vorher): `cp -a ~/.config/systemd/user/stt-button.service{,.repo-bak}`
  (Unit-Backup = Rückholanker für R6) und Bestandsaufnahme
  `python3 -m pip list --user --format=freeze` (leer oder dokumentiert).
R1 `sudo dpkg --purge stt-button`
  (entfernt /opt/stt-button + /usr/bin/stt-button; Paket war iU=kaputt).
  Nachher: Produktion (Repo-Unit, .venv-ExecStart) unberührt und gesund.
  Rollback: keiner nötig; bei Reinstallation alte 0.2.0 NICHT verwenden.
R2 `sudo apt install -y python3-pip`  — Rollback: `sudo apt remove python3-pip`.
  Dann Agent: V0-Messtor (s. V0). V0-Bruch → STOPP hier, Addendum (c), kein R3.
R3 (Betreiber, Netz): ML-Stack ins User-Site:
  `python3 -m pip install --user --break-system-packages \
   -r packaging/runtime-requirements.txt` und
  `python3 -m pip install --user --break-system-packages \
   torch==2.14.1+cpu --index-url https://download.pytorch.org/whl/cpu`
  (Exakt-Versionen aus bootstrap.py:41 beibehalten.)
  Rollback: `python3 -m pip uninstall --user --break-system-packages <Liste>`.
R4 (Agent): V1–V6 implementiert, V7-Suite grün, .deb 0.2.1 gebaut
  (`tools/build-deb.sh`). Kein Dienst-Restart bis R6 — Repo-Produktion läuft weiter.
R5 (Betreiber): `sudo apt install ./dist/stt-button_0.2.1_amd64.deb`
  (oder `sudo dpkg -i`; apt löst Depends python3-pip selbst auf).
  Danach `stt-button setup` — jetzt SICHER: /opt existiert, aber V1-Launcher ist
  reif (V5-Wächter) → Unit wird auf `/usr/bin/stt-button run` gebogen.
  Rollback: Unit-Backup zurückkopieren (R0), `daemon-reload`, `restart`.
R6 (Betreiber): `systemctl --user daemon-reload && systemctl --user restart
  stt-button` — Rollback: R0-Backup restaurieren + restart; .deb:
  `sudo dpkg --purge stt-button`; User-Site: R3-Uninstall. Repo-.venv bleibt als
  letzter Anker unberührt.

## Beweis-Aufteilung
Agent (ohne root): Testsuite, Import-Proben (system python3 + user-site),
pip-freeze-Diff gegen Lock, `systemd-analyze verify`, journalctl-Leseprobe nach
R6 (`systemctl --user is-active` darf Agent lesen).
Betreiber: root-Zeilen R1/R2-apt/R5-install, R6-Restart, und der Blick auf
`systemctl --user status stt-button` + erste Diktaturprobe nach R6.

VERDICT: GO — unter Vorbehalt des V0-Messtors (numpy 2 vs. Distro-Erweiterungen).
Bei V0-Bruch: NO-GO für Variante (a), Addendum (c) PYTHONUSERBASE pro Dienst.
