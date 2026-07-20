# You42 Avatar POC — RunPod Deployment Guide

Companion to the POC brief (`you42-avatar-poc-brief.md` in the planning
repo). Everything code-side is already merged on this branch and unit-tested
(115 tests green); pod time is for GPU verification and the test protocol
only. **Never edit code on the meter** — fix locally, push, `git pull` on
the pod.

---

## 0. Before renting anything

- [ ] RunPod account funded (≥ $30)
- [ ] OpenRouter API key created (Configs B & C)
- [ ] Assets ready: **~10-sec well-lit frontal video** (primary template),
      15–30 sec clean voice clip, 1 photo (optional extra run)
- [ ] This branch pushed to the fork (`git push`)

## 1. Create the pod

| Setting | Value |
|---|---|
| GPU | RTX 3090 24 GB (or 4090; fallback A5000) |
| Template | RunPod **PyTorch 2.x + CUDA 12.x** |
| Container disk | 30 GB |
| **Network volume** | 40 GB, mounted at `/workspace` (persists across pods — model weights live here) |
| Exposed HTTP ports | `8000` (backend), `3000` (frontend) |
| Billing | On-demand. **Stop the pod after every session.** |

The proxy URLs will be
`https://<POD_ID>-8000.proxy.runpod.net` and `…-3000…`. The app streams
over plain WebSocket (verified), so the proxy works as-is — no TURN needed.

## 2. First-time setup (once per network volume, ~20 min)

```bash
cd /workspace
git clone https://github.com/topherclaude-png/ai-avatar-system.git
cd ai-avatar-system
git checkout poc/tools-and-qr

# Backend deps — KEEP THE POD'S CUDA TORCH (the pinned torch in
# requirements.txt would replace it with a mismatched build), and work
# around the chatterbox-tts resolver conflict (0.1.7 demands
# transformers 5.x; ≤0.1.6 caps numpy below what opencv needs on
# py3.12). Verified install order:
python -m venv --system-site-packages /workspace/venv   # venv on the VOLUME
ln -sfn /workspace/venv backend/venv
source /workspace/venv/bin/activate
cd backend
grep -viE '^(torch|torchvision|torchaudio|chatterbox-tts)' requirements.txt \
  | sed 's/^librosa==0.10.1$/librosa==0.11.0/' > /tmp/reqs.txt
pip install -r /tmp/reqs.txt
pip install --no-deps chatterbox-tts==0.1.6
pip install s3tokenizer resemble-perth conformer

# MuseTalk + weights (~3 GB) — onto the network volume, symlinked in
mkdir -p /workspace/models
ln -s /workspace/models models   # backend/models -> volume
cd .. && bash scripts/setup_musetalk.sh

# Keep HuggingFace + Chatterbox downloads on the volume too
export HF_HOME=/workspace/hf     # add to ~/.bashrc on the pod

# Frontend
cd frontend && npm install && cd ..
```

Restarted pods on the same volume skip everything except `git pull`.

## 3. Configure `.env`

```bash
cp .env.example .env
```

Then set (everything else can stay default):

```env
# ── LLM (swap per config — THIS IS THE WHOLE TEST MATRIX) ──────────────
LLM_PROVIDER=openai
# Config A (vLLM, same box):  LLM_MODEL=Qwen/Qwen2.5-14B-Instruct-AWQ
#                             OPENAI_BASE_URL=http://localhost:8001/v1
#                             OPENAI_API_KEY=vllm
# Config B (OpenRouter):      LLM_MODEL=qwen/qwen-2.5-72b-instruct
#                             OPENAI_BASE_URL=https://openrouter.ai/api/v1
#                             OPENAI_API_KEY=<openrouter key>
# Config C (Claude ceiling):  LLM_MODEL=anthropic/claude-sonnet-4.5
#                             OPENAI_BASE_URL=https://openrouter.ai/api/v1
#                             OPENAI_API_KEY=<openrouter key>
LLM_MODEL=qwen/qwen-2.5-72b-instruct
OPENAI_BASE_URL=https://openrouter.ai/api/v1
OPENAI_API_KEY=

# ── kiosk stack (already wired on this branch) ─────────────────────────
TOOLS_ENABLED=true
EVENTS_FILE=data/events.json
SYSTEM_PROMPT_FILE=prompts/you42_kiosk.md
TRANSCRIPT_LOG_DIR=logs/transcripts
MAX_SESSION_TURNS=40
MAX_SESSION_MINUTES=10

# ── engines ────────────────────────────────────────────────────────────
AVATAR_ENGINE=musetalk
TTS_PROVIDER=chatterbox
STT_PROVIDER=whisper
WHISPER_MODEL=large-v3-turbo

# ── runtime / security ─────────────────────────────────────────────────
DEBUG=false                                    # token-less demo sessions off
SECRET_KEY=<python -c "import secrets; print(secrets.token_hex(32))">
JWT_SECRET_KEY=<same recipe, different value>
DATABASE_URL=sqlite+aiosqlite:////workspace/avatar.db
CORS_ORIGINS=https://<POD_ID>-3000.proxy.runpod.net
```

