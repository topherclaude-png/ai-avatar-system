# You42 Avatar Kiosk — Tech Stack

Architecture v2 (post-pivot, demo state 2026-07-22). Fully open-source
rendering with zero per-minute avatar fees; the only metered external
service is the LLM API (pennies per conversation).

```
[Guest]
  │ voice (hands-free VAD)                 ┌─────────────────────────────┐
  ▼                                        │  RunPod RTX 3090 pod        │
[Kiosk UI  ·  Next.js /kiosk] ◄─WebRTC────│  LiveTalking :8010          │
  │ WS (utterances, captions, QR)          │   MuseTalk V1.5 lip-sync    │
  ▼                                        │   edge-tts / cloned audio   │
[Kiosk brain · FastAPI :8000] ──/human────►│                             │
  │  STT → LLM(+tools) → guardrails        │  (both read a 50 GB         │
  ▼                                        │   network volume)           │
[OpenRouter — Qwen 2.5 72B]                └─────────────────────────────┘
```

## Frontend (`frontend/`)

| Concern | Tech |
|---|---|
| Framework | Next.js 16 · React 18 · TypeScript |
| Styling | Tailwind CSS |
| State / data | Zustand · TanStack Query · axios |
| Kiosk mode | `/kiosk` — full-screen, touch-to-start, zero chrome |
| Hands-free mic | Silero VAD via `@ricky0123/vad-web` (WASM, in-browser, assets self-hosted) |
| Avatar stream | WebRTC recv-only (`LiveTalkingView`), STUN: Google |
| Voice barge-in | speech onset → WS `stop` → server flushes speech queue |
| Payment QR | `react-qr-code` overlay on `show_payment_qr` tool events |

## Kiosk brain (`backend/`)

| Concern | Tech |
|---|---|
| API / WS | FastAPI (Python 3.12) · uvicorn · WebSocket chat protocol |
| Auth | JWT (python-jose) · bcrypt |
| DB | SQLAlchemy 2 async · SQLite (POC) / Postgres-ready |
| STT | faster-whisper `small` on GPU (fp16) |
| LLM | OpenAI-compatible client — **env-swappable**: OpenRouter (Qwen 2.5 72B, current) / vLLM local / Claude; native Anthropic path with prompt caching also present |
| Tool calling | Streamed agentic loop: `get_events` (server-side `start >= now` filter), `show_payment_qr` (stub checkout at `/pay`) |
| Guardrails | 5 layers: scope prompt · injection resistance · I/O moderation hook points (pass-through in POC) · session caps · flagged JSONL transcripts |
| Voice clone | Chatterbox (Resemble AI) → audio pushed to LiveTalking `/humanaudio`; markdown stripped before all speech |
| Rendering client | `livetalk.py` → LiveTalking REST (`/human`, `/interrupt_talk`) |

## Rendering server (LiveTalking fork)

| Concern | Tech |
|---|---|
| Framework | [LiveTalking](https://github.com/lipku/LiveTalking) (aiohttp + aiortc) — continuous WebRTC stream, ~45 fps MuseTalk on a 3090 |
| Lip-sync | MuseTalk V1.5 (UNet + SD-VAE + Whisper-tiny audio features + BiSeNet face parsing) |
| Avatar identity | Baked per template video via `genavatar.py` (frames + landmarks + latents + blend masks) |
| TTS (default) | edge-tts `en-US-AndrewMultilingualNeural` (~0.5 s to first audio) |
| TTS (cloned) | Chatterbox audio pushed from the kiosk brain (~3–4 s/sentence) |

## Models on the volume

MuseTalk V1.5 weights (~5 GB) · Whisper-tiny (MuseTalk audio encoder) ·
faster-whisper small (STT) · Chatterbox multilingual (voice clone) ·
Silero VAD (browser) · s3fd + BiSeNet (avatar prep). LLM is hosted
(OpenRouter) — no local LLM VRAM in the demo config.

## Infrastructure

| Concern | Tech |
|---|---|
| GPU host | RunPod RTX 3090 24 GB (on-demand, ~$0.46/hr) |
| Persistence | 50 GB network volume at `/workspace` (weights, venvs, repos, DB, avatars) |
| Process mgmt | tmux sessions: `livetalking` · `backend` · `frontend` |
| Ingress | RunPod HTTP proxy (Cloudflare-fronted) — ports 3000/8000/8010 (+8888 Jupyter) |
| Portability | All config via env; brief §13 target: same stack on Hetzner / owned hardware |

## Forks (fork-and-own, both with upstream fixes)

- `topherclaude-png/ai-avatar-system` — branch `poc/tools-and-qr` (kiosk brain + UI; ~15 upstream bugs fixed during the POC)
- `topherclaude-png/LiveTalking` — `main` (PyTorch ≥2.6 `weights_only` patch, out-of-frame crop-box blend fix)

## Notable per-conversation costs

LLM (OpenRouter Qwen 72B): ~$0.01–0.05 per conversation · TTS: free
(edge-tts) or local GPU (Chatterbox) · Rendering/STT: local GPU only.
