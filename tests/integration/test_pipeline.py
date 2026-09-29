"""apply_observation on real PostgreSQL: outcomes, ordering and concurrency (docs/solution.md sections 3-8).

Each test uses its own partner_sku, so tests do not interfere on the shared test database.
"""

import asyncio
import random
import uuid
from collections import Counter
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Row, event, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from cdms.core.fingerprint import fingerprint
from cdms.core.pipeline import (
    ApplyResult,
    Outcome,
    ProductObservation,
    Source,
    apply_observation,
    apply_observations,
)
from cdms.db.models import Product, ProductChange

T0 = datetime(2026, 9, 28, 10, 0, tzinfo=UTC)

Sessions = async_sessionmaker[AsyncSession]


def new_sku() -> str:
    return f"P-{uuid.uuid4().hex[:12]}"


def obs(
    sku: str,
    at: datetime = T0,
    source: Source = Source.POLLING,
    ref: str = "run-1:page-0",
    **fields: Any,
) -> ProductObservation:
    raw: dict[str, Any] = {
        "productId": 1,
        "sku": sku,
        "partnerSKU": sku,
        "productName": "Kẽm",
        "isActive": True,
        "units": ["PCS"],
        "categories": [{"categoryCode": "C01", "categoryName": "Raw"}],
    } | fields
    return ProductObservation.from_raw(raw, observed_at=at, source=source, source_ref=ref)


@pytest.fixture(scope="session")
def sessions(db_engine: AsyncEngine) -> Sessions:
    return async_sessionmaker(db_engine, expire_on_commit=False)


async def apply(sessions: Sessions, o: ProductObservation) -> ApplyResult:
    async with sessions() as session, session.begin():
        return await apply_observation(session, o)


async def changes_of(engine: AsyncEngine, sku: str) -> list[Row[Any]]:
    async with engine.connect() as conn:
        result = await conn.execute(
            select(ProductChange).where(ProductChange.partner_sku == sku).order_by(ProductChange.version)
        )
        return list(result.all())


async def state_of(engine: AsyncEngine, sku: str) -> Row[Any]:
    async with engine.connect() as conn:
        return (await conn.execute(select(Product).where(Product.partner_sku == sku))).one()


async def assert_history_is_consistent(engine: AsyncEngine, sku: str) -> None:
    """The invariants of scripts/verify_invariants.sql, for one product."""
    changes = await changes_of(engine, sku)
    state = await state_of(engine, sku)
    assert [c.version for c in changes] == list(range(1, len(changes) + 1))
    prev = None
    for change in changes:
        assert change.prev_fingerprint == prev
        prev = change.fingerprint
    assert state.version == changes[-1].version
    assert state.fingerprint == changes[-1].fingerprint


# --- outcomes ---------------------------------------------------------------------------------------------


async def test_new_product_is_created(sessions: Sessions, db_engine: AsyncEngine) -> None:
    sku = new_sku()
    o = obs(sku, productName="  Kẽm ")

    assert await apply(sessions, o) == ApplyResult(sku, Outcome.CREATED, 1)

    state = await state_of(db_engine, sku)
    assert (state.product_name, state.units, state.version, state.observed_at) == ("Kẽm", ["PCS"], 1, T0)
    assert state.fingerprint == fingerprint(o.product)
    [change] = await changes_of(db_engine, sku)
    assert (change.change_type, change.prev_fingerprint, change.diff) == ("CREATED", None, {})
    assert (change.source, change.source_ref, change.data) == ("POLLING", "run-1:page-0", o.product)


async def test_same_state_again_is_unchanged(sessions: Sessions, db_engine: AsyncEngine) -> None:
    sku = new_sku()
    await apply(sessions, obs(sku))

    # Same state, different order / spacing / transport, from another mechanism.
    again = obs(sku, T0, Source.WEBHOOK, "evt-1", units=[" PCS", "PCS"], productName="Kẽm ")
    assert await apply(sessions, again) == ApplyResult(sku, Outcome.UNCHANGED, 1)
    assert len(await changes_of(db_engine, sku)) == 1


