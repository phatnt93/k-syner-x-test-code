# Load test results (C-10)

Spike test of the webhook endpoint, requirements §10, targets in decision D10. Runs 2026-09-29, first against the
development processes on the host (runs 1–2), then against the containerized deployment (runs 3–5, C-13).
**Outcome: correctness held in every run (5 runs, up to 25,920 accepted events). In containers the system accepted
up to 451 events/s with 0 % errors and drained the backlog in 101 s (D10: ≤ 2 min), but accept latency p95 stayed
in seconds (D10: < 200 ms).** User decision (ISSUES I-14 option C): keep the design, document the limits and the ways
to lift them — the container runs only changed deployment settings (own PostgreSQL volume, 4 API processes).

## Setup

| Item | Value |
|---|---|
| Host | Windows 11, all processes on one machine (A7) |
| PostgreSQL | 17.6 in the user's existing Docker container (`synchronous_commit = on`, `wal_sync_method = fdatasync`, `shared_buffers = 128MB`, `max_connections = 100`, 16 connections used by other databases) |
| cdms-api | 1 uvicorn process, `--loop cdms.loop:selector_loop` (async psycopg needs a selector loop on Windows), DB pool 10 + 10 |
| cdms-worker | 1 process: job runner (batches of up to 100 events per transaction) + poll scheduler (every 30 s, 5,000 emulator products) |
| Emulator | running, polled by the worker during the whole test |
| Load generator | k6 v2.2.0, `load/k6/spike.js`, open model (`ramping-arrival-rate`), up to 1,000 VUs |

Scenario (`load/k6/spike.js`): 30 s at 10 req/s → 10 s ramp → **60 s at the peak** → 10 s ramp down → 60 s at
10 req/s. Every request is a signed `PRODUCT_UPSERTED` event with one item, spread over 1,000 load-test products
(`LT<run>-00001`…, never the emulator's catalogue). Mix: 60 % a new state (new id, newer timestamp), 25 % an exact
redelivery of an event already sent (expect `200 duplicate`), 15 % an old state under a new event id.

Reproduce (in the repository root, with cdms-api, emulator and worker running):

```bash
python scripts/run_spike.py --peak 500 --summary load/results/run1-summary.json
python scripts/verify_invariants.py --wait --prefix LT<run tag printed by k6>-
```

## Results

| | Run 1 — peak 500 req/s (D10) | Run 2 — peak 60 req/s |
|---|---|---|
| Run tag | `tm3tx8` | `tm3un7` |
| Requests sent / average rate | 9,755 / 57 req/s | 5,199 / 31 req/s |
| Arrivals dropped by k6 (all VUs busy) | **26,244** | 0 |
| Latency avg / p50 / p95 / p99 / max | 7.8 s / 9.4 s / **19.0 s** / 21.4 s / 30.1 s | 136 ms / 71 ms / **552 ms** / 718 ms / 1.2 s |
| Failed (non-2xx) | 0.25 % (24 connections reset) | 0 % |
| `202 accepted` / `200 duplicate` | 7,566 / 2,165 | 3,899 / 1,300 |
| Peak accepted per second | 139 | ≈ 60 |
| Accept → processed p50 / p95 / max | 4.0 s / 11.7 s / 12.3 s | 0.31 s / 0.97 s / 4.4 s |
| Queue drained after the last request | ≤ 1 s | ≤ 1 s |
| k6 thresholds (p95 < 200 ms, < 1 % failed) | latency **failed**, errors ok | latency **failed**, errors ok |

### Correctness (`scripts/verify_invariants.py`)

| Check | Run 1 | Run 2 |
|---|---|---|
| Versions contiguous 1..n per product | ok | ok |
| Every change chains to the previous fingerprint and changes something | ok | ok |
| Current state = latest change | ok | ok |
| Every processed event has its job DONE; no accepted event without a job | ok | ok |
| Final state of every load-test product = its newest accepted state | ok (997 products) | ok (962 products) |
| Changes ≤ distinct states received (no duplicate change) | ok — 5,656 changes for 6,269 states | ok — 3,175 for 3,175 |
| Outcomes | created 997 · updated 4,659 · unchanged 409 · stale 1,501 | — |

Run 1's 1,501 `stale` outcomes are intermediate states that arrived after a newer state of the same product (the
backlog reordered them); discarding them is correct (D4) and the final states are right. In run 2 events were
processed in order, so every distinct state became exactly one change.

## Why the targets were missed

1. **Commit latency of this PostgreSQL.** A single accept is one transaction: 56 ms median, almost all of it the
   commit (ISSUES I-02). Raw commit rate of the database, measured without CDMS:

   | Concurrent connections | 1 | 4 | 16 | 40 |
   |---|---|---|---|---|
   | Commits / s | 26 | 48 | 171 | 284 |

   PostgreSQL's group commit helps, but even with 40 connections the database cannot commit 500 transactions a
   second here. With one commit per accepted event (the design: answer only after the event is durable), 500 req/s
   is out of reach on this machine whatever the application does. The API also shares this commit budget with the
   worker, which commits the processed batches.
