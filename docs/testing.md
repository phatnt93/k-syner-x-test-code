# Running and testing

Status 2026-09-29: core pipeline (C-02), emulator C-03a, polling C-04, webhook + job queue C-05 implemented;
plus emulator callbacks C-03b, the query / config API C-07 and the `/ui` console C-08, spike test C-10, failure scenarios C-11, cross-mechanism tests C-09, structured logs C-14; 206 tests (unit + integration on `cdms_test`), ruff, mypy strict. Other rows are the planned interface.

## Ports and services

| Service | Host URL | Notes |
|---|---|---|
| `cdms-api` | `http://localhost:8100` (`/ui`, `/docs`) | |
| `inventory-emulator` | `http://localhost:8101` (`/docs`) | `cdms.emulator`, separate process, schema `vietful` (D12). Needs `INVENTORY_API_TOKEN` in `.env` |
| PostgreSQL | `DB_HOST:DB_PORT` from `.env` | User-managed server (existing Docker container). DBs `cdms` (app) and `cdms_test` (wiped by integration tests) |


## Containerized (`compose.yaml`, C-13)

```bash
python scripts/make_compose_env.py      # once: compose.env with random secrets (gitignored)
docker compose up -d --build            # postgres (own volume) → migrate → api, worker, emulator
```

Open `http://localhost:8100/ui` → Seed → "Subscribe this CDMS" (the page subscribes `http://api:8100/...`, the
address the emulator container reaches) → Mutate / Poll now. Stop with `docker compose down` (`-v` also drops the
database). Host ports: 8100 (CDMS), 8101 (emulator), `127.0.0.1:5433` (PostgreSQL, for host scripts: set
`DB_HOST=localhost DB_PORT=5433 DB_USER/DB_PASSWORD` from `compose.env`). Project name `cdms`: nothing else running on
the Docker host is touched.

**Regenerating `compose.env`:** drop the volume first, then `up`. PostgreSQL reads `POSTGRES_PASSWORD` only when its
volume is initialized, so a new `compose.env` next to an old volume makes `migrate` exit 1 with
`password authentication failed` (the script warns when volume `cdms_pgdata` exists). Order:

```bash
docker compose down -v                       # also deletes the stack's database
python scripts/make_compose_env.py --force
docker compose up -d --build
```

## Setup

```bash
conda activate fastapi-env
cp .env.example .env            # fill DB_HOST / DB_PORT / DB_USER / DB_PASSWORD
pip install -r requirements-dev.txt -e .
alembic upgrade head
```

## Run

| Mode | Command |
|---|---|
| From source | `uvicorn cdms.api.main:app --port 8100 --reload` (Windows without `--reload`: add `--loop cdms.loop:selector_loop`) |
| Emulator | `uvicorn cdms.emulator.main:app --port 8101 --loop cdms.loop:selector_loop` |
| Worker (poll scheduler + job runner) | `python -m cdms.worker` (Ctrl+C to stop) |
| Poll now via the API | `curl -X POST localhost:8100/api/v1/polling/run` (the worker runs it) |
| Failure scenarios F1–F5 | `python scripts/failure_scenarios.py` (or `… F2 F4`) |
| Verify invariants | `python scripts/verify_invariants.py [--wait] [--prefix LT<run>-]` |
| One poll now | `python -m cdms.worker --poll-once` (exit 1 if it failed or another run holds the lock) |
| Demo console | start `cdms-api`, `vietful-emulator` (launch configs) and `python -m cdms.worker`, open `http://localhost:8100/ui` → "Subscribe this CDMS" → Mutate (with / without webhook), Poll now, Faults `down` + Poll now (run FAILED, next run recovers) |
| Webhook demo | 1. `POST localhost:8101/api/v1/WebhookSubscribers` `{"endpoint":"http://localhost:8100/api/v1/webhooks/vietful"}` (Bearer token) · 2. `PUT localhost:8101/_admin/callback` `{"duplicateRate":0.3,"maxRetries":5,"retryDelayMs":1000,"concurrency":4}` · 3. `POST localhost:8101/_admin/mutate` `{"count":20,"notify":"webhook"}` · 4. worker running → `GET localhost:8101/_admin/callbacks`, `inbox_event`, `product_changes` |
| Seed emulator | `curl -X POST localhost:8101/_admin/seed -d '{"count":5000,"seed":42}' -H 'content-type: application/json'` |

## Checks

| Check | Command | Needs |
|---|---|---|
| Lint / format | `ruff check . && ruff format --check .` | — |
| Types | `mypy` | — |
| Unit | `pytest tests/unit` | — |
| Integration | `pytest tests/integration` | Postgres (`cdms_test`, reset per session) |
| E2E | `pytest tests/e2e` | api + worker + emulator running |
| Invariants | `psql "$DATABASE_URL_PLAIN" -f scripts/verify_invariants.sql` (plain `postgresql://` URL) | DB |
| Spike load | `k6 run load/k6/spike.js` | services running |

## Test plan (requirements §13 → tests)

