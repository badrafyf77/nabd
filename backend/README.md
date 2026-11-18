# Voice Agent Backend

For active **NABD text development**, use the
[NABD development guide](../docs/nabd/README.md) and `docker-compose.nabd.yml`.
The text app has separate dependencies, configuration, and startup. This page
documents the legacy voice backend.

FastAPI backend for realtime voice-agent sessions. It receives microphone audio
over WebSocket, transcribes speech, runs the selected agent, streams TTS audio
back to the client, and exposes a few support endpoints.

This README stays project-level and agent-neutral. Agent-specific behavior lives
inside each folder under [`code/agents`](./code/agents).

## Runtime Flow

```text
Browser microphone
  -> FastAPI WebSocket /ws
  -> core/transport/websocket.py
  -> core/nodes/stt/faster_whisper.py
  -> core/session.py
  -> agents/support/agent.py   (identity steps, supervisor, orders agent, shop tools)
  -> core/pipeline.py          (speaks the text the supervisor streams)
  -> core/nodes/tts/realtime.py
  -> WebSocket tts_chunk messages
```

For the detailed file-by-file map, see [`code/ARCHITECTURE.md`](./code/ARCHITECTURE.md).

## Important Files

```text
code/
├── server.py                    # FastAPI app and agent selection
├── config/global.yaml           # shared defaults
├── agents/
│   ├── base.py                  # BaseAgent.run_turn(...) contract
│   ├── graph.py                 # shared LangGraph model <-> tools nodes
│   ├── models.py                # LangChain ChatOpenAI from config.yaml
│   ├── support/                 # supervisor graph: identity -> model <-> tools
│   └── orders/                  # orders specialist graph (background)
├── studio.py                    # the graphs for LangGraph Studio
├── shop/                        # fake ShopEasy database and its tools
├── core/
│   ├── session.py               # WebSocket session lifecycle
│   ├── trace.py                 # one trace file per call
│   ├── pipeline.py              # LLM -> TTS orchestration
│   ├── voice_runtime_pool.py    # warmed runtime leasing/reuse
│   ├── transport/websocket.py   # audio packet handling/resampling
│   └── nodes/
│       ├── stt/faster_whisper.py
│       └── tts/realtime.py
└── services/
    └── llm.py                   # vLLM/OpenAI-compatible LLM client
```

## Docker Quick Start

From `backend`:

```bash
cp .env.example .env   # then set OPENAI_API_KEY
docker compose -f docker-compose.openai.yml up -d --build
```

The agents use the OpenAI API; the GPU runs speech-to-text, text-to-speech,
voice detection and turn detection. `docker-compose.yml` still starts a local
vLLM server if you want to try an open model instead.

Default port: backend app on `http://localhost:8080`.

Useful checks:

```bash
docker compose -f docker-compose.openai.yml logs -f app
curl http://localhost:8080/
```

The first build/start can take a while because Docker downloads Python
dependencies and model assets.

## Running on Modal (serverless GPU)

