# Architecture

Status: **partly implemented** (2026-09-29: core pipeline C-02, emulator C-03a). Decisions referenced as D-* are in [`assumptions.md`](assumptions.md).

## Components

```text
                        ┌──────────────────────────────────────┐
                        │ inventory-emulator  (FastAPI + Faker)│
                        │  GET /api/v1/Products (Vietful shape) │
                        │  /_admin: mutate, faults, callbacks   │
                        └───────┬──────────────────▲───────────┘
             webhook (HMAC) POST│                  │ GET Products / inventories
                                ▼                  │
┌───────────────┐   POST  ┌────────────────────┐   │   ┌────────────────────────────┐
│ REST client / │ ──────► │ cdms-api (FastAPI) │   │   │ cdms-worker                 │
│ /ui console   │  Excel  │  webhook, uploads, │   │   │  poll scheduler ────────────┘
└───────────────┘         │  queries, config,  │   │   │  job runner (SKIP LOCKED)   │
                          │  /ui (static HTML) │       │  change pipeline            │
                          └─────────┬──────────┘       └──────────────┬──────────────┘
                                    │ accept durably (inbox / job)      │ process
                                    ▼                                   ▼
                          ┌──────────────────────────────────────────────────────┐
                          │ PostgreSQL: entity_state, change_log, inbox_event,    │
                          │ excel_upload, job, poll_run, sync_config              │
                          └──────────────────────────────────────────────────────┘
```

| Service | Process | Responsibility |
|---|---|---|
| `postgres` | PostgreSQL 17 | Database `cdms`: CDMS tables in `public`, emulator tables in schema `vietful` (D12) |
| `inventory-emulator` | `uvicorn cdms.emulator.main:app --port 8101` | Same package, separate process (D12). Vietful-shaped Products / inventories API with Faker data; admin endpoints to mutate data, inject faults, send webhooks (acts as the Callback Client) |
| `cdms-api` | `uvicorn cdms.api.main:app` | Accept webhooks and Excel uploads durably; query / config APIs; `/ui` test console; `/health`, `/ready` |
| `cdms-worker` | `python -m cdms.worker` | Poll scheduler + job runner; the only place that runs the change pipeline |

`cdms-api`, `cdms-worker` and the emulator share one image (`Dockerfile`) and one package (`cdms`); the
command decides the role. `compose.yaml` (C-13) runs them with their own PostgreSQL and a one-shot
`migrate` service; development runs the same commands on the host.

## One pipeline for all mechanisms

```text
Polling page ─┐
Webhook event ├─► normalize ─► validate ─► canonical entity ─► fingerprint
Excel row ────┘                                                    │
                                                                   ▼
                       ┌──────────── one DB transaction per entity ─────────────┐
                       │ lock-or-insert entity_state row (FOR UPDATE)            │
                       │ observed_at older?  → STALE                             │
                       │ same fingerprint?   → UNCHANGED                         │
                       │ else version+1, update state, INSERT change_log         │
                       └─────────────────────────────────────────────────────────┘
```

- `cdms.core.canonical.canonical_product(raw)` — normalizes a Vietful `ProductDto`, webhook item or Excel row into
  one canonical dict (camelCase keys); raises `InvalidProductError` (outcome INVALID).
- `cdms.core.fingerprint` — canonical JSON + SHA-256 of the business fields (D2).
- `cdms.core.pipeline.apply_observation(session, observation) -> ApplyResult` (and `apply_observations` for a batch:
  locks the batch's rows in `partner_sku` order, decides UNCHANGED / STALE set-based, row by row only for new / changed
  products — [`ISSUES.md`](ISSUES.md) I-01) — the single write path to `products` / `product_changes` (D3, D4). The caller owns
  the transaction, so inbox / job updates commit together with it.
- Ingestion adapters only build `ProductObservation.from_raw(raw, observed_at=…, source=…, source_ref=…)` objects.
  Implemented 2026-09-28 (C-02); the generic `entity_state` / `change_log` variant is not used for products.

## Flows

### Scheduled polling (worker)

Implemented 2026-09-29 (C-04): `cdms.ingestion.inventory_client`, `cdms.ingestion.polling.run_poll`, `cdms.worker`.

1. Every tick (`sync_config.poll_interval_seconds`) the scheduler tries `pg_try_advisory_lock(POLL_LOCK)`; if taken, skip.
2. Mark leftover `RUNNING` runs `ABORTED`; insert `poll_run` (`RUNNING`). For `PageIndex = 0..` call
   `GET /api/v1/Products?PageIndex&PageSize` with timeouts and retries (D8). An empty / short page ends the scan.
