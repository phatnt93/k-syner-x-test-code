"""apply_observation on real PostgreSQL: outcomes, ordering and concurrency (docs/solution.md sections 3-8).

Each test uses its own partner_sku, so tests do not interfere on the shared test database.
"""

import asyncio
import uuid
from collections import Counter
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Row, select
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
