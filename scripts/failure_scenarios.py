"""Failure scenarios F1-F5 (docs/testing.md, requirements §9) against real processes.

    python scripts/failure_scenarios.py            # all scenarios
    python scripts/failure_scenarios.py F2 F4      # some of them

Starts its own cdms-api (port 8110), emulator (8111) and workers on the `cdms` database, breaks them on
purpose (hard kills, crash hooks, database cut through a TCP proxy), restarts them, and checks that nothing
acknowledged was lost, nothing was stored twice, and CDMS ends with exactly the emulator's data. Every
scenario ends with `scripts/verify_invariants.py`. Logs: load/results/failure-logs/. Needs INVENTORY_API_TOKEN
and WEBHOOK_SECRET in .env, and the ports 8110, 8111, 15432 free. Leaves nothing running; `sync_config` is
restored at the end.
"""

import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import create_engine, text

from cdms.config import get_settings
from cdms.core.canonical import canonical_product
from cdms.core.fingerprint import fingerprint

ROOT = Path(__file__).resolve().parents[1]
LOGS = ROOT / "load" / "results" / "failure-logs"
PY = sys.executable
API_PORT, EMU_PORT, PROXY_PORT = 8110, 8111, 15432
API = f"http://localhost:{API_PORT}"
EMU = f"http://localhost:{EMU_PORT}"
ENDPOINT = f"{API}/api/v1/webhooks/vietful"
LEASE_S = 5

settings = get_settings()
db = create_engine(settings.database_url)
http = httpx.Client(timeout=30)


class Failed(AssertionError):
    pass


def check(condition: bool, message: str) -> None:
    if not condition:
        raise Failed(message)
    print(f"    ✓ {message}")


def wait(condition: Callable[[], Any], what: str, timeout: float = 60, interval: float = 0.5) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if value := condition():
                return value
        except (httpx.HTTPError, OSError):
            pass
        time.sleep(interval)
    raise Failed(f"timed out after {timeout:.0f} s waiting for: {what}")


def sql(query: str, **params: Any) -> list[Any]:
    with db.connect() as conn:
        return list(conn.execute(text(query), params).all())


def scalar(query: str, **params: Any) -> Any:
    with db.connect() as conn:
        return conn.execute(text(query), params).scalar()


# --- processes ----------------------------------------------------------------------------------------------


class Process:
    def __init__(self, name: str, args: list[str], env: dict[str, str] | None = None) -> None:
        self.name = name
        LOGS.mkdir(parents=True, exist_ok=True)
        self.log = open(LOGS / f"{name}.log", "a", encoding="utf-8")  # noqa: SIM115 - closed in stop()
        self.log.write(f"\n===== start {time.strftime('%H:%M:%S')} =====\n")
        self.log.flush()
        full_env = os.environ | {
            "PYTHONPATH": str(ROOT / "src"),
            "INVENTORY_BASE_URL": EMU,
            "LOG_LEVEL": "INFO",
        }
        self.proc = subprocess.Popen(
            [PY, *args], cwd=ROOT, env=full_env | (env or {}), stdout=self.log, stderr=subprocess.STDOUT
        )

    def kill(self) -> None:
        """Hard kill (TerminateProcess on Windows): no shutdown code runs, like a crash or power loss."""
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(10)

    def stop(self) -> None:
        self.kill()
        self.log.close()

    def exit_code(self, timeout: float) -> int:
        return self.proc.wait(timeout)


def uvicorn(app: str, port: int) -> list[str]:
    return ["-m", "uvicorn", app, "--port", str(port), "--loop", "cdms.loop:selector_loop"]


def start_api(env: dict[str, str] | None = None, name: str = "api") -> Process:
    process = Process(name, uvicorn("cdms.api.main:app", API_PORT), env)
    wait(lambda: http.get(f"{API}/health").status_code == 200, "cdms-api up", 30)
    return process


def start_emulator() -> Process:
    process = Process("emulator", uvicorn("cdms.emulator.main:app", EMU_PORT))
    wait(lambda: http.get(f"{EMU}/health").status_code == 200, "emulator up", 30)
    return process


def start_worker(env: dict[str, str] | None = None, name: str = "worker") -> Process:
    return Process(name, ["-m", "cdms.worker"], {"JOB_LEASE_SECONDS": str(LEASE_S)} | (env or {}))


def start_proxy() -> Process:
    return Process("proxy", ["scripts/tcp_proxy.py", "--listen", str(PROXY_PORT),
                             "--target", f"{settings.db_host}:{settings.db_port}"])  # fmt: skip


# --- emulator / CDMS helpers --------------------------------------------------------------------------------


