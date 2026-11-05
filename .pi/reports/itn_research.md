# ITN-Recherche: Zahlen & Tech-Tokens im Diktat (2026-11-04)

Legende: **[M]** = gemessen / gelesen in diesem Lauf, **[I]** = Schlussfolgerung oder Wissen aus Veröffentlichungen, hier nicht geprüft.

## 0. Kurzfassung
- Das Problem heißt *Inverse Text Normalization* (ITN): gesprochene Form → geschriebene Form.
- Der Industriestandard sind gewichtete FSTs mit Grammatiken (NeMo text processing, WeTextProcessing, Sparrowhawk), dazu zunehmend LLM-Nachbearbeitung.
- Für unseren Fall reicht keiner der beiden Wege allein. Die FST-Grammatiken kennen Kardinalzahlen, Uhrzeiten und Datumsangaben, aber nicht „Kimi K drei Punkt eins“. LLMs kosten Latenz, und niemand garantiert, dass sie die Bedeutung nicht ändern.
- **Empfehlung:** eine eigene, kleine, deterministische ITN-Stufe auf Regelbasis mit **Validierungs-Gate**. Sie läuft nur auf dem FINAL-Text vor dem Tippen. Strukturierte Tokens (IBAN mod-97, Kreditkarte nach Luhn, IPv4 mit 4 Oktetten ≤ 255, Uhrzeit nur mit Ankerwort „Uhr“) werden nur umgeschrieben, wenn die Validierung besteht. Sonst bleibt der Text unverändert. Für Ziffernfolgen ohne Prüfsumme (IP, Port, Version) gelten Struktur, Trigger-Wort und konservativer Default (§1b).

## 1. Stand der Technik

### (a) FST-ITN
| System | Reife | DE-ITN | Latenz | Offline | Wartung |
|---|---|---|---|---|---|
| NVIDIA NeMo-text-processing (`nemo_text_processing.inverse_text_normalization`, Sprache `de`) | hoch, produktiv in Riva | **ja**: cardinal, ordinal, decimal, money, time, date, measure, telephone, electronic, whitelist [I] | ms pro Satz nach dem Grammatik-Build; der erste Build der .far-Dateien dauert Minuten [I] | ja | aktiv; braucht **pynini** (OpenFst). Ein cp312-Wheel `pynini-2.1.6 manylinux_2_28` ist herunterladbar **[M]** (nur nach /tmp geladen, nicht installiert) |
| WeTextProcessing (wenet) | hoch für zh/en | **nein, kein DE** [I] | sehr niedrig | ja | aktiv, für uns irrelevant |
| sherpa-onnx `rule_fsts` | produktiv | lädt NeMo/WeText-.fst-Dateien. Für DE müsste man die Datei aus NeMo selbst exportieren [I] | sehr niedrig | ja | aktiv |
| Sparrowhawk (Google/Kaldi-Umfeld) | alt, kaum gepflegt | keine DE-Grammatiken mitgeliefert [I] | niedrig | ja | faktisch tot |

Bewertung: NeMo-DE ITN deckt **„siebzehn Uhr dreißig → 17:30“**, „eins Punkt zwei Punkt drei Punkt vier“ (electronic/IP, Abdeckung unsicher [I]) und Kardinalzahlen ab. Die Kardinalregel ist allerdings **aggressiv**: Sie würde „drei und sechs“ umwandeln, aber auch „ein paar“ oder „einer der beiden“ riskieren. NeMo hält `ein/eine` mit Whitelists zurück [I]. Mischtokens wie „GPT six“, „Kimi K drei eins“ oder „PCIe eins“ kennt sie **nicht**, ebenso wenig gemischtes DE/EN im selben Satz. IBAN hat keine eigene Klasse. Das Ergebnis wäre eine Ziffernfolge, ungeprüft und ohne 4er-Gruppen (gegebenenfalls über die `telephone`-Klasse) [I].

