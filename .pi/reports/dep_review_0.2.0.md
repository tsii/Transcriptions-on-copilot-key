# Kurzgutachten v2 (Betreiber-Auftrag 22:44): kein venv mehr, offizielle Ubuntu-Pakete; alte Fragen 1–4 komprimiert in §5.

## 1. Belegte Aussage: „nur offizielle Ubuntu-Pakete“ ist NICHT erfüllbar
Zusatz-Messung 22:44 (noble/universe): python3-torch ohne Kandidat; transformers/onnxruntime/ctranslate2/
huggingface-hub/sounddevice = KEIN PAKET; numpy nur 1:1.26.4. Die App verlangt 60-Zeilen-Frostlock
(`runtime-requirements.txt`: numpy==2.5.4, transformers==5.20.2, ctranslate2==4.8.4) + `torch==2.14.1+cpu`
(bootstrap.py:40) — kein apt-Solver erfüllbar ⇒ zwingend „PyPI statt apt“; danach nur noch der Schadensradius.

## 2. Optionen ohne venv
A System-pip (`--break-system-packages`): ersetzt apt-eigene numpy/scipy/llvmlite/numba in
  dist-packages ohne dpkg-Rekorde; ABI-Bruch (noble scipy 1.11.4 gegen numpy 1.x gebaut,
  Import unter numpy 2.5 fehlerhaft); systemweit, apt-Updates wirkungslos. Höchstes Risiko — abgelehnt.
B User-pip (`--user --break-system-packages`): ~/.local/…/site-packages, dist-packages
  unangetastet, nur für diesen Nutzer. Geprüft: unit/Launcher ohne PYTHONNOUSERSITE/
  PYTHONUSERBASE (service:18 nur HF_HOME; Launcher PYTHONPATH :23 prozessgebunden) ⇒
  user-site gilt auch im User-Dienst. Restrisiko: numpy-2-Shadow nutzerweit (apt-scipy-
  Tools des Nutzers), aber sauber revidierbar.
C Manuell-venv vom Betreiber, Launcher baut nichts: beste Isolation + Verifizierbarkeit
  (pip-freeze-Diff gegen den Lock), System/~/.local sauber; weicht formal von „kein venv“ ab.
D intel-oneapi-pytorch: nicht tragfähig — apt-cache show: Version 2.0.1.0 (≈ torch 2.0.1
  vs. gepinnt 2.14.1, bootstrap.py:40), Depends: intel-oneapi-condaindex/-python = Intel-
eigenes Python, nicht system-python3; transformers/ctranslate2/onnxruntime/hub fehlen weiter.

## 3. Kleinste Abweichung, die läuft = B (verletzt nur „apt statt PyPI“); C = kleinster Radius, Empfehlung bei Zweifel
Vorschlag 0.2.1: control:7 → `Depends: python3 (>= 3.10), pipewire-bin,
pulseaudio-utils, wtype` (python3-venv/-pip raus; bei C: venv → Recommends). Launcher:
Bootstrap-Block (:23-31) raus, stattdessen _CORE_MODULES-Import-Check (bootstrap.py:50-57)
+ pip-Hint, dann `exec python3 -m stt_button "$@"` (KEIN PYTHONPATH-Export → shadowed ~/.local).
`setup` (:350-364): kein venv-Bau, nur Check/Config/Unit; postinst (build-deb.sh:97-109): B-Hinweis;
TimeoutStartSec=1800 + venv-Kommentar (service:13-15) entfallen.

## 4. `iU` sicher beenden (root = nur Betreiber, P5)
`sudo dpkg --purge stt-button` — bei unpacked zulässig, entfernt per dpkg-Dateiliste; KEIN
`apt -f install` (überflüssiges python3-pip). Vorher NIEMALS `stt-button setup`: /opt/stt-button
existiert schon → Unit wird auf den scheiternden Launcher umgebogen (__main__.py:388-389) → Repo-Produktion bräche beim nächsten Restart.

## 5. Unter-Gutachten (alte Fragen 1–4, 0.2.0-deb)
- `iU` = dpkg entpackte, Configure brach: Depends-Closure ungesättigt (control:7; venv/pip=`un`);
  postinst (nur-Hint) lief nie; `dpkg -i` löst — anders als apt — nicht auf; Dateien nur /usr/bin, /opt.
- Damalige Minimalbehebung: `sudo apt-get install -y python3-venv && sudo dpkg --configure stt-button`
  (12-venv allein sättigt Meta nicht; pip aus venv-ensurepip, bootstrap.py:113-119; unter v2 erübrigt, s. "4").
- Stamp-Semantik korrekt: Stamp nur bei vollem Erfolg (bootstrap.py:161-164, 287-288, 200-208) —
  halbfertiger venv wurde nie „ready“. Pre-Depends ungeeignet (Vor-Unpack-Infra), Recommends
  verworfen (still kaputter Erstanlauf).

