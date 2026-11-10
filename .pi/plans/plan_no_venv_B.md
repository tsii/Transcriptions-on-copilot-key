# EXECUTION PLAN — Weg B: Runtime ohne venv (Planer, 2026-11-10 23:1x)

Base: Repo `STT-Button`, Produktion läuft aus dem Repo (`.venv/bin/stt-button -v run`, Dienst gesund,
Baseline 821 passed / 2 skipped). Auftrag `.pi/briefs/planner_task_no_venv_B.md`, Entscheidung
`decision_no_venv_B.md` (Betreiber 22:44), Gutachten `.pi/reports/dep_review_0.2.0.md`.

## VERDICT: **GO mit Gate — Weg B ist machbar, aber erst nach V1 (Mess-Gate) und den zwei Root-Schritten des Betreibers. Die Repo-Produktion wird in keinem Block angefasst: kein Block schreibt an die lebende Unit, `setup` verliert seine Unit-Biegefähigkeit.**

## 1. GROUND TRUTH (Planer, 23:0x, eigene Messung auf diesem Rechner)

| # | Fakt | Beleg |
|---|---|---|
| N1 | `/usr/bin/python3 -m pip` fehlt (`No module named pip`), `EXTERNALLY-MANAGED` vorhanden | Brief 22:47; Root-Schritt R2 nötig |
| N2 | **`/usr/bin/python3` kennt gar kein numpy/scipy/soundfile/pyaudio** (`find_spec` = False für alle vier) | Messung 23:05 — eine Shadowing-Opferliste existiert heute nicht |
| N3 | Der **aktuelle** Produktiv-`.venv` läuft mit `numpy 2.5.4` auf Python 3.12.3 mit exakt dem Pin-Set: torch 2.14.1+cpu, ctranslate2 4.8.4, transformers 5.20.2, onnxruntime 1.31.0, onnx_asr 0.12.0 | `.venv/bin/python -c "import numpy"`, dist-info-Versionen 23:05 |
| N4 | Kein Installations-Metadaten-Pin verlangt `numpy<2`: torch ohne numpy-Pin, ctranslate2 ungebunden, transformers `>=1.17`, onnxruntime `>=1.21.6`, onnx_asr `>=1.22.4` | dist-info `METADATA` 23:05 |
| N5 | **`numpy<2` wäre gemessen schädlich:** installiertes `scipy 1.18.1` verlangt `numpy>=2.0,<2.8` | scipy-METADATA 23:05 |
| N6 | Die App importiert auf Modulebene nur `numpy` + `requests`; torch/onnxruntime/ctranslate2 kommen lazy aus den Engine-Modulen | grep über `src/stt_button/*.py` 23:05 |
| N7 | Launcher: `PYTHONPATH="$ROOT/src" python3 -c … bootstrap.run_bootstrap(...)` (`:23-30`), danach venv-Python (`:33-40`) | `systemd/stt-button-launcher.sh` |
| N8 | Unit: `ExecStart=@EXEC@`, `TimeoutStartSec=1800` + venv-Kommentar (`:13-15`), `Environment=HF_HOME` (`:18`) | `systemd/stt-button.service` |
| N9 | `control:7` `Depends: python3, python3-venv, python3-pip, …`; postinst in `tools/build-deb.sh:93-109` ist nur ein Hinweis, der das venv-Bauen empfiehlt | Dateien 23:05 |
| N10 | **`_cmd_setup` biegt die Unit um:** `__main__.py:388-389` wählt `/usr/bin/stt-button run`, sobald `/opt/stt-button` existiert — und das ist hier der Fall | `__main__.py:380-389` + `ls /opt/stt-button` |

## 2. ENTSCHEIDUNGEN (Planer, hier gefallen — nicht dem Orchestrator überlassen)

### D1 numpy-Härte → **(a) Pin halten: `numpy==2.5.4`. Kein `numpy<2`, kein eigener Site-Pfad — dafür ein Gate, das Shadowing misst, sobald es auftritt.**
* Für (a) spricht N3–N5: genau dieser Pin läuft heute in Produktion; kein Pin verlangt
  numpy<2 (N4); `numpy<2` würde das gepinnte `scipy 1.18.1` (`numpy>=2.0,<2.8`) brechen (N5) und
  damit den Lock selbst verletzen — (b) ist gemessen die riskantere Variante, nicht die sichere.
