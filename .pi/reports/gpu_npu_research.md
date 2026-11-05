# GPU/NPU-Recherche für den ASR-Stack (Parakeet + Qwen3-ASR) — 2026-11-04

Gerät: ThinkPad, Intel Core Ultra 7 258V (Lunar Lake), Arc-iGPU (8086:64a0), NPU „Intel AI Boost“. Kernel 6.17.0-1033-oem.
Alle Experimente liefen im User-Space unter /tmp. Nichts wurde in `.venv` installiert, keine Systempakete, kein sudo, kein Commit.

## 1. Inventar (MEASURED)
- `lspci`: `00:02.0 VGA compatible controller [0300]: Intel Corporation Lunar Lake [Intel Graphics] [8086:64a0] (rev 04)`
- `/dev/dri`: `card1` (video), `renderD128` (render). Der Nutzer ist in der Gruppe `render`. `/dev/accel/accel0` ist vorhanden (NPU).
- `sycl-ls` und `clinfo`: nicht installiert.
- `.venv`-torch: `2.14.1+cpu None True` → `torch.xpu` ist zwar vorhanden, `is_available()=False` (CPU-Build).
- `.venv`: **openvino 2026.4.1 war bereits installiert** (nicht von mir). optimum-intel ist nicht installiert. onnxruntime 1.31.0 bietet die Provider `['AzureExecutionProvider','CPUExecutionProvider']`.
- System: `intel-level-zero-npu 1.38`, `libze1 1.32`, `libigdgmm12 22.3`, `ocl-icd-libopencl1`. **Die GPU-Compute-Runtime (intel-opencl-icd / libze-intel-gpu1) ist nicht installiert.**
- `ov.Core().available_devices` → `['CPU', 'NPU']`. **Ohne Compute-Runtime ist die GPU für OpenVINO und torch nicht sichtbar.**
- Festplatte: 3,2 TB frei, RAM 30 GB.

## 2. Freischaltung der GPU im User-Space (MEASURED)
- Die Debs aus GitHub intel/compute-runtime 26.40.40196.8 (intel-opencl-icd, libze-intel-gpu1, libigdgmm12 22.10.2) und aus intel-graphics-compiler v2.42.6 (igc-core, igc-opencl) wurden mit `dpkg-deb -x` nach `/tmp/neo/root` entpackt (474 MB). Dazu kam eine eigene ICD-Datei; aktiviert wird alles über `OCL_ICD_VENDORS=/tmp/neo/icd` und `LD_LIBRARY_PATH`.
- Ergebnis: `['CPU','GPU','NPU']`, GPU = „Intel(R) Arc(TM) Graphics (iGPU)“. **Es wurde kein Systempaket installiert.** Einen „score“-OpenCL-Treiber oder andere Systeminstallationen hat es nicht gegeben, deshalb muss nichts deinstalliert werden.
- Für die Produktion wäre stattdessen nötig: `sudo apt install intel-opencl-icd libze-intel-gpu1` (Intel-PPA). **Dafür ist die Freigabe des Betreibers erforderlich (P5).**

Fehlgeschlagene Installationsversuche:
- `python3.12 -m venv /tmp/ovenv` → exit 1 (ensurepip fehlt, python3.12-venv nicht installiert). `uv` ist nicht vorhanden (exit 127).
- `pip install --target ... torch --index-url .../whl/xpu` → nach 10 Minuten Timeout. Ursache: IPv6 „Network is unreachable“. Abhilfe: Download mit `curl -4` (Wheel 860 MB).
- Die Installation des Wheels ohne Extra-Index scheiterte mit `No matching distribution found for triton-xpu~=3.8.0` (exit 1). Mit `--extra-index-url .../whl/xpu` funktionierte sie (56 s, 3,5 GB in /tmp/xpupkgs).
- Beim Import fehlten zunächst `libsycl.so.9` und danach `libccl.so.2`, weil `--target` die `lib/`-Data-Files verwirft. Die Intel-Runtime-Wheels sowie oneccl und impi-rt wurden deshalb mit `--no-deps --prefix /tmp/xpuprefix` nachinstalliert. Danach meldete `torch 2.14.1+xpu`: xpu True, „Intel(R) Arc(TM) Graphics“.

