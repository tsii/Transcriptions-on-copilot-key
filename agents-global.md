# AGENTS.md — Globale Harness- & Delegationsregeln (pi)

Gilt in jedem Projekt. Delegation ist erst abgeschlossen, wenn das verifizierte
Ergebnis beim Betreiber angekommen ist — nicht, wenn der Subagent gestartet wurde
oder intern auf `done` steht.

## Warum diese Regeln
- Versagen a: Turn nach subagent-spawn beendet, Ergebnis nie abgeholt → fertiges
  Experten-Ergebnis lag unbeachtet, der Mensch musste darauf hinweisen.
- Versagen b: `wait_all(960)` blockierte den Agenten → Betreiber nicht
  ansprechbar, entgegen „immer im Hintergrund, bleib ansprechbar".

## Erzwungen vs. appelliert
- HART (Extension `.pi/extensions/no-wait-all.ts`): jeder
  `subagent(action=wait_all)` wird hart zu `list` umgeschrieben; Timeout und
  runIds werden entfernt. Ein blockierendes `wait_all` existiert nicht.
- APPELLIERT (diese Datei = Policy): alle übrigen Regeln. Verstöße sind
  verboten, nicht technisch unmöglich.

## Regeln

R1 Wartemechanismus. Wenn auf ein Subagenten-Ergebnis gewartet wird, dann NIE
   `wait_all` (wird hart zu `list` umgeleitet), sondern `subagent(action=list)`
   für Status/runId und `subagent(action=get_result)` für das Ergebnis.
   (erzwungen — Extension)
   Einziger echter Wartemechanismus ist der `schedule`-Wakeup (R5/R6).

R2 Modell-Bindung. Wenn ein bestimmtes Modell verlangt ist, dann
   `subagent(action=spawn, model=provider/id)` verwenden, nie `delegate_task`
   mit Template. Nach dem Spawn `list` zur Identitätsprüfung; bei Abweichung
   `stop` + neu spawnen; nie fremdes Ergebnis als bestelltes Modell ausgeben.
   (appelliert)

R3 Ergebnis im selben Turn. Wenn ein Ergebnis für den laufenden Turn benötigt
   wird, dann `subagent(action=get_result)` aufrufen, BEVOR der Turn endet.
   Nie den Turn mit bloßer Warte-Ankündigung beenden. (appelliert)

R4 Ergebnis prüfen. Wenn `get_result` geliefert wurde, dann Vollständigkeit und
   Aufgabenbezug prüfen und dem Betreiber berichten. Ungeprüft ≠ abgeholt.
   (appelliert)

R5 Asynchroner Lauf. Wenn bewusst asynchron gearbeitet wird, dann VOR dem
   Turn-Ende einen einmaligen `schedule`-Wakeup (+N min) mit runId und
   Abhol-Anweisung setzen. Kein offener benötigter Lauf ohne Wakeup.
   (appelliert)

R6 Wakeup feuert. Wenn ein Wakeup feuert, dann zuerst `subagent(action=list)`;
   `done` → `get_result` und verarbeiten; `running` → genau EIN begrenzter
   Folge-Wakeup, danach ehrliche Meldung statt Wakeup-Kette. (appelliert)

R7 Turn-Ende-Check. Vor JEDEM Turn-Ende: kein selbst gestarteter Lauf offen ohne
   (a) verifiziertes übernommenes Ergebnis, (b) gesetzten Wakeup oder
   (c) ausdrücklich begründete Übergabe. Sonst jetzt abholen oder Wakeup setzen.
   (appelliert)

R8 Ansprechbarkeit zuerst. Wenn eine Blockade droht (langes `bash`,
   `delegate_task`, `wait_all` mit hohem Timeout), dann nur mit expliziter
   Warte-Freigabe des Betreibers ausführen. Standard: `spawn` → antwortbar
   bleiben; vor jeder Blockade Dauer + Abbruchweg ankündigen. (appelliert)