* Shadowing (Kernangst des Gutachtens) trifft heute nichts: `/usr/bin/python3` hat gar kein
  numpy/scipy/soundfile/pyaudio (N2), und die App importiert auf Modulebene nur `numpy` + `requests`
  (N6). Das Risiko ist also **zukünftig und beobachtbar**, nicht vorhanden und akut.
* Deshalb (c) **nicht** als Vorleistung, sondern als **Rezept im Regelfall** (V7): sobald das Gate
  (V1) eine apt-Installation von `python3-numpy|scipy|soundfile|pyaudio` in `dist-packages`
  meldet, wird der Dienst auf `Environment=PYTHONUSERBASE=%h/.local/stt` umgestellt und die
  pip-Installation mit derselben Variable wiederholt. Das ist ein dokumentierter Zweig, keine
  tote Komplexität im Launcher.

### D2 `bootstrap.py` und `stt-button setup` → **Modul bleibt (entwicklungs-Werkzeug), aber raus aus jedem Laufzeitpfad; `setup` wird zum Prüfer, der die Unit nicht mehr anfassen darf.**
* `bootstrap.py` bleibt erhalten (Stamp-/Fingerprint-Logik ist getestet, `tests/test_bootstrap.py`)
  und wird per Docstring als **dev-only** markiert. Kein Block löscht es — Löschen wäre ein
  Eingriff in getesteten Code ohne Nutzen.
* Raus damit aus: Launcher (V2), `_cmd_setup` (V4), Unit-Kommentar (V3), postinst-Hinweis (V6),
  `control`-Depends (V5). Ein Guard-Test (V4) hält dauerhaft fest, dass weder Launcher noch
  `__main__.py` noch die Unit `bootstrap` referenzieren.
* `stt-button setup` darf künftig: prüfen (System-Python, pip, Pflichtmodule, Versionen gegen den
  Lock, PEP-668-Hinweis), Konfiguration anlegen, **und nur auf explizites `--install-unit`** eine
  Unit schreiben — mit Verweigerung, wenn die existente Unit auf ein anderes Ziel zeigt als die
  vorlagende Vorlage (N10 ist genau die Gefahr, die die Repo-Produktion beim nächsten Neustart
  getötet hätte). Ohne Flag bleibt es ein Dry-Run mit Klartext-Ausgabe des Zielziels.

## 3. BLÖCKE V1..V8 (ROT→GRÜN, ein Commit je Block, Suite `nice -n 19 .venv/bin/python -m pytest -q -p no:randomly -o addopts=""`)

Reihenfolge ist sicherheitsrelevant: **V1 zuerst**, dann R1/R2/R3 (§4), dann **V2..V8 im Repo
committet**, erst danach **R4** (Gate + `tools/build-deb.sh`) — denn das gebaute 0.2.1-Paket enthält
Launcher, Unit-Vorlage, `control` und postinst, also müssen V2/V3/V5/V6 schon committet sein, bevor
R4 das Deb baut. Danach R5 (Paket root-seitig) und R6 (Unit-Tausch). V2..V8 fassen keinen laufenden
Dienst an; die Repo-Produktion läuft während V1..V8 unverändert weiter.

