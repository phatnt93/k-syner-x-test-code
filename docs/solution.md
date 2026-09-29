# Solution overview

How CDMS meets the test requirements, in one read. Details and rationale: [`architecture.md`](architecture.md),
[`exactly-once.md`](exactly-once.md), [`database.md`](database.md), decisions D-* in [`assumptions.md`](assumptions.md).
Written 2026-09-28; tables are named after the implemented product model (`products`, `product_changes`).

> Scope (D13, 2026-09-29): polling and webhook are implemented; the **Excel** parts below (sections 7, 9) are a
> design only — see [`excel-solution.md`](excel-solution.md).

## 1. The problem

The same Product data reaches CDMS through three mechanisms, each of which may deliver it **more than once**, **in any
order**, **at the same time**:

```text
Scheduled polling (CDMS asks Vietful every N seconds) ─┐
Webhook callback  (Vietful pushes an event)           ─┼──► CDMS ──► PostgreSQL
Excel upload      (a client uploads a file)           ─┘
```

CDMS must store **only real changes, each exactly once**, even when:

- the same data arrives twice (webhook retries, the same file uploaded again);
- the same data arrives via different mechanisms (poll and webhook both see price 120);
- two requests for the same product are processed simultaneously;
- a process or the database crashes midway.

**Core idea:** delivery cannot be made exactly-once (networks retry, senders resend), so storage is made
**idempotent** — processing the same input again has no additional effect.
At-least-once delivery + idempotent storage = **exactly-once change**.

## 2. Two tables

| Table | Role | Rows |
|---|---|---|
| `products` | **Current state** — the latest known version of each product | one per `partner_sku` |
| `product_changes` | **Change Database** — append-only history of every real change | one per detected change |

Example: a product whose price changes twice.

```text
products:         P-1001 | price=150 | version=3 | fingerprint=C

product_changes:  P-1001 | v1 | CREATED | prev=∅ → A | data {price:100}
                  P-1001 | v2 | UPDATED | prev=A → B | diff {price:[100,120]}
                  P-1001 | v3 | UPDATED | prev=B → C | diff {price:[120,150]}
```

## 3. Fingerprint — deciding "changed or not"

Every incoming product is first **normalized** into one canonical shape, whatever its source:

- strings trimmed, `""` → `null`;
- booleans parsed (`true` / `1` / `yes`);
- `units` and `categories` sorted, so a different order is not a change;
- transport metadata removed (event id, timestamp, source).

Then `fingerprint = SHA-256(canonical JSON)`. Comparing two 32-byte hashes replaces comparing every field.

| Situation | Outcome |
|---|---|
| `partner_sku` not in `products` | **CREATED** — insert state row + change v1 |
| Fingerprint differs from the stored one | **UPDATED** — version + 1, insert change row |
| Same fingerprint | **UNCHANGED** — nothing written |
| Data older than the stored state | **STALE** — ignored (section 8) |

This also removes duplicates **across** mechanisms: if polling stored "price 120" first, the webhook carrying the same
data has the same fingerprint and becomes UNCHANGED.

## 4. One pipeline for all three mechanisms

The mechanisms only differ in how they **receive** data; afterwards everything goes through the same function:

```text
Polling page  ─┐
Webhook event ─┼─► normalize ─► fingerprint ─► apply_observation() ─► DB
Excel row     ─┘
```

One place to get right, one place to test, no risk of three slightly different duplicate checks.

## 5. `apply_observation` — the exactly-once transaction

Per product, in **one database transaction**:

```text
BEGIN
 1. INSERT INTO products (...) ON CONFLICT (partner_sku) DO NOTHING
      → inserted?  CREATED: insert product_changes v1, COMMIT
 2. SELECT ... FROM products WHERE partner_sku = ? FOR UPDATE
      → row lock: other transactions for the SAME product wait here
 3. compare:
      incoming observed_at older than stored → STALE      → COMMIT (no write)
      same fingerprint                       → UNCHANGED  → COMMIT (no write)
      otherwise:
        UPDATE products SET version = version + 1, fingerprint = new, data..., observed_at = ...
        INSERT product_changes (version = old + 1, prev_fingerprint = old, fingerprint = new, diff)
COMMIT
```

- **Atomic:** state update and change row commit together or not at all — a crash leaves nothing half-written.
- **Serialized per product:** `FOR UPDATE` makes concurrent writers of the same product take turns; different products
  never block each other.

## 6. Race condition (requirements §8.2)

Polling and a webhook bring P-1001 with price 120 at the same instant.

Naive check-then-insert:

```text
A: check → not changed     B: check → not changed
A: insert change           B: insert change          → DUPLICATE ❌
```

This design:

```text
A: SELECT ... FOR UPDATE   (gets the lock)
B: SELECT ... FOR UPDATE   (waits)
A: fingerprint differs → version 2, insert change → COMMIT (lock released)
B: reads version 2, fingerprint equals its own → UNCHANGED   ✅ one change
```

**Safety net in the database:** `product_changes` has `UNIQUE (partner_sku, version)` and
`CHECK (prev_fingerprint IS DISTINCT FROM fingerprint)`. Even with a code bug, PostgreSQL rejects a second v2 or a
"change" that changes nothing. The **database is the final authority**, not application memory — API and worker are
separate processes that share no memory.

## 7. Transport duplicates — idempotency keys

Some duplicates are stopped before the pipeline runs:

| Mechanism | Idempotency key | On duplicate |
|---|---|---|
| Webhook | envelope `id` → `UNIQUE (source, event_id)` in `inbox_event` | `200 duplicate`, not reprocessed |
| Excel | file SHA-256 → `UNIQUE file_sha256` in `excel_upload` | returns the existing upload |
| Polling | not needed | state-based: unchanged product ⇒ UNCHANGED |

