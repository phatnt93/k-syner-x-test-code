# Assumptions and design decisions

The test leaves many points open (requirements §19). This file records what CDMS assumes (A-*) and decides (D-*).
Status: **decided 2026-09-28**, before implementation. Change a decision here first, then in code.

## Assumptions

| ID | Assumption |
|---|---|
| A1 | `partnerSKU` identifies a product from the partner's (CDMS's) point of view. Vietful's `productId` is stored when known but is not the key (Excel rows rarely carry it). |
| A2 | A change is defined by the **business fields** of an entity, excluding transport metadata (event id, timestamps, source, request id). |
| A3 | Polling, webhook and Excel may deliver the **same logical state** of the same entity, in any order, any number of times. Duplicate and out-of-order input is normal, not an error. |
| A4 | Every input carries the **full state** of an entity (no partial patches). A blank / missing optional value means `null`, not "unchanged". |
| A5 | The database is the final authority for duplicate prevention. Application checks are optimizations only. |
| A6 | All mechanisms map to one canonical model and one processing pipeline. |
| A7 | CDMS, the emulator and PostgreSQL run on one machine (host processes), so clocks are close (sub-second skew). |
| A8 | Real Vietful is never called; the emulator is the only inventory backend. Its API mirrors Vietful's OpenAPI ([`reference/vietful-api-notes.md`](reference/vietful-api-notes.md)). |
| A9 | A product that disappears from the Products API is **not** deleted in CDMS; deactivation is modeled by the `isActive` field. |

## Decisions

### D1 — Tracked entities

| Entity | Key | Source | Priority |
|---|---|---|---|
| `product` | `partnerSKU` | `GET /api/v1/Products` (Vietful `ProductDto`) | P0 |
| `inventory` | `warehouseCode \| partnerSKU \| unitCode \| conditionTypeCode` | `GET /api/v1/Products/inventories` (`InvProductBriefDto`) | P1 (task C-12) |

One generic pipeline handles both (`entity_type`, `entity_key`), so adding `inventory` adds no new tables.