### (b) Was die Engines selbst können
- **Parakeet TDT v3 (NeMo/ONNX)**: Das Modell gibt Text mit Großschreibung und Satzzeichen aus. Ziffern kommen *manchmal* direkt aus dem Modell: Im Korpus stehen „Slot 3 und 6“ und „Opus 5.5“ **[M]**. Das Verhalten ist aber inkonsistent: „Opus fünf.5“, „Gpt Six“, „Chimika 31 Pro“ **[M]**. Unser Code (`asr.py`) macht nach der Erkennung nur `_strip_ansi`/`strip` **[M]**. Es gibt keinen ITN-Hook. NeMo würde ITN als separates Paket anbieten, nicht im Modell [I].
- **Qwen3-ASR 1.7B**: Im Pairwise-Report schreibt es Zahlwörter („Slot drei und sechs“, „PCIe eins und zwei“, „Kimi K drei eins Pro“), aber „GPT6“ als Ziffer **[M, docs/reports/qwen_pairwise_wer_20261104.md]**. Es hat keinen Formatierungsschalter. Per Prompt oder Kontext lässt es sich steuern [I]. Ein Audio-Probe mit synthetischer Sprache (zum Beispiel „fünf Uhr“, IP) war **nicht möglich**: Auf dem System ist kein TTS-Programm installiert (espeak/piper/pico fehlen) **[M]**. Die Aussage stützt sich daher nur auf die Korpusbelege.
- **Fazit:** Beide Engines liefern Ziffern zufällig. Eine ITN-Stufe muss auch bereits korrekt gesetzte Ziffern unverändert lassen (Idempotenz).

### (c) LLM-Nachbearbeitung
- Lokal ist **kein kleines Text-LLM gecacht**. Im HF-Cache liegen nur ASR-Modelle (granite-speech, parakeet, whisper, Qwen3-ASR) **[M]**. Ollama oder llama-server gibt es nicht **[M]**. Einen Benchmark gab es deshalb nicht.
- Schätzung [I]: Ein 0,5–1,5B-Modell in transformers auf CPU (8 Kerne **[M]**, torch CPU-only **[M]**) läuft mit etwa 10–30 Token/s Decode. Ein Satz mit 40 Token kostet damit **1,5–4 s** zusätzlich, ein 3–4B-Modell 5–10 s. Zum Vergleich: Unsere Finalize-Latenz liegt bei etwa 1 s (`latency[finalize end] +1.042s` **[M]**). Ein LLM würde die gefühlte Latenz verdoppeln bis verfünffachen.
- Risiko: Halluzinationen und stilles Umformulieren. Das widerspricht der Anforderung, die Bedeutung nie zu ändern. Wenn überhaupt, dann nur eingeschränkt: Das LLM darf nur Spans markieren, die eine deterministische Stufe dann umsetzt.

### (d) Kommerzielle und verwandte Systeme
- **Dragon/Nuance**: eingebaute ITN mit Formatierungsoptionen (Zahlen, Uhrzeit, Datum, Telefon). „Numeric mode“ und Custom Words sind dokumentiert [I]. IBAN gibt es nicht als eigene Klasse [I].
- **macOS Diktat / Voice Control, Android Gboard**: Server- bzw. on-device-ITN auf FST-Basis (Google: Kestrel/Sparrowhawk-Linie), keine Benutzerregeln [I].
- **Wispr Flow**: LLM-Nachbearbeitung in der Cloud, „Formatting“ inklusive Zahlen. Nicht offline [I].
- **voice-keyboard-0.1.1.apk** (read-only geprüft): Das APK enthält nur onnxruntime-Libs und Parakeet-int8-Dateien (`encoder-model.int8.onnx`, `decoder_joint-model.int8.onnx`, `nemo128.onnx`). Es gibt **keine** FST/.far-Dateien und keine Normalisierungsklassen. Die Strings in `/tmp/voice-keyboard-selected-source.txt` enthalten nur `replaceBeforeCursor` (Editor) **[M]**. → **Das APK hat keine ITN.**

