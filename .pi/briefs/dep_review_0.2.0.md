# Experten-Auftrag: Debian-Abhängigkeiten von stt-button 0.2.0 (dpkg-Stand iU)

## Gemessener Grundwahrheits-Stand (2026-11-10, 22:36 UTC, Host `thinkbert`)
- `sudo dpkg -i dist/stt-button_0.2.0_amd64.deb` ⇒ ausgepackt, **nicht konfiguriert**:
  `dpkg -l` zeigt `iU`, `dpkg-query -W -f='${db:Status-Status}'` = `unpacked`.
- Grund laut dpkg-Ausgabe: `stt-button depends on python3-venv; not installed`,
  `stt-button depends on python3-pip; not installed`.
- `packaging/debian/control` Zeile 7/8:
  `Depends: python3, python3-venv, python3-pip, pipewire-bin, pulseaudio-utils, wtype`
  `Recommends: wl-clipboard, dotool, gnome-shell`
- apt-Kandidaten vorhanden: `python3-venv 3.12.3-0ubuntu2.1`, `python3-pip 24.0+dfsg-1ubuntu1.3`.
- `python3 -V` = 3.12.3; `python3 -c "import venv"` geht, `import ensurepip` **fehlt**
  ⇒ System-python kann ohne `python3-venv` **keinen** venv mit pip bauen.
- Dateien sind durchs Unpack bereits da: `/usr/bin/stt-button` (Launcher, root-owned),
  `/opt/stt-button/` (Code, 132 kB Paket, **kein** venv, keine Modelle).
