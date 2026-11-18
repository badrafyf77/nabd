# NABD — ShopEasy customer support

Active development is **NABD**, a multilingual text support and complaint
intelligence platform backed by Salesforce. Start with the
[NABD development guide](docs/nabd/README.md),
[phased roadmap](docs/nabd/roadmap.md), and [current status](docs/nabd/status.md).

The documentation below describes the original voice prototype. Its reusable
agent foundation is being carried into NABD; voice development is deferred.

Realtime browser-to-backend voice-agent project with:

- a **FastAPI voice backend** in `backend`
- a **Next.js voice chat frontend** in `frontend`

The backend currently runs a simple chat agent. The architecture is intentionally
modular so another agent, such as customer support, can be added under
`backend/code/agents/`.

The plan for turning it into a customer support agent is in
[`docs/goals/`](docs/goals/README.md).

## Project Structure

```text
.
├── backend/
│   ├── code/
│   │   ├── agents/              # voice agents (chat)
│   │   ├── core/                # session, transport, STT/TTS nodes, pipeline
│   │   ├── services/            # LLM API client
│   │   └── server.py            # FastAPI entrypoint
│   ├── docker-compose.yml       # backend + vLLM stack
│   ├── Dockerfile
│   └── README.md
└── frontend/
    ├── src/app/page.tsx         # voice chat UI
    ├── public/static/           # audio worklets
    └── README.md
```

## How It Works

```text
Browser microphone
  -> frontend audio worklet
  -> backend WebSocket /ws
  -> STT node
  -> active agent
  -> LLM provider
  -> TTS node
  -> streamed audio response
  -> browser playback
```

For a backend file-by-file architecture map, see
[`backend/code/ARCHITECTURE.md`](backend/code/ARCHITECTURE.md).

## Quick Start

### 1. Start The Backend

The default backend stack runs FastAPI plus vLLM.

```bash
cd backend
cp .env.example .env
docker compose up -d --build
```

Backend URLs:

- App: `http://localhost:8080`
- vLLM: `http://localhost:8000`

Check it:

```bash
curl http://localhost:8080/
docker compose logs -f app
```

### 2. Start The Frontend

In another terminal:

```bash
cd frontend
cp .env.example .env.local
npm install
npm run dev
```

Open:

```text
http://localhost:3000
```

The frontend defaults to:

```bash
NEXT_PUBLIC_VOICE_SERVER_URL=http://localhost:8080
```

## Runtime Modes

### Chat Mode

Connect to `/ws`.

This uses:

- `backend/code/agents/chat/agent.py`

## Configuration

Backend config is layered:

1. `backend/code/config/global.yaml`
2. `backend/code/agents/<agent>/config.yaml`
3. environment variables from `backend/.env`

Frontend config:

```bash
NEXT_PUBLIC_VOICE_SERVER_URL=http://localhost:8080
```

## LLM Providers

| Provider | Use Case | Required Settings |
| --- | --- | --- |
| `vllm` | Default local/GPU stack | `LLM_PROVIDER=vllm`, `VLLM_BASE_URL=http://vllm:8000/v1` |
| `openai` | External API testing | `LLM_PROVIDER=openai`, `OPENAI_API_KEY=...`, `LLM_MODEL=...` |

## Development Notes

Backend docs:

- [`backend/README.md`](backend/README.md)
- [`backend/code/ARCHITECTURE.md`](backend/code/ARCHITECTURE.md)

Frontend docs:

- [`frontend/README.md`](frontend/README.md)

Useful checks:

```bash
# backend syntax
python3 -m compileall backend/code

# frontend production build
cd frontend
npm run build
```

## Current Boundaries

- `core/` owns runtime flow: session, transport, pipeline, STT/TTS nodes.
- `agents/` owns agent behavior and prompts.
- `services/` owns external integrations, such as the LLM API client.
- `frontend/` owns browser capture/playback and transcript rendering.

Keep provider-specific STT/TTS logic in `core/nodes/`; do not duplicate it under
`services/`.