### IBAN als zentraler Fall
- Bei FST-Systemen fällt eine diktierte IBAN in die Ziffernfolgen- oder Telefonregel. Heraus kommt eine Ziffernkette ohne Ländercode-Logik, ohne Prüfung und ohne 4er-Gruppen [I]. Dragon und Gboard bieten keine IBAN-Klasse [I]. LLM-Systeme formatieren IBANs „plausibel“, prüfen aber nicht und können Ziffern verdrehen [I]. Das ist der schlimmste denkbare Fehler: eine falsche Kontonummer, die korrekt aussieht.
- **Prüfsummen-Gate (unser Vorschlag):** Ein Kandidat ist `D E` + Zahlwörter oder Ziffern. Er wird nur umgeschrieben, wenn er für DE genau 22 Zeichen hat und **mod 97 == 1** ergibt. Erst dann folgt die Ausgabe `DE89 3704 0044 0532 0130 00`. Schlägt die Prüfung fehl, bleibt der Text **wörtlich** stehen; optional kommt ein Warnton. Der Nutzer sieht damit sofort, dass er sich verhört oder vertippt hat. Die ITN *erzeugt* also nie eine ungültige IBAN.
- **Probe [M]** (`/tmp/iban_probe.py`, etwa 30 Zeilen Python):
  - „Bitte überweise an D E acht neun drei sieben null vier …“ wird zu `DE89 3704 0044 0532 0130 00`.
  - Dieselbe Folge mit einer veränderten Ziffer (`88…`) bleibt **unverändert**.
  - Laufzeit: **≈ 23 µs pro Aufruf**.
- Die mod-97-Prüfung erkennt alle Einzelziffernfehler und fast alle Vertauschungen benachbarter Ziffern (ISO 7064) [I].
- Dieselbe Technik passt auf weitere Formate:
  - Kreditkarte: Luhn-Prüfung plus 13–19 Stellen.
  - IPv4: genau 4 Oktette ≤ 255, Ankerwort „IP“ oder „Adresse“ in der Nähe.
  - Uhrzeit: nur mit Anker „Uhr“ bzw. „viertel/halb/dreiviertel + Zahl“ **und** Stunden ≤ 24, Minuten < 60.
  - Datum: Tag/Monat-Plausibilität.
  - Telefon: hat keine Prüfsumme, deshalb nur mit Anker „Telefon/Nummer“ und sonst nicht anfassen.
- Naive Ziffern-Gruppierung ohne Prüfung ist **abzulehnen**.
- Hinweis zu „um viertel drei“: Regional heißt das 14:15 (ost- und süddeutsch: „viertel drei“ = 2:15) und nicht 14:45. 14:45 wäre „dreiviertel drei“. Zudem ist AM/PM ohne Kontext unklar. **Empfehlung:** „viertel/halb“-Formen nur umschreiben, wenn eine Konfiguration die Konvention festlegt, und sonst unverändert lassen.

## 1b. Ziffernfolgen OHNE Prüfsumme (IP, Ports, Versionen, freie Zahlenfolgen)

Wo es keine Prüfziffer wie mod-97 oder Luhn gibt, braucht die Umschreibung eine andere Sicherheit. Es gibt drei Anker, die sich kombinieren lassen.

### Anker 1: Strukturprüfung (allein am Token entscheidbar)
| Klasse | Regel | Entscheidbar ohne Kontext? |
|---|---|---|
| IPv4 | genau 3× „Punkt/dot“, 4 Oktette, jedes 0–255. Führende Null ablehnen (`01`), also nicht umschreiben | fast: Strukturell gleich wäre eine vierteilige Version „1.2.3.4“, die Ausgabe ist dann aber identisch, also unkritisch |
| IPv6 | 2–8 Hex-Gruppen mit „Doppelpunkt“, höchstens ein „::“ | ja, aber diktiert praktisch nie. **Weglassen** |
| MAC | 6 Hex-Paare (12 Hex-Zeichen) mit „Doppelpunkt/Bindestrich“ | ja, Buchstaben a–f werden diktiert. Selten, deshalb Phase 2 |
| Port | ganze Zahl 1–65535 | **nein**: Jede Zahl ≤ 65535 passt, also nur mit Trigger (Anker 2) |
| URL/Domain | `<wort> Punkt <tld>` mit TLD-Whitelist (com, de, org, io …) | weitgehend. „economist.com“ hat Parakeet bereits selbst richtig geschrieben **[M]** |
| Version | `Zahl (Punkt Zahl){1,3}` | **nein**: „fünf Punkt fünf“ könnte auch Aufzählung oder Satzende sein, also nur mit Trigger oder Lexikon-Namen davor |