def admin(method: str, path: str, **body: Any) -> Any:
    resp = http.request(method, f"{EMU}{path}", json=body or None)
    resp.raise_for_status()
    return resp.json()


def mutate(count: int, webhook: bool) -> list[dict[str, Any]]:
    mutations: list[dict[str, Any]] = admin(
        "POST", "/_admin/mutate", count=count, notify="webhook" if webhook else "none"
    )["mutations"]
    return mutations


def callbacks_of(mutations: list[dict[str, Any]]) -> list[Any]:
    ids = [m["id"] for m in mutations]
    return sql(
        "SELECT event_id, status, attempts, deliveries, last_status FROM vietful.callbacks "
        "WHERE mutation_id = ANY(:ids)",
        ids=ids,
    )


def events_processed(event_ids: list[str]) -> bool:
    done = scalar(
        "SELECT count(*) FROM inbox_event WHERE event_id = ANY(:ids) AND status = 'PROCESSED'", ids=event_ids
    )
    return bool(done == len(event_ids))


def config(**values: Any) -> None:
    sets = ", ".join(f"{key} = :{key}" for key in values)
    with db.begin() as conn:
        conn.execute(text(f"UPDATE sync_config SET {sets} WHERE id = 1"), values)


def emulator_products(skus: list[str] | None = None) -> dict[str, bytes]:
    headers = {"Authorization": f"Bearer {settings.inventory_api_token.get_secret_value()}"}  # type: ignore[union-attr]
    found: dict[str, bytes] = {}
    page = 0
    while True:
        params: dict[str, Any] = {"PageIndex": page, "PageSize": 1000}
        if skus:
            params["PartnerSKUs"] = ",".join(skus)
        resp = http.get(f"{EMU}/api/v1/Products", params=params, headers=headers)
        resp.raise_for_status()
        items = resp.json()
        found |= {item["partnerSKU"]: fingerprint(canonical_product(item)) for item in items}
        if len(items) < 1000:
            return found
        page += 1


def matches_mutations(mutations: list[dict[str, Any]]) -> bool:
    """CDMS holds the state each mutation produced (= the emulator's current state of those products)."""
    expected = {m["partnerSKU"]: fingerprint(canonical_product(m["data"])) for m in mutations}
    stored = dict(
        sql("SELECT partner_sku, fingerprint FROM products WHERE partner_sku = ANY(:s)", s=list(expected))
    )
    return all(stored.get(sku) == fp for sku, fp in expected.items())


def matches_emulator(skus: list[str] | None = None) -> bool:
    expected = emulator_products(skus)
    stored = dict(
        sql("SELECT partner_sku, fingerprint FROM products WHERE partner_sku = ANY(:s)", s=list(expected))
    )
    return all(stored.get(sku) == fp for sku, fp in expected.items())


def invariants() -> None:
    result = subprocess.run(
        [PY, "scripts/verify_invariants.py", "--wait", "--timeout", "120"],
        cwd=ROOT, env=os.environ | {"PYTHONPATH": str(ROOT / "src")}, capture_output=True, text=True,
    )  # fmt: skip
    check(result.returncode == 0, "invariants hold (scripts/verify_invariants.py)" if result.returncode == 0
          else f"invariants FAILED:\n{result.stdout}\n{result.stderr}")  # fmt: skip


# --- scenarios ----------------------------------------------------------------------------------------------


def f1_worker_killed_mid_batch(procs: list[Process]) -> None:
    """F1: the worker dies inside a batch transaction (changes applied, not committed)."""
    procs += [start_emulator(), start_api()]
    mutations = mutate(30, webhook=True)
    wait(lambda: all(c.status == "DELIVERED" for c in callbacks_of(mutations)), "30 callbacks delivered")
    events = [c.event_id for c in callbacks_of(mutations)]
    doomed = start_worker({"FAULT_CRASH_IN_JOB_BATCH": "true"}, "worker-doomed")  # takes all 30 in one batch
    procs.append(doomed)

    check(doomed.exit_code(60) == 3, "worker crashed inside the batch (simulated crash, exit 3)")
    changes = scalar(
        "SELECT count(*) FROM product_changes WHERE source_ref = ANY(:refs)",
        refs=[f"webhook:{e}" for e in events],
    )
    check(changes == 0, "nothing of the crashed batch was committed (0 changes)")
    check(not events_processed(events), "events still PENDING — accepted, not lost")

    procs.append(start_worker(name="worker-2"))
    wait(lambda: events_processed(events), "a second worker re-claims the jobs after the lease", 60)
    attempts = sql(
        "SELECT DISTINCT j.attempts FROM job j JOIN inbox_event e ON j.ref_id = e.id::text "
        "WHERE e.event_id = ANY(:ids)",
        ids=events,
    )
    check({a for (a,) in attempts} == {2}, "each job ran twice (crashed attempt + successful retry)")
    changes = scalar(
        "SELECT count(*) FROM product_changes WHERE source_ref = ANY(:refs)",
        refs=[f"webhook:{e}" for e in events],
    )
    check(changes == 30, "exactly 30 changes — one per mutation, none twice")
    check(
        matches_mutations(mutations),
        "CDMS state = emulator state for the 30 products",
    )


