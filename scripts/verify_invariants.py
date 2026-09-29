"""Check the Change Database after a test / load / failure run (docs/testing.md).

    python scripts/verify_invariants.py                         # SQL invariants (verify_invariants.sql)
    python scripts/verify_invariants.py --wait                  # first wait until the queue is drained
    python scripts/verify_invariants.py --wait --prefix LTabc-  # + final state of a k6 run's products

`--prefix`: for every product whose partnerSKU starts with it, the newest state CDMS accepted (the inbox item
with the latest event timestamp) must be exactly the stored state — no lost change, no older state winning.
Exit code 1 when anything is violated.
"""

import argparse
import asyncio
import sys
import time
from pathlib import Path
from typing import Any

from sqlalchemy import text

from cdms.core.canonical import InvalidProductError, canonical_product
from cdms.core.fingerprint import fingerprint
from cdms.db.session import engine

SQL = Path(__file__).with_name("verify_invariants.sql")


async def wait_for_drain(timeout_s: float) -> float:
    started = time.monotonic()
    last_print = 0.0
    async with engine.connect() as conn:
        while True:
            backlog = (
                await conn.execute(text("SELECT count(*) FROM job WHERE status IN ('PENDING', 'RUNNING')"))
            ).scalar_one()
            await conn.commit()
            elapsed = time.monotonic() - started
            if backlog == 0:
                return elapsed
            if elapsed - last_print >= 5:
                print(f"  waiting: {backlog} jobs in the queue ({elapsed:.0f} s)")
                last_print = elapsed
            if elapsed > timeout_s:
                raise TimeoutError(f"queue not drained after {timeout_s:.0f} s ({backlog} jobs left)")
            await asyncio.sleep(0.5)


async def sql_checks() -> int:
    failed = 0
    # Drop comment lines before splitting: a comment may contain ";" too.
    lines = SQL.read_text(encoding="utf-8").splitlines()
    code = "\n".join(line for line in lines if not line.lstrip().startswith("--"))
    async with engine.connect() as conn:
        for body in filter(str.strip, code.split(";")):
            name, violations = (await conn.execute(text(body))).one()
            print(f"  {'ok  ' if violations == 0 else 'FAIL'} {name}: {violations}")
            failed += violations != 0
    return failed


async def expected_state_check(prefix: str) -> int:
    async with engine.connect() as conn:
        events = (
            await conn.execute(
                text(
                    "SELECT event_ts, event_id, payload FROM inbox_event "
                    "WHERE event_type = 'PRODUCT_UPSERTED' AND payload->'items'->0->>'partnerSKU' LIKE :p"
                ),
                {"p": prefix.replace("%", r"\%").replace("_", r"\_") + "%"},
            )
        ).all()
        stored = dict(
            (
                await conn.execute(
                    text("SELECT partner_sku, fingerprint FROM products WHERE partner_sku LIKE :p"),
                    {"p": prefix.replace("%", r"\%").replace("_", r"\_") + "%"},
                )
            ).all()
        )
        changes = (
            await conn.execute(
                text("SELECT count(*) FROM product_changes WHERE partner_sku LIKE :p"),
                {"p": prefix.replace("%", r"\%").replace("_", r"\_") + "%"},
            )
        ).scalar_one()

    newest: dict[str, tuple[Any, list[bytes]]] = {}  # sku -> (timestamp, fingerprints seen at that timestamp)
    distinct_states: dict[str, set[bytes]] = {}
    for event_ts, _event_id, payload in events:
        for item in payload.get("items") or []:
            try:
                product = canonical_product(item)
            except InvalidProductError:
                continue
            sku, fp = product["partnerSKU"], fingerprint(product)
            distinct_states.setdefault(sku, set()).add(fp)
            best = newest.get(sku)
            if best is None or event_ts > best[0]:
                newest[sku] = (event_ts, [fp])
            elif event_ts == best[0]:
                best[1].append(fp)

    wrong = [sku for sku, (_, fps) in newest.items() if stored.get(sku) not in fps]
    ambiguous = sum(1 for _, fps in newest.values() if len(set(fps)) > 1)
    states = sum(map(len, distinct_states.values()))
    print(
        f"  products {len(newest)} · accepted events {len(events)} · distinct states {states}"
        f" · stored changes {changes}"
    )
    print(
        f"  {'ok  ' if not wrong else 'FAIL'} final_state_is_newest_accepted: {len(wrong)} wrong"
        + (f" (e.g. {wrong[:5]})" if wrong else "")
        + (f", {ambiguous} ties" if ambiguous else "")
    )
    extra = changes - states
    print(f"  {'ok  ' if extra <= 0 else 'FAIL'} no_change_without_a_new_state: {max(extra, 0)} extra")
    return int(bool(wrong)) + int(extra > 0)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--wait", action="store_true", help="wait until no job is PENDING / RUNNING")
    parser.add_argument("--timeout", type=float, default=900, help="max seconds to wait for the drain")
    parser.add_argument("--prefix", help="partnerSKU prefix of a k6 run to check the expected final state")
    args = parser.parse_args()
    try:
        if args.wait:
            print("waiting for the job queue to drain…")
            drained = await wait_for_drain(args.timeout)
            print(f"  drained in {drained:.1f} s (from when this script started waiting)")
        print("invariants:")
        failed = await sql_checks()
        if args.prefix:
            print(f"expected final state ({args.prefix}*):")
            failed += await expected_state_check(args.prefix)
    finally:
        await engine.dispose()
    print("RESULT:", "all invariants hold" if not failed else f"{failed} check(s) FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    loop_factory = None
    if sys.platform == "win32":
        from cdms.loop import selector_loop

        loop_factory = selector_loop
    sys.exit(asyncio.run(main(), loop_factory=loop_factory))