Strukturprüfung allein reicht nur bei IPv4 (mit 4 Oktetten) und Domains mit TLD-Whitelist. Alle anderen Klassen brauchen zusätzlich Anker 2.

### Anker 2: Trigger-Wort-Gates
- **Trigger:** `IP(-)Adresse`, `IP`, `Port`, `Subnetz`/`Netzmaske`, `Host`, `Slot`, `Version`/`v`, `Kapitel`/`chapter`, `PCIe`, `Uhr`, dazu Lexikon-Namen (Opus, Kimi K, GPT, Qwen …). Das Muster `zwischen Port X und Y` wird zum Bereich `X–Y`.
- **Wie FST-Systeme das lösen [I]:** NeMo und WeTextProcessing arbeiten in zwei Stufen: *Tagger/Classifier* und *Verbalizer*. Der Tagger ist eine Vereinigung gewichteter Klassengrammatiken (`CardinalFst`, `TimeFst`, `ElectronicFst`, `TelephoneFst` …). Kontext steckt nur *innerhalb* der Grammatik, etwa dass „Uhr“ Teil des TimeFst-Musters ist. Freie Kontextregeln wie „Port im Umkreis von 3 Wörtern“ gibt es nicht. Konflikte entscheiden Gewichte über den kürzesten Pfad, und Whitelists bilden die Ausnahmen. Für unsere wenigen Klassen ist eine **eigene kleine Regelschicht** (Regex über eine tokenisierte Wortfolge plus Trigger-Fenster) einfacher, testbarer und ohne pynini-Abhängigkeit. Deutschsprachige Kardinalzahlen von 0 bis 99999 lassen sich mit etwa 60 Zeilen parsen.

### Anker 3: konservativer Default
**Kein Trigger und keine Struktur heißt: Wörter bleiben unverändert.**

Messung **[M]** über alle 528 deduplizierten Diktate des Journals:
- **Ports:** 0 Vorkommen. IP-Diktate: 2.
- Versions- und Modellnamen: etwa 12 Diktate (Opus fünf Punkt fünf ×7, Kimi K drei Punkt eins, „Version null.1“, „Version Nullpunkt eins. Vierundvierzig“, „zwei Punkt null“, „Gwen ASR One Point six b“).
- **Was der Default verpasst:**
  - „punkt hundert zwei neunzig Punkt hundert sechsundachtzig Punkt eins Punkt eins“: Der Trigger „IP“ steckte im vorherigen Segment, außerdem fehlt ein Oktett. → Bleibt Text. Das ist **korrekt so**, weil eine Rekonstruktion von 192.168.1.1 Raterei wäre.
  - „Asr Interval Adaptive K … zwei Punkt null“: Kein Trigger und kein Lexikon-Name, also bleibt es Text. Als Option kann man einen Trigger „auf … setzen“ ergänzen.
  - „Version Nullpunkt eins. Vierundvierzig“: Die ASR hat den Satz in der Mitte zerschnitten. Das ist keine ITN-Aufgabe mehr.
  - Insgesamt bleiben etwa **3 von ≈ 15** Zahl/Tech-Fällen im Korpus unkonvertiert. Fehlkonvertierungen: 0, vorausgesetzt, „in diesem Punkt“ ohne Zahl wird korrekt ignoriert.

### Verifizierbare LLM-Edits (nur relevant für Option 3)
Das LLM-Ergebnis wird mechanisch gegen das Original gediffed (Token-Diff):
1. Erlaubt ist nur, eine *zusammenhängende* Folge aus Zahlwort-Lexikon-Tokens (DE/EN: null…neunzig, hundert, tausend, zero…, Punkt/dot/Komma/Doppelpunkt/Bindestrich/Uhr) durch einen Ziffern- bzw. Trennzeichen-String zu ersetzen.
2. Diesen String parsen wir selbst aus den Originalwörtern und **vergleichen** ihn mit der LLM-Ausgabe. Weicht er ab, wird der Edit abgelehnt. Das LLM darf also nur *wo* entscheiden, nie *was*.
3. Jede andere Änderung (Wort, Satzzeichen, Großschreibung, Reihenfolge) verwirft den ganzen Edit, und es gilt das Original.