| Block | Ziel | Dateien | Test (zuerst ROT) | Commit-Titel | Rückholweg |
|---|---|---|---|---|---|
| **V1** Mess-Gate | `tools/deps_gate.py`: prüft pip-Vorhandensein, Import der Pflicht-Module unter `/usr/bin/python3` mit `--src`-Pfad (Default `/opt/stt-button/src`, vor der Paket-Installation auf das Repo zu setzen), Versionsvergleich gegen `--requirements` (Default `/opt/stt-button/packaging/runtime-requirements.txt`; **vor R5 existiert dieser Pfad nicht — der Lauf auf der Repo-Kopie ist der vorgesehene Weg**), und ob `dist-packages` numpy/scipy/soundfile/pyaudio enthält. Geschlossene Exitlevel-Menge: **0** = alles gut, **1** = `pip_missing`/`deps_missing`, **2** = `requirements_missing` (Datei nicht gefunden — genau die R1/R3-Verwechslung), **3** = `shadow_risk`. Schreibt `.pi/reports/deps_gate.md` | `tools/deps_gate.py` (neu) | `tests/test_deps_gate.py` (neu): heute meldet das Gate `pip_missing`/Level 1; `--requirements /nonexistent` meldet **`requirements_missing`/Level 2** und **nicht** `deps_missing`; Fake-Vollständigkeit meldet 0; apt-numpy-Fall meldet `shadow_risk`/3 | `feat(tools): dependency gate for venv-free runtime` | `rm tools/deps_gate.py tests/test_deps_gate.py` (neue Dateien) |
| **V2** Launcher ohne Bootstrap | Bootstrap-Block (`:23-31`) und venv-Teil (`:32-38`) raus; stattdessen Import-Check der Kernmodule mit klarem pip-Hint, dann `exec /usr/bin/python3 -m stt_button "$@"`. `PYTHONPATH` **nur** `$ROOT/src` (enthält nur `stt_button`, kein Fremdmodul) | `systemd/stt-button-launcher.sh` | `tests/test_launcher.py` (neu): `sh -n` grün; enthält kein `run_bootstrap`/`venv`; enthält `exec /usr/bin/python3 -m stt_button`; `PYTHONPATH`-Zeile nennt ausschließlich `$ROOT/src`; Import-Check + Hint vorhanden | `refactor(deb): launcher runs system python without bootstrap` | `git revert` (eine Datei) |
| **V3** Unit-Vorlage ohne Bootstrap-Timeout | `TimeoutStartSec=1800` + venv-Kommentar (`:13-15`) raus, `@EXEC@`-Kommentar auf Weg B; `HF_HOME` bleibt. **Reine Vorlagenänderung, die lebende Unit wird nicht angefasst** | `systemd/stt-button.service` | `tests/test_unit_template.py` (neu): kein `1800`, kein Wort `venv`, `Environment=HF_HOME` vorhanden, `ExecStart=@EXEC@` unverändert | `refactor(systemd): drop one-time venv build timeout` | `git revert`; geladene Unit ist davon unberührt |

| **V4** `setup` ohne venv, mit Unit-Schutz | `_cmd_setup` (`__main__.py:330-405`) umbauen: kein `run_bootstrap`/`probe_venv`/`mark_ready`; stattdessen Gate-Prüfung (V1-Funktionen aus `tools/deps_gate.py` importierbar machen oder identische Prüfroutine in `bootstrap`-freier Helper), Konfig-Seeding wie bisher, Unit **nur** mit `--install-unit`; bei existenter Unit mit anderem `ExecStart` → Abbruch mit Grund; ohne Flag Dry-Run | `src/stt_button/__main__.py` | `tests/test_setup_cli.py` (neu): (a) `setup` ohne Flag schreibt **keine** Unit-Datei; (b) `setup --install-unit` mit abweichender existenter Unit → Exitlevel 1, Datei unverändert; (c) kein Aufruf von `bootstrap` aus `__main__.py` (Quelltext-Grep); (d) Seeding einer fehlenden Config funktioniert | `feat(cli): setup verifies runtime, no venv build, guarded unit install` | `git revert` (eine Datei); an der Nutzer-Unit wurde nichts geändert |
| **V5** `control`-Abhängigkeiten | `Depends: python3, python3-pip, pipewire-bin, pulseaudio-utils, wtype` (`python3-venv` raus); Beschreibungstext auf `pip install --user --break-system-packages` umformulieren | `packaging/debian/control` | `tests/test_deb_control.py` (neu): `python3-venv` nicht in `Depends`, `python3-pip` darin, Beschreibung enthält `--user`, kein Wort `venv` in der Laufzeitbeschreibung | `fix(packaging): drop python3-venv, require python3-pip` | `git revert` |
| **V6** postinst-Hinweis auf Weg B | Text `tools/build-deb.sh:93-109`: Schritte 1) `pip install --user --break-system-packages -r /opt/stt-button/packaging/runtime-requirements.txt` (+ torch-Zeile), 2) `stt-button setup`, 3) Neustart; kein venv-Satz mehr | `tools/build-deb.sh` | in `tests/test_setup_script.py` (bestehend, erweitern): `bash -n` grün, postinst-Text enthält `--break-system-packages` und kein `venv` | `fix(packaging): postinst guides the user-site install` | `git revert` |

