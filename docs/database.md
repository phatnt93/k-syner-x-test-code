# Database (PostgreSQL)

Status: **design** (2026-09-28). Migrations will live in `migrations/` (Alembic); this file must match them.
Database `cdms`. CDMS tables live in `public`; the Vietful emulator's tables live in schema `vietful` (D12), see
[Emulator schema](#emulator-schema-vietful). CDMS code never reads `vietful.*`.

## Emulator schema `vietful`

Owned by `cdms.emulator` (models in `cdms/emulator/models.py`), created by migration `0985b84c1738`.

| Table | Notes |
|---|---|
| `vietful.products` | What the emulated Products API serves. `product_id` int identity PK (Vietful "product identity number", list order), `sku`, `partner_sku` UNIQUE, the other `ProductDto` fields, `units` / `categories` JSONB, `updated_at`. |
| `vietful.mutations` | Append-only log of every product created (`CREATED`) or changed (`UPDATED`) by the emulator: `product_id`, `partner_sku`, `changed_fields` (camelCase names), `data` (full `ProductDto` after), `created_at`. Ground truth for invariant 5. Cleared by `/_admin/seed`. |
| `vietful.settings` | Single row (`CHECK id = 1`, inserted by the migration): `fault_mode` (`ok`/`down`/`slow`/`flaky`), `fault_latency_ms`, `fault_error_rate`, `webhook_endpoint`, `callback_duplicate_rate`, `callback_max_retries`, `callback_retry_delay_ms`, `callback_concurrency` (migration `67eeb373a7e9`). |
| `vietful.callbacks` | Webhook outbox (migration `67eeb373a7e9`): `event_id` UNIQUE, `event_type`, `mutation_id`, `body` (exact JSON signed and sent), `status` (`PENDING`/`DELIVERED`/`FAILED`), `attempts` (failed deliveries), `deliveries` (all POSTs incl. duplicates), `next_attempt_at` (retry time / claim lease), `last_status`, `last_error`, `created_at`, `delivered_at`. Cleared by `/_admin/seed`. |

## Implemented

| Table | Model | Notes |
|---|---|---|
| `products` | `cdms.db.models.product.Product` | Current product state, one row per `partner_sku` (unique), Vietful `ProductDto` fields as snake_case columns (`units`, `categories` as JSONB) + `fingerprint`, `version`, `observed_at`. Product-specific alternative to the generic `entity_state` below; key = `partner_sku` only (D1). |
| `product_changes` | `cdms.db.models.product_change.ProductChange` | Change Database, append-only. `partner_sku` (FK → `products.partner_sku`), `version`, `change_type`, `prev_fingerprint` → `fingerprint`, `data`, `diff`, `source`, `source_ref`, `observed_at`, `recorded_at`. Constraints: `UNIQUE (partner_sku, version)`, `CHECK prev ≠ new`, `CHECK (version = 1) = (prev IS NULL)`, `CHECK (version = 1) = CREATED`, source / change_type enums. Product-specific alternative to `change_log` below. |

Migration for both tables: `58085105cd6f` (`create products and product_changes`), applied to `cdms` 2026-09-28.

## Tables (design)

### `entity_state` — current snapshot (one row per entity)

| Column | Type | Notes |
|---|---|---|
| `entity_type` | `text` | `product` \| `inventory` |
| `entity_key` | `text` | D1 key (`partnerSKU`, or the inventory composite key) |
| `version` | `int` | = latest `change_log.version` |
| `fingerprint` | `bytea` | SHA-256 of canonical data (D2) |
| `data` | `jsonb` | canonical data |
| `external_id` | `bigint` null | Vietful `productId` when known |
| `observed_at` | `timestamptz` | source observation time of this state (D4) |
| `updated_at` | `timestamptz` | |

PK `(entity_type, entity_key)`.

### `change_log` — the Change Database (append-only)

| Column | Type | Notes |
|---|---|---|
| `id` | `bigint identity` | global order |
| `entity_type`, `entity_key` | `text` | |
| `version` | `int` | 1 = CREATED |
| `change_type` | `text` | `CREATED` \| `UPDATED` |
| `prev_fingerprint` | `bytea` null | null for CREATED |
| `fingerprint` | `bytea` | |
| `data` | `jsonb` | full new state |
| `diff` | `jsonb` | `{ field: [old, new] }` (empty for CREATED) |
| `source` | `text` | `POLLING` \| `WEBHOOK` \| `EXCEL` |
| `source_ref` | `text` | poll run id + page / inbox event id / upload id + row |
| `observed_at` | `timestamptz` | |
| `recorded_at` | `timestamptz` default `now()` | |

Constraints — the exactly-once backstop:

- `UNIQUE (entity_type, entity_key, version)`
- `CHECK (prev_fingerprint IS DISTINCT FROM fingerprint)` — an "unchanged change" can never be stored.
- `CHECK ((version = 1) = (prev_fingerprint IS NULL))`

Index `(entity_type, entity_key, version DESC)`, `(recorded_at)`, `(source, recorded_at)`.

### `inbox_event` — webhook idempotency (implemented, `cdms.db.models.inbox_event`, migration `1e2338f24ff9`)

| Column | Type | Notes |
|---|---|---|
| `id` | `bigint identity` | |
| `source` | `text` | `vietful` |
| `event_id` | `text` | envelope `id` |
| `event_type` | `text` | `PRODUCT_UPSERTED`, `INV_CHANGED`, … |
| `body_sha256` | `bytea` | SHA-256 of the canonical JSON of the body (same id + different content → 409) |
| `payload` | `jsonb` | |
| `event_ts` | `timestamptz` | envelope `timestamp` |
| `status` | `text` | `PENDING` \| `PROCESSED` \| `IGNORED` (unsupported event) \| `DEAD` |
| `outcome` | `jsonb` null | counts per outcome |
| `error` | `text` null | last processing error |
| `received_at`, `processed_at` | `timestamptz` | |

`UNIQUE (source, event_id)`.

### `excel_upload`

`id uuid PK`, `file_name`, `file_sha256 bytea UNIQUE`, `content bytea`, `size_bytes`, `status`
(`PENDING` \| `PROCESSING` \| `SUCCEEDED` \| `FAILED`), `total_rows`, `processed_rows` (checkpoint), `created`, `updated`,
`unchanged`, `stale`, `invalid`, `error`, `received_at`, `finished_at`.

`excel_row_error`: `upload_id FK`, `row_number`, `errors jsonb`, `raw jsonb`; `UNIQUE (upload_id, row_number)` (re-processing a
chunk does not duplicate errors).

### `job` — Postgres-backed queue (D7; implemented, `cdms.db.models.job`, `cdms.jobs.queue`)

| Column | Type | Notes |
|---|---|---|
| `id` | `bigint identity` | |
| `kind` | `text` | `webhook_event` \| `poll_now` |
| `ref_id` | `text` | inbox id / request id (poll_now) |
| `status` | `text` | `PENDING` \| `RUNNING` \| `DONE` \| `DEAD` (partial index on `run_after` for unfinished jobs) |
| `attempts`, `max_attempts` | `int` | |
| `run_after` | `timestamptz` | backoff |
| `locked_by`, `locked_until` | `text`, `timestamptz` | lease (`JOB_LEASE_SECONDS`, default 60); expired lease ⇒ re-claimable |
| `last_error` | `text` | |
| `created_at`, `updated_at` | `timestamptz` | |

`UNIQUE (kind, ref_id)` (one job per inbox event / poll request). Claim (one statement, `cdms.jobs.queue.claim`) —
leases the selected rows and counts the attempt:

```sql
UPDATE job SET status = 'RUNNING', attempts = attempts + 1, locked_by = :worker, locked_until = now() + '60 s'
WHERE id IN (
  SELECT id FROM job
  WHERE status IN ('PENDING','RUNNING') AND run_after <= now()
    AND (locked_until IS NULL OR locked_until < now())
  ORDER BY id LIMIT :n
  FOR UPDATE SKIP LOCKED)
RETURNING id, kind, ref_id, attempts, max_attempts;
```

### `poll_run` (implemented, `cdms.db.models.poll_run.PollRun`, migration `2fc4ba688033`)

`id bigint identity`, `trigger` (`SCHEDULE` \| `MANUAL`), `status` (`RUNNING` \| `SUCCEEDED` \| `FAILED` \| `ABORTED`),
`pages`, `items`, `created`, `updated`, `unchanged`, `stale`, `invalid`, `error`, `started_at`, `finished_at`.

### `sync_config` — runtime configuration (single row, D9; implemented, `cdms.db.models.sync_config.SyncConfig`)

`id = 1` (row inserted by the migration), `poll_enabled bool` (true), `poll_interval_seconds int` (30, 5–86400),
`poll_page_size int` (100, 1–500), `http_connect_timeout_ms` (3000), `http_read_timeout_ms` (10000),
`http_max_attempts` (3, 1–10), `job_max_attempts` (5, used from C-05), `updated_at`. Ranges are CHECK constraints.

## Invariants (checked after every test / load run — `scripts/verify_invariants.sql`, run by `scripts/verify_invariants.py`)

1. No two `change_log` rows share `(entity_type, entity_key, version)` (constraint) and versions per entity are
   contiguous `1..n`.
2. For every entity, consecutive versions have different fingerprints and `v(n).prev_fingerprint = v(n-1).fingerprint`.
3. `entity_state.version` / `fingerprint` equal the latest `change_log` row of that entity.
4. Every `inbox_event` with `status = PROCESSED` has exactly one `job` in `DONE`; no `PENDING` rows remain after drain.
5. Expected changes from the emulator's mutation log = distinct transitions in `change_log` (no lost, no extra).