This layer is an optimization; correctness still comes from section 5.

## 8. Ordering — old data arriving late (STALE)

The case the fingerprint alone does not catch:

```text
10:00  webhook A: price 100 → stored
10:01  webhook B: price 120 → stored (v2)
10:02  webhook A redelivered (retry)
```

With only the fingerprint, "100 ≠ 120" would be recorded as a new change and wrongly revert the product to old data.
So each state keeps `observed_at` — when the source saw that state:

- webhook → the event `timestamp`;
- polling → the time the page request **started** (never over-claims freshness);
- Excel → the `observedAt` column, else the upload time.

Input older than the stored `observed_at` is **STALE** and ignored — this is the "no old data" requirement.
An UNCHANGED input with a newer `observed_at` still advances the stored `observed_at` (no change row), so a poll at
10:05 that confirms price 120 makes a late webhook observed at 10:03 STALE as well.

## 9. Webhook and Excel — accept first, process later

```text
POST /webhooks/vietful ──► verify HMAC ──► INSERT inbox_event + job (1 transaction) ──► 202 Accepted
                                                        │
                                   worker ◄─────────────┘  claims the job (FOR UPDATE SKIP LOCKED),
                                                           runs apply_observation, marks job DONE
                                                           in the SAME transaction
```

- **Spike load:** each request does one small insert and returns quickly; the worker drains the backlog at its own pace.
- **Crash safety:** once the API answered `202`, the event is on disk. If the worker dies, the job lease expires and
  another worker re-claims it; re-running is harmless (section 5).
- **Crash after commit but before the response:** the sender retries → unique event id → `200 duplicate` → no second
  change.

The queue is a PostgreSQL `job` table (`SELECT ... FOR UPDATE SKIP LOCKED`) — no Redis / Kafka needed. Excel uploads
use the same queue and are processed in chunks with a `processed_rows` checkpoint.

## 10. Failure handling (requirements §9)

| Failure | Behavior | Why nothing is lost or duplicated |
|---|---|---|
| **CDMS crash mid-processing** | transaction rolled back; job re-run after its lease expires | nothing partial committed; retry is idempotent |
| **Vietful / emulator down** | timeout, 3 retries with backoff, poll run `FAILED`; next tick retries | accepted webhooks wait in the inbox until processed |
| **PostgreSQL down** | API `503`, sender retries; worker waits and reconnects | nothing buffered in memory ⇒ a dying process loses nothing |
| **Commit outcome unknown** (connection lost) | treated as a failure and retried | the retry is UNCHANGED / duplicate |
| **Host crash** | Postgres data durable on disk; on restart the worker resumes pending jobs and aborts stale poll runs | committed ⇒ durable; not committed ⇒ retried |

## 11. Spike load test (requirements §10)

k6 scenario: 10 req/s → **500 req/s for 60 s** → 10 req/s; mix of new states, exact redeliveries and cross-mechanism
duplicates, with polling and Excel uploads running concurrently. Measured:

- throughput, p95 latency, error rate;
- time for the job backlog to drain after the spike;
- most important — **SQL invariants** after the run (`scripts/verify_invariants.sql`): versions per product are
  contiguous 1..n, every change chains to the previous fingerprint, `products.version` equals the latest change,
  zero duplicates.

Details: [`testing.md`](testing.md).

## 12. Known limitations

- Products are matched on `partner_sku` only (confirmed decision, D1). If Vietful renames a partnerSKU
  (`update-partner-sku`), CDMS sees a new product under the new code and the history is split; if two products swap
  codes (`swap-partner-sku`), both are recorded as full-state changes.
- A product going A → B → A **within one polling interval** with no webhook is invisible to polling (it only sees
  snapshots).
- Ordering relies on source timestamps (second precision for webhooks); a poll that started just before a newer
  webhook may be judged stale and is corrected by the next poll.
- Vietful's real `INV_CHANGED` webhook carries quantity **deltas** (`changeQty`), not states. Deltas are never applied:
  the event is treated as a signal and CDMS re-fetches the current state, so a redelivered delta cannot double-count.

## 13. Requirement → mechanism

| Requirement | Mechanism |
|---|---|
| Store only new / changed data | fingerprint compare → CREATED / UPDATED / UNCHANGED |
| No old data | `observed_at` → STALE |
| Exactly-once update | one transaction + row lock + `UNIQUE (partner_sku, version)` + idempotency keys |
| Concurrent mechanisms | `FOR UPDATE` per product; database as the final authority |
| Failure recovery | atomic transactions, durable inbox / job tables, idempotent retries |
| Spike load | accept-then-process, PostgreSQL queue, invariant verification |

## 14. `product_changes` shape (next model)

| Column | Purpose |
|---|---|
| `id` | global order of changes |
| `partner_sku`, `version`, `change_type` | which product, which version, `CREATED` / `UPDATED` |
| `prev_fingerprint` → `fingerprint` | the transition this change represents |
| `data`, `diff` | full new state; `{field: [old, new]}` |
| `source`, `source_ref` | `POLLING` / `WEBHOOK` / `EXCEL` and the poll run, event id or file row that produced it |
| `observed_at`, `recorded_at` | source observation time; time CDMS stored it |

Constraints: `UNIQUE (partner_sku, version)`, `CHECK (prev_fingerprint IS DISTINCT FROM fingerprint)`,
`CHECK ((version = 1) = (prev_fingerprint IS NULL))`.