3. Each page → observations (`observed_at` = page request start) → pipeline, one transaction per page; update
   `poll_run` counters (`pages`, `created`, `updated`, `unchanged`, `stale`).
4. End: `SUCCEEDED` / `FAILED` (+ error). Manual run: `python -m cdms.worker --poll-once`; the API trigger
   `POST /api/v1/polling/run` (enqueues the same work) comes with the job queue (C-05).

### Webhook callback (api → worker)

Implemented 2026-09-29 (C-05): `cdms.api.routes.webhooks`, `cdms.ingestion.webhook`, `cdms.jobs`. Design and
failure analysis: [`webhook-solution.md`](webhook-solution.md).

1. `POST /api/v1/webhooks/vietful`: verify `x-vf-hmacsha256` on the raw body; parse the envelope.
2. `INSERT INTO inbox_event (source, event_id, body_sha256, payload) … ON CONFLICT DO NOTHING` + `INSERT job` in one
   transaction → `202`. Conflict → compare `body_sha256` → `200 duplicate` or `409`.
3. Worker claims the job, maps items to observations (`observed_at` = envelope `timestamp`), runs the pipeline and marks
   the inbox row `PROCESSED` **in the same transaction**.
   `INV_CHANGED` → fetch current inventory for the listed keys first (C-12).

### Excel upload (api → worker) — design only, not implemented (D13, [`excel-solution.md`](excel-solution.md))

1. `POST /api/v1/uploads/excel` (multipart): size check, SHA-256 of the file. Existing hash → `200` with the existing
   upload. Else store the file bytes in `excel_upload` + `INSERT job` → `202 { uploadId }`.
2. Worker parses with openpyxl (read-only mode), validates rows, processes in chunks of 500 rows, one transaction per
   chunk; stores row errors; updates `processed_rows` as a checkpoint. A crash resumes from the checkpoint (re-processing
   a chunk is harmless).
3. `GET /api/v1/uploads/{id}` → status and counters.

### Test console (`/ui`)

Implemented 2026-09-29 (C-08). One static page served by `cdms-api`: stats cards (auto-refresh), emulator controls
(seed, mutate with / without webhook, subscription, callback delivery, faults), CDMS controls (poll now, sync config),
tables (changes with diffs, products, webhook inbox, poll runs, emulator callbacks) and a version-history dialog. The
browser calls the emulator directly (CORS on the emulator), so CDMS code still never talks to the emulator admin API.

## Failure model (summary)

| Failure | Behavior | Detail |
|---|---|---|
| CDMS api / worker crash | Nothing half-written (single transactions). Jobs with expired lease are re-claimed. Senders retry; idempotency absorbs it. | [`exactly-once.md`](exactly-once.md) |
| Inventory service down / slow | Timeouts + bounded retries, run `FAILED`, next tick retries. Webhook jobs needing a fetch are retried with backoff (max attempts → `DEAD`). | D8 |
| PostgreSQL down | API returns `503` (sender retries); worker backs off and reconnects; no in-memory buffering. Ambiguous commits are safe to retry. | [`exactly-once.md`](exactly-once.md) |
| Host crash / reboot | Postgres data is durable on disk (`fsync`, `synchronous_commit=on`); processes are restarted; on start the worker aborts stale `RUNNING` poll runs and resumes pending jobs. | [`testing.md`](testing.md) |

## Code layout

```text
./
  pyproject.toml, requirements.txt, requirements-dev.txt, .env.example
  alembic.ini, migrations/               # Alembic (CDMS DB only)
  src/cdms/
    config.py                            # pydantic-settings
    db/ (engine.py, tables.py)           # SQLAlchemy Core tables
    core/ (canonical.py, fingerprint.py, pipeline.py, models.py)
    ingestion/ (polling.py, webhook.py, excel.py, inventory_client.py)
    jobs/ (queue.py, runner.py)
    api/ (main.py, routes/*.py, errors.py)
    ui/static/index.html                 # test console, vanilla JS
    worker.py                            # python -m cdms.worker
  src/cdms/emulator/ (main.py, routes.py, catalog.py, data.py, models.py, schemas.py, errors.py)
                                         # separate process, schema `vietful`, HTTP-only boundary (D12)
  tests/ (unit/, integration/, e2e/)
  load/k6/ (spike.js, …)
  scripts/ (verify_invariants.sql, failure/*.ps1, demo/*.ps1)
```