async def test_changed_state_is_updated_with_diff(sessions: Sessions, db_engine: AsyncEngine) -> None:
    sku = new_sku()
    await apply(sessions, obs(sku))
    later = T0 + timedelta(minutes=1)

    result = await apply(
        sessions, obs(sku, later, Source.EXCEL, "upload-7:row-3", isActive=False, units="PCS,BOX")
    )

    assert result == ApplyResult(sku, Outcome.UPDATED, 2)
    v1, v2 = await changes_of(db_engine, sku)
    assert v2.change_type == "UPDATED"
    assert v2.prev_fingerprint == v1.fingerprint
    assert v2.diff == {"isActive": [True, False], "units": [["PCS"], ["BOX", "PCS"]]}
    assert (v2.source, v2.source_ref, v2.observed_at) == ("EXCEL", "upload-7:row-3", later)
    state = await state_of(db_engine, sku)
    assert (state.version, state.is_active, state.observed_at) == (2, False, later)
    await assert_history_is_consistent(db_engine, sku)


async def test_product_id_alone_is_not_a_change(sessions: Sessions, db_engine: AsyncEngine) -> None:
    sku = new_sku()
    await apply(sessions, obs(sku, productId=None))  # e.g. first seen in an Excel file without productId

    result = await apply(sessions, obs(sku, T0 + timedelta(seconds=1), productId=555))

    assert result.outcome == Outcome.UNCHANGED
    assert (await state_of(db_engine, sku)).product_id == 555  # backfilled, but no change recorded
    assert len(await changes_of(db_engine, sku)) == 1


async def test_older_state_is_stale(sessions: Sessions, db_engine: AsyncEngine) -> None:
    sku = new_sku()
    await apply(sessions, obs(sku, T0, productName="A"))
    await apply(sessions, obs(sku, T0 + timedelta(minutes=1), productName="B"))

    # Webhook A redelivered after B: must not revert the product.
    assert await apply(sessions, obs(sku, T0, Source.WEBHOOK, "evt-a", productName="A")) == ApplyResult(
        sku, Outcome.STALE, 2
    )
    assert (await state_of(db_engine, sku)).product_name == "B"
    assert len(await changes_of(db_engine, sku)) == 2


async def test_unchanged_newer_observation_advances_observed_at(
    sessions: Sessions,
    db_engine: AsyncEngine,
) -> None:
    sku = new_sku()
    await apply(sessions, obs(sku, T0, productName="B"))
    updated_at = (await state_of(db_engine, sku)).updated_at
    poll_time = T0 + timedelta(minutes=5)

    assert (await apply(sessions, obs(sku, poll_time, productName="B"))).outcome == Outcome.UNCHANGED
    state = await state_of(db_engine, sku)
    assert state.observed_at == poll_time
    assert state.updated_at == updated_at  # not a change

    # A state from before the poll, delivered late, is older than what the poll confirmed.
    late = obs(sku, T0 + timedelta(minutes=3), Source.WEBHOOK, "evt-late", productName="A")
    assert (await apply(sessions, late)).outcome == Outcome.STALE


async def test_same_observed_at_different_state_applies_in_arrival_order(
    sessions: Sessions,
    db_engine: AsyncEngine,
) -> None:
    sku = new_sku()
    await apply(sessions, obs(sku, T0, productName="A"))
    assert (await apply(sessions, obs(sku, T0, productName="B"))).outcome == Outcome.UPDATED
    assert (await state_of(db_engine, sku)).product_name == "B"


async def test_rolled_back_transaction_leaves_nothing(
    sessions: Sessions,
    db_engine: AsyncEngine,
) -> None:
    sku = new_sku()
    async with sessions() as session:
        await apply_observation(session, obs(sku))
        await session.rollback()  # e.g. the worker crashed before marking its job done
    async with db_engine.connect() as conn:
        assert (await conn.execute(select(Product).where(Product.partner_sku == sku))).first() is None
    assert await changes_of(db_engine, sku) == []


async def test_naive_observed_at_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        obs(new_sku(), datetime(2026, 9, 28, 10, 0))


# --- batches ----------------------------------------------------------------------------------------------


async def test_batch_results_follow_input_order(sessions: Sessions, db_engine: AsyncEngine) -> None:
    a, b = sorted([new_sku(), new_sku()])
    later = T0 + timedelta(seconds=1)
    batch = [
        obs(b, ref="upload-1:row-2"),
        obs(a, ref="upload-1:row-3"),
        obs(a, ref="upload-1:row-4"),  # duplicate row in the same file
        obs(a, later, ref="upload-1:row-5", color="Red"),
    ]

    async with sessions() as session, session.begin():
        results = await apply_observations(session, batch)

    assert [(r.partner_sku, r.outcome, r.version) for r in results] == [
        (b, Outcome.CREATED, 1),
        (a, Outcome.CREATED, 1),
        (a, Outcome.UNCHANGED, 1),
        (a, Outcome.UPDATED, 2),
    ]
    assert [c.source_ref for c in await changes_of(db_engine, a)] == ["upload-1:row-3", "upload-1:row-5"]