[`modal_app.py`](./modal_app.py) runs the same backend on [Modal](https://modal.com)
instead of docker compose. Modal builds the image from the `Dockerfile`, starts
one GPU container when someone connects, and serves the WebSocket at a public
HTTPS address. The container stops after 5 idle minutes, so nothing is billed
while nobody uses it. The free Starter plan includes $30 of credits a month
(about $0.80 an hour on an L4).

```bash
cd backend
.venv/bin/pip install modal
.venv/bin/modal token new              # once, signs the CLI in to your account
.venv/bin/modal deploy modal_app.py    # builds (long the first time), prints the https://…modal.run address
.venv/bin/modal app stop shopeasy-voice-agent   # when you are done
```

`modal deploy` is more reliable than `modal serve` here: `serve` watches files and
reloads, and changes under `code/` only reach the image after a full restart.

- Keys: `OPENAI_API_KEY` and the `LANGSMITH_*` settings are read from `backend/.env`
  when you run the command. Only those names are sent to Modal.
- GPU: `MODAL_GPU=T4 .venv/bin/modal serve modal_app.py` for a T4 (default L4).
- Cold start: the first connection after a pause took about 8 minutes in our tests
  (Modal copies the large image to a GPU machine, then the speech models load in
  about 30 s). Send `curl https://…modal.run/` first and wait for `"status":"ok"`.
- Traces: `.venv/bin/modal volume ls shopeasy-voice-state traces` and
  `.venv/bin/modal volume get shopeasy-voice-state traces/<file>.jsonl`.
- The ShopEasy database starts fresh in every new container.

Test it without a browser (macOS): a spoken test call that prints what the
agent hears and says, and how fast it answers:

```bash
.venv/bin/python tests/voice_call.py https://<your-address>.modal.run
```

Or with the frontend: `npm run dev` in `frontend`, open `http://localhost:3000`
and enter the `https://…modal.run` address as the server URL.

## Agent Selection

All voice sessions use the same WebSocket endpoint:

```text
/ws
```

Current selection logic lives in [`code/server.py`](./code/server.py):

| Connection | Agent |
| --- | --- |
| `/ws` | `support` (change it with the `VOICE_AGENT` variable) |

The selected agent is built in `server.py`, then passed into
`core/session.py::run_session(...)`. The session and pipeline only depend on the
shared `BaseAgent.run_turn(...)` interface.

To change which agent runs:

1. Edit `code/server.py::ws_endpoint`.
2. Import the target agent builder, for example `build_support_agent`.
3. Load the matching config with `load_agent_config("<agent_name>")`.
4. Pass the built agent and config to `run_session(...)`.

To add a new agent:

1. Create `code/agents/<name>/`.
2. Add `agent.py`, `config.yaml`, and `README.md`.
3. Implement a builder that returns a `BaseAgent`.
4. Keep prompts, tools, and agent-only helpers inside that folder.
5. Register the agent selection branch in `code/server.py`.

## Existing Agents

| Agent | Folder | Purpose |
| --- | --- | --- |
| `support` | [`code/agents/support`](./code/agents/support) | ShopEasy supervisor: identity steps, then answers with tools. Talks to the customer. |
| `orders` | [`code/agents/orders`](./code/agents/orders) | Orders specialist. Runs in the background for the supervisor; never talks to the customer. |

## Configuration

Copy [`.env.example`](./.env.example) to `.env`.

Common variables:

| Variable | Purpose |
| --- | --- |
| `LLM_PROVIDER` | `openai` (default) or `vllm`. Each agent's `config.yaml` can override it. |
| `LLM_MODEL` | Fallback model name. Each agent sets its own in `config.yaml` (both `gpt-5.4-mini` for now). |
| `VLLM_BASE_URL` | OpenAI-compatible vLLM URL, usually `http://vllm:8000/v1` in Docker. |
| `OPENAI_API_KEY` | Required for the OpenAI models. |
| `SHOP_DB_PATH` | Where the ShopEasy database lives. Default `backend/data/shop.db`. |
| `TRACE_DIR` | Where trace files go. Default `backend/logs/traces`. |
| `VOICE_AGENT` | Which agent `/ws` runs. Default `support`. |
| `STT_MODEL` | Main faster-whisper model, default `medium.en`. |
| `STT_REALTIME_MODEL` | Optional realtime model when not sharing the main STT model. |
| `STT_USE_MAIN_MODEL_FOR_REALTIME` | Use one STT model for both realtime and final transcription. |
| `TURN_DETECTION_DEVICE` | Device for turn detection, usually `cpu` or `cuda`. |
| `VOICE_RUNTIME_MIN_WARM` | Number of warmed voice runtimes to keep ready. |
| `VOICE_RUNTIME_MAX_CONCURRENT` | Maximum concurrent voice sessions. |

Agent config is layered:

1. `code/config/global.yaml`
2. `code/agents/<agent>/config.yaml`
3. supported environment overrides

## HTTP / WebSocket API

### `GET /`

Health/status payload.

### `WS /ws`

Runs a voice session with the selected agent.

## Local Development Without Docker

Local backend runs are useful for quick code iteration, but the Docker stack is
more representative because it handles GPU/runtime dependencies and vLLM.

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cd code
python server.py
```

For local development, set:

```bash
LLM_PROVIDER=openai
OPENAI_API_KEY=...
```

## The ShopEasy Database

A made-up shop with 10 customers and 25 orders, built to cover the tricky
support cases (late, lost, too late to return, items that can't be returned).
Dates are counted from today, so the cases stay true on any day.

The server builds `data/shop.db` on first use. To rebuild it from scratch
(this also removes tickets and handoffs):

```bash
cd backend/code
python -m shop.seed
```

Test phone numbers: Sara Benali `07700900101` (one order), James Carter
`07700900102` (three orders); see `code/shop/seed.py` for the rest.

## Debugging with LangSmith and LangGraph Studio

The agents are LangGraph graphs, so two LangChain tools can show what they do.

**LangSmith** shows every run as a tree: each model call (prompt, answer, tokens,
time), each tool call with its input and output, and the orders agent's run
under the supervisor's `ask_orders_agent` call. All runs of one phone call are
grouped as one thread (by `session_id`). LangSmith is a website; it can't run on
your computer (self-hosting is only for paid Enterprise plans).

1. Create a free account at <https://smith.langchain.com> (the Developer plan
   includes about 5,000 traces a month).
2. Settings -> API Keys -> create a key.
3. In `backend/.env`:

   ```bash
   LANGSMITH_TRACING=true
   LANGSMITH_API_KEY=lsv2_...
   LANGSMITH_PROJECT=shopeasy-voice-agent
   ```

Now every scenario test, Studio run and voice call shows up in that project.
The free unit tests never send anything to LangSmith.

**LangGraph Studio** lets you chat with a graph yourself and watch it move from
node to node:

```bash
cd backend
.venv/bin/langgraph dev
```

It opens <https://smith.langchain.com/studio/?baseUrl=http://127.0.0.1:2024>.
The graphs run on your computer; the page is only the screen. Pick
`support_agent`, then send one message per run on the same thread: the phone
number (`07700900101`), then `yes`, then your question. In Studio the orders
agent runs inside the `ask_orders_agent` tool call, so the whole run is in one
place. `orders_agent` can also be tried alone: give it `customer_id` (e.g.
`C002`) and a message. Each message calls the real OpenAI models, so it costs a
little, like a scenario run.

## Traces

Every call writes `logs/traces/<session_id>.jsonl`, one JSON line per step:
what the customer said, identity steps, every model call (with token counts),
every tool call, calls to the orders agent and their results, fillers, limits
reached, and what was spoken. `parent_id` links the lines into a tree. This file
stays on your computer and is what the scenario tests check; LangSmith is the
place to look at runs by eye.

## Tests

The tests only need the agent packages, not the speech ones, so they run in a
small virtual environment (a separate folder of Python packages):

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest               # unit tests: fake models, free, about 2 seconds
SCENARIO_RUNS=1 .venv/bin/pytest -m llm -k track_single_order   # one real scenario, once
.venv/bin/pytest -m llm        # all 15 scenarios x 3 runs with the real models (costs money)
```

The scenarios are YAML files in `tests/scenarios/`. Each customer turn lists the
tool and agent calls we expect, calls that must not happen, the orders agent's
answer, and facts the customer must hear. The checks read the call's trace, so
they test what really happened. The trace of every scenario run is kept in
`logs/traces/scenarios/`.

## Frontend Integration

The frontend connects to this backend through `NEXT_PUBLIC_VOICE_SERVER_URL`.
For the default Docker backend:

```bash
NEXT_PUBLIC_VOICE_SERVER_URL=http://localhost:8080
```

See [`../frontend/README.md`](../frontend/README.md).

## Boundaries

- `core/` owns runtime flow: session, transport, pipeline, STT/TTS nodes.
- `agents/` owns agent behavior, prompts, tools, and per-agent config.
- `services/` owns external integrations such as LLM API access.
- Provider-specific STT/TTS logic belongs in `core/nodes/`, not `services/`.
