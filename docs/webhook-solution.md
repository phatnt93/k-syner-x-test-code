# Webhook callback — solution design

Status: **implemented (C-05, 2026-09-29)** on the CDMS side — `cdms.api.routes.webhooks`, `cdms.ingestion.webhook`,
`cdms.jobs.queue` / `cdms.jobs.runner`, worker. The emulator's callback sender is C-03b (done 2026-09-29, `cdms.emulator.callbacks`); still to do:
the spike run (C-10). Details that differ from the first draft: duplicates are detected on the canonical JSON of the body (not the
raw bytes); job statuses are `PENDING / RUNNING / DONE / DEAD` (a failed attempt goes back to `PENDING` with backoff);
`INV_CHANGED` is stored `IGNORED` (C-12 out of scope). Related: contract in [`api.md`](api.md#webhook-request), decisions
D5 / D7 in [`assumptions.md`](assumptions.md), exactly-once analysis in [`exactly-once.md`](exactly-once.md).

## 1. Goal

Vietful (the emulator acting as Callback Client) POSTs product / inventory events to CDMS. CDMS must:

- answer fast, also during a spike (requirements §10: 10 → 500 req/s);
- never lose an event it acknowledged (2xx), even if it crashes right after;
- store each real change exactly once, although senders **retry** (Vietful: every non-2xx is retried after 15 min),
  may deliver **duplicates**, **out of order**, and the same state may also arrive via polling or Excel.

## 2. Contract (Vietful-compatible)

```http
POST /api/v1/webhooks/vietful
Content-Type: application/json
x-vf-hmacsha256: Base64(HMAC-SHA256(raw body, WEBHOOK_SECRET))

{ "id": "5b6c…", "timestamp": 1790000000, "event": "PRODUCT_UPSERTED", "items": [ { ProductDto }, … ] }
```

| Event | Meaning | Processing |
|---|---|---|
| `PRODUCT_UPSERTED` | extension E2 (Vietful has no product event): 1–500 full `ProductDto` states | each item → observation → pipeline |
| `INV_CHANGED` | Vietful's inventory event, carries **deltas** (`changeQty`) | never apply deltas; re-fetch current inventory state for the listed keys, then pipeline (C-12) |
| anything else | Vietful sends all events to one subscriber | stored as `IGNORED`, `202` |

| Response | When |
|---|---|
| `202 {status:"accepted", inboxId}` | first delivery, stored durably |
| `200 {status:"duplicate", inboxId}` | same `id`, same body — already stored; sender stops retrying |
| `409 EVENT_ID_CONFLICT` | same `id`, different body — a sender bug; never overwrite |
| `401 INVALID_SIGNATURE` | missing / wrong HMAC (checked on the **raw** bytes, before JSON parsing, constant-time compare) |
| `422 INVALID_PAYLOAD` | not a valid envelope |
| `503 UNAVAILABLE` | database down — nothing stored, the sender retries |

## 3. Flow: accept durably, process asynchronously

```text
Vietful ──POST──► cdms-api                                   cdms-worker
                   1. verify HMAC (raw body)
                   2. BEGIN
                      INSERT inbox_event (source, event_id, body_sha256, payload, event_ts)
                        ON CONFLICT (source, event_id) DO NOTHING RETURNING id
                      conflict → compare body_sha256 → 200 duplicate / 409
                      INSERT job (kind='webhook_event', ref_id=inbox id)
                      COMMIT                                   3. claim job
                   ◄── 202 accepted                               SELECT … FOR UPDATE SKIP LOCKED
                                                                  4. BEGIN
                                                                     items → ProductObservation
                                                                       (observed_at = envelope timestamp,
                                                                        source=WEBHOOK, source_ref=event id)
                                                                     apply_observations(...)
                                                                     inbox_event.status = PROCESSED, outcome
                                                                     job.status = DONE
                                                                     COMMIT
```

- The API does **one small transaction** and returns; no pipeline work in the request. That keeps latency flat in a
  spike: the backlog grows in the `job` table and the worker drains it at its own pace.
- The worker marks inbox + job done **in the same transaction** as the product changes, so "processed" and "changed"
  can never disagree.

## 4. Why nothing is duplicated or lost

| Situation | Result |
|---|---|
| Sender retries a delivered event | `UNIQUE (source, event_id)` → `200 duplicate`, no second job |
| Same state from polling / Excel / another event id | pipeline fingerprint → `UNCHANGED` |
| Old event arrives after a newer one | `observed_at` (envelope timestamp) older than stored → `STALE` |
| Two workers, same product | row lock in `apply_observations` (sorted `SELECT … FOR UPDATE`) |
| API crashes after COMMIT, before answering | sender retries → `200 duplicate`; the job already exists |
| API crashes before COMMIT | nothing stored, no 2xx sent → sender retries |
| Worker crashes mid-job | transaction rolled back; job lease (`locked_until`) expires; another worker re-claims; re-run is idempotent |
| Job keeps failing (e.g. inventory down for `INV_CHANGED`) | retry with backoff up to `sync_config.job_max_attempts`, then `DEAD` (visible, re-queue by hand) |
| Database down | API answers `503` (sender retries); worker backs off; nothing buffered in memory |

## 5. Tables

- `inbox_event` — one row per distinct event: `source`, `event_id` (`UNIQUE (source, event_id)`), `event_type`,
  `body_sha256`, `payload jsonb`, `event_ts`, `status` (`PENDING` / `PROCESSED` / `IGNORED` / `FAILED` / `DEAD`),
  `outcome jsonb`, `received_at`, `processed_at`. Details: [`database.md`](database.md#inbox_event--webhook-idempotency).
- `job` — generic Postgres queue: `kind`, `ref_id` (`UNIQUE (kind, ref_id)`), `status`, `attempts`, `run_after`,
  `locked_by`, `locked_until`, `last_error`. Claim: `… FOR UPDATE SKIP LOCKED LIMIT n`
  ([`database.md`](database.md#job--postgres-backed-queue-d7)).

## 6. Spike load and the commit cost (ISSUES I-02)

Measured on the local setup: **~60 ms per commit**. With one inbox commit per request, 500 req/s needs ≥ 30 commits in
flight. Plan:

1. Size for concurrency: several uvicorn workers × async connection pool ≥ 30–50 connections (PostgreSQL
   `max_connections` 100). PostgreSQL group-commits concurrent transactions, so throughput scales with concurrency
   although each commit still waits ~60 ms (p95 target < 200 ms stays reachable).
2. Keep the request transaction minimal: 2 inserts, no reads of product data, no pipeline.
3. The worker claims **many jobs per transaction** (e.g. 100) and applies all their items with one
   `apply_observations` call — the batch fast path (I-01) and one commit per 100 events instead of per event.
4. Measure with k6 (`ramping-arrival-rate` 10 → 500 → 10 req/s; mix of new states, redeliveries, cross-mechanism
   duplicates), then verify invariants (versions contiguous, fingerprint chain, 0 duplicates, every acknowledged
   event `PROCESSED`).

Rejected: `synchronous_commit = off` (would lose acknowledged events on a crash), in-memory queues / batching before
commit (an acknowledged event must already be on disk).

## 7. Implementation outline (if built later)

| Piece | Where | Size |
|---|---|---|
| `WEBHOOK_SECRET` setting | `cdms.config` | small |
| `inbox_event`, `job` models + migration | `cdms.db.models` | small |
| HMAC check + envelope schema + route | `cdms.api.routes.webhooks` | small |
| Job queue (enqueue, claim with lease, complete, retry / DEAD) | `cdms.jobs.queue` | medium |
| Webhook job handler (items → observations → pipeline, mark processed) | `cdms.ingestion.webhook` | small |
| Worker: job runner loop next to the poll scheduler | `cdms.worker` | small |
| Emulator callback sender (C-03b): sign, deliver, retry, duplicate rate, subscriber endpoint | `cdms.emulator` | medium |
| Tests: signature, duplicate / conflict, crash after inbox commit, stale event, spike + invariants | `tests/` | medium |

Estimated effort: 1.5–2 days including tests and the load run.