# --- concurrency (requirements section 8.2) ---------------------------------------------------------------

WRITERS = 50


@pytest.fixture
async def wide_sessions(migrated_db: str) -> AsyncIterator[Sessions]:
    """One connection per concurrent writer, so all of them really hit the database at the same time."""
    engine = create_async_engine(migrated_db, pool_size=WRITERS, max_overflow=0)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def run_concurrently(
    sessions: Sessions,
    observations: list[ProductObservation],
) -> Counter[Outcome]:
    start = asyncio.Event()

    async def writer(o: ProductObservation) -> Outcome:
        await start.wait()
        return (await apply(sessions, o)).outcome

    tasks = [asyncio.create_task(writer(o)) for o in observations]
    start.set()
    return Counter(await asyncio.gather(*tasks))


async def test_concurrent_same_new_state_creates_once(
    wide_sessions: Sessions,
    db_engine: AsyncEngine,
) -> None:
    sku = new_sku()
    sources = [Source.POLLING, Source.WEBHOOK, Source.EXCEL]
    observations = [obs(sku, T0, sources[i % 3], f"ref-{i}") for i in range(WRITERS)]

    outcomes = await run_concurrently(wide_sessions, observations)

    assert outcomes == {Outcome.CREATED: 1, Outcome.UNCHANGED: WRITERS - 1}
    assert len(await changes_of(db_engine, sku)) == 1


async def test_concurrent_same_changed_state_updates_once(
    wide_sessions: Sessions,
    db_engine: AsyncEngine,
) -> None:
    sku = new_sku()
    await apply(wide_sessions, obs(sku, T0, productName="old"))
    later = T0 + timedelta(minutes=1)
    observations = [obs(sku, later, Source.WEBHOOK, f"evt-{i}", productName="new") for i in range(WRITERS)]

    outcomes = await run_concurrently(wide_sessions, observations)

    assert outcomes == {Outcome.UPDATED: 1, Outcome.UNCHANGED: WRITERS - 1}
    assert [c.version for c in await changes_of(db_engine, sku)] == [1, 2]
    await assert_history_is_consistent(db_engine, sku)


async def test_concurrent_different_states_keep_a_consistent_history(
    wide_sessions: Sessions,
    db_engine: AsyncEngine,
) -> None:
    sku = new_sku()
    observations = [
        obs(sku, T0 + timedelta(seconds=i), ref=f"ref-{i}", productName=f"name-{i}") for i in range(WRITERS)
    ]

    outcomes = await run_concurrently(wide_sessions, observations)

    changes = await changes_of(db_engine, sku)
    assert outcomes[Outcome.CREATED] == 1
    assert outcomes[Outcome.CREATED] + outcomes[Outcome.UPDATED] == len(changes)
    assert outcomes[Outcome.UNCHANGED] == 0  # every state is distinct
    # Whatever the arrival order, observed_at never goes backwards and the newest state wins.
    assert [c.observed_at for c in changes] == sorted(c.observed_at for c in changes)
    assert (await state_of(db_engine, sku)).product_name == f"name-{WRITERS - 1}"
    await assert_history_is_consistent(db_engine, sku)


# --- batch fast path (docs/project-info/ISSUES.md I-01) ---------------------------------------------------


def mixed_batch(prefix: str) -> tuple[list[ProductObservation], list[ProductObservation]]:
    """(setup, batch) covering every outcome, duplicates included, for keys starting with `prefix`."""
    k = [f"{prefix}-{i}" for i in range(7)]
    t1, t2 = T0 + timedelta(minutes=1), T0 + timedelta(minutes=2)
    setup = [obs(key, T0, productName="A") for key in k[:5]] + [obs(k[5], t2, productName="A")]
    batch = [
        obs(k[0], t1, productName="A"),  # unchanged, newer → observed_at advanced
        obs(k[1], T0, productName="A"),  # unchanged, same time
        obs(k[2], t1, productName="B"),  # changed
        obs(k[5], t1, productName="B"),  # stale
        obs(k[6], t1, productName="A"),  # new
        obs(k[3], t1, productName="B"),  # duplicate key: changed, back, again unchanged
        obs(k[3], t1, productName="A"),
        obs(k[3], t2, productName="A"),
        obs(k[4], t1, productId=77, productName="A"),  # unchanged, fills product_id
    ]
    return setup, batch


