# Dispatch V7b — Doku-Altbestand auf Weg B umschreiben (Kleinpaket: 3 Textdateien + BESTEHENDE Testdatei)

Repo `/home/salmon/pi-web-ui/STT-Button`. Plan `.pi/plans/plan_no_venv_B.md` — **nur die
V7b-Zeile (Zeile 75)** lesen, sonst nichts.

## Vorgabe des Planers (V7b-Zeile, inhaltlich woertlich)
Der alte Installations-Text ist **eine falsche Installationsanweisung an Nutzer**, kein
historischer Rest. Umzuschreiben sind, gemessen und im Plan vermerkt:
* `README.md:29-31` — venv unter `~/.local/stt-button/venv` beim „first invocation" (nach V2/V4 falsch),
* `README.md:37` — nennt `python3-venv` als automatisch gezogen (V5 hat es aus `Depends` genommen),
* `README.md:43` — `stt-button setup  # build venv + ...` (V4 nimmt genau das),
* `tools/build-deb.sh:5` und `:75` sowie `packaging/debian/control:15` — Launcher „self-healing"
  (nach V2 falsch).

**Ziel-Formulierung:** System-Python + `pip install --user --break-system-packages`;
`setup` = Prüfen / Config / (geschützte) Unit; Launcher = schlanker Starter (kein Selbst-Heilen,
kein venv-Bau).

## Testpflicht — BESTEHENDE `tests/test_docs.py` erweitern
* README enthaelt **kein** `~/.local/stt-button/venv`, **kein** `python3-venv`, **kein** „build venv",
* ueber `README.md` + `packaging/debian/control` + `tools/build-deb.sh` kommt das Wort
  **`self-healing`** nirgends mehr vor (case-insensiv),
* die bestehenden Tests dieser Datei (25) bleiben gruen — insbesondere der Weg-B-Abschnitt
  (V7) und `no .venv` im build-deb-Kopf (Zeilen 2 und 40) **muessen bleiben**.
Tests lesen nur Textdateien — kein Build, kein Deb, kein Netzwerk.

## TDD + Abschluss
Tests ZUERST (ROT: Altbestand ist noch drin), dann Text (GRUEN). **Blocktests dieser Datei** —
**keine VolSuite** (FH1 faehrt gerade seine 10 Abnahme-Laefe, Timing-empfindlich).
Commit: `docs: rewrite the venv-era installation text`

## Erlaubte Dateien
`README.md`, `tools/build-deb.sh` (**nur Kommentare**), `packaging/debian/control`
(**nur Beschreibungstext** — die `Depends:`-Zeile ist fertig und fertig zu lassen),
`tests/test_docs.py`
Verboten: `src/**` (alles fertig: V4/V4a/V7), `systemd/**`, `pyproject.toml`,
`packaging/debian/changelog`, `CHANGELOG.md`, `tests/test_controller.py` (parallel in Arbeit —
nicht anfassen, nicht in einen Testlauf ziehen), andere `tests/*`, `install.sh`;
kein sudo/apt/pip/dpkg, kein Deb-Build, kein Netzwerk, keine Tastatur-/Clipboard-/Ton-Emission,
**kein git push**, kein Dienst-Neustart, kein Schreiben unter `~/.config/`.

## Parallelitaet + Stall-Prophylaxe (verbindlich)
* Ein anderes Kind schreibt an `tests/test_controller.py` und faehrt VolSuite-Laefe. Du laesst
  nur `tests/test_docs.py`.
* README nur in Stuecken <= 80 Z. lesen; aendern mit `edit` — **fremde Abschnitte nicht
  ueberschreiben** (es gibt inzwischen einen NEUEN Weg-B-Abschnitt ab Zeile 49, der bleiben muss).
* KEINE `while pgrep`-Wartespruenge, KEINE 20x-Schleifen, KEIN conftest.py-Gesuche.
* Wenn dein letzter Schritt ein Werkzeug war und dir nichts mehr bleibt: melde das in EINER
  Zeile, statt schweigend zu enden.
* Schlussmeldung max 5 Zeilen (Sha, N passed, tatsaechlich gelaufenes Modell).