Halluzinations-Testplan:
- Golden-Set plus 200 Negativ-Sätze ohne Zahlen: erwartete Diff-Rate 0.
- Adversariale Fälle wie „ein paar“, „einer von zwei“ und Code-Diktat.
- Jeden Sample 10× mit temperature>0 laufen lassen: Die Ausgabe muss nach dem Diff-Filter deterministisch sein.
- Metrik „rejected edits pro 100 Sätze“ im Journal loggen.

Wenn der Validator ohnehin den Zielstring selbst berechnet, bringt das LLM nur noch Span-Erkennung. Genau die liefert Anker 2 billiger. Das stützt die Empfehlung gegen Option 3.

### Deutsche Fallen
| Falle | Option 1 (Regeln) | Option 2 (NeMo) | Option 3 (LLM + Diff) |
|---|---|---|---|
| „null Komma fünfzehn“ → 0,15 vs. „Version null Punkt fünfzehn“ → 0.15 | Komma → Dezimalkomma nur mit Einheit oder Messwort danach, sonst Text. „Punkt“ → „.“ nur in Versions-, IP- oder Lexikon-Kontext. Korpus: 0× Komma-Zahl **[M]** | DecimalFst kann „Komma“ → „,“. Bei „Punkt“ ist das Verhalten unklar und kann sich mit Satzende-„Punkt“ beißen [I] | LLM entscheidet, der Validator prüft nur den Wert, nicht das Trennzeichen. Deshalb muss das Trennzeichen aus dem Originalwort folgen (Komma→„,“, Punkt→„.“) |
| Komposita „zweiundachtzig“, „sechsundachtzig“, „Vierundvierzig“ (3× im Korpus **[M]**), auch zerlegt als „zwei und achtzig“ | Parser für Komposita und Getrenntschreibung. „drei und sechs“ ist dabei mehrdeutig (36 oder „3 und 6“). Regel: Die getrennte Form gilt als **Aufzählung**, nur das zusammengeschriebene Wort als Kompositum | NeMo parst Komposita und tendiert bei „drei und sechs“ eher zu Einzelzahlen [I] | Risiko „36“. Der Validator lehnt das ab, wenn unser Parser „3 und 6“ ergibt |
| Englischer Code-Switch: „one nineteen dot two“, „Int eight“, „Fp Three Two“, „One Point six b“ **[M]** | EN-Zahlwörter im selben Lexikon. „one nineteen“ (gesprochene Ziffernpaare) nur im IP-Kontext → `119`. INT8/FP32/1.6B über das Lexikon (`Int`,`Fp` + Zahl). „Fp Three Two“ ergibt FP32, weil im Lexikon-Kontext Ziffern aneinandergehängt werden | DE-Grammatik versteht keine englischen Zahlen. Man bräuchte eine zweite EN-Pipeline und Sprachumschaltung pro Span, das ist schwach | Stärkster Punkt des LLM. Gegen Halluzination schützt trotzdem nur der Diff-Filter |

## 2. Gap-Analyse auf unserem Korpus [M]
Methode:
1. Aus `journalctl --user -u stt-button` alle Zeilen `Transcription of … -> '…' (committed=…)` extrahieren: **812 Zeilen**.
2. Die letzten 200 nehmen.
3. Live-Zwischenstände nach den ersten 40 Zeichen deduplizieren: **143 Diktate**.
4. Regex auf Zahlwörter DE/EN, wobei `ein/eins/one` ausgeschlossen bleiben (Artikel, Falschtreffer).

Ergebnisse:
- **17 von 143 Diktaten (≈ 12 %)** enthalten ein Zahlwort. Nach manueller Durchsicht sind etwa **12–13 davon echte ITN-Fälle**, also etwa **9 %**. Falschtreffer sind zum Beispiel „fifty percent off would be two“ (Satzabbruch) sowie Teile der englischen Telefon-Testdiktate.
- Rohtreffer ohne Deduplizierung: 28 von 200.
- Echte Fälle (Auswahl):
  - „Kimi K drei Punkt eins Pro“
  - „Opus fünf.5“ und „Opus fünf Punkt fünf“ (gemischt, also kaputt)
  - „Gwen ASR One Point six b“
  - „Int eight“ / „Fp Three Two“ (→ INT8/FP32)
  - „M I und M zwei … P sechs“
  - „siebzehn Uhr dreißig“
  - „IP Adresse eins Punkt zwei Punkt drei Punkt vier“
  - „…hundert zwei neunzig Punkt hundert sechsundachtzig Punkt eins Punkt eins“ (192.168.1.1, zerlegt)
  - „chapter zero five dash two zero“
  - „one a four zero four seven“ (PLZ/Adresse)
