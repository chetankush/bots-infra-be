# Project status

Last updated: 2026-09-08

Snapshot of where this stands, what is proven, and what to do next. Written so the work
can be picked up cold.

---

## Short answer

**Website chat is genuinely usable.** A client's site can be crawled, a widget embedded,
and the agent will answer from their content with guardrails, capture leads, escalate to a
human, and stay inside a cost ceiling.

**It is not yet safely deployable**, for three reasons in order of severity:

1. **Schema upgrades do not work.** A *fresh* database builds correctly from
   `scripts/seed.py` (`Base.metadata.create_all`), but an *existing* one cannot be
   migrated — the Alembic history is empty and several tables were applied to the dev
   database by hand. See "Known problems" below. This is the one thing that will bite in
   production and it should be fixed before anything else.
2. **Three config blockers** (below) — no code needed, but a live client without them is
   an open, unbilled endpoint.
3. **Integration paths are proven but not repeatable.** They were verified end to end
   against local fakes, by hand, once. They are not pytest tests, so nothing stops a
   future change from breaking them silently.

---

## Built and verified

| Feature | State | How it was proven |
|---|---|---|
| Multi-tenant config chain | Done | pack → tenant → location → channel merge, unit tested |
| Site crawl → chunk → embed → retrieve | Done | Crawled a real site; retrieval scores 0.65–0.70 |
| LangGraph agent + tool loop | Done | Live turns; `agent ⇄ tools` cycle observed |
| Prohibition guards (`guard_in` / `guard_out`) | Done | Financing question blocked in 1.1s, retrieval skipped |
| Lead capture + escalation | Done | Rows written; signed webhook delivered and HMAC-verified |
| Rate limiting (session / IP / tenant) | Done | Trips at burst 15 |
| Per-tenant token budgets | Done | Unit tested incl. period rollover |
| Tenant isolation (`TenantScope`) | Done | Cross-tenant read raises; forged `tenant_id` overwritten |
| Credential storage (Fernet) | Done | Round-trip via API; ciphertext opaque; other tenant sees `None` |
| Retention purge / erasure / export | Done | 3 transcripts + 6 messages deleted, **4 leads and 4 appointments survived**; idempotent; export leaks no secrets |
| Consent capture (EU/UAE) | Done | EU refused at 451 with nothing persisted; timestamp + version recorded |
| Google Calendar | Done | Local fake: degrade, real booking, busy exclusion, timeout degrade |
| Email (Resend) | Done | Queued not inline; 4xx permanent vs 5xx retry; dev sink logs metadata only |
| Reminders | Done | Sent once; replan deduped; second sweep sent nothing; cancelled with appointment |
| WhatsApp / SMS (Twilio) | Done | Forged requests: bad sig 403, valid → reply, **exact replay deduped** |
| Bilingual EN/ES | Done | 10/10 detection with no model call; live Spanish turn |
| Backups → S3-compatible | Done | Real dump → gzip → MinIO → restore verified → prune |
| Ops console + charts | Done | Onboard, crawl, chat with graph trace, inbox, leads, usage |
| Eval harness | Done, **not run recently** | Last full run predates the guard-model swap |

**105 tests, all offline.** Lint and format clean.

---

## Known problems

### 1. Schema is not reproducible from migrations — *fix first*

`scripts/seed.py` builds the schema with `Base.metadata.create_all`, so a **fresh**
database is correct. But during development these were applied to the dev database with
raw SQL and never captured as migrations:

- `leads.conversation_id` / `appointments.conversation_id` → nullable + `ON DELETE SET NULL`
- `conversations.consent_at`, `conversations.consent_version`
- `reminders` table
- `channel_routes` table
- `inbound_messages` table

`alembic/versions/0001_detach_business_records.py` covers only the first item, and it
contains an `upgrade_consent()` function that Alembic never calls — dead code.

**To fix:** drop the hand-written 0001, autogenerate a real baseline against the models,
verify it produces an identical schema on an empty database, then `alembic stamp head`
on any existing environment.

```bash
alembic revision --autogenerate -m "baseline"
# then diff a create_all database against a migrated one before trusting it
```

### 2. Config blockers before any client goes live

1. `ADMIN_API_KEY` is still `change-me-in-production`.
2. Both seeded tenants have `allowed_origins: []` — **any website can use that bot on
   your credits**.
3. `ENV=dev`, so tools are mocked and nothing reaches a real calendar, inbox or phone.

### 3. Coverage is 47%

The number is soft rather than wrong. Integration paths *are* verified — calendar
degradation, WhatsApp replay dedupe, retention preserving leads — but by one-off scripts,
not pytest. They are not repeatable and do not gate a build. Converting them would take
coverage into the 70s and, more usefully, make a regression fail CI instead of failing a
client.

### 4. Not exercised as a long-running process

`app/rag/worker.py` has been called function-by-function (purge, reminder sweep, webhook
delivery) but never run as its own container for a sustained period. Its scheduling loop,
backoff behaviour and memory profile over hours are unproven.

### 5. Eval gate is stale

The last full run predates the guard-model swap from `qwen3.7-flash` to
`gemini-2.5-flash-lite`. Safety metrics have not been re-measured since. A run costs about
$0.13 with the Opus judge.

---

## Not built

- **Voice.** Telephony + STT + TTS is a separate project. The channel profile exists.
- **Named CRM connectors.** HubSpot, Salesforce, Housecall Pro, Jobber. The signed HMAC
  webhook covers Zapier / n8n / Make / Power Automate today.
- **Calendly.**
- **Stripe billing / self-serve signup** — these live in the Node control plane, not here.

---

## Suggested order when picking this up

1. **Alembic baseline** — the only item that can cause data loss in production.
2. **Turn the one-off E2E scripts into pytest tests** — makes every guarantee above
   repeatable, and lifts coverage as a side effect.
3. **Run the eval gate** to re-baseline safety after the guard-model change.
4. **Run the worker as a container** for a day and watch memory and job outcomes.
5. **Harden one real tenant** (admin key, origins, `ENV=production`) and pilot it on a
   friendly client's site.
6. Then: WhatsApp Meta verification (weeks of lead time — start early), reminders in
   production, named CRM connectors as clients ask.

---

## Costs

- **Infrastructure: $0.** Oracle Always Free box, Supabase free tier, Cloudflare,
  local ONNX embeddings, MinIO or Oracle object storage.
- **Inference: ~$0.004 per conversation measured**, so roughly $20/month at 1,000
  conversations. This is the only real cost.
- OpenRouter key is capped at $2, which the provider enforces server-side.

---

## Local development

```bash
./run.sh                    # postgres + schema + demo tenant + server
pytest -q                   # 105 tests, offline
```

Console at `http://localhost:8000/console`, widget demo at `/demo`.

Secrets live in `.env`, which is gitignored and has never been committed.
`.env.example` documents all 22 settings the code reads.
