# Exactly-once changes

Status: **design** (2026-09-28). Goal: one logical change is stored **exactly once**, whatever the mechanism, the number
of deliveries, the concurrency, or the failure point. Tables: [`database.md`](database.md); decisions: D2–D5, D7 in
[`assumptions.md`](assumptions.md).

## Principle

Delivery is **at-least-once** (senders and the worker retry); storage is made **idempotent**, so the effect is exactly
once. Three layers, each enforced by the database:

| Layer | Stops | Mechanism |
|---|---|---|
| Transport idempotency | the same webhook / file delivered again | `UNIQUE (source, event_id)` on `inbox_event`; `UNIQUE file_sha256` on `excel_upload`; `UNIQUE (kind, ref_id)` on `job` |
| State idempotency | the same state arriving from any mechanism | fingerprint compare under a row lock ⇒ UNCHANGED |
| Change-log integrity | two writers racing on one entity | row lock + `UNIQUE (entity_type, entity_key, version)` + `CHECK prev ≠ new` |

## The pipeline transaction

```sql
BEGIN;
-- 1. insert-or-lock the state row
INSERT INTO entity_state (entity_type, entity_key, version, fingerprint, data, observed_at, …)
VALUES (:t, :k, 1, :fp, :data, :obs, …)
ON CONFLICT (entity_type, entity_key) DO NOTHING
RETURNING version;                 -- a row back ⇒ CREATED: insert change_log v1, COMMIT

SELECT version, fingerprint, observed_at, data
FROM entity_state WHERE entity_type = :t AND entity_key = :k
FOR UPDATE;                        -- concurrent writers queue here

-- 2. decide
--   :obs < observed_at        → STALE      (no write)
--   :fp  = fingerprint        → UNCHANGED  (advance observed_at if newer, D4)
--   otherwise                 → UPDATED
UPDATE entity_state SET version = version + 1, fingerprint = :fp, data = :data, observed_at = :obs, updated_at = now()
WHERE entity_type = :t AND entity_key = :k;
INSERT INTO change_log (…, version, prev_fingerprint, fingerprint, diff, source, source_ref, …)
VALUES (…, :old_version + 1, :old_fp, :fp, :diff, …);

-- 3. mark the transport record done in the SAME transaction
UPDATE inbox_event SET status = 'PROCESSED', processed_at = now(), outcome = :o WHERE id = :inbox_id;
UPDATE job SET status = 'DONE' WHERE id = :job_id;
COMMIT;
```

Isolation level: `READ COMMITTED` is enough because the decision is made on a row locked with `FOR UPDATE`.
Batches (a poll page, an Excel chunk) lock rows in **sorted key order** to avoid deadlocks (one
`SELECT … ORDER BY partner_sku FOR UPDATE` for the whole batch, then UNCHANGED / STALE are decided on the locked rows); a deadlock / serialization
error rolls back the batch and the job is retried.

## Race example (requirements §8.2)

```text
Poll  (A): INSERT … ON CONFLICT DO NOTHING → inserts v1 → COMMIT
Webhook(B): INSERT … ON CONFLICT DO NOTHING → conflict (waits for A's commit), then
            SELECT … FOR UPDATE → sees fingerprint = own fingerprint → UNCHANGED
Result: change_log has one row (v1).
```

Without the lock, the `UNIQUE (…, version)` constraint still rejects the second `v1`; the loser retries and becomes
UNCHANGED.

## Failure scenarios

| Where it fails | What happens | Why no duplicate / no loss |
|---|---|---|
| Webhook: crash **before** the inbox commit | Sender gets no 2xx and retries | Nothing was stored |
| Webhook: inbox committed, crash **before** the response | Sender retries the same `id` | `ON CONFLICT` ⇒ `200 duplicate`; the job is already queued |
| Worker: crash **inside** the pipeline transaction | Transaction rolled back; job lease expires; another worker re-claims | Nothing partial was committed |
| Worker: pipeline committed, crash before anything else | Nothing else to do — state, change and "done" markers are one commit | — |
| Poll run: crash mid-scan | Run marked `ABORTED` on restart; next run rescans | Already-applied pages are UNCHANGED on rescan |
| Excel: crash mid-file | Job re-claimed, resumes from `processed_rows` | A re-run chunk is UNCHANGED; row errors are upserted |
| DB: connection lost, commit outcome unknown | Caller treats it as failure and retries | Retry is idempotent (layers 1–3) |
| DB down | API `503`, worker backs off | Nothing is buffered in memory, so nothing is lost when a process dies |
| Emulator / Vietful down | Poll run `FAILED`; `INV_CHANGED` jobs retried with backoff, then `DEAD` (visible in `/ui`) | Accepted events stay in the inbox until processed |
| Host crash | Postgres durable on disk; services restart; worker resumes | Committed ⇒ durable; uncommitted ⇒ retried |

## Non-goals / known limits

- Exactly-once is defined on **states**, not on deltas. Vietful `INV_CHANGED` deltas are never applied; CDMS re-fetches
  the state (D5).
- An entity that goes A → B → A **within one polling interval** with no webhook shows no change (polling only sees
  snapshots). Webhooks close this gap.
- Ordering relies on source timestamps (second precision for webhooks); two different states with the same
  `observed_at` are applied in arrival order.