- Nur **4 von 200** Zeilen enthalten überhaupt Ziffern.
- **IBAN/Bank/Überweisung/Konto:** In allen 812 Zeilen gibt es **keine diktierte IBAN**. Treffer sind nur „Ibands“ (das Diktat dieses Auftrags) und viermal „Bank“ in einem E-Mail-Diktat **[M]**. Der IBAN-Fall ist damit Anforderung, kein beobachteter Schmerz. Gerade deshalb muss er sicher sein, wenn er auftritt.
- Größte Fallgruppe: **Modell- und Versionsnamen** (Opus 5.5, K3.1, GPT-6, INT8, FP32). Genau die decken FST-Grammatiken nicht ab.

## 3. Vorschläge (gerankt)

### Option 1 (EMPFOHLEN): eigene deterministische ITN-Stufe mit Validierungs-Gates
- **Seam:** neues Modul `src/stt_button/itn.py` mit `normalize(text, cfg) -> str`. Es wird **nur im Finalize-Pfad** aufgerufen, vor `self._delta_typer.finalize(result.text, …)` (controller.py `_finalize_worker`). Live-Parakeet-Deltas bleiben roh. Der Final-Delta ersetzt den Text ohnehin, und Qwen hat ohnehin keinen Live-Text. Wichtig: `committed_len` bezieht sich auf den Rohtext. Wenn die ITN den bereits committeten Präfix verändert, muss der Delta-Typer neu abgleichen. Einfachste Regel: ITN nur auf den nicht committeten Rest anwenden, oder Präfix-Änderungen verbieten und dann die ITN für diesen Span auslassen.
- **Regelklassen** (jede einzeln per Config abschaltbar):
  1. Gate-Klassen: IBAN (mod-97), Kreditkarte (Luhn), IPv4 (Oktette und Anker), Uhrzeit (Anker „Uhr“, Bereichsprüfung).
  2. **Tech-Lexikon plus Versionsmuster**: `<Bekannter Name> <Zahlwort>(Punkt <Zahlwort>)*` → `Opus 5.5`, `Kimi K3.1`, `GPT-6`, `INT8`, `FP32`, `PCIe 1`. Die Namen kommen aus einer Whitelist in der Config (das Operator-Vokabular), nicht generisch. Dazu gehört das Reparieren von „fünf.5“ zu „5.5“.
  3. Kardinalzahlen nur im Kontext eines Zählworts („Slot drei und sechs“, „Port acht null acht null“), nie freistehend.
- **Klasse ohne Prüfsumme (§1b):** **Anker 1 + 2 + 3 kombiniert.** IPv4 wird umgewandelt bei Struktur (4 Oktette, 0–255, keine führende Null) **und** Trigger (IP/Host/Subnetz/Adresse im Fenster von ±4 Wörtern). Ports, Slots, Versionen und Kapitel **nur** mit Trigger plus Bereichsprüfung (Port 1–65535). Alles andere fällt auf den Default „unverändert“ (Anker 3) zurück. Kein LLM.
- **Latenz:** < 1 ms **[M für den IBAN-Teil: 23 µs]**. Komplett offline, keine neuen Abhängigkeiten (reines Python, `re`).
- **Fehlermodi:**
  - Zu hohe Trefferquote der Lexikonregel: deshalb ausschließlich Whitelist und Anker.
  - Code-Diktat („zwei Punkt null“ in Versionsstrings): Ergebnis „2.0“ ist erwünscht. In Code-Kontexten wie „range zwei Punkt null“ ist es ebenfalls harmlos, weil sich der Zahlenwert nicht ändert.
  - Grundsatz: Bei Unsicherheit gilt **Identität**.
  - Idempotenz ist Pflicht: `normalize(normalize(x)) == normalize(x)`.