- Der Launcher (`systemd/stt-button-launcher.sh`) baut den Per-User-venv
  `~/.local/stt-button/venv` beim Erstaufbau selbst („self-healing", 5–15 min),
  die User-Unit hat `TimeoutStartSec=1800` genau dafür.
- `stt-button setup` (`src/stt_button/__main__.py::_cmd_setup`, Zeile 330) ist idempotent,
  seedet `~/.config/stt-button/config.toml` **nur wenn fehlend** und schreibt die
  User-Unit auf `/usr/bin/stt-button run`, sobald `/opt/stt-button` existiert.
- Produktion läuft aktuell **aus dem Repo** (`ExecStart=/home/salmon/pi-web-ui/STT-Button/.venv/bin/stt-button -v run`),
  Dienst gesund (`active`, NRestarts=0, 0 ERROR-Zeilen).
- `.pi/tools/finish_install_0.2.0.sh` (commit `1b99d4e`) weigert sich jetzt in diesem Zustand
  (Exit 20; `dpkg -W -f Version` liefert nämlich trotzdem `0.2.0`).

## Entscheidungsfragen (Antwort als Kurzgutachten, max 20 Zeilen)
1. **Ist `Depends: python3-venv, python3-pip` korrekt** — oder gehört das nicht in
   `Depends`, weil es nur für den Bootstrap beim Erstaufbau nötig ist, nicht für den Betrieb
   eines bereits gebauten venv? (Debian-Policy-Sicht: `Depends` vs `Recommends` vs `Pre-Depends`,
   und was bedeutet die Wahl für `apt install ./stt-button_*.deb` auf einem Minimal-System?)
2. **Minimale Behebung auf DIESEM Host** in einer Zeile für den Betreiber:
   `python3-venv` (Meta) vs `python3.12-venv` (exakt) vs `python3-full`; braucht der Bootstrap
   überhaupt `python3-pip` im System, oder reicht venv + pip-from-venv (siehe
   `src/stt_button/bootstrap.py`: was genau wird aufgerufen — `python3 -m venv`,
   `pip install`, `ensurepip`, `uv`?). Bitte aus dem Code belegen, nicht aus Gewohnheit.
3. **Sollte der Launcher/`setup` fehlende venv-Fähigkeit abfangen**, bevor er 5–15 min
   Bootstrap probiert? Prüfe, was `bootstrap.run_bootstrap()`/`probe_venv()` heute tut, wenn
   `ensurepip` fehlt (Fehlermeldung? halbfertiger venv mit `ready`-Stamp?), und schlage die
   kleinste Korrektur vor.
4. **Was muss am Paket für 0.2.1 geändert werden** (`packaging/debian/control`,
   `tools/build-deb.sh`)? Konkret: Abhängigkeitsart, Versionsexaktheit, und ob der
   Bootstrap-Beleg (venv-Stamp) erst nach erfolgreichem Lauf gesetzt werden darf.

## harte Grenzen
Read-only: **kein** `sudo`, **kein** `apt`, **kein** `dpkg -i/--configure`, kein Schreiben außer
in `.pi/reports/dep_review_0.2.0.md`. Keine Tastatur-/Clipboard-/Ton-Emission, kein Netzwerk,
keine Änderung an `src/**` (das entscheidet später die Implementierungskette), kein `git push`.

## Erwartete Lieferung
Datei `.pi/reports/dep_review_0.2.0.md` mit: (a) Befund, (b) eine Befehlszeile für den
Betreiber, (c) `control`/`build-deb.sh`-Änderungsvorschläge für 0.2.1 mit Begründung,
(d) Risiko, das beim jetzigen `iU`-Zustand besteht (halbfertiger Zustand, `apt -f install`,
Purge-Pfad). Modelle/Tools: `read`/`grep`/`bash` (nur lesende Befehle).

---

# NEUER AUFTRAG (Betreiber, 22:44): Es soll GAR KEIN venv mehr gebaut werden.

> "Ich möchte, dass kein venv installiert wird. Ich möchte die offiziellen Ubuntu
> Python Pakete nutzen."

Damit ist die Frage nicht mehr "welche Abhängigkeiten für den Bootstrap", sondern
**"wie läuft der Service ohne venv, auf Ubuntu-System-python"** — und der
Self-Healing-Bootstrap im Launcher wird zum Kandidaten fuer die Loeschung.

## Zusatz-Messung (22:44, Ubuntu 24.04.5 LTS "noble", sources: main restricted universe multiverse)

| Paket | Kandidat |
|---|---|
| `python3-torch` | **kein Kandidat** (`Installed: (none)`, `Candidate: (none)`) |
| `python3-torchaudio`, `python3-transformers`, `python3-onnxruntime`, `python3-ctranslate2`, `python3-huggingface-hub`, `python3-sounddevice` | **KEIN PAKET** |
| `python3-numpy` | 1:1.26.4 (App verlangt laut `pyproject.toml` `numpy>=2`) |
| `python3-scipy` 1.11.4, `python3-soundfile` 0.12.1, `python3-pyaudio` 0.2.13, `python3-yaml` 6.0.1, `python3-toml` 0.10.2, `python3-requests` 2.31.0 | vorhanden |
| `intel-oneapi-pytorch` | vorhanden (Quelle `apt.repos.intel.com/oneapi` ist auf diesem Rechner already eingerichtet) |
| System-python3 = 3.12.3, `import torch` → ModuleNotFoundError |

App-Bedarf laut `pyproject.toml`: `faster-whisper`, `transformers>=5.20`,
`onnxruntime>=1.31`, `onnx-asr>=0.12`, `numpy>=2`, `ctranslate2>=4.8`
(Pinek-Quelle: `packaging/runtime-requirements.txt`).

## Neue Entscheidungsfragen (Punkt 5 im Gutachten, Pflicht)
1. **Ist "nur offizielle Ubuntu-Pakete" für diesen Service überhaupt erfüllbar?**
   Belege es mit obiger Tabelle: torch/transformers/ctranslate2/onnxruntime/hub fehlen
   komplett. Wenn nein: was ist die kleinste Abweichung vom Wunsch, die läuft?
2. **Optionen ohne venv**, mit Vor-/Nachteilen und Risiko für das **System-python**:
   - (A) `python3 -m pip install --break-system-packages -r packaging/runtime-requirements.txt`
     direkt ins System (`/usr/lib/python3/dist-packages`) — PEP 668, Kollisionsgefahr mit
     apt-verwalteten Paketen, `apt`-Tools könnten leiden.
   - (B) `python3 -m pip install --user --break-system-packages` → `~/.local/lib/python3.12/site-packages`,
     apt-Verzeichnis **unangetastet**, nur User-Umgebung. Bitte prüfen: ignoriert der
     systemd-User-Dienst `~/.local`? (Nein, aber `PYTHONNOUSERSITE`/`PYTHONUSERBASE`-Fallen
     in `systemd/stt-button.service` und Launcher kontrollieren.)
   - (C) venv doch, aber **vom Betreiber von Hand** gebaut, Launcher/`setup` bauen nichts mehr.
   - (D) `intel-oneapi-pytorch` als apt-Quelle für torch — reicht das nachweislich **nicht**
     (transformers/hub/faster-whisper/ctranslate2 fehlen weiterhin), also eher nicht tragfähig;
     bitte aus den Paketinhalten begründen, nicht aus der Vermutung.
3. **numpy-Konflikt:** Ubuntu 1.26 vs. `numpy>=2`. Was passiert bei (B) mit dem
   C-ABI-andern Paketen (soundfile, scipy, pyaudio)? Welcher Bruch wäre konkret?
4. **Was am Paket ändern**, wenn (B) oder (C) gewählt wird: `Depends`-Zeile
   (`python3-venv, python3-pip` raus?), Launcher ohne Bootstrap, `stt-button setup` ohne
   venv-Bau, `postinst`-Hinweis, `TimeoutStartSec=1800` nicht mehr noetig, und **der
   aktuelle `iU`-Zustand** (purge-Pfad).