def f2_api_crash_after_inbox_commit(procs: list[Process]) -> None:
    """F2: the API commits the event, then dies before answering; the sender retries."""
    procs += [start_emulator(), start_worker()]
    api = start_api({"FAULT_CRASH_AFTER_INBOX_COMMIT": "true"}, "api-doomed")
    procs.append(api)
    [mutation] = mutate(1, webhook=True)

    check(api.exit_code(30) == 3, "API died right after committing the event (no response sent)")
    callback = wait(
        lambda: next((c for c in callbacks_of([mutation]) if c.attempts >= 1), None),
        "sender records the failure",
        30,
    )
    check(callback.status == "PENDING", "sender saw a failed delivery and will retry")
    check(scalar("SELECT count(*) FROM inbox_event WHERE event_id = :e", e=callback.event_id) == 1,
          "the event is already stored (durable before the crash)")  # fmt: skip

    procs.append(start_api(name="api-2"))
    wait(lambda: callbacks_of([mutation])[0].status == "DELIVERED", "sender's retry delivered", 30)
    [callback] = callbacks_of([mutation])
    check(callback.last_status == 200, "the retry was answered 200 duplicate")
    check(scalar("SELECT count(*) FROM inbox_event WHERE event_id = :e", e=callback.event_id) == 1,
          "still one inbox row for the event")  # fmt: skip
    wait(lambda: events_processed([callback.event_id]), "event processed")
    changes = scalar(
        "SELECT count(*) FROM product_changes WHERE source_ref = :r", r=f"webhook:{callback.event_id}"
    )
    check(changes == 1, "exactly one change")
    check(matches_mutations([mutation]), "CDMS state = emulator state")


def f3_inventory_down_during_polling(procs: list[Process]) -> None:
    """F3: the inventory service is down during a poll; the next poll after recovery catches up."""
    procs += [start_emulator(), start_api(), start_worker()]
    admin("PUT", "/_admin/faults", mode="down")
    mutations = mutate(10, webhook=False)  # only polling can see these

    def poll_now() -> Any:
        job = http.post(f"{API}/api/v1/polling/run").json()["jobId"]
        wait(
            lambda: scalar("SELECT status FROM job WHERE id = :j", j=job) == "DONE", "manual poll handled", 90
        )
        return sql("SELECT id, status, updated, error FROM poll_run ORDER BY id DESC LIMIT 1")[0]

    failed = poll_now()
    check(failed.status == "FAILED" and "after 3 attempts" in (failed.error or ""),
          f"poll run {failed.id} FAILED after 3 attempts with backoff (inventory down)")  # fmt: skip
    check(not matches_mutations(mutations), "the 10 changes are not in CDMS yet")

    admin("PUT", "/_admin/faults", mode="ok")
    recovered = poll_now()
    check(recovered.status == "SUCCEEDED" and recovered.updated >= 10,
          f"poll run {recovered.id} after recovery SUCCEEDED with {recovered.updated} updates")  # fmt: skip
    check(matches_mutations(mutations), "CDMS caught up with the emulator")


def f4_database_down(procs: list[Process]) -> None:
    """F4: PostgreSQL unreachable for CDMS (TCP proxy cut) while webhooks arrive; then it comes back."""
    proxy = start_proxy()
    procs += [proxy, start_emulator()]
    via_proxy = {"DB_HOST": "127.0.0.1", "DB_PORT": str(PROXY_PORT)}
    procs += [start_api(via_proxy), start_worker(via_proxy)]
    warmup = mutate(3, webhook=True)
    wait(lambda: events_processed([c.event_id for c in callbacks_of(warmup)]) and callbacks_of(warmup),
         "warm-up events processed through the proxy")  # fmt: skip

    proxy.kill()
    procs.remove(proxy)
    ready = http.get(f"{API}/ready")
    check(ready.status_code == 503, "database cut: /ready answers 503")
    mutations = mutate(20, webhook=True)
    wait(
        lambda: any(c.last_status == 503 for c in callbacks_of(mutations)),
        "webhooks answered 503 during the outage",
        30,
    )
    time.sleep(5)
    check(all(c.status == "PENDING" for c in callbacks_of(mutations)),
          "sender keeps all 20 events and retries (nothing acknowledged, nothing lost)")  # fmt: skip

    procs.append(start_proxy())
    wait(
        lambda: all(c.status == "DELIVERED" for c in callbacks_of(mutations)),
        "all 20 delivered after recovery",
        90,
    )
    events = [c.event_id for c in callbacks_of(mutations)]
    wait(lambda: events_processed(events), "worker reconnected and processed them", 60)
    check(http.get(f"{API}/ready").status_code == 200, "/ready is 200 again")
    changes = scalar(
        "SELECT count(*) FROM product_changes WHERE source_ref = ANY(:refs)",
        refs=[f"webhook:{e}" for e in events],
    )
    check(changes == 20, "exactly 20 changes")
    check(matches_mutations(mutations), "CDMS state = emulator state")