| **V7** Doku | README-Abschnitt „Installation ohne venv (Weg B)“: Root-Zeilen, `pip install --user --break-system-packages`, **und das Isolations-Rezept für den Shadow-Fall** (`Environment=PYTHONUSERBASE=%h/.local/stt` in der Unit + identische Variable bei der Installation); CHANGELOG 0.2.1; `bootstrap.py`-Docstring auf „dev-only, kein Laufzeitpfad mehr“ | `README.md`, `CHANGELOG.md`, `src/stt_button/bootstrap.py` (nur Docstring) | `tests/test_docs.py` (bestehend, erweitern): README enthält „Weg B“, `--break-system-packages`, `PYTHONUSERBASE`; CHANGELOG enthält 0.2.1 | `docs: venv-free installation path (way B)` | `git revert` |
| **V8** Nahtkontrolle | Sammelnder Guard, dass Weg B tatsächlich überall gilt: Launcher, `__main__.py`, Unit-Vorlage, `control`, postinst und `build-deb.sh` erwähnen kein Bootstrap/venv mehr; `bootstrap.py` bleibt importierbar (dev) | `tests/test_wayb_wiring.py` (neu) | Testdatei selbst ist der Block: jede der sechs Stellen wird per Muster geprüft; ROT, solange ein anderer Block noch nicht umgebaut ist — V8 wird deshalb **zuletzt** committet | `test(packaging): guard that no runtime path uses bootstrap` | `git revert` |

**Kein Block** darf: die geladene Unit unter `~/.config/systemd/user/` schreiben, `sudo` ausführen,
etwas unter `/opt` oder `~/.local` installieren, oder `summary`-/`typing`-/ASR-Code anfassen.

## 4. ROOT-ZEILEN FÜR DEN BETREIBER (Ausführungsreihenfolge, P5; Agenten führen root nicht aus)

Zwei Lücken des ersten Entwurfs sind hier geschlossen: (a) **R1-`purge` löscht `/opt/stt-button`
und `/usr/bin/stt-button`** (dpkg-Dateiliste) — ab da liegt die requirements-Datei **nur noch im
Repo**, also installiert R3 aus der Repo-Kopie; (b) nach R1 gibt es kein `stt-button`-Programm mehr,
deshalb kommt die **Paket-Installation der 0.2.1 als eigene root-Zeile R5 NEU dazu**, und zwar
**vor** dem Unit-Tausch. Grund gegen die Repo-Alternative: `.venv/bin/stt-button setup --install-unit`
schreibt `ExecStart=/usr/bin/stt-button run` — ein Ziel, das nach R1 nicht existiert, also eine tote
Unit beim nächsten Start. Erst das installierte Paket bringt den Weg-B-Launcher.

* **R0 (Agent, kein Root):** `tools/deps_gate.py --src <repo>/src --requirements <repo>/packaging/runtime-requirements.txt`
  → `.pi/reports/deps_gate.md`. Erwartet: Level 1 `pip_missing`. *Rückhol:* nichts (nur lesend + Report).
* **R1 (root):** `sudo dpkg --purge stt-button` — beendet `iU`; **löscht `/opt/stt-button` und
  `/usr/bin/stt-button`**. Kein `apt -f install`. Repo-Produktion (`.venv`, geladene Unit) bleibt an.
  *Rückhol:* `sudo dpkg -i <0.2.0.deb>`; Dateien sind wieder da.
* **R2 (root):** `sudo apt install -y python3-pip`. *Rückhol:* `sudo apt remove -y python3-pip`.
* **R3 (User, kein root):** `python3 -m pip install --user --break-system-packages -r <repo>/packaging/runtime-requirements.txt`
  (die **Repo-Kopie**, `/opt` existiert nicht mehr), danach `python3 -m pip install --user
  --break-system-packages --index-url https://download.pytorch.org/whl/cpu torch==2.14.1+cpu torchaudio==2.11.0+cpu`.
  *Rückhol:* `python3 -m pip uninstall --break-system-packages -r <…>` oder `rm -rf ~/.local/lib/python3.12/site-packages`.