2. **Windows event loop.** Async psycopg requires a `select()`-based loop on Windows, which is limited to about
   512 sockets and scans all of them on every wake-up. Under 1,000 waiting clients this adds latency and caused the
   24 connection resets of run 1. Linux (containers, C-13) uses epoll and has neither limit.
3. **One API process with a pool of 20.** Requests queue for a connection once 20 transactions are in flight.

The design itself behaved as intended under overload: nothing was lost, nothing duplicated, the queue never grew
(the worker kept up with what the API could accept), and requests were slowed down rather than failed.

## How the limit could be lifted (not done — option C)

| Option | Expected effect | Cost / risk |
|---|---|---|
| Group commit in the API: a short batcher (a few ms) inserts many accepted events in one transaction and answers each request after that commit | throughput × batch size (hundreds of events per commit); durability unchanged | moderate code in the accept path; one failed batch answers `503` to all its requests (senders retry) |
| Faster storage for PostgreSQL: a named Docker volume / native install instead of the current volume, run in the containerized setup (C-13) | commit latency from ~40–60 ms to ~1–5 ms is typical on local SSD storage | none in code; to be measured |
| More API processes and a larger pool (within `max_connections`) | uses group commit better (≈ 280 commits/s at 40 connections here) | still capped by the commit rate |
| `commit_delay` / `commit_siblings` in PostgreSQL | larger commit groups | server setting of the user's database — not changed |
| `synchronous_commit = off` | fast commits | **rejected**: an acknowledged event could be lost on a crash |

## Containerized runs (C-13)

Same script and mix; `docker compose` stack of `compose.yaml`: its own PostgreSQL 17 (named volume),
cdms-api, 1 cdms-worker, emulator. Nothing in the application changed; only deployment settings.

Raw commit rate of the compose PostgreSQL (measured from the host, same test as above): **194 commits/s with
1 connection, 500 with 4, 436 with 16, 582 with 40** — 7× the development database (26 / 48 / 171 / 284).

| | Run 3 | Run 4 | Run 5 |
|---|---|---|---|
| Setup | 1 API process (pool 10 + 10), k6 on the Windows host → Docker port | 4 API processes (`--workers 4`, pool 8 + 4 each), k6 on the host | same as run 4, **k6 in a container on the compose network** (`http://api:8100`) |
| Run tag | `tm3x0y` | `tm3x89` | `tm3xeq` |
| Requests / average rate | 12,731 / 75 req/s | 27,362 / 161 req/s | **33,990 / 179 req/s** (of ~36,000 offered) |
| Dropped arrivals (1,000 VUs busy) | 23,268 | 8,637 | **2,009** |
| Latency p50 / p95 / p99 / max | 6.9 s / 14.7 s / 21.7 s / 43.4 s | 2.4 s / 5.2 s / 7.7 s / 20.3 s | 1.3 s / **4.5 s** / 7.4 s / 17.3 s |
| Failed (non-2xx) | 0.02 % | 0 % | **0 %** |
| `202` / `200 duplicate` | 9,759 / 2,970 | 20,833 / 6,529 | 25,920 / 8,070 |
| Peak accepted per second | 188 | 392 | **451** |
| Accept → processed p50 / p95 | 3.1 s / 11.3 s | 88.7 s / 129.5 s | 107.8 s / 163.5 s |
| Backlog drained after the last request | 0.4 s | 75 s | **101 s** (D10: ≤ 120 s ✓) |
| Invariants + final state (1,000 products each) | all ok (6,740 changes / 7,986 states) | all ok (14,599 / 16,732) | all ok (18,554 / 21,049) |

What the container runs showed:

1. **The database is no longer the limit** (7× faster commits on a named volume).
2. **One API process is CPU-bound** at ~150–200 accepts/s (inside the container one accept takes 8.9 ms; 20 concurrent
   → 164 req/s). Four processes raised throughput 2–3× — hence `--workers 4` in `compose.yaml`.
3. **The Windows host → Docker Desktop port path adds ~60 ms per POST** (67 ms from the host vs 8.9 ms inside the
   container for the same request); running k6 inside the compose network removed most drops.
4. **The worker becomes the bottleneck once accepts are fast**: one worker applied ~250 events/s, so during the spike
   events waited up to ~2.5 min in the queue — still drained within the D10 window, and never lost or duplicated.
   More worker replicas (the queue and row locks already support them) would shorten it.

Reproduce inside the network:

```bash
set -a && . ./compose.env && set +a
docker run --rm --network cdms_default -v "$PWD/load:/load" -e WEBHOOK_SECRET -e CDMS_URL=http://api:8100 \
  grafana/k6 run /load/k6/spike.js
docker compose exec api python scripts/verify_invariants.py --wait --prefix LT<run>-
```
