# bots-infra-be

**A multi-tenant runtime for production AI agents.** One LangGraph agent, many clients —
adding a customer is a database row, not a fork of the codebase.

Built to serve real chatbots for an agency's clients across web chat and WhatsApp, with
the parts that usually get skipped: guardrails that are enforced structurally, per-tenant
cost ceilings, data-retention that actually deletes, and an evaluation gate that blocks a
release when quality regresses.

```
                    ┌──────────────┐
   web widget ─────►│              │
   WhatsApp   ─────►│   Envelope   │──►  guard_in ─┐
   SMS        ─────►│ (normalised) │      retrieve ─┴──► agent ⇄ tools ──► guard_out ──► reply
   voice*     ─────►│              │                        │
                    └──────────────┘                        └── calendar · leads · handoff
                                                                 (each with a mock twin)
```

Every channel collapses to one envelope, so the graph never knows what it is serving.
Adding a channel is an adapter plus a config profile — never a second agent.

---

## The idea that holds it together

Behaviour is **configuration resolved at request time**, not code:

```
platform defaults → vertical pack → tenant → location → channel profile
```

Layers deep-merge and validate into one `AgentConfig`. A dealership's "never quote
financing" rule and a clinic's intake fields are the same mechanism with different rows.
Onboarding a client is an `INSERT` and a site crawl.

The same tenant renders differently per channel from one config: 1200 characters with
markdown on web, 220 plain-text characters on voice — with identical guardrails.

---

## Engineering decisions worth defending

**Guardrails are nodes, not prompt text.** `guard_in` classifies the incoming message;
`guard_out` inspects the draft reply against the tenant's prohibition list. A rule written
only into a prompt gets argued around; a check node does not. When a guard model itself
fails, the turn is recorded as `guard_out:unverified` rather than passing silently —
because a check that did not run is not a check that passed.

**Replies are held until they are cleared.** Streaming tokens live would show the visitor
a prohibited answer for ~1.4s before retracting it. Tenants with prohibitions get the
plain JSON path; streaming is opt-in per tenant.

**Tenant isolation is injected, not remembered.** Every query goes through a `TenantScope`
that adds the predicate and raises on a cross-tenant read. One forgotten `WHERE` clause is
the failure that ends an agency.

**Idempotency lives in the database.** Reminders are `UNIQUE(appointment_id, rule_key)`;
inbound Twilio messages are `UNIQUE(provider, provider_sid)`. Two workers racing are
*physically unable* to double-send. Twilio's signature carries no timestamp, so a captured
webhook stays replayable forever — signature validity alone cannot stop it.

**Business records outlive transcripts.** Leads and appointments originally cascaded from
conversations, so the advertised retention job would have deleted every client's pipeline
the first time it ran. They are `SET NULL` now, with a test that fails if anyone reverts it.

**Cost is the only unbounded axis, so everything that spends is bounded.** Per-tenant token
budgets with graceful degrade, three-tier rate limiting (session / IP / tenant), capped
batch loops, and terminal job states that a sweep can never resurrect.

**Cheap where it is free to be cheap.** Embeddings run locally through ONNX — no PyTorch,
no per-token cost, and no cloud dependency. Language detection is stopword-based rather
than a model call, which removes ~800ms and a charge from every turn.

---

## Evaluation as a release gate

The differentiator, and the reason this is infrastructure rather than a demo.

A tenant's golden set is replayed through the **real graph with mocked tools**, each reply
judged on correctness, grounding, scope adherence and prohibition safety. CI fails the PR
when a metric regresses.

Two rules the runner enforces, both learned the hard way:

- **Tools are force-mocked regardless of environment.** An eval replaying thousands of
  conversations in a production environment would otherwise book real appointments on real
  customers' calendars.
- **A failed judge is not a zero score.** Judge errors are excluded from the metrics and
  reported separately; above a 10% error rate the run fails outright. Without this, a flaky
  judge call reads as a safety violation and the gate lies in both directions.

Because the model is config, "is the cheaper model good enough for *this* client?" becomes
a measurement:

| answer model | correctness | grounded | scope | prohibition-safe | verdict |
|---|---|---|---|---|---|
| `claude-haiku-4.5` | 0.917 | 0.933 | 0.992 | 1.000 | pass |
| `gemini-2.5-flash-lite` | 0.754 | 0.941 | 0.855 | 1.000 | pass, scope at the gate |

---

## Measured

| | |
|---|---|
| Blocked turn | **~1.1s** — `guard_in` short-circuits; no retrieval, no answer model |
| Normal turn | **~2.9–3.6s** — `guard_in` ∥ `retrieve`, then agent, then `guard_out` |
| Retrieval | 5ms, concurrent with the guard |
| Cost per conversation | **~$0.004** measured |
| Widget | 8 KB, Shadow DOM, zero framework |

Fanning `guard_in` and `retrieve` out in parallel and skipping `guard_out` on
already-refused turns cut blocked-turn latency by 85%.

One trap worth naming: `cache_control` must sit **inside a content block**. Put it in a
message's `additional_kwargs` and `langchain-openai` drops it silently — the request still
succeeds, nothing caches, and the only symptom is a bill several times larger than it
should be.

---

## Stack

**Python 3.12** · FastAPI · **LangGraph** · LangChain · OpenRouter (per-tenant model choice
with provider fallback) · **PostgreSQL + pgvector** · **fastembed** (local ONNX,
Hugging Face `bge-small-en-v1.5`) · boto3 → S3-compatible storage · Docker Compose · Caddy

One datastore does vectors, queue and relational. **No Redis, no Celery, no hosted vector
database** — Postgres holds all three until there is evidence it cannot.

---

## Testing

**105 tests, no network, no cloud account.** Third-party integrations are exercised
against local fakes that speak the real wire protocol — including the failures that matter:

- Google returns **HTTP 200 with a per-calendar `errors` array** on a bad calendar id.
  Reading that as "no busy periods" silently double-books the entire day.
- Twilio signature validation is asserted against **Twilio's own published test vector**,
  not against my implementation.
- Retention is proven on real data: transcripts deleted, leads preserved, second run a
  no-op.

```bash
./run.sh                      # postgres + schema + demo tenant + server
pytest -q                     # 105 tests, offline
python -m app.evals.runner <tenant>   # the release gate
```

---

## Not built

Voice (telephony + STT + TTS is its own project). Named CRM connectors — the signed
HMAC webhook covers Zapier / n8n / Make / Power Automate today. Calendly.

## Before a client goes live

1. Set `ADMIN_API_KEY` — the default ships as a placeholder.
2. Set `allowed_origins` — empty means any site can use that bot on your credits.
3. `ENV=production` — tools are mocked in dev, so nothing reaches a real system.
4. WhatsApp needs Meta verification and US SMS needs 10DLC registration. Both take weeks.