def f5_host_restart(procs: list[Process]) -> None:
    """F5: every CDMS process is killed during a poll run with webhooks in flight, then all restart."""
    procs.append(start_emulator())
    config(poll_enabled=True, poll_interval_seconds=5)
    api, worker = start_api(), start_worker()
    procs += [api, worker]
    mutate(200, webhook=True)
    mutate(200, webhook=False)
    wait(
        lambda: scalar("SELECT count(*) FROM poll_run WHERE status = 'RUNNING'"),
        "a poll run in progress",
        30,
        0.1,
    )
    api.kill()
    worker.kill()
    stale = scalar("SELECT id FROM poll_run WHERE status = 'RUNNING' ORDER BY id DESC LIMIT 1")
    check(stale is not None, f"all CDMS processes killed during poll run {stale}")
    time.sleep(3)

    restarted_at = scalar("SELECT now()")
    procs += [start_api(name="api-2"), start_worker(name="worker-2")]
    wait(lambda: scalar("SELECT status FROM poll_run WHERE id = :i", i=stale) == "ABORTED",
         "the interrupted poll run is marked ABORTED", 60)  # fmt: skip
    wait(lambda: scalar("SELECT count(*) FROM vietful.callbacks WHERE status <> 'DELIVERED'") == 0,
         "every webhook delivered", 90)  # fmt: skip
    succeeded_after = "SELECT count(*) FROM poll_run WHERE status = 'SUCCEEDED' AND started_at > :t"
    wait(lambda: scalar(succeeded_after, t=restarted_at),
         "a full poll run succeeded after the restart", 90)  # fmt: skip
    wait(
        lambda: scalar("SELECT count(*) FROM job WHERE status IN ('PENDING', 'RUNNING')") == 0,
        "queue drained",
        60,
    )
    wait(matches_emulator, "all 5,000 products equal the emulator's", 60, 2)
    check(True, "CDMS holds exactly the emulator's catalogue (5,000 fingerprints compared)")


SCENARIOS: dict[str, Callable[[list[Process]], None]] = {
    "F1": f1_worker_killed_mid_batch,
    "F2": f2_api_crash_after_inbox_commit,
    "F3": f3_inventory_down_during_polling,
    "F4": f4_database_down,
    "F5": f5_host_restart,
}


def main() -> int:
    chosen = [name.upper() for name in sys.argv[1:]] or list(SCENARIOS)
    if settings.inventory_api_token is None or settings.webhook_secret is None:
        print("INVENTORY_API_TOKEN and WEBHOOK_SECRET must be set in .env", file=sys.stderr)
        return 2
    saved = sql("SELECT poll_enabled, poll_interval_seconds FROM sync_config WHERE id = 1")[0]
    results: dict[str, str] = {}
    try:
        for name in chosen:
            print(f"\n{SCENARIOS[name].__doc__}")
            procs: list[Process] = []
            config(poll_enabled=False)
            try:
                # Emulator in a known state: no faults, fast sender retries, subscribed to this CDMS.
                sql_setup = ("UPDATE vietful.settings SET fault_mode = 'ok', webhook_endpoint = :e, "
                             "callback_duplicate_rate = 0, callback_max_retries = 50, "
                             "callback_retry_delay_ms = 500, "
                             "callback_concurrency = 4 WHERE id = 1")  # fmt: skip
                with db.begin() as conn:
                    conn.execute(text(sql_setup), {"e": ENDPOINT})
                SCENARIOS[name](procs)
                for process in procs:
                    process.kill()
                invariants()
                results[name] = "PASS"
            except Failed as exc:
                print(f"    ✗ {exc}")
                results[name] = "FAIL"
            finally:
                for process in procs:
                    process.stop()
    finally:
        config(poll_enabled=saved.poll_enabled, poll_interval_seconds=saved.poll_interval_seconds)
        db.dispose()
    print("\nsummary: " + "  ".join(f"{name} {result}" for name, result in results.items()))
    return 0 if all(result == "PASS" for result in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
