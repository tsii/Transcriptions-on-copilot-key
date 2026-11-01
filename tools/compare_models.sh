#!/usr/bin/env bash
# Model comparison on a WAV file. Runs sequentially and outputs sections.
set -uo pipefail
WAV="${1:-/home/salmon/projects/Whisper-local/whisper.cpp/samples/jfk.wav}"
cd /home/salmon/pi-web-ui/STT-Button

run() {
  echo ""
  echo "## $1"
  shift
  .venv/bin/python -c "$1" 2>&1 | grep -vE "Warn|httpx|huggingface|Loading weights|it/s|tokenizer" | grep -vE "^$"
}

run "Qwen3-ASR-1.7B" '
import sys,time,wave,numpy as np,tempfile
sys.path.insert(0,"src"); import torch
from transformers import AutoModelForMultimodalLM, AutoProcessor
p=AutoProcessor.from_pretrained("Qwen/Qwen3-ASR-1.7B-hf")
m=AutoModelForMultimodalLM.from_pretrained("Qwen/Qwen3-ASR-1.7B-hf", dtype=torch.float32).to("cpu").eval()
wf=wave.open("'"$WAV"'","rb"); d=wf.readframes(wf.getnframes())
a=np.frombuffer(d,dtype=np.int16).astype(np.float32)/32768.0
with tempfile.NamedTemporaryFile(suffix=".wav") as f:
    with wave.open(f.name,"wb") as w:
        w.setnchannels(1);w.setsampwidth(2);w.setframerate(16000);w.writeframes((np.clip(a,-1,1)*32767).astype(np.int16).tobytes())
    inp=p.apply_transcription_request(audio=f.name).to(m.device,m.dtype)
    t=time.time(); o=m.generate(**inp,max_new_tokens=256); g=o[:,inp["input_ids"].shape[1]:]
    print(p.decode(g,return_format="transcription_only")[0]); print(f"(inference {time.time()-t:.1f}s)")
'

run "Granite Speech 4.1-2B" '
import sys,time,wave,numpy as np
sys.path.insert(0,"src"); import torch
from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor
p=AutoProcessor.from_pretrained("ibm-granite/granite-speech-4.1-2b")
m=AutoModelForSpeechSeq2Seq.from_pretrained("ibm-granite/granite-speech-4.1-2b", dtype=torch.float32).to("cpu").eval()
wf=wave.open("'"$WAV"'","rb"); d=wf.readframes(wf.getnframes())
a=np.frombuffer(d,dtype=np.int16).astype(np.float32)/32768.0
prompt="<|audio|>transcribe the speech with proper punctuation and capitalization."
chat=[{"role":"user","content":prompt}]
tp=p.tokenizer.apply_chat_template(chat,tokenize=False,add_generation_prompt=True)
wav=torch.from_numpy(a).unsqueeze(0)
inp=p(tp,wav,return_tensors="pt",sampling_rate=16000)
t=time.time(); o=m.generate(**inp,max_new_tokens=200,do_sample=False,num_beams=1)
ni=inp["input_ids"].shape[-1]
print(p.tokenizer.batch_decode(o[:,ni:],skip_special_tokens=True)[0]); print(f"(inference {time.time()-t:.1f}s)")
'

run "Parakeet TDT 0.6B v3" '
import sys,time,wave,numpy as np
sys.path.insert(0,"src"); import torch
from transformers import AutoModelForTDT, AutoProcessor
p=AutoProcessor.from_pretrained("nvidia/parakeet-tdt-0.6b-v3")
m=AutoModelForTDT.from_pretrained("nvidia/parakeet-tdt-0.6b-v3", dtype=torch.float32).to("cpu").eval()
wf=wave.open("'"$WAV"'","rb"); d=wf.readframes(wf.getnframes())
a=np.frombuffer(d,dtype=np.int16).astype(np.float32)/32768.0
inp=p([a],sampling_rate=16000); inp.to("cpu",dtype=torch.float32)
t=time.time(); o=m.generate(**inp,return_dict_in_generate=True)
print(p.decode(o.sequences,skip_special_tokens=True)[0]); print(f"(inference {time.time()-t:.1f}s)")
'

run "faster-whisper large-v3-turbo (reference)" '
import sys,time,wave
sys.path.insert(0,"src")
from stt_button.asr import WhisperEngine
wf=wave.open("'"$WAV"'","rb"); d=wf.readframes(wf.getnframes())
e=WhisperEngine("large-v3-turbo",language="en",compute_type="int8")
t=time.time(); r=e.transcribe(d)
print(r.text); print(f"(inference {time.time()-t:.1f}s)")
'