Do NOT add extra keys (e.g. `HF_HOME`) to `.env` — the app's pydantic
Settings forbid unknown variables and will crash at import. Shell-level
vars like `HF_HOME=/workspace/hf` go in `~/.bashrc` instead.

Create the DB schema once (sqlite + DEBUG=false skips auto-create):

```bash
cd backend
python -c "
import asyncio
from app.database import engine, Base
import app.models
async def m():
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
asyncio.run(m())
"
```

## 4. Start services

```bash
# Backend (terminal 1)
cd /workspace/ai-avatar-system/backend
uvicorn main:app --host 0.0.0.0 --port 8000

# Frontend (terminal 2)
cd /workspace/ai-avatar-system/frontend
NEXT_PUBLIC_API_URL=https://<POD_ID>-8000.proxy.runpod.net npm run build
NEXT_PUBLIC_API_URL=https://<POD_ID>-8000.proxy.runpod.net npm start
```

(Config A only — terminal 3: `pip install vllm && vllm serve
Qwen/Qwen2.5-14B-Instruct-AWQ --port 8001 --max-model-len 8192`. Watch VRAM:
MuseTalk + Whisper + Chatterbox + vLLM must coexist in 24 GB; if it stutters,
that's a scorecard data point, not a bug to fix.)

## 5. Smoke test (session 1 gate)

1. `curl https://<POD_ID>-8000.proxy.runpod.net/health` → `healthy`
2. Open the frontend proxy URL → register/login
3. Upload the **10-sec video** as the avatar (Avatars → upload). First
   animation job pays one-time prep (frame extraction + landmarks + VAE,
   up to ~5 min budgeted) — later jobs are seconds.
4. Upload the voice clip (Voices)
5. Text turn: type "What events are coming up?" → expect a `get_events`
   tool call and lip-synced video with idle motion from your clip
6. Mic round-trip + talk over the avatar mid-reply (barge-in)
7. "I want two tickets to the showcase" → QR overlay appears

If step 5 fails on the base repo after 4 focused hours → pivot per brief §13.

## 6. Protocol runs (brief §8)

Screen-record every run (OBS on your local machine capturing the browser —
recordings never live on the pod). Per configuration (A/B/C × video/photo
template): the 9 scripted interactions from the brief, §8. Transcripts with
guardrail flags land in `backend/logs/transcripts/<session>.jsonl` — pull
them off the pod after each block:

```bash
# from your machine
scp -r root@<pod-ssh>:/workspace/ai-avatar-system/backend/logs/transcripts ./runs/<config-name>/
```

Record per config: GPU util (`nvidia-smi -l 5`), VRAM, latency probe
timings, RunPod $ burned.

## 7. Portable image (deliverable 1)

Standard RunPod pods can't `docker build` (no docker-in-docker). Bake the
image from the fork instead — GitHub Actions builds `backend/Dockerfile` +
`frontend/Dockerfile` and pushes to GHCR; then boot a **fresh pod from that
image** with only the `.env` above. That boot-from-registry test IS the §13
portability proof (Hetzner/owned hardware take the same image unchanged).

## 8. Shutdown checklist (every session)

- [ ] Transcripts + recordings pulled off the pod
- [ ] `.env` backed up locally (it's not in git)
- [ ] Pod **stopped** in the RunPod console (not just idle) — verify the
      console shows $0/hr before closing the laptop

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| First video-template job times out | Prep budget is 300 s on GPU — if exceeded, check worker stderr; try a shorter/720p clip |
| Avatar speaks in a generic voice + `tts_fallback` toast | Chatterbox model still downloading or failed to load — check backend logs; verify `HF_HOME` is on the volume |
| `tools` param rejected by endpoint | That LLM endpoint lacks function calling — set `TOOLS_ENABLED=false` for that config and note it on the scorecard |
| Avatar interrupts itself when speaker volume is up | Browser AEC/VAD tuning (brief §13) — lower speaker volume or use a headset to isolate, note kiosk implication |
| WS closes with 4401 | Login expired or `DEBUG=false` with no token — sign in again |
| vLLM OOM alongside MuseTalk (Config A) | Reduce `--max-model-len`, or record the contention and prioritize Config B (that's the data point the brief wants) |
