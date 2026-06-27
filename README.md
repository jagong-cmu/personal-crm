# Network Intelligence Platform

Personal "network intelligence" app: unify Gmail / Calendar / Contacts / LinkedIn
into a queryable knowledge base with a RAG query layer.

See `PLAN.md` for the locked architecture decisions.

## What's built

```
Contacts / Gmail (metadata) / Calendar / LinkedIn CSV
        │  (each connector owns its fetch/parse loop)
        ▼
   shared ingest tail ── resolve ─▶ people ── embed (Voyage) ─▶ pgvector
        │   (alias-first → exact-email → fuzzy(name+company) → provisional)
        ▼
   /query ── heuristic intent router (company | recency | semantic | hybrid) ─▶ Claude answer + citations
```

- **Sources:** Google Contacts, Gmail (`gmail.metadata` — headers only, never bodies),
  Google Calendar, and LinkedIn `Connections.csv` import. Incremental sync via persisted
  `historyId`/`syncToken`; deletions reconciled (soft-delete + drop embedding).
- **Tenant isolation:** Postgres Row-Level Security, **enforced** via a non-superuser app
  role (see step 5b) — the tenant is re-applied on every transaction.
- **Entity resolution:** alias-first → exact-email → non-human filter → provisional →
  fuzzy(name+company ≥ 0.85). Manual merge / un-merge with sticky aliases that survive
  re-sync.
- **RAG:** heuristic-first intent classifier (Claude only on ambiguity, hybrid fallback),
  structured + semantic retrieval, grounded cited synthesis.
- **Quality:** per-record error isolation (`sync_errors` dead-letter), Voyage backoff,
  HNSW index, re-embed backfill, and a RAG eval harness (intent accuracy / recall@k /
  citation-grounding gates).

See `PLAN.md` for the locked decisions and `tests/` for the proof of each.

---

## What you need to do

### 1. Prereqs
- Docker (for Postgres+pgvector)
- Python 3.10+

### 2. Get API keys
- **Anthropic:** https://console.anthropic.com → API key → `ANTHROPIC_API_KEY`
- **Voyage AI:** https://dashboard.voyageai.com → API key → `VOYAGE_API_KEY`

### 3. Get Google OAuth credentials
1. https://console.cloud.google.com → create/select a project.
2. **APIs & Services → Library →** enable **People API**, **Gmail API**, **Calendar API**.
3. **OAuth consent screen:** User type *External*; publishing status stays **Testing**;
   add your own Google account under **Test users**. (Testing mode = refresh token
   expires every 7 days, so you'll re-run step 7 weekly. Fine for v1.)
4. Add scopes (all read-only): `.../auth/contacts.readonly`,
   `.../auth/gmail.metadata`, `.../auth/calendar.readonly`.
   (Gmail metadata = headers only; message bodies are never requested.)
5. **Credentials → Create credentials → OAuth client ID → Web application.**
   Authorized redirect URI: `http://localhost:8000/auth/google/callback`.
6. Copy the client ID + secret into `.env`.

### 4. Configure
```bash
cp .env.example .env
mkdir -p secrets
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" > secrets/token.key
python -c "import uuid; print(uuid.uuid4())"   # paste into DEFAULT_TENANT_ID
# then edit .env: paste the API keys + Google client id/secret
```
The Fernet key lives in `secrets/token.key` (gitignored), separate from `DATABASE_URL` — decision T12.

### 5. Install + start DB
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
docker compose up -d            # Postgres+pgvector on :5432
alembic upgrade head            # create the schema
```

### 5b. Create the non-superuser app role (REQUIRED — makes RLS real)
The `docker-compose` `crm` user is a **superuser**, and superusers BYPASS Row-Level
Security. So the app must connect as a non-superuser role, or tenant isolation is a no-op:
```bash
docker exec -i personal-crm-db-1 psql -U crm -d crm -f - < scripts/setup_app_role.sql
```
Then point `DATABASE_URL` at that role (note `crm_app`, not `crm`):
```
DATABASE_URL=postgresql+psycopg://crm_app:crm_app@localhost:5432/crm
```
Run migrations as the `crm` owner (above); run the **app/workers** as `crm_app`.

### 6. Run the app
```bash
uvicorn app.main:app --reload
```
(If startup complains about an embedding-dim mismatch, your `VOYAGE_DIM` and the DB
column disagree — that assertion is decision A3 doing its job.)

### 7. Connect Google + sync + ask
```bash
open http://localhost:8000/auth/google/start         # consent (Contacts + Gmail + Calendar)
curl -X POST localhost:8000/sync/contacts            # pull contacts
curl -X POST localhost:8000/sync/gmail               # pull Gmail headers (incremental)
curl -X POST localhost:8000/sync/calendar            # pull Calendar events (incremental)
curl -X POST localhost:8000/sync/linkedin -F file=@Connections.csv   # import LinkedIn export
curl -X POST localhost:8000/query \
  -H 'content-type: application/json' \
  -d '{"question": "who do I know at <a company in your network>?"}'
```
**Run any sync twice — people count must not double** (the idempotency acceptance check).

### 8. Keep it fresh (polling workers) + maintenance
```bash
python -m app.workers.poll --once     # sync all sources once (cron-friendly)
python -m app.workers.poll            # run the interval loop
python -m app.workers.backfill        # re-embed everything after a model/dim change
```

## API
- `GET  /auth/google/start` · `GET /auth/google/callback` — OAuth
- `POST /sync/{source}` — `contacts` | `gmail` | `calendar`
- `POST /sync/linkedin` — multipart upload of `Connections.csv`
- `POST /query` — `{ "question" }` → `{ answer, intent, citations[] }`
- `GET  /people` · `GET /people/{id}` — list / provenance (sources + recent interactions)
- `POST /people/merge` `{ winner_id, loser_id }` · `POST /people/unmerge` `{ loser_id }`
- `GET  /healthz`

## Tests + eval
```bash
DATABASE_URL=postgresql+psycopg://crm_app:crm_app@localhost:5432/crm pytest -q
```
Pure tests (parsers, intent, resolution, crypto) run offline; DB-backed tests (RLS,
merge, ingest isolation, eval recall/grounding) need Postgres and **skip** without it.
The eval harness (`tests/eval/`) uses a deterministic offline embedder — no Voyage/Anthropic
calls — and gates on intent accuracy ≥ 0.9, recall@k ≥ 0.8, and zero hallucinated citations.

## Encryption key rotation (T12)
Tokens are Fernet-encrypted with the key in `secrets/token.key` (gitignored, separate from
`DATABASE_URL`). To rotate: generate a new key, decrypt existing `oauth_credentials` with the
old key and re-encrypt with the new one (or simplest for v1: rotate the key file and re-run
`/auth/google/start` to re-mint tokens under the new key).