## 3. Messungen
Clip A = `~/stt-korpus/20261028-165550_600050.wav` (18,8 s). Zusätzliche Clips: `20261028-211208_305138.wav` (9,5 s), `-211229_274418` (8,6 s), `-211246_292168` (9,1 s), `-211532_580252` (18,1 s).

### Parakeet ONNX (Skripte /tmp/ovprobe.py, /tmp/encprobe.py, /tmp/hybrid.py, /tmp/hybrid32.py)
| Pfad | Clip | Ergebnis | Status |
|---|---|---|---|
| onnx_asr INT8, ORT CPU (Produktion), end-to-end, Bestwert aus 3 Läufen nach Warmup | A | **1,10 s, RTF 0,059** | MEASURED |
| onnx_asr INT8 über ORT-OpenVINO-EP 1.24.1 (`device_type=GPU`) | A | **FAIL**: `OpenVINO-EP-subgraph_7 ... Output names mismatch between OpenVINO and ONNX` | MEASURED |
| Nur Encoder INT8, ORT CPU | A | 0,49 s (RTF 0,026) | MEASURED |
| Nur Encoder INT8, OV GPU (FP16) | A | 0,27 s, aber **Ausgabe NaN** → unbrauchbar. Kompilieren dauert 29 s | MEASURED |
| Nur Encoder FP32, ORT CPU | A | 1,06 s | MEASURED |
| Nur Encoder FP32, OV GPU (FP16-Inferenz) | A | **0,06 s**, cos 0,999997 gegenüber CPU, Kompilieren 4–8 s | MEASURED |
| Hybrid: FP32-Encoder auf OV GPU, Decoder auf CPU, gegen CPU FP32 | 5 Clips | **5 von 5 textidentisch**. 0,89 / 0,19 / 0,21 / 0,27 / 0,40 s, auf CPU FP32 dagegen 3,21 / 1,37 / 1,10 / 1,21 / 1,66 s | MEASURED |
| Derselbe Hybrid gegen CPU INT8 (Produktion), Einzelmessung | 5 Clips | Hybrid 1,06 / 0,25 / 0,20 / 0,31 / 0,41 s, CPU INT8 2,02 / 0,69 / 0,58 / 0,58 / 0,87 s → **etwa 2–3× schneller**. Der Text unterscheidet sich (FP32 statt INT8) und war qualitativ eher besser: „Kimi“ statt „Chimi“, kein halluziniertes „Herr Präsident“ | MEASURED (n=1 je Clip, Rauschen möglich) |

### Qwen3-ASR-1.7B-hf BF16 (Produktionsklasse `QwenEngine(device=...)`, /tmp/qwenprobe.py)
| Gerät | 18,8 s (A) | 9,5 s | 8,6 s | Status |
|---|---|---|---|---|
| CPU (torch 2.14.1+cpu) | 9,78 s (RTF 0,52) | 3,27 s (0,34) | 6,94 s (0,81) | MEASURED |
| XPU (torch 2.14.1+xpu) | **5,20 s (0,28)** | **1,53 s (0,16)** | **1,77 s (0,21)** | MEASURED |

Auf der XPU belegt das Modell 4,28 GB Spitzenspeicher, das Laden dauert 7,3 s. Die Texte sind praktisch gleich und weichen nur in einzelnen Token ab („Prefill“ statt „Prefix“, „Vorbegfälle“ statt „Vorabfälle“). Die Integration ist ohne Codeänderung möglich, weil `QwenEngine` bereits einen `device`-Parameter hat.

