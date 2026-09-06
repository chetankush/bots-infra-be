![Tests](https://img.shields.io/badge/Tests-105%20passing-brightgreen)
![Python](https://img.shields.io/badge/Python-3.12-blue)
![LangGraph](https://img.shields.io/badge/LangGraph-agent%20runtime-orange)
![Postgres](https://img.shields.io/badge/Postgres-pgvector-informational)
![License](https://img.shields.io/badge/License-MIT-lightgrey)

# Multi-Tenant AI Agent Runtime (bots-infra-be)

Backend that serves production AI chatbots for many clients from one LangGraph agent.
Adding a customer is a database row, not a fork of the codebase.

Built for an agency running client bots across web chat and WhatsApp, with the parts that
usually get skipped: guardrails enforced structurally, per-tenant cost ceilings, real
data-retention, and an evaluation gate that blocks a release when quality regresses.

## Getting Started

1. **Clone the repository:**

    ```bash
    git clone https://github.com/chetankush/bots-infra-be.git
    cd bots-infra-be
    ```

2. **Run everything with one command:**

    ```bash
    ./run.sh
    ```

    This creates `.env`, installs dependencies, starts Postgres, applies the schema,
    seeds a demo tenant, builds the widget, and starts the server.

3. **To run tests:**

    ```bash
    pytest -q
    ```

4. **To see the coverage:**

    ```bash
    pytest --cov=app --cov-report=term-missing
    ```

**step by step instructions**

### Prerequisites

- Install **Python 3.12** (or `uv`, which will fetch it for you).
- Install **Docker** on your system — used for Postgres with pgvector.
- Install **Node.js** (only needed to rebuild the chat widget).
- Get an **OpenRouter API key** from [openrouter.ai](https://openrouter.ai) — the single
  gateway for every model the agent uses.

### Set up the environment

1. **Copy the template:**

    ```bash
    cp .env.example .env
    ```

2. **Generate an encryption key** (this encrypts each client's integration
   credentials at rest):

    ```bash
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    ```

3. **Fill in `.env`:** set `FV_OPENROUTER_API_KEY`, paste the key above into
   `CREDENTIAL_ENCRYPTION_KEY`, and change `ADMIN_API_KEY`.

### Install Postgres on Docker

The database needs the `pgvector` extension, so the compose file uses the
`pgvector/pgvector:pg16` image rather than plain Postgres.

```bash
docker compose up -d db
```

To open a psql shell against it:

```bash
docker compose exec db psql -U engine -d engine
```

### Install dependencies and create the schema

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"
python scripts/seed.py          # extension, tables, and one demo tenant
```

`seed.py` prints a `public_key` — the token a client's website uses to load its widget.

## To Run the Project

Use this command:

```bash
uvicorn app.main:app --reload
```

| URL | What it is |
|---|---|
| `http://localhost:8000/console` | Ops console — onboard clients, crawl sites, chat, inspect traces, leads, usage |
| `http://localhost:8000/demo` | The embeddable widget on a stand-in client site |
| `http://localhost:8000/docs` | OpenAPI reference |
| `http://localhost:8000/healthz` | Health check |

**Give the agent something to answer from:**

```bash
python scripts/crawl.py <tenant_key> https://your-client-site.com 25
```

**Talk to it from the terminal:**

```bash
python scripts/chat.py <public_key>
```

**Embed it on a client's site:**

```html
<script src="https://cdn.example.com/widget.js" data-key="pk_..." async></script>
```

**Optional services:**

```bash
docker compose --profile bi up -d metabase       # BI at :3002
docker compose --profile storage up -d minio     # local S3 for backups at :9001
```

## To Run tests

Run these commands in the root directory →

```bash
pytest -q
```

To see coverage →

```bash
pytest --cov=app --cov-report=term-missing
```

All 105 tests run **offline** — no cloud account and no network. Google Calendar,
Twilio and Resend are exercised against local fakes that speak the real wire protocols,
including the failures that actually matter (Google answering `HTTP 200` with a
per-calendar `errors` array, which silently double-books a day if mishandled).

**To run the evaluation gate** (this one does call the model, so it costs money):

```bash
python -m app.evals.runner <tenant_key>
python -m app.evals.runner <tenant_key> --model google/gemini-2.5-flash-lite
```

## Architecture

Every channel collapses into one envelope, so the graph never knows what it is serving.
Adding a channel is an adapter plus a config profile — never a second agent.

```
                    ┌──────────────┐
   web widget ─────►│              │
   WhatsApp   ─────►│   Envelope   │──►  guard_in ─┐
   SMS        ─────►│ (normalised) │      retrieve ─┴──► agent ⇄ tools ──► guard_out ──► reply
                    │              │                       │
                    └──────────────┘                       └── calendar · leads · handoff
                                                                (each with a mock twin)
```

Behaviour is **configuration resolved at request time**, not code:

```
platform defaults → vertical pack → tenant → location → channel profile
```

Layers deep-merge and validate into one `AgentConfig`. A dealership's "never quote
financing" rule and a clinic's intake fields are the same mechanism with different rows.
The same tenant renders 1200 characters with markdown on web and 220 plain-text
characters on voice — from one config, with identical guardrails.

### Decisions worth defending

- **Guardrails are graph nodes, not prompt text.** A rule written only into a prompt gets
  argued around; a check node does not. When the guard model itself fails, the turn is
  recorded as `guard_out:unverified` rather than passing silently.
- **Replies are held until cleared.** Streaming live would show a prohibited answer for
  ~1.4s before retracting it. Streaming is opt-in per tenant.
- **Tenant isolation is injected, not remembered.** Every query goes through a
  `TenantScope` that adds the predicate and raises on cross-tenant reads.
- **Idempotency lives in the database.** Reminders are `UNIQUE(appointment_id, rule_key)`;
  inbound Twilio messages are `UNIQUE(provider, provider_sid)`. Twilio's signature carries
  no timestamp, so a captured webhook stays replayable forever — signature validity alone
  cannot stop it.
- **Business records outlive transcripts.** Leads and appointments originally cascaded
  from conversations, so the retention job would have deleted every client's pipeline the
  first time it ran. They are `SET NULL` now, with a test that fails if anyone reverts it.
- **Everything that spends is bounded.** Per-tenant token budgets, three-tier rate
  limiting, capped batch loops, and terminal job states a sweep can never resurrect.

### Evaluation as a release gate

`app/evals` is a self-contained harness: a golden dataset per tenant, an LLM-judge graph,
runs persisted in Postgres, and a CI job that fails the PR on regression. A tenant's
golden set is replayed through the **real graph with tools force-mocked**, then judged on
correctness, grounding, scope adherence and prohibition safety.

Because the model is configuration, "is the cheaper model good enough for *this* client?"
becomes a measurement:

| answer model | correctness | grounded | scope | prohibition-safe | verdict |
|---|---|---|---|---|---|
| `claude-haiku-4.5` | 0.917 | 0.933 | 0.992 | 1.000 | pass |
| `gemini-2.5-flash-lite` | 0.754 | 0.941 | 0.855 | 1.000 | pass, scope at the gate |

### Measured

| | |
|---|---|
| Blocked turn | **~1.1s** — `guard_in` short-circuits; no retrieval, no answer model |
| Normal turn | **~2.9–3.6s** |
| Retrieval | 5ms, concurrent with the guard |
| Cost per conversation | **~$0.004** |
| Widget | 8 KB, Shadow DOM, zero framework |

## Built With

* **LangGraph** — the agent runtime: typed state with reducers, parallel fan-out, conditional routing.
* **LangChain** (`langchain-core`, `langchain-openai`) — model abstraction and message types.
* **OpenRouter** — one gateway for every provider, with per-tenant model choice and automatic fallback.
* **FastAPI** — async API layer, with SSE for streaming responses.
* **PostgreSQL + pgvector** — one datastore for vectors, job queue and relational data. No Redis, no Celery, no hosted vector database.
* **SQLAlchemy 2.0 (async) + Alembic** — ORM and migrations.
* **fastembed** — local ONNX embeddings (Hugging Face `bge-small-en-v1.5`); no PyTorch and no per-token cost.
* **trafilatura + selectolax** — main-content extraction and HTML parsing for the site crawler.
* **boto3** — S3-compatible object storage for encrypted backups (AWS S3, Oracle, R2, MinIO).
* **cryptography (Fernet)** — per-tenant integration credentials encrypted at rest.
* **structlog** — JSON logging with PII redaction.
* **pytest + pytest-asyncio** — 105 offline tests.
* **ruff** — linting and formatting.
* **Docker Compose + Caddy** — local stack and production reverse proxy with automatic TLS.

## Versioning

Python 3.12.13

PostgreSQL 16 with pgvector (`pgvector/pgvector:pg16`)

Docker Compose v2

Node.js 24 (widget build only)

Models: `claude-haiku-4.5` (answer) · `gemini-2.5-flash-lite` (guards) · `claude-sonnet-5` (escalation) · `claude-opus-5` (eval judge)

## Not built

Voice (telephony + STT + TTS is its own project). Named CRM connectors — the signed HMAC
webhook covers Zapier / n8n / Make / Power Automate today. Calendly.

## Before a client goes live

1. Set `ADMIN_API_KEY` — the default ships as a placeholder.
2. Set `allowed_origins` — empty means any website can use that bot on your credits.
3. `ENV=production` — tools are mocked in dev, so nothing reaches a real system.
4. WhatsApp needs Meta Business verification and US SMS needs 10DLC registration.
   Both take weeks; start them in parallel with the build.

## Author

* **Chetan Kushwah**
