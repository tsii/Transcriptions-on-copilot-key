# Diagnose-Auftrag FL4 — `TestStreaming::test_text_reaches_typer` schlaegt fehl

Repo `/home/salmon/pi-web-ui/STT-Button`. NUR diagnose, **keine** Aenderung am Code, **kein**
Commit, **keine** VolSuite (Last soll sauber bleiben — nur der Einezeltest, hoechstens 10x).

## Gemessener Sachverhalt (vom Orchestrator, 23:38)
```
.venv/bin/python -m pytest -q -p no:randomly -o addopts="" \
    "tests/test_controller.py::TestStreaming::test_text_reaches_typer"
```
5 Laeufe im LEERLAUF: exit 0,1,1,1,0 — also **3/5 Fehler ohne jede Parallel-Last**. Drei
spaetere Laeufe wieder 3/3 gruen. Der Test ist praedatiert (V1..V4a haben ausschliesslich
`tools/`, `src/stt_button/deps_gate.py`, `systemd/`-Vorlage, neue Testdateien beruehrt).

Fehlersignatur:
```
ctrl, ctx = make_controller()
ctrl.start_recording()
for _ in range(3):
    ctrl.run_once()
> assert any(c[0] == "type" for c in ctx["typer"].calls)
E assert False            (tests/test_controller.py:343)
```
Nachbar-Test `test_run_once_feeds_and_transcribes` (Zeilen 328-335) erwartet nach **2**
`run_once()`-Aufrufen `engine.calls >= 1` undcomment: "2 chunks of 0.5s = 1.0s >= min_audio_s (0.8s)".
Der flake-Test nimmt 3 Aufrufe und期望 einen `type`-Aufruf.

## Zeitabhaengigkeit (Fundstelle)
`src/stt_button/controller.py`: `:228 self._started_at = time.monotonic()`,
`:229 self._last_audio_at = time.monotonic()`, `:204 now = time.monotonic()`,
`:350 now = time.monotonic()`, `:471 expired = time.monotonic() - self._started_at >= self._max_record_s`.

## Zu beantworten (max 12 Zeilen, mit Datei:Zeile-Belegen)
1. **Warum intermittierend?** Welche Groesse ist im Test NICHT deterministisch — echte
   Wall-Clock-Zeit in `run_once()` (Zeile 204/350), Chunk-Fuellung ueber den Fake-Source,
   Threads, oder `min_audio_s`-Schwelle? Fuehre den Beleg an (Code, kein Ratentee).
2. **Testfehler oder Produktfehler?** Ist die Erwartung des Tests begrundet (wuerde ein
   deterministischer Fake dasselbe Verhalten zeigen?), oder versteckt sich ein echter
   Bug im Live-Pfad (z. B. Chunk-Ataigehaeltigkeit bei schnellem `run_once`,
   `min_audio_s`-Pruefung gegen echte statt gegen gelieferte Audiozeit)?
3. **Reproduktionshebel**: ein Weg, die Fehlerrate gezielt hochzutreiben (z. B. Patch
   einer Sleep-/Zeitschwelle, `--count`, Umgebungsvariable) — damit die spaetere Abnahme
   eine Beweiskraft hat.
4. **Heilungsvorschlaege**, je 1 Zeile mit Risiko: (a) Test deterministisch machen
   (Fake-Zeit injectieren), (b) Produktcode entwirren, (c) Test als unzuverlaessig
   markieren/loeschen. Nenne deine Präferenz mit Grund.

## Erlaubt / verboten
Erlaubt: `read`, `grep`, `ls`, Einzeltest-Ausfuehrungen, `python - <<` zum Lesen von Konstanten.
Verboten: jede Dateischreibens, `git commit`, pytest-Volllaeufe/VolSuite, Paralleltests
(`-n auto`), sudo, Netzwerk, Dienstzugriff, Tastatur/Clipboard/Ton.

## Stall-Prophylaxe
Keine `while pgrep`-Schleifen, keine 20x-Wiederholungen (max 10 Einzeltest-Laeufe), Dateien
nur in Stuecken <= 80 Z. lesen. Wenn dein letzter Schritt ein Werkzeug war und dir nichts
mehr bleibt: melde das in EINER Zeile statt schweigend zu enden.