## 4. NPU
- Parakeet: weiterhin **abgelehnt**, aufgrund der fünf früheren Ground-Truth-Punkte (exklusives Gerät, unter Last 55× „No available devices“, Decoder lässt sich nicht kompilieren, kein Vorteil gegenüber CPU INT8). Ich habe das nicht erneut geprüft.
- Qwen auf der NPU: **INFERRED / nicht getestet.** Die NPU braucht statische Shapes. Ein autoregressiver LLM-Decoder ist dort nur über die OpenVINO-GenAI-Pipeline (StaticLLM) praktikabel, nachdem das Modell mit optimum-intel exportiert wurde. Für die Qwen3-ASR-Multimodal-Architektur ist mir keine Export-Unterstützung bekannt. Dazu kommen die schon gemessenen Probleme mit dem exklusiven Gerät. Aufwand: mehrere Tage, Erfolgsaussicht niedrig. **Nicht verfolgen.**

## 5. Entscheidungstabelle
| Engine × Gerät | Läuft? | Latenz gegenüber CPU | Setup-Kosten | Risiko | Empfehlung |
|---|---|---|---|---|---|
| Parakeet INT8 × CPU (heute) | J | Basis, RTF 0,03–0,06 | – | gering | **beibehalten (Default)** |
| Parakeet INT8 × OV-GPU | N (NaN im Encoder, Fehler im EP) | – | – | – | verwerfen |
| Parakeet FP32-Encoder × OV-GPU + CPU-Decoder | J, MEASURED | ca. 2–3× schneller als CPU INT8 (z. B. 0,58 → 0,20 s) und bessere Qualität (FP32) | apt-Paket für die Compute-Runtime (sudo) + etwa 40 Zeilen Encoder-Adapter + 4–8 s Kompilieren beim Start (Cache möglich) | mittel: Treiber-/Kernel-Updates, das hier eingesetzte private Attribut `asr._encoder` von onnx_asr | optional, niedrige Priorität |
| Parakeet × NPU | N (früher gemessen) | – | – | hoch | verwerfen |
| Qwen BF16 × CPU | J | RTF 0,34–0,81 | – | gering | nur für Batch/Vergleich |
| Qwen BF16 × XPU (torch-xpu) | J, MEASURED | **ca. 2–4× schneller** (RTF 0,16–0,28) | torch-xpu (3,5 GB plus Intel-Runtime-Wheels) + apt-Compute-Runtime; eine eigene venv wäre sinnvoll | mittel: großer Stack, Konflikte mit CPU-torch | **der einzige Weg, Qwen interaktiv nutzbar zu machen** |
| Qwen × OV-GPU (optimum/genai-Export) | BLOCKED/INFERRED (kein Exportpfad bekannt) | – | Tage | hoch | nein |
| Qwen × NPU | INFERRED nein | – | Tage | hoch | nein |

**Abwägung für Parakeet (Produktion):** Nach M1/M2/A/B liegt die Zeit vom Loslassen bis zum Text bei etwa 2 s. Der Großteil davon ist kein Encoder-Compute. Am Ende muss nur noch das Endstück dekodiert werden, und der Encoder-Anteil eines solchen Fensters liegt auf der CPU bei ungefähr 0,2–0,5 s. Die GPU würde realistisch **etwa 0,1–0,4 s pro Release** einsparen und zusätzlich FP32-Qualität ohne Zeitaufschlag liefern. Das ist spürbar, aber kein Sprung, und es erfordert sudo-Treiber, einen Hack mit privatem Attribut und Startup-Kompilierung. → **Für Parakeet vorerst CPU INT8 behalten.** Der GPU-Hybrid kommt in den Backlog als Option „FP32-Qualität gratis“.
Für **Qwen** ist die XPU dagegen entscheidend: Erst mit RTF 0,16–0,28 wird Qwen überhaupt interaktiv denkbar.

## 6. Aufräumen
- `/tmp/neo` (entpackte Runtime), `/tmp/ovpkgs` (ORT-OV-EP), `/tmp/xpupkgs` + `/tmp/xpuprefix` (torch-xpu) und `/tmp/*.py` werden entfernt (siehe unten). Für eine Wiederholung findet sich das Rezept in Abschnitt 2.
- `.venv` wurde nicht verändert. openvino 2026.4.1 war schon vorher installiert.
