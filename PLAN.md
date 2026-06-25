# Network Intelligence Platform — Implementation Plan

Locked after `/plan-eng-review` (2026-06-25). The original build brief stands; this
file records the review decisions that amend or sharpen it. Where this file and the
brief disagree, **this file wins**.

## Locked decisions from review

| # | Decision | Resolution |
|---|----------|-----------|
| A1 | Tenant isolation | **Postgres RLS** (`tenant_id` policy per table, `SET LOCAL app.current_tenant` per request/worker) + thin helper for ergonomics. `tenant_id` leading column on every index. Use `SET LOCAL`, never `SET` (pooled-connection leak). |
| A2 | Re-sync idempotency | `UNIQUE(tenant_id, source_type, source_record_id)` on `interactions`; content-hash key on `embedding_chunks`. `INSERT ... ON CONFLICT`. Skip Voyage call when `chunk_text` hash unchanged. |
| A3 | Embedding model | `VOYAGE_MODEL` + `VOYAGE_DIM` in settings (default `voyage-3.5` @ 1024). Pass `output_dimension`/`input_type` explicitly. Assert `VOYAGE_DIM == DB column dim` at startup; migration reads dim from config. |
| C1 | Connector seam | Share only the **ingest tail** `ingest_record(normalized) -> resolve/upsert/embed`. Each source owns its own sync/parse loop (cursor, deletions, refresh for OAuth; parse for CSV). No shared `sync()` base. |
| C2 | Error isolation | Per-record `try/except`; failures -> `sync_errors` dead-letter (record id + reason, **never** raw body/tokens). Voyage batch retry w/ exponential backoff. Cursor advances only past handled records. |
| P1 | Intent classify | Heuristic (regex/keyword) classifier first; Claude only on ambiguity; **fallback to hybrid** on classifier failure (never hard-fail a query). |
| Sync | Source freshness | **Poll** with `historyId` (Gmail) + `syncToken` (Calendar/Contacts). Push notifications (Pub/Sub, webhook channels) **deferred**. |
| Merge | Persistence | `person_aliases(tenant_id, source_type, source_record_id, person_id, decided_by, created_at)`. Resolution checks aliases **first**, before exact/fuzzy. Un-merge = delete alias row. |
| Gmail | Scope + privacy | `gmail.metadata` only (headers). Embed `subject + participant names + company + LinkedIn headlines + calendar titles`. **No email body text leaves the machine** (no snippet to Voyage). |
| Del | Deletions | Process delete records -> `interaction.deleted_at` + drop its embedding. On `historyId` 404 / `syncToken` 410 -> full resync reconciles absent records as deletes. Retrieval filters `deleted_at IS NULL`. |
| Att | Participant policy | Filter non-humans (`no-reply@`, bounces, `calendar-*`, own aliases, lists). Email-only participants = **provisional people** resolved by exact email only. Fuzzy `name+company` fires **only when company present**. |
| Sec | Key mgmt | Fernet key from gitignored mounted key file / OS keychain, **separate** from `DATABASE_URL`. Document rotation (decrypt-old / re-encrypt-new). |
| Seq | Build order | **Thin end-to-end slice first** (Contacts -> person -> chunk -> cited `/query`), then harden (RLS enforce, error isolation, deletions, eval, remaining sources). |
| Eval | RAG tests | Eval harness: ~15-20 labeled questions (structured/semantic/hybrid/zero-result). Assert intent accuracy + recall@k + citation grounding. CI gate on hallucinated citation. |

## Resolution order (consult in sequence)

```
resolve(normalized_record):
    1. alias hit?            -> return aliased person_id      (manual + confident-auto merges persist)
    2. exact normalized email match? -> bind to that person
    3. non-human / list?     -> drop
    4. has email, no company -> PROVISIONAL person (email-keyed only; NO name fuzzy)
    5. has company           -> fuzzy(name+company); >=0.85 merge, else NEW + manual-review queue
```

## Build sequence

```
Slice 0 (prove the loop):  Contacts -> 1 person -> 1 chunk -> embed -> /query w/ citation
Then harden:               RLS enforce + per-record error isolation
Then sources:              Gmail + Calendar polling (historyId/syncToken) + deletions
Then merge:                LinkedIn CSV + manual merge + person_aliases
Then quality:              RAG eval harness + pgvector HNSW index tuning
```

See the eng-review tasks JSONL in `~/.gstack/projects/jagong-cmu-personal-crm/` for the
19 build-actionable tasks (T1-T19) derived from these decisions.