**Product key confirmed by the user (2026-09-28): `partnerSKU` only.** Existence and change detection match on
`partner_sku`; `productId` is stored but never used for matching. Why: it is the partner's own code, Vietful's product
lookup key (`GET /Products/{partnerSKU}`), never empty (defaults to `sku`), and the only key all three mechanisms can
supply (Excel rows usually lack `productId`; `sku` is Vietful's warehouse SKU / barcode). Accepted limitation: Vietful can
rename (`update-partner-sku`) or swap (`swap-partner-sku`) a partnerSKU; CDMS then sees a new product under the new code
(history split) or, for a swap, a full-state change on both codes. `productId`-first matching was considered and
rejected for simplicity.

### D2 — Definition of a change (fingerprint)

- Input is normalized to a canonical dict: strings Unicode-NFC and trimmed, `""` → `null`, numeric cells in text
  fields become text (`1001.0` → `"1001"`), booleans parsed (`true/false/1/0/yes/no`), list-valued fields treated as
  **sets** (`units` deduplicated and sorted; `categories` deduplicated and sorted by `categoryCode`). Excel string forms
  (`units` comma-separated, `categories` `code:name;code:name`) are accepted by the same normalizer
  (`cdms.core.canonical`). A missing `partnerSKU`, an unparsable boolean / `productId`, or a category without a code
  → INVALID.
- `fingerprint = SHA-256(canonical JSON)` — keys sorted, no whitespace, UTF-8.
- Product fields in the fingerprint: `sku, partnerSKU, productName, assetType, hasSerial, hasExpiration, color, size,
  description, isActive, units, categories`. Excluded: `productId` (internal id), all transport metadata.
- Inventory fields in the fingerprint: `physicalQty, availableQty, pendingInQty, pendingOutQty, freezeQty, inTransitQty`.
  Excluded: `lastUpdatedDate`, `categoryCode`, `categoryName`.
- Outcome per input: **CREATED** (unknown key), **UPDATED** (fingerprint differs), **UNCHANGED** (same fingerprint),
  **STALE** (older than the current state, see D4), **INVALID** (fails validation).
  Only CREATED / UPDATED write a row to the change log.

### D3 — Identity of a logical change and exactly-once

- A logical change = the transition `(entity, version n-1 → n)` with `prev_fingerprint → fingerprint`.
- Enforced by `UNIQUE (entity_type, entity_key, version)` on the change log plus a row lock (`SELECT … FOR UPDATE`) on the
  current state, inside one transaction that also writes the state. Details: [`exactly-once.md`](exactly-once.md).
- Transport-level duplicates are stopped earlier by idempotency keys: webhook event `id`, Excel file SHA-256.

### D4 — Ordering and stale data

- Every state row keeps `observed_at` = when the source saw that state:
  webhook → envelope `timestamp`; polling → the time the page request **started** (never over-claims freshness);
  Excel → optional `observedAt` column, else the upload's receive time.
- Input with `observed_at` **older** than the stored one is **STALE** and ignored (counted, not persisted as a change).
  This stops an old redelivered event from reverting newer data (A→B→A problem).
- An UNCHANGED input with a **newer** `observed_at` advances the stored `observed_at` (no change row, `updated_at`
  kept): the state is known to be current at that time, so a different state observed earlier and delivered late is
  STALE. Equal `observed_at` with a different fingerprint is applied (arrival order).
- Trade-off: a poll that started just before a webhook may be judged stale although its data is newer; the next poll
  corrects it (eventual consistency, documented in limitations).

### D5 — Webhook contract

- Vietful-compatible: envelope `id / timestamp / event`, header `x-vf-hmacsha256` (Base64 HMAC-SHA256 of the raw body
  with the shared secret). Bad / missing signature → `401`.
- Events: `INV_CHANGED` (Vietful) and **`PRODUCT_UPSERTED`** (emulator extension E2 — Vietful has no product event).
  `PRODUCT_UPSERTED.items` carries full `ProductDto` states.
- `INV_CHANGED` carries **deltas** (`changeQty`). CDMS never applies deltas; it would treat the event as a
  notification and re-fetch the current inventory state (C-12, out of scope): for now it is stored as `IGNORED`, like
  every event type other than `PRODUCT_UPSERTED` (Vietful sends all events to the one subscriber).
- Idempotency key = envelope `id`. First delivery → stored in the inbox, `202`. Same `id` + same content → `200
  duplicate`; same `id` + different content → `409`. "Content" = SHA-256 of the canonical JSON (sorted keys, no
  spacing), so a sender that re-serializes the same event is still a duplicate.
- `observed_at` of every item = envelope `timestamp` (Unix seconds). Body limit 1 MB, 1–500 items; items that fail
  normalization are counted `invalid` in the event's outcome. `WEBHOOK_SECRET` unset → `503` (retry later).

### D6 — Excel upload (design only, D13)

- `.xlsx` only (openpyxl), max 10 MB / 50,000 rows. Sheet `Products` (else the first sheet). Row 1 = header.
- Columns = product fields of D2 (+ optional `productId`, `observedAt`). Required: `partnerSKU`, `productName`.
  `units` = comma-separated; `categories` = `code:name;code:name`. Template: `GET /api/v1/uploads/template.xlsx`.
- Processing is **asynchronous**: upload → `202` + `uploadId`; a worker processes rows in chunks.
- Row errors do not fail the file; they are stored per row and exposed at `GET /api/v1/uploads/{id}/errors`.
- Same file (same SHA-256) uploaded again → returns the existing upload (`200`), nothing reprocessed.
- Duplicate rows inside a file are processed in order; the later identical row is UNCHANGED.

### D7 — Processing model and queue

- Ingestion endpoints only **accept durably** (commit to the inbox / job table) and return; processing is done by the
  worker. This keeps webhook latency low during spikes and makes crashes recoverable.
- Queue = PostgreSQL `job` table with `FOR UPDATE SKIP LOCKED` and a lease (`locked_until`, 60 s). No Redis / Kafka /
  RabbitMQ. Kinds: `webhook_event`, `poll_now` (`POST /api/v1/polling/run`).
- `attempts` is counted when a job is claimed, so a job whose worker dies every time still ends `DEAD` after
  `sync_config.job_max_attempts` (default 5). A failed attempt → back to `PENDING` with backoff 2^attempts s (max
  300 s); last failure → `DEAD` (job and inbox event).
- The worker claims up to 100 jobs and processes all webhook events of the batch in one transaction (one
  `apply_observations` call, one commit — ISSUES I-02); if that fails, each event is retried alone so one bad event
  does not block the others. Before finishing, the worker re-locks the jobs still leased to it, so a job re-claimed by
  another worker after a lease expiry is never finished twice.
- Several worker processes may run; per-entity row locks keep results correct. `python -m cdms.worker` runs the poll
  scheduler and the job runner side by side.

### D8 — Polling

- Interval, page size, timeouts and retries come from the `sync_config` table (editable via API / UI); its defaults
  are the column defaults of the migration (interval 30 s, page size 100) — env holds only connections / secrets.
- Single-flight: a run takes a session-level `pg_try_advisory_lock` on a dedicated connection for its whole
  duration; an overlapping tick is skipped and logged. A dead process's connection closes, which releases the lock.
- A run scans all pages (Vietful has no "updated since" filter for products). Each page is processed in its own
  transaction together with the `poll_run` counters. A run interrupted by a crash is marked `ABORTED` by the next run
  (which holds the lock, so no live run can be RUNNING) and that run starts from page 0 — safe because processing is
  idempotent. A page transaction that hits a deadlock / serialization failure / unique violation is retried (3×).
- `observed_at` of every item = the time its page request started. Items that fail normalization are counted as
  `invalid` and logged, the rest of the page is applied.
- HTTP: connect timeout 3 s, read timeout 10 s, 3 attempts with exponential backoff + jitter per page; then the run is
  `FAILED` and the next tick retries. Retried: connection errors, timeouts, 429, 5xx; not retried: other 4xx
  (configuration / bug) and a body that is not a JSON array of objects.
- Scheduler (`python -m cdms.worker`): fixed rate from the start of each run; `sync_config` is re-read every second,
  so changes apply without restart. Manual run: `python -m cdms.worker --poll-once` (the API trigger
  `POST /api/v1/polling/run` needs the job queue, C-05).

### D9 — Configuration

- Environment (pydantic-settings, `.env`): connections and secrets — `DB_*`, `INVENTORY_BASE_URL`,
  `INVENTORY_API_TOKEN`, `WEBHOOK_SECRET`.
- Database (`sync_config`): runtime-tunable schedule / behavior; changes take effect on the next worker tick.

### D10 — Performance targets (self-defined, the test gives none)

Measured 2026-09-29 ([`load-test-results.md`](load-test-results.md)): correctness targets met, latency / throughput
targets **not met** on the development machine (database commit rate, ISSUES I-14); the user kept the design (option C).

| Metric | Target |
|---|---|
| Data set | 5,000 products in the emulator |
| Baseline / spike | 10 req/s webhook baseline → **500 req/s for 60 s** → back to 10 req/s; polling + Excel running concurrently |
| Correctness | **0** duplicate changes, **0** lost accepted events (verified by SQL invariants) |
| Accept latency | p95 < 200 ms at spike for `POST /webhooks/vietful` |
| Error rate | < 1 % non-2xx (excluding deliberate duplicate / invalid requests) |
| Recovery | job backlog drained ≤ 2 min after the spike ends |

### D11 — Scope limits

- No authentication on CDMS read / config APIs and the `/ui` console (local prototype). Webhook uses HMAC.
- Times stored in UTC (`timestamptz`).
- Emulator auth is a static bearer token (extension E1), not OAuth2.

### D12 — Emulator placement (user decision 2026-09-29)

- The Vietful emulator lives in the **same source** (`cdms.emulator`) and the **same database** (`cdms`) as CDMS, in a
  separate PostgreSQL schema **`vietful`** (`products`, `mutations`, `settings`), migrated by the same Alembic chain.
- It runs as a **separate process** (`uvicorn cdms.emulator.main:app --port 8101`), so "Inventory Service down"
  (requirements §9.3) can be demonstrated by stopping it while CDMS keeps running, and spike measurements of CDMS are
  not mixed with the emulator's work.
- Boundary: CDMS reaches the emulator over HTTP only (`INVENTORY_BASE_URL` + `INVENTORY_API_TOKEN`) and never imports
  `cdms.emulator` or reads `vietful.*`; the emulator uses only shared infrastructure (`cdms.config`, `cdms.db`), not
  CDMS's change detection. Enforced by `tests/unit/test_boundaries.py`.
- Trade-offs: PostgreSQL down stops both; both share one server's connections during a spike (acceptable for a
  local prototype). Same image / different command when containerized.
