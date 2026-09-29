"""CDMS worker process: the poll scheduler (C-04) and the job runner (C-05) side by side.

    python -m cdms.worker              # run until Ctrl+C
    python -m cdms.worker --poll-once  # one manual poll run, then exit

The schedule is read from `sync_config` on every tick, so changing the interval or disabling polling takes
effect without a restart. Several workers may run: the poll's advisory lock lets only one scan at a time.
"""

import argparse
import asyncio
import contextlib
import logging
import sys
import time

from sqlalchemy.ext.asyncio import AsyncEngine

from cdms.config import get_settings
from cdms.ingestion.polling import Trigger, load_config, run_poll
from cdms.jobs.runner import default_worker_id, run_jobs

log = logging.getLogger("cdms.worker")

TICK_S = 1.0
ERROR_BACKOFF_S = 5.0  # e.g. database down: retry the tick later instead of spinning


async def run_scheduler(engine: AsyncEngine, stop: asyncio.Event) -> None:
    next_poll = time.monotonic()
    while not stop.is_set():
        try:
            config = await load_config(engine)
            if config.poll_enabled and time.monotonic() >= next_poll:
                # Fixed rate from the start of a run; a run longer than the interval is followed immediately.
                next_poll = time.monotonic() + config.poll_interval_seconds
                await run_poll(engine, trigger=Trigger.SCHEDULE)
            delay = TICK_S
        except Exception:
            log.exception("scheduler tick failed; retrying in %.0fs", ERROR_BACKOFF_S)
            delay = ERROR_BACKOFF_S
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=delay)


async def _main(poll_once: bool) -> int:
    from cdms.db.session import engine  # imported here so `--help` works without a database

    try:
        if poll_once:
            result = await run_poll(engine, trigger=Trigger.MANUAL)
            if result is None:
                log.warning("another poll run is in progress; nothing done")
                return 1
            log.info("poll run %d: %s", result.run_id, result.status)
            return 0 if result.error is None else 1
        worker_id = default_worker_id()
        log.info("worker %s started (Ctrl+C to stop)", worker_id)
        stop = asyncio.Event()
        await asyncio.gather(run_scheduler(engine, stop), run_jobs(engine, stop, worker_id))
        return 0
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m cdms.worker", description=__doc__.split("\n\n")[0])
    parser.add_argument("--poll-once", action="store_true", help="run one manual poll and exit")
    args = parser.parse_args()
    logging.basicConfig(
        level=get_settings().log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    loop_factory = None
    if sys.platform == "win32":  # async psycopg needs a selector loop on Windows (cdms.loop)
        from cdms.loop import selector_loop

        loop_factory = selector_loop
    try:
        sys.exit(asyncio.run(_main(args.poll_once), loop_factory=loop_factory))
    except KeyboardInterrupt:
        log.info("worker stopped")


if __name__ == "__main__":
    main()