async def test_batch_fast_path_matches_one_by_one(sessions: Sessions, db_engine: AsyncEngine) -> None:
    prefix_fast, prefix_slow = new_sku(), new_sku()
    setup_fast, batch_fast = mixed_batch(prefix_fast)
    setup_slow, batch_slow = mixed_batch(prefix_slow)
    for o in setup_fast + setup_slow:
        await apply(sessions, o)

    async with sessions() as session, session.begin():
        fast = await apply_observations(session, batch_fast)
    async with sessions() as session, session.begin():
        order = sorted(range(len(batch_slow)), key=lambda i: batch_slow[i].partner_sku)
        by_index = {i: await apply_observation(session, batch_slow[i]) for i in order}
        slow = [by_index[i] for i in range(len(batch_slow))]

    def strip(key: str) -> str:
        return key.split("-")[-1]

    assert [(strip(r.partner_sku), r.outcome, r.version) for r in fast] == [
        (strip(r.partner_sku), r.outcome, r.version) for r in slow
    ]
    assert [r.outcome for r in fast] == [
        Outcome.UNCHANGED, Outcome.UNCHANGED, Outcome.UPDATED, Outcome.STALE, Outcome.CREATED,
        Outcome.UPDATED, Outcome.UPDATED, Outcome.UNCHANGED, Outcome.UNCHANGED,
    ]  # fmt: skip
    for i in range(7):
        fast_state = await state_of(db_engine, f"{prefix_fast}-{i}")
        slow_state = await state_of(db_engine, f"{prefix_slow}-{i}")
        # (fingerprints differ only because `obs` copies the key into `sku`)
        columns = ("version", "product_name", "observed_at", "product_id")
        assert [getattr(fast_state, c) for c in columns] == [getattr(slow_state, c) for c in columns]
        await assert_history_is_consistent(db_engine, f"{prefix_fast}-{i}")


async def test_unchanged_page_costs_two_statements(sessions: Sessions, db_engine: AsyncEngine) -> None:
    keys = [new_sku() for _ in range(50)]
    async with sessions() as session, session.begin():
        await apply_observations(session, [obs(key, T0) for key in keys])
    statements: list[str] = []

    def count(*args: Any) -> None:
        statements.append(args[2])

    event.listen(db_engine.sync_engine, "before_cursor_execute", count)
    try:
        async with sessions() as session, session.begin():
            results = await apply_observations(session, [obs(key, T0 + timedelta(minutes=1)) for key in keys])
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", count)

    assert {r.outcome for r in results} == {Outcome.UNCHANGED}
    data_statements = [s for s in statements if not s.lstrip().upper().startswith(("BEGIN", "COMMIT"))]
    assert len(data_statements) == 2  # one locking SELECT + one UPDATE … FROM unnest(…) for the page
    assert {(await state_of(db_engine, key)).observed_at for key in keys} == {T0 + timedelta(minutes=1)}


async def test_concurrent_batches_update_each_product_once(
    wide_sessions: Sessions, db_engine: AsyncEngine
) -> None:
    keys = [new_sku() for _ in range(30)]
    async with wide_sessions() as session, session.begin():
        await apply_observations(session, [obs(key, T0, productName="old") for key in keys])
    later = T0 + timedelta(minutes=1)

    async def batch(seed: int) -> list[ApplyResult]:
        shuffled = keys[:]
        random.Random(seed).shuffle(shuffled)
        async with wide_sessions() as session, session.begin():
            return await apply_observations(session, [obs(key, later, productName="new") for key in shuffled])

    results = [
        r for batch_results in await asyncio.gather(*(batch(i) for i in range(10))) for r in batch_results
    ]

    assert Counter(r.outcome for r in results) == {Outcome.UPDATED: 30, Outcome.UNCHANGED: 270}
    for key in keys:
        assert [c.version for c in await changes_of(db_engine, key)] == [1, 2]


async def test_advancing_observed_at_keeps_known_product_id(
    sessions: Sessions, db_engine: AsyncEngine
) -> None:
    keys = [new_sku(), new_sku()]
    async with sessions() as session, session.begin():
        await apply_observations(session, [obs(keys[0], T0, productId=9), obs(keys[1], T0, productId=None)])

    later = T0 + timedelta(minutes=1)
    async with sessions() as session, session.begin():  # every productId in the array is NULL
        results = await apply_observations(session, [obs(key, later, productId=None) for key in keys])

    assert {r.outcome for r in results} == {Outcome.UNCHANGED}
    assert [(await state_of(db_engine, key)).product_id for key in keys] == [9, None]
    assert {(await state_of(db_engine, key)).observed_at for key in keys} == {later}
