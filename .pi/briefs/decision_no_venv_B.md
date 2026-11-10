# Betreiber-Entscheidung 2026-11-10 22:44: **Weg B — kein venv, Ubuntu-System-python3**

> "Wir setzen auf Weg B: Ubuntu-System-python3"

Kern: Der Service läuft mit **`/usr/bin/python3` aus dem Ubuntu-Paket** (3.12.3).
Dritte Python-Bibliotheken kommen per **`pip install --user`** nach
`~/.local/lib/python3.12/site-packages`. **Kein venv, kein Self-Healing-Bootstrap**
im Launcher. Offizielle Ubuntu-Pakete bleiben die Basis; PyPI liefert nur das, was
Ubuntu nicht hat (s. Tabelle unten).

## Gemessene Randbedingungen (22:47, eigene Messung, Host thinkbert / noble)
| Fakt | Messwert | Folge für Weg B |
|---|---|---|
| `/usr/lib/python3.12/EXTERNALLY-MANAGED` | vorhanden (645 B) | PEP 668 aktiv ⇒ `pip install` braucht `--break-system-packages` (auch für `--user`) |
| `python3 -m pip --version` | **`No module named pip`**, `pip3` nicht installiert | **`sudo apt install -y python3-pip` ist Schritt 1** (offizielles Ubuntu-Paket, passt zum Wunsch) |
| `site.ENABLE_USER_SITE` | `True`, `USER_SITE=~/.local/lib/python3.12/site-packages` (existiert noch nicht) | User-Site greift; **kein** `PYTHONNOUSERSITE` in der Unit (Unit setzt nur `HF_HOME=%h/.cache/huggingface`) ⇒ systemd-User-Dienst sieht `~/.local` |
| `systemd/stt-button.service` | `ExecStart=@EXEC@`, `TimeoutStartSec=1800` | 1800 s waren nur für den venv-Bootstrap ⇒ für Weg B überflüssig |
| `python3-torch` | **kein Kandidat** | torch/transformers/ctranslate2/onnxruntime/hub/faster-whisper **müssen** von PyPI — "nur Ubuntu-Pakete" ist für die ASR nicht erfüllbar |
| `python3-numpy` | 1.26.4, App will `numpy>=2` | Bei `--user` installiert pip numpy 2.x nach `~/.local` und **überdeckt** die Distro-Version: Distro-Programme, die gegen 1.26 gebaut sind (z. B. `python3-soundfile`, `python3-scipy`, `python3-pyaudio`), können im **user-site-**Modus ABI-Fehler bekommen — genau das ist der Punkt, den das Gutachten prüfen muss |
| App-Bedarf | `packaging/runtime-requirements.txt` (Pinek-Quelle), `pyproject.toml`: faster-whisper, transformers>=5.20, onnxruntime>=1.31, onnx-asr>=0.12, numpy>=2, ctranslate2>=4.8 | alles PyPI |

## Was das für das Paket bedeutet (noch zu planen, nicht decided)
- `Depends` in `packaging/debian/control`: `python3-venv` fliegt raus (wird nicht mehr
  gebraucht); `python3-pip` bleibt/kehrt als offizielle Paket-Abhängigkeit rein.
- `systemd/stt-button-launcher.sh`: Bootstrap-Abschnitt raus, `exec /usr/bin/python3 -m stt_button "$@"`
  mit `PYTHONPATH=/opt/stt-button/src`.
- `stt-button setup`: kein venv-Bau mehr; stattdessen Prüfen + Anleiten
  (pip-Pakete vorhanden? Versionen? numpy-Konflikt?).
- `src/stt_button/bootstrap.py`: für diesen Pfad funktionslos — Schicksal (behalten für
  Source-Tree-Entwicklung, oder entfernen) ist **Planungsentscheidung**, keine Orchestrator-Entscheidung.
- Aktueller dpkg-Zustand: `0.2.0 state=unpacked` (`iU`). Vor Weg B muss entschieden werden:
  `sudo dpkg --purge stt-button` und mit neuem Paket weiter, oder bestehendes Paket nach
  Dependency-Fix konfigurieren lassen.

## Verfahren (P9)
1. **Experte** `sa-42a77639` (`cortecs/glm-5.3`) liefert das Gutachten inkl. A/B/C/D und
   numpy-ABI-Risiko → `.pi/reports/dep_review_0.2.0.md`.
2. **Planer** `conv-f5bc04ce` bekommt diese Entscheidung + Gutachten und schreibt den
   TDD-Block-Plan (Launcher ohne Bootstrap, `setup`-Umstellung, `control`, Unit, Tests).
3. **Orchestrator** implementiert den Plan delegiert, Block für Block, und nimmt selbst ab
   (`git show --stat` + eigene VolSuite).

## Arbeitsgrundlage (Betreiber, 23:15, wörtlich)
> "Uebernehme den Kimi-Plan als Arbeitsgrundlage"

- **Grundlage:** `.pi/plans/plan_no_venv_B.md` (Planer `conv-f5bc04ce`, 143 Z., §4 korrigiert:
  R5 = `sudo apt install ./dist/stt-button_0.2.1_amd64.deb`).
- **Zweite Meinung, nicht Grundlage:** `.pi/plans/plan_no_venv_B_glm.md`
  (GLM-Planer `sa-8f7324cf`, 169 Z.). Behalten, weil ihre root-Kette R0-R6 die
  Pfad-Evidenz enthielt, die den Kimi-Plan korrigiert hat.
- Stand: **V1 abgenommen** (`44a4a31`, Gate-Exitlevel 1/2 selbst gemessen),
  **V2 in Arbeit** (`sa-dbaa7574`, `cortecs/glm-5.3`).
