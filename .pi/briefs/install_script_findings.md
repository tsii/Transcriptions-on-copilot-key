# Nachtrag zum Experten-Auftrag — die Installation ist an drei Stellen blockiert (gemessen)

Dein Skript `.pi/tools/install_wayb_0.2.1.sh` ist in der Abnahme durchgekommen und wurde
ausgefuehrt: **R2 und R3 sind erfolgreich erledigt** (62 Pakete im User-Site, torch
`2.14.1+cpu` + alle 60 Pins aus `packaging/runtime-requirements.txt`, pip 24.0现 da).
Dann blieb die Kette bei **R4** haengen — und der Grund ist **nicht** Dein Skript, sondern
drei Fehler, die das Skript nur sichtbar macht. Alles unten ist gemessen, nicht interpretiert.

## Befund A — das Gate kann Level 0 nie erreichen (blockiert R4 und damit R5)
```
$ .venv/bin/python tools/deps_gate.py --python /usr/bin/python3 --src ./src \
      --requirements ./packaging/runtime-requirements.txt --report ...
exit=1   deps_missing   not importable: pyaudio
```
Gegenprobe: `pyaudio` kommt **nirgends** im Code vor (`grep -rniE "pyaudio" src/` trifft nur
`deps_gate.py` selbst: `REQUIRED_MODULES = ["numpy","scipy","soundfile","pyaudio","requests"]`
Zeile 32, `SHADOW_MODULES` Zeile 33). Es steht **nicht** in `packaging/runtime-requirements.txt`,
es ist **nicht** im User-Site und es war **nie** im Produktions-`.venv` (nachgemessen: 0).
Die Aufnahme laeuft ueber das externe Programm **`pw-record`** (`src/stt_button/audio.py:3,88`),
also ohne Python-Audio-Bibliothek.
Folge: Der harte Level-0-Zwang vor R5 ist mit dem aktuellen `REQUIRED_MODULES` **unerfuellbar** —
die Kette kann nicht durchlaufen, egal wie richtig R3 ist.

## Befund B — der verpackte Launcher wuerde den Dienst beim Start verweigern
```
$ PYTHONPATH=./src /usr/bin/python3 -c 'import sounddevice, numpy, stt_button'
ModuleNotFoundError: No module named 'sounddevice'
$ PYTHONPATH=./src /usr/bin/python3 -c 'import numpy, faster_whisper, onnx_asr, requests, stt_button'
(exit 0, alles importierbar)
```
Der Launcher (`systemd/stt-button-launcher.sh:16`) prueft genau `import sounddevice, numpy,
stt_button`. `sounddevice` wird vom Code **nicht** verwendet, ist nicht in den Requirements,
nicht im User-Site, nicht im System-`dist-packages` und war nie im `.venv`. Nach R6 (Unit-Tausch)
wuerde der Dienst also mit deiner Import-Pruefung **exit 1** liefern, ohne je zu starten.
Dein Skript wuerde das im Gesundheits-Check (`active`/ERROR/`Ready.`) sehen und die Sicherung
zurueckspielen — die Installation bliebe aber unvollendet.
Anmerkung zur Abnahme: Die Launcher-Tests pruefen nur Textmuster; der Inhalt der
Import-Pruefung wurde nie ausgefuehrt — deshalb fiel das erst jetzt auf.

## Befund C — der Pip-Hint im Launcher verweist auf eine Datei, die es im Paket nicht gibt
`systemd/stt-button-launcher.sh:19-20` nennt `pip install '/opt/stt-button[stt]'` und
`$ROOT/requirements-stt.txt`. Im gebauten Paket existiert **keine** `requirements-stt.txt`
(`dpkg-deb -c dist/stt-button_0.2.1_amd64.deb | grep -c requirements-stt` = 0); shipped wird
`/opt/stt-button/packaging/runtime-requirements.txt`.

