# Issues and follow-ups

Open problems, measured findings and pending decisions, so they are not lost between sessions. Update the status
here when something changes; close an item with the commit / decision that closed it. Created 2026-09-29.
I-06 and I-07 were housekeeping items of the development workspace and are not listed here.

Status: **OPEN** (needs work) · **DECISION** (waiting for the user) · **MITIGATED** (improved, residual cost known) ·
**ACCEPTED** (known limitation, documented) · **CLOSED**.

| ID | Title | Area | Status |
|---|---|---|---|
| [I-01](#i-01) | Full poll scan of unchanged products was slow (30 s / 5,000) | polling, pipeline | MITIGATED |
| [I-02](#i-02) | Every commit costs ~60 ms on the local PostgreSQL | environment | OPEN |
| [I-03](#i-03) | New / changed products cost ~5–15 ms each (row-by-row path) | pipeline | OPEN |
| [I-04](#i-04) | Default poll page size: 100 or 500 | polling config | CLOSED — keep 100 |
| [I-05](#i-05) | Containerized deployment (requirements §11.2) | deployment | CLOSED — C-13 |
| [I-08](#i-08) | API trigger / history for polling not available yet | polling | CLOSED — C-05 / C-07 |
| [I-09](#i-09) | Emulator matches query parameter names case-sensitively | emulator | ACCEPTED |
| [I-10](#i-10) | Spike test target without webhooks | load test | CLOSED — webhook is back in scope |
| [I-11](#i-11) | Requests hung ~2 min when PostgreSQL was unreachable | database | CLOSED — connect timeout |
| [I-12](#i-12) | Poll trigger and scheduled run can collide | polling | ACCEPTED |
| [I-13](#i-13) | Webhook and poll timestamps come from different clocks | ordering | ACCEPTED |
| [I-14](#i-14) | Webhook accept throughput capped by per-request commit (spike run 1) | load / webhook | ACCEPTED — option C; containers: 451/s peak, p95 4.5 s |
| [I-15](#i-15) | POSTs from the Windows host to Docker ports take ~60 ms extra | environment | ACCEPTED |

---

## I-01

**Full poll scan of unchanged products was slow** — MITIGATED 2026-09-29.

- Found: first real run of C-04 (5,000 emulator products, page size 100, local Docker PostgreSQL on Windows). A scan
  where nothing changed took **30.2 s** — the default interval is 30 s, so runs would follow each other back to back.
- Cause: every product went through the row-by-row pipeline (insert-or-lock, locking select, `observed_at` update):
  3 round trips per product.
- Fix: batch fast path in `cdms.core.pipeline.apply_observations` — one locking `SELECT … ORDER BY partner_sku FOR
  UPDATE` per page, STALE / UNCHANGED decided in Python, one `UPDATE … FROM unnest(…)` for the `observed_at`
  advances; only new / changed / duplicate-key items take the row-by-row path. Same outcomes as the row-by-row path
  (test `test_batch_fast_path_matches_one_by_one`), 2 statements per unchanged page
  (`test_unchanged_page_costs_two_statements`), concurrent batches still update each product once.

| Step | Unchanged scan, 5,000 products |
|---|---|
| Before (row by row, page 100) | 30.2 s |
| Fast path with `executemany` UPDATE (one round trip per row) | 7.4–9.8 s |
| Fast path with one `UPDATE … FROM unnest(…)` (page 100, 51 pages) | 3.5–4.9 s |
| Same, page size 500 (11 pages) | 2.4 s |
| Page 500 with 50 real changes | 3.2 s |

- Residual cost per page (profiled): HTTP ~24 ms, the page's commit ~59 ms (I-02), SQL ~14 ms.

## I-02

**Every commit costs ~60 ms on the local PostgreSQL** — OPEN (environment).

- Measured 2026-09-29 while profiling I-01: 53 commits took 3.14 s (~59 ms each), far more than the statements.
  Most likely the WAL flush (`synchronous_commit = on` + fsync) of the Docker PostgreSQL on Windows storage.
- Impact: any design with one transaction per event is capped by it. The webhook target (D10: 500 req/s accept, one
  inbox commit per request) needs ≥ 30 concurrent commits in flight at 60 ms each — plan C-05 / C-10 for it
  (connection pool size, API workers), or accept a lower measured number and document it.
- Options (not done — the database is the user's container and durability settings must not change silently):
  measure `SHOW synchronous_commit` / `wal_sync_method` and the volume type; move the data directory to a WSL2 /
  named volume; compare with a native PostgreSQL. `synchronous_commit = off` would lose acknowledged events on a
  crash and is **not** acceptable for exactly-once.

## I-03

**New / changed products cost ~5–15 ms each** — OPEN.

- Measured 2026-09-29: first sync of 5,000 new products took 26 s (~5 ms / product); 50 real changes added ~0.8 s to
  an otherwise unchanged scan (~15 ms / change, includes the change row and diff).
- Cause: the row-by-row path — insert-or-lock, locking select, update, change insert — per product.
- Matters for: initial sync, large Excel uploads (50,000 rows, D6), webhook spike processing (C-10).
- Possible next step: set-based insert of new keys in `apply_observations` (`INSERT … ON CONFLICT DO NOTHING
  RETURNING` for the batch + one multi-row insert of the v1 change rows). Measure again in C-06 / C-10 first.

## I-04

**Default poll page size: 100 or 500** — CLOSED 2026-09-29: user keeps **100** (shorter page transactions); 500
remains available at runtime via `sync_config.poll_page_size`.

- `sync_config.poll_page_size` default is 100 (range 1–500, editable at runtime). With the ~60 ms commit (I-02),
  page 500 halves the scan time (4.4 s → 2.4 s for 5,000 products) at the cost of longer page transactions (row locks
  held ~0.2 s instead of ~0.05 s) and a larger re-scan after a failure mid-page.
- Real Vietful's maximum page size is unknown (not in its OpenAPI).

## I-05

**Containerized deployment** — OPEN. Decided 2026-09-29: **option A** — add a `Dockerfile` (one image for api /
worker / emulator) and a `docker-compose.yml` (postgres, one-shot migrate, cdms-api, cdms-worker, emulator) at the
end (C-13); development keeps running on the host. Note: the user's existing PostgreSQL uses port 5432, so the compose
one needs another host port.

- Requirements §11.2 asks for a containerized environment; Docker was removed on request (2026-09-28). Options:
  containerize at the end (one image, commands `cdms-api` / `cdms-worker` / emulator — D12 already allows it) or list
  it as not implemented in `README.md`.

## I-08

**API trigger / history for polling not available yet** — CLOSED 2026-09-29: `POST /api/v1/polling/run` (C-05),
`GET /api/v1/polling/runs` and `GET/PUT /api/v1/config` (C-07).

- `POST /api/v1/polling/run` implemented with C-05 (a `poll_now` job run by the worker); `GET /api/v1/polling/runs` and
  `GET/PUT /api/v1/config` are C-07. Meanwhile: `python -m cdms.worker --poll-once` and the `poll_run` / `sync_config` tables.

## I-09

**Emulator matches query parameter names case-sensitively** — ACCEPTED.

- Real Vietful (ASP.NET) probably accepts `pageIndex` as well as `PageIndex`; the emulator only the documented
  names. CDMS always sends the documented names, so this cannot hide a CDMS bug. Documented in `docs/api.md`.

## I-10

**Spike test target without webhooks** — CLOSED 2026-09-29: D13 was revised the same day (webhook implemented,
Excel design only), so the spike targets the webhook endpoint as planned in D10 / `testing.md`.

- Requirements §10 asks for spike / load testing; D10 and `testing.md` planned it on the webhook endpoint (10 → 500
  req/s), which is now design only. Options: (a) spike on **Excel upload** — many concurrent uploads / one large file,
  measure accept latency, backlog drain time and invariants; (b) spike on **polling** — emulator mutates thousands of
  products per second while polling runs (and several workers compete for the lock); (c) both, smaller; (d) document
  the webhook spike plan ([`../webhook-solution.md`](webhook-solution.md) §6) and run no load test.

## I-11

**Requests hung ~2 min when PostgreSQL was unreachable** — CLOSED 2026-09-29.

- Found by the C-05 test "database down → 503": without a connect timeout, psycopg on Windows kept trying for
  ~130 s, so a webhook sender would time out instead of getting a quick `503` and retrying.
- Fix: `connect_args={"connect_timeout": DB_CONNECT_TIMEOUT_S}` (default 5 s) on the engine (`cdms.db.session`); the
  test now takes 2 s (libpq's minimum).

## I-12

**Poll trigger and scheduled run can collide** — ACCEPTED.

- Seen in the real worker run (2026-09-29): a `poll_now` job claimed at the same moment as a scheduled run found the
  poll lock taken and was finished as "skipped: another poll run was in progress". Correct by design (single-flight,
  D8) — the running scan covers the request — but the user gets no second run. The job's `last_error` records it.

## I-13

**Webhook and poll timestamps come from different clocks** — ACCEPTED (limitation of D4, now measured).

- Found 2026-09-29 by the C-03b end-to-end test: a poll started ~30 ms after a webhook event counted 3 products as
  `STALE` instead of `UNCHANGED`. Cause: the webhook `timestamp` is the emulator's mutation time taken from the
  **database clock** (the Docker PostgreSQL runs **~50 ms ahead** of the host), the poll's `observed_at` is the
  worker's **host clock**; with real Vietful it would be Vietful's clock against CDMS's.
- Effect: no wrong data — STALE writes nothing, nothing is reverted — but a poll inside that window can skip a newer
  state it saw; the next poll (interval 30 s) stores it. This is the eventual-consistency trade-off already listed in
  D4 / `README.md` limitations.
- Whole-second webhook timestamps widen the same window: a change whose webhook timestamp is not newer than the last
  poll's `observed_at` is STALE and stored by the next poll. Pinned by
  `test_webhook_older_than_the_last_poll_waits_for_the_next_poll` (C-09, `tests/integration/test_cross_mechanism.py`);
  the other cross-mechanism tests inject the poll clock so they do not depend on this window.
- Options if it matters later: take `observed_at` for polls as "request start minus a skew margin" (makes polls
  lose more races against webhooks, never win wrongly), or compare with a tolerance and fall back to the fingerprint.

## I-14

**Webhook accept throughput capped by per-request commit** — ACCEPTED 2026-09-29: user chose option C (keep the
design, document the limit). Full report: [`../load-test-results.md`](load-test-results.md) (run 2 at 60 req/s: 0 %
errors, p95 552 ms, all invariants hold).

- Spike run 1 (k6 `load/k6/spike.js`, 10 → 500 → 10 req/s, 1 API process, pool 10 + 10, 1 worker, emulator polling):
  9,755 requests at **57 req/s average**, latency p50 9.4 s / **p95 19 s** / max 30 s, 0.25 % failed (24 connections
  reset), **26,244 arrivals dropped** by k6 (1,000 VUs all waiting). D10 targets (p95 < 200 ms at 500 req/s) **not
  met**.
- Correctness **held**: `verify_invariants.py --wait --prefix LTtm3tx8-` → all invariants hold; 7,566 accepted
  events, 997 products all at their newest accepted state, 5,656 changes ≤ 6,269 distinct states; queue drained.
- Diagnosis: single request 56 ms (≈ one commit, I-02); 20 concurrent → 80 req/s. Raw PostgreSQL commits on this
  machine: 26/s with 1 connection, 48/s with 4, 171/s with 16, **284/s with 40** (WAL flush of the Docker volume on
  Windows). With one commit per request, 500 req/s is out of reach whatever the app does. Secondary: on Windows the
  API runs on a `select()` event loop (async psycopg), capped at ~512 sockets — connections reset under 1,000
  concurrent clients.
- Options: (A) group commit in the API — a short batcher commits many accepted events per transaction and answers
  each request only after that commit (durability unchanged); (B) run the spike in the containerized setup (C-13:
  own PostgreSQL volume, Linux epoll) and compare; (C) keep the design, document the measured limit.
- Update 2026-09-29 (C-13): in the containerized stack the database commits 7× faster; with 4 API processes and
  k6 inside the Docker network the service accepted up to **451 events/s**, 0 % errors, backlog drained in 101 s,
  p95 accept latency 4.5 s (target 200 ms). New limits: API CPU (~150–200 accepts/s per process) and one worker
  (~250 events/s). Details: [`../load-test-results.md`](load-test-results.md#containerized-runs-c-13).

## I-15

**POSTs from the Windows host to Docker ports take ~60 ms extra** — ACCEPTED (environment, measured 2026-09-29).

- The same webhook takes 8.9 ms from inside the api container and 67 ms from a Python client on the Windows host
  (`/health` GET: 3 ms from the host). Most likely Docker Desktop's port proxy / TCP segmenting of POST bodies.
- Effect: load tests from the host understate the service; run k6 inside the compose network
  (`load-test-results.md`, run 5). Not a CDMS issue; nothing to change in code.