- **Tests (TDD-tauglich):** `tests/test_itn.py` mit einem **Golden-Set aus den 13 echten Korpus-Sätzen oben**, plus
  - Negativ-Set: Sätze mit „ein/eine/eins“ als Artikel, „zwei Leute“, Satzabbrüche → müssen identisch bleiben,
  - IBAN gültig/ungültig, Luhn gültig/ungültig, IP 300.1.1.1 → unverändert,
  - Property-Test Idempotenz.
  - Jede Regel gilt erst nach RED→GREEN als fertig.
- **Aufwand:** etwa 1–1,5 PT (Gates 0,5 PT, Lexikon/Versionen 0,5 PT, Integration und Delta-Abgleich 0,25–0,5 PT).

### Option 2: NeMo-DE-ITN (pynini) plus unsere Gates und das Lexikon davor
- **Seam:** wie Option 1, die NeMo-ITN deckt zusätzlich Kardinalzahlen, Datum und Geld ab.
- **Latenz:** ms pro Satz, plus einmaliger Grammatik-Build (Minuten) und Speicherbedarf [I].
- **Fehlermodi:** aggressive Kardinalregel und Whitelist-Pflege, schwach bei Mischsprache. Neue schwere Abhängigkeiten (pynini/OpenFst, nemo_text_processing), Versionsdrift [I].
- **Aufwand:** 2–3 PT inklusive Negativtests, um Überkonvertierung zu dämpfen.
- **Sinnvoll** erst, wenn Option 1 an Datums- und Geldbeträgen spürbar scheitert.

### Option 3: lokale LLM-Nachbearbeitung (Span-Tagger statt Freitext-Rewrite)
- **Seam:** Finalize, asynchron. Das LLM liefert nur Span-Vorschläge, die deterministischen Gates entscheiden.
- **Latenz:** geschätzt 1,5–4 s auf dieser CPU [I]. Es ist kein Modell gecacht, also ist ein Download nötig.
- **Fehlermodi:** Halluzination und Nicht-Determinismus. Schwer testbar, weil Golden-Tests flaky werden.
- **Aufwand:** 3+ PT.
- Nur als spätere Ergänzung für die Lexikon-Pflege (offline Vorschläge generieren), nicht im Hot Path.

**Empfehlung: Option 1.** Sie deckt die tatsächlich gemessene Fallgruppe ab (Versions- und Modellnamen, IP, Uhrzeit), ist schneller als jede Alternative und hat keine Abhängigkeiten. Mit den Prüfsummen- und Bereichs-Gates liefert sie die einzige *beweisbare* Garantie gegen Bedeutungsänderungen: Wenn die Prüfung fehlschlägt, bleibt der Text wörtlich stehen.

## 4. Quellen
- **[M] gelesen/ausgeführt:**
  - `docs/reports/qwen_pairwise_wer_20261104.md`
  - `journalctl --user -u stt-button` (812 Transkriptionszeilen, Extrakt `/tmp/itn_corpus_all.txt`, `/tmp/itn_last200.txt`, `/tmp/itn_dedup.txt`)
  - `src/stt_button/asr.py` (nur Post-Processing `_strip_ansi`)
  - `src/stt_button/controller.py` `_finalize_worker` (Seam bei `delta_typer.finalize`)
  - `~/Downloads/voice-keyboard-0.1.1.apk` (`unzip -l`, dex-Strings)
  - `/tmp/voice-keyboard-selected-source.txt`
  - HF-Cache-Liste, `pip list`, `pip download pynini` (Wheel verfügbar)
  - `/tmp/iban_probe.py`
- **[I] aus Vorwissen, in diesem Lauf nicht nachgelesen:** NeMo-text-processing-Doku/Repo (github.com/NVIDIA/NeMo-text-processing), WeTextProcessing (github.com/wenet-e2e/WeTextProcessing), sherpa-onnx `rule_fsts`, Sparrowhawk, ISO 13616/7064 (IBAN mod-97), Produktverhalten von Dragon/Gboard/Wispr. Diese Angaben vor der Umsetzung verifizieren.
- **Nicht gemessen:** Qwen3-ASR-Audio-Probe (kein TTS verfügbar), lokale LLM-Latenz (kein Modell gecacht).
