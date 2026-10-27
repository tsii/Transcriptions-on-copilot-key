# AGENTS.md — Projektregeln STT-Button

Gilt nur in diesem Repo (git@github.com:tsii/Parakeet-on-copilot-key.git, Branch
`master`). Die globale AGENTS.md gilt zusätzlich.

## Lehren aus dieser Session (Kontext)
- Subagenten-Ergebnisse IMMER abholen (`list` → `get_result`); nie den Turn mit
  offenem benötigtem Lauf ohne `schedule`-Wakeup beenden.
- Experten-Pläne verifizieren, nicht glauben: zwei Kimi-Analysen (CapsLock) waren
  falsch; ein Ground-Truth-Experiment hat entschieden.

## Workflow / Modelle
- Planen & Debuggen: openrouter/moonshotai/kimi-k3.1-pro
- Implementieren: TDD-Ketten mit cortecs/glm-5.3
- Review: cortecs/deepseek-v4-pro-0813

## Regeln

P1 Python & Layout. Wenn Code geändert wird, dann Python 3.12 im src-Layout
   (`src/stt_button/`) und die venv `.venv/bin/python` verwenden.

P2 Testkommando. Wenn getestet wird, dann AUSSCHLIESSLICH
   `.venv/bin/python -m pytest`. Nie System-Python, nie andere Runner.

P3 TDD. Wenn ein Fix/eine Funktion umgesetzt wird, dann zuerst Test schreiben,
   ROT dokumentieren (Test schlägt fehl), dann GRÜN (`.venv/bin/python -m
   pytest`). Nie Code ohne zugehörigen Test committen.

P4 Stil. Wenn neue Module/Funktionen/Docstrings entstehen, dann auf Deutsch.

P5 Sicherheitsgrenzen. Wenn ein Eingriff Systemrechte braucht (apt, /etc, keyd,
   systemd, sudo), dann NICHT ohne ausdrückliche Betreiber-Freigabe ausführen.
   Die App darf CapsLock nie ungefragt togglen (Default
   `capslock_autocorrect=false` → nur warnen).

P6 Dienst. Wenn der Dienst gestartet/gestoppt/enabled wird, dann nur nach
   ausdrücklicher Betreiber-Freigabe. Aktuell: manuell gestartet, Autostart
   bewusst noch nicht gesetzt.

P7 Git. Wenn committet/gepusht wird, dann NUR nach
   git@github.com:tsii/Parakeet-on-copilot-key.git (Remote `origin`, Branch
   `master`). Nie auf andere Remotes pushen.