# Network Intelligence Platform

Personal "network intelligence" app: unify Gmail / Calendar / Contacts / LinkedIn
into a queryable knowledge base with a RAG query layer.

See `PLAN.md` for the locked architecture decisions.

## Slice 0 (what's built now)

The thinnest end-to-end loop, to prove the core works before hardening:

```
Google Contacts → people → embed (Voyage) → pgvector → /query → Claude answer + citations
```

Built: scaffolding, Docker Postgres+pgvector, Alembic schema, Google OAuth (Contacts),
Contacts sync, embedding pipeline, semantic `/query`. **Not yet:** RLS enforcement,
Gmail/Calendar/LinkedIn, fuzzy entity resolution, manual merge, eval harness (later slices).

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
2. **APIs & Services → Library →** enable **People API**.
3. **OAuth consent screen:** User type *External*; publishing status stays **Testing**;
   add your own Google account under **Test users**. (Testing mode = refresh token
   expires every 7 days, so you'll re-run step 7 weekly. Fine for v1.)
4. Add scope `.../auth/contacts.readonly`.
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

### 6. Run the app
```bash
uvicorn app.main:app --reload
```
(If startup complains about an embedding-dim mismatch, your `VOYAGE_DIM` and the DB
column disagree — that assertion is decision A3 doing its job.)

### 7. Connect Google + sync + ask
```bash
open http://localhost:8000/auth/google/start     # consent in the browser
curl -X POST localhost:8000/sync/contacts        # pulls contacts, embeds them
curl -X POST localhost:8000/query \
  -H 'content-type: application/json' \
  -d '{"question": "who do I know at <a company in your contacts>?"}'
```

You should get a natural-language answer plus `citations[]` that trace back to real
contacts. That's Slice 0 working. **Run the sync twice — people count must not double**
(the acceptance check).

## API (Slice 0)
- `GET  /auth/google/start` — begin OAuth
- `GET  /auth/google/callback` — OAuth return (Google calls this)
- `POST /sync/{source}` — `contacts` works; others return 501
- `POST /query` — `{ "question": "..." }` → `{ answer, citations[] }`
- `GET  /healthz`