* **R4 (Agent/User, kein root):** Gate erneut mit Repo-Pfaden — **Level 0 ist Bedingung** für alles
  Folgende (`requirements_missing`/Level 2 bedeutet: falscher Pfad, nicht fehlende Pakete). Danach
  baut der Agent das Paket: `tools/build-deb.sh` → `dist/stt-button_0.2.1_amd64.deb`.
  *Rückhol:* Report löschen, `rm -rf dist/` — System unverändert.
* **R5 (root, NEU):** `sudo apt install ./dist/stt-button_0.2.1_amd64.deb` — bringt
  `/usr/bin/stt-button` (Weg-B-Launcher) und `/opt/stt-button` zurück. Postinst schreibt nichts.
  *Rückhol:* `sudo dpkg --purge stt-button` (Zustand von R1), Repo-Dienst läuft weiter.
* **R6 (User, P6-Freigabe nötig):** `/usr/bin/stt-button` ist da → erst Dry-Run
  `/usr/bin/stt-button setup` (ohne Flag, schreibt nichts), dann
  `cp ~/.config/systemd/user/stt-button.service ~/.config/systemd/user/stt-button.service.bak-<datum>`,
  `sudo`-frei `/usr/bin/stt-button setup --install-unit`, `systemctl --user daemon-reload`,
  `systemctl --user restart stt-button`, Journal lesen; Gate einmal mit Default-Pfaden (`/opt/...`)
  als Beleg, `systemd-analyze verify` der geschriebenen Unit.
  *Rückhol:* `.bak` zurückkopieren, `daemon-reload`, `restart` — die Repo-Produktion läuft wieder.
* **R7 (optional, erst nach grünem R6):** Repo-`.venv` bleibt als Rückfall-Reserve erhalten und wird
  **nicht** gelöscht; ihr Wegfall ist ein eigener Betreiberbeschluss.


## 5. VERIFIKATION

**Agenten liefern (ohne Root):**
1. VolSuite unverändert grün: 821 passed / 2 skipped + die neuen Tests, mit FL1/FL2-Nachweisprotokoll
   (`plan_summary_mode_v2.md` §6.1) — ein Ausfall dort ist erst nach 3/3 Einzelgrün als Flake verbucht.
2. `git diff`-Beleg: keine Änderung an `typing.py`, `asr.py`, `config.py`, `controller.py`, `summary.py`.
3. Importnachweis: `PYTHONPATH=<repo>/src python3 -c "import stt_button"` und
   `… -c "import numpy, ctranslate2, onnxruntime, transformers, faster_whisper, torch"` unter
   `/usr/bin/python3` mit Nutzer-Site — **vor R5 gegen das Repo**, ab R5 zusätzlich gegen
   `/opt/stt-button/src` (Beleg für R4 bzw. R6).
4. `pip list --user` gegen `packaging/runtime-requirements.txt` (Diff-Liste, keine Abweichung außer
   dokumentierten), `tools/deps_gate.py`-Report, `systemd-analyze verify` der **vorlagen**Unit aus
   einer Temp-Kopie (die geladene Unit wird nicht verifiziert, sie ist ja noch die alte).

**Bleibt beim Betreiber (nicht durch Agenten beweisbar):** R1–R3, R5 (Neustart des lebenden Dienstes),
und der praktische Diktat-Lauf nach R5 (Taste drücken, Text im Feld). Abnahme: Repo-Dienst läuft
während V1–V8 durchgehend unverändert; nach R5 läuft der System-Python-Dienst.

**Risiken, die offen bleiben (ehrlich):**
* PyPI-Erreichbarkeit/Zustellbarkeit der Pins ohne venv ist erst nach R2/R3 messbar; V1 meldet das
  als `pip_missing`/`deps_missing` und blockiert damit R5 — der Umstieg stoppt, statt einen toten
  Dienst zu bauen.
* Shadowing durch spätere apt-Installationen ist nicht ausgeschlossen, aber durch V1 sichtbar und
  über D1(c) isolierbar.
* `numpy 2.5.4` auf dem System-Python-Pfad ist eine Nutzer-weite Änderung; der Rückschritt ist
  R3-Rückhol (Site-Verzeichnis löschen), nicht `apt`.

**Noch offene Betreiberfragen: null** — D1 und D2 sind hier entschieden; die einzigen vom Betreiber
auszuführenden Punkte sind R1/R2/R3 (root) und R5 (P6-Freigabe), jeweils mit Rückholweg.

