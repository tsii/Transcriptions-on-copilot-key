# Abschlussbericht — Latenz & INT8/NPU (Stand 29.10., Commit a669f23)

## Endergebnis (Live-Beweis aus echtem Diktat, 07:08)

| Metrik | Alt (FP32, Baseline Sessionswoche) | Neu (int8 ONNX, Live 07:08) |
|---|---|---|
| Finale-ASR 31,1 s Diktat | 5–7 s gesamt Release→Text | **1,54 s** Finalize (RTF **0,050**) |
| Release→paste komplett | 5–7 s | **3,10 s** (inkl. 0,73 s Backspaces + 0,80 s Paste) |
| Modell-Kaltstart (Boot) | ~7 s (HF-FP32) | **3,2 s** (int8 ONNX) |
| Einzel-Encoder 30-s-Bucket (CPU) | 0,94 s (FP32 A/B-Log) | ~0,57 s (int8) |

RTF 0,050 bei 16,4-s-Referenzclip (warm, gemessen an exakt deinem
07:08-Diktat-Audio /tmp/stt_last.wav) — deckt sich exakt mit dem
A/B-Log-Befund 0,043–0,060 und dem Live-Finalize.

## Gate-Bewertung

| Gate | Ziel | Ergebnis |
|---|---|---|
| WER int8 vs. fp32 (5 Korpusclips) | ≤ +0,5 pp | **bestanden** (besser: int8 liest Satzenden vollständig) |
| Korpus-Gate cached_mid_tail (31 Clips) | 0 Dup, 0 Frag | **bestanden** (WER 0,00 pp Differenz) |
| Suite | 100 % grün | **420 passed** |
| Live-Latenzziel Finalize | < 2 s | **1,54 s ✔** |

## NPU: geprüft und verworfen (Ground-Truth, nicht Meinung)

1. Encoder fp32 auf NPU: 0,129 s (30-s-Bucket, exklusiv, warm) — funktioniert.
2. Encoder int8 auf NPU: **0,569 s median** (9 Warmläufe) — kein Gewinn gegen CPU-int8 (~0,45 s).
3. NPU **unter echter Diktatlast**: 55× `No available devices` im Journal —
   Gerät ist exklusiv, Capture + Benchmark + Dienst konkurrieren.
4. Decoder/Joint NPU-Compile: scheitert (`Transpose_192/48372 cannot
   compute output bounds dimension`) — AR-Loop mit dynamischen Dims.
5. OpenVINO-Encoder im Produktzweig: statischer Bucket unverträglich mit
   echten Feature-Längen; Ergebnis wurde nie konsumiert → ausgebaut.

**Fazit:** Auf Lunar Lake unter diesem Arbeitsmix (Diktat + Capture +
KI-Last parallel) ist CPU-int8 (ORT) der schnellste stabile Pfad. Die
NPU-Infrastruktur (Level-Zero, Firmware, render-Gruppe) bleibt installiert
und ist für passende statische Lasten (z. B. lang laufende Batch-Jobs)
sofort nutzbar.

## Warm-Halte-Frage (Betreiber 06:4x)

Blob-Cache erfüllt den Zweck bereits: NPU-Nachladen 53 s → 2,4 s.
Ein dauerhafter Keepalive-Prozess kostet 1,3 GB RAM, blockiert das
exklusive NPU-Gerät für den produktiven Dienst (genau der 55×-Fehler)
und bringt gegen CPU-int8 nichts — **bewusst nicht gebaut**.

## Produktivpfad jetzt (config: compute_type="int8")

`ParakeetEngine.load()` → `onnx_asr.load_model('nemo-conformer-tdt',
int8, ORT-CPU, threads aus Config)`. Fehlen die Dateien oder schlägt das
Laden fehl: HF-FP32-Fallback, niemals Absturz. `transcribe()` und
`transcribe_with_timestamps()` erkennen den int8-Zweig vor jeder
HF-Verwendung.

## Bekannte offene Punkte (nicht Blocker)

- `live_asr_until_s=8`: Live-Ticks laufen bis 8 s, danach Pause-Indikator;
  Mid-Tail-Checkpoints griffen im 07:08-Diktat nicht (kein `mid_tail:`-Log) —
  Finalizer fiel auf Vollpuffer zurück, der dank int8 trotzdem nur 1,54 s
  kostet. Mögliche Nachbesserung nur mit messbarem Nutzen.
- Backspace-Kosten (0,73 s bei 3 Zeichen): `max_revision_chars`/committed-
  Takt sind die Stellschrauben, falls subjektiv störend.
