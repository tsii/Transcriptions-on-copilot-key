# Dispatch V8 — Nahtkontrolle: gilt Weg B wirklich ueberall? (Kleinstpaket: EINE neue Testdatei)

Repo `/home/salmon/pi-web-ui/STT-Button`. Plan `.pi/plans/plan_no_venv_B.md` — **nur die
V8-Zeile** lesen (Tabelle §3), sonst nichts.

## Vorgabe des Planers (V8-Zeile, inhaltlich woertlich)
Sammelnder Guard, dass Weg B tatsaechlich ueberall gilt:
* **Launcher** (`systemd/stt-button-launcher.sh`), **`src/stt_button/__main__.py`**,
  **Unit-Vorlage** (`systemd/stt-button.service`), **`packaging/debian/control`**,
  **postinst-Text in `tools/build-deb.sh`** und **`tools/build-deb.sh`** selbst
  — sie erwaehnen **kein Bootstrap / venv** mehr als Laufzeitpfad.
* zusaetzlich: **kein „self-healing"** in `control` / `build-deb.sh` / `README.md`.
* `bootstrap.py` **bleibt importierbar** (dev-Werkzeug, Entscheidung D2 — es darf also nicht
  verschwinden oder kaputt gehen).
Die Testdatei **ist** der Block; jede der sechs Stellen wird per Muster geprueft.

## Was bereits gilt (Stand beim Dispatch, damit du nicht umsonst grabst)
V2 Launcher, V3 Unit-Vorlage, V4 `setup`-Umbau, V5 `control`, V6 postinst, V7 Doku,
V7b Doku-Altbestand — alle committet. Dein Test soll also **GRUEN** herauskommen; er ist der
Beweis, dass die Serie nahtlos ist, und in Zukunft die Regressionssperre.

## Testpflicht — NEUE Datei `tests/test_wayb_wiring.py`
Pruefe jede Stelle einzeln und mit **klar benannter Fehlermeldung pro Stelle** (welche Datei
welches Muster verletzt — ein Sammel-assert ohne Herkunft ist unbrauchbar):
1. Launcher: kein `bootstrap` / `run_bootstrap`, **kein** `venv`; enthaelt
   `exec /usr/bin/python3 -m stt_button`.
2. `__main__.py`: **im Rumpf von `_cmd_setup`** (per `inspect.getsource`) kein `bootstrap`;
   der Unterbefehl `stt-button bootstrap` bleibt erlaubt (die Datei insgesamt enthaelt ihn).
3. Unit-Vorlage: `venv` nirgends, `ExecStart=@EXEC@` vorhanden.
4. `control`: kein `python3-venv` in `Depends`, `python3-pip` darin, kein `venv` im
   Beschreibungstext, kein `self-healing` (case-insensiv).
5. postinst-Text in `build-deb.sh`: enthaelt `--break-system-packages`, kein `venv`,
   kein `self-healing`.
6. `bootstrap.py`: weiter importierbar (`import stt_button.bootstrap`) und sein Docstring
   sagt dev-only.
Zusatz: `tools/deps_gate.py` bleibt eine duenne Huelle (keine Messlogik) — das haelt V4a fest.

## TDD + Abschluss
Lege die Datei an; wenn ein Muster **rot** bleibt, ist das ein echter Fund: **nicht** das
Muster absaeben, sondern in der Schlussmeldung mit Datei:Zeile melden (der Orchestrator
eskaliert es an den Planer). Nur wenn alles gruen ist: commiten.
Blocktests dieser Datei laufen lassen. **Keine VolSuite** (FH1 faehrt parallel seine
Abnahme-Laefe, Timing-empfindlich).
Commit: `test(packaging): guard that no runtime path uses bootstrap`

## Erlaubte Dateien
NEU `tests/test_wayb_wiring.py` — **mehr nicht**.
Verboten: jede andere Datei im Repo (Produktcode, Doku, Packaging, Skripte, andere Tests),
insb. `tests/test_controller.py` (parallel in Arbeit); kein sudo/apt/pip/dpkg, kein Netzwerk,
keine Tastatur-/Clipboard-/Ton-Emission, **kein git push**, kein Dienst-Neustart,
kein Schreiben unter `~/.config/`.

## Stall-Prophylaxe (verbindlich)
* Datei muss kurz bleiben (<= 120 Z.), `write` in einem Stueck ist hier ok.
* KEINE `while pgrep`-Wartespruenge, KEINE 20x-Schleifen, KEIN conftest.py-Gesuche.
* Wenn dein letzter Schritt ein Werkzeug war und dir nichts mehr bleibt: melde das in EINER
  Zeile, statt schweigend zu enden.
* Schlussmeldung max 5 Zeilen (Sha, N passed, gefundene Musterverletzungen oder "keine",
  tatsaechlich gelaufenes Modell).