- Split: C-03a = seed, Products API, subscriber registration, mutate / create / mutation log, faults;
  C-03b = webhook callbacks (outbox, HMAC, retries, duplicates, concurrency) — `export.xlsx` dropped with Excel (D13).
- The emulator signs callbacks with its own HMAC code (not CDMS's), so the two implementations check each other.

### D13 — Scope of the deliverable (user decision 2026-09-29, revised the same day)

- **Implemented and demoed:** scheduled polling (C-04) and the webhook callback with its job queue (C-05), both
  through the single change pipeline; the emulator (C-03a) plus its webhook callback sender (C-03b).
- **Excel upload: solution design only** — [`excel-solution.md`](excel-solution.md). Not built: upload routes,
  `excel_upload` / `excel_row_error`, emulator `export.xlsx`. D6 describes the format that design uses.
- The spike test (requirements §10, D10) targets the webhook endpoint as originally planned.
- Deployment: containerized at the end (Dockerfile + docker compose, one image for api / worker / emulator — C-13,
  ISSUES I-05). Day-to-day development keeps running on the host.

## Answers to the pre-coding questions (requirements §23)

| # | Question | Answer |
|---|---|---|
| 1 | What is a change? | D2 — fingerprint of canonical business fields differs from the stored one (or key is new). |
| 2 | Same logical change from several mechanisms? | Same entity + same fingerprint ⇒ UNCHANGED after the first; D3. |
| 3 | Schema that prevents duplicates? | `UNIQUE(entity_type, entity_key, version)`, `UNIQUE` inbox keys, state PK — [`database.md`](database.md). |
| 4 | Two concurrent writers? | `SELECT … FOR UPDATE` on the state row (insert-or-lock), one transaction; the second sees the new fingerprint ⇒ UNCHANGED. |
| 5 | Commit succeeded, app crashed before responding? | Sender retries; inbox key / fingerprint ⇒ duplicate / UNCHANGED, no second change. |
| 6 | Polling restart resumes from? | New run from page 0 (D8); idempotent. |
| 7 | Webhook retry detection? | Envelope `id` unique in the inbox (D5). |
| 8 | Duplicate rows in Excel? | Later identical row ⇒ UNCHANGED (D6). |
| 9 | Excel and webhook with the same data? | First one wins, the other is UNCHANGED. |
| 10 | Rapid successive changes, order? | Per-entity lock + `observed_at` ordering (D4); each distinct state becomes its own version. |
| 11 | Old event after a new one? | STALE, ignored (D4). |
| 12 | Spike metrics? | D10 + [`testing.md`](testing.md). |
| 13 | Failures demoed? | F1–F5 automated in `scripts/failure_scenarios.py`: worker crash mid-batch, API crash after commit, inventory down, database cut (TCP proxy), CDMS restart during a poll — all pass ([`testing.md`](testing.md)). |
| 14 | Must-have features? | Polling and webhook with exactly-once change storage; Excel as a design (D13); implemented / not implemented list in the root `README.md`. |
| 15 | Limitations? | Tracked in `README.md` → "Limitations" as they are found. |