## Befund D — Reihenfolgschutz wirkt, laesst sich aber ohne R5-R4-Nachweis erreichen (gemessen)
Der Betreiber hat aus Versehen die Reihenfolge uebersprungen und **R6 direkt** gestartet
(R4 war durch Befund A blockiert, R5 nie ausgefuehrt):
```
$ bash .pi/tools/install_wayb_0.2.1.sh --apply --only=R6 --switch-unit
  dpkg state: <none>
  service is-active: active
== R6: switch the live unit (requires --switch-unit) ==
ABORT: unit source missing: /usr/lib/systemd/user/stt-button.service
```
Das ist **korrektes Verhalten** und die laufende Unit blieb unberuehrt (live ExecStart vorher
noch `/home/.../.venv/bin/stt-button -v run`, keine Schreibgriffe). Aber es zeigt: ein einzelner
`--only`-Sprung prueft nur die *Spuren* des vorigen Schritts, nicht dass der Schritt selbst
成功 war. Pruefe und beantworte: erfuellt R5 denselben Massstab (haelt es R4_Level_0 als
Voraussetzung, oder prueft es nur die .deb-Datei auf Platte)? Wenn nicht, schlage einen
Nachweis vor, der **ohne** Zustand zwischen den Aufrueten traegt (z. B. R5 fuehrt das Gate
selbst noch einmal aus, statt auf R4 zu vertrauen) — Begrundung in zwei Zeilen.

## Was ich von dir will (in dieser Reihenfolge)
1. **Diagnose in max 10 Zeilen**: Halte ich fuer zutreffend? Wo siehst du es anders?
   Besondere Pruefung: Ist Befund A ein Designfehler des Gates (Module, die der Code nicht
   importiert, werden verlangt) oder ein Hinweis darauf, dass `pyaudio` frueher gebraucht und
   spaeter durch `pw-record` ersetzt wurde — und was folgt daraus fuer `SHADOW_MODULES`
   (Zeile 33, betrifft die Level-3-Erkennung)?
2. **Minimale Korrekturvorschlaege je Befund, als exakte Patch-Angabe (Datei, Zeile,
   Vorher/Nachher)** fuer: `src/stt_button/deps_gate.py` (A), `systemd/stt-button-launcher.sh`
   (B und C), und was die zugehoerigen Tests (`tests/test_deps_gate.py`,
   `tests/test_launcher.py`) jeweils neu verlangen muessen — insbesondere ein Test, der die
   Import-Pruefung des Launchers **ausfuehrt** statt nur zu lesen (ideell gegen ein tmp-Verzeichnis,
   nicht gegen das echte System).
3. **Dein Skript anpassen, soweit es dir gehoert**: R4 soll die Level-0-Pflicht nicht
   unmoeglich machen, bevor A gefixt ist — schlage vor (a) R4 mit `--permissive`-Schalter
   (Level 1 mit ONLY `pyaudio` als voruebergehend akzeptiert, klar im Log vermerkt), oder
   (b) R4 laesst Level 0 verlangen und die Kette bleibt bis zum Gate-Fix zu. **Entscheide du**,
   begruende in zwei Zeilen und implementiere nur die Version, die das Skript allein leisten kann
   (Gate und Launcher aenderst du NICHT selbst, siehe Grenzen).
4. **Kurzer Beleg** dass eine Aenderung an R4 die vorhandene Sicherheitskette nicht aufweicht:
   Dry-Run unveraendert, `--apply --only=R5` prueft weiter vor der Installation, Rollback unveraendert.

## Grenzen (verbindlich)
* Erlaubt zu aendern: `.pi/tools/install_wayb_0.2.1.sh` und `tests/test_install_wayb_script.py`.
* **Verboten zu aendern**: `src/**` (insb. `deps_gate.py`), `systemd/**` (insb. den Launcher),
  `packaging/**`, `tools/**`, Doku, `pyproject.toml`, `tests/**` ausser Deiner Testdatei.
  Fuer Befund A/B/C schreibst du nur den Patch-Vorschlag — umsetzen darf das nur der
  Planer-Auftrag ueber mir (P9-Planungsbesitz).
* Kein `git commit`/`push`, kein sudo/apt/pip/systemctl/dpkg, kein Netzwerk, kein Schreiben
  unter `~/.config/`, keine Tastatur-/Clipboard-/Ton-Aktionen. Die Produktion des Betreibers
  laeuft weiter aus dem Repo und wird live genutzt.
* Schlussmeldung max 12 Zeilen, mit dem **tatsaechlich gelaufenen Modell**.