| Req | Test | Level | Expected |
|---|---|---|---|
| §13.1 change detection | new / changed / unchanged / stale product; each normalization rule (trim, `""`→null, set order) | unit + integration | new→CREATED, changed→UPDATED, unchanged→no row, stale→no row |
| §13.2 duplicates | same observation ×N sequentially; same webhook `id` ×N; same Excel file ×N | integration | 1 change; `200 duplicate`; existing upload |
| §13.3 cross-mechanism | product v2 via polling and webhook in every order: poll → webhook, webhook → poll, poll started before the webhook, late webhooks after a newer polled state, re-sent state under a new event id, poll and webhook processing concurrently (×3); plus the whole-second webhook limitation. `tests/integration/test_cross_mechanism.py` (C-09). Excel: same fingerprint only (unit, Excel design only) | integration (emulator + CDMS in-process) | v2 stored once; an older state never reverts a newer one |
| §13.4 concurrency | 50 concurrent tasks apply the same state; 50 apply different states to one key; batches touching overlapping keys (deadlock check) | integration | 1 change / contiguous versions; no deadlock left unhandled; invariants hold |
| §13.5 failures | see below | e2e / scripted | no loss, no duplicate, recovers |
| §13.6 spike | k6 scenario below | load | D10 targets |

### Failure scenarios (`scripts/failure_scenarios.py`, each ends with the invariant check)

Implemented 2026-09-29 (C-11). `python scripts/failure_scenarios.py [F1 … F5]` starts its own cdms-api (8110),
emulator (8111) and workers on the `cdms` database, breaks them, restarts them and checks the result; logs in
`load/results/failure-logs/`, `sync_config` restored at the end, nothing left running. Last run 2026-09-29:
**F1–F5 all PASS** (≈ 2 min).

| ID | Scenario | How | Checked (all passed) |
|---|---|---|---|
| F1 | Worker killed mid-batch | 30 webhook events accepted; a worker started with `FAULT_CRASH_IN_JOB_BATCH=true` applies the batch and dies (`os._exit`) before the commit; a second worker starts (`JOB_LEASE_SECONDS=5`) | crash exit 3; 0 changes committed; events still PENDING; after the lease every job re-claimed and run twice; exactly 30 changes; CDMS = emulator |
| F2 | API crash after inbox commit, before response | API started with `FAULT_CRASH_AFTER_INBOX_COMMIT=true`; emulator sends one webhook; API restarted normally | event stored before the crash; sender saw a failure and retried; retry answered `200 duplicate`; one inbox row; exactly 1 change |
| F3 | Inventory service down during polling | emulator faults `down`, 10 products changed without webhook, `POST /api/v1/polling/run`; faults `ok`, poll again | first run `FAILED` after 3 attempts with backoff, changes not in CDMS; run after recovery `SUCCEEDED` and CDMS caught up |
| F4 | PostgreSQL down | API + worker connect through `scripts/tcp_proxy.py`; the proxy is killed (every connection cut, new ones refused) while 20 webhooks arrive, then restarted. The real PostgreSQL is **not** stopped — other databases on the user's server use it | `/ready` 503 during the cut; webhooks answered 503 and kept by the sender; after recovery all delivered and processed, `/ready` 200; exactly 20 changes; CDMS = emulator |
| F5 | Host restart (CDMS side) | polling every 5 s, 200 products changed with webhooks + 200 without; API and worker hard-killed during a poll run, restarted 3 s later (the emulator — the external system — keeps running; PostgreSQL is not restarted, see F4) | interrupted run → `ABORTED`; every webhook delivered; a full poll succeeded after the restart; queue drained; **all 5,000 products equal the emulator's** |

Crash hooks (`cdms.faults.crash`, settings `FAULT_CRASH_AFTER_INBOX_COMMIT`, `FAULT_CRASH_IN_JOB_BATCH`) are off by
default and only set by the scenario script; integration tests cover both (`tests/integration/test_webhook.py`).

### Spike scenario (`load/k6/spike.js`)

- Implemented 2026-09-29 (C-10): `load/k6/spike.js`, run with `python scripts/run_spike.py [--peak 500]` (reads
  `WEBHOOK_SECRET` from `.env`), then `python scripts/verify_invariants.py --wait --prefix LT<run>-`. **Results and
  analysis: [`load-test-results.md`](load-test-results.md).**
- Pre: emulator seeded with 5,000 products; CDMS synced by one poll.
- Load: webhook `PRODUCT_UPSERTED` at 10 req/s (30 s) → **500 req/s (60 s)** → 10 req/s (60 s), `ramping-arrival-rate`.
  Mix: 60 % new states, 25 % exact redeliveries (same `id`), 15 % an old state with a new `id` (cross-mechanism style).
  Products `LT<run>-…` (1,000). In parallel: scheduled polling of the emulator (Excel is design only, D13).
- Collected: k6 throughput, p50 / p95 / p99 latency, error rate; `/api/v1/stats` sampled every second (job backlog,
  outcomes); process CPU / memory; Postgres `pg_stat_database` deadlocks / rollbacks.
- Verdict: D10 targets + invariants (`scripts/verify_invariants.sql` + expected final state per product).
