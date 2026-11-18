"""Background worker: leader election + periodic loops."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

from sqlalchemy import text

from memora.db import get_engine
from memora.timetable.sync import sync_timetable
from memora.worker import jobs

log = logging.getLogger(__name__)

ADVISORY_LOCK_KEY = 0x4D454D4F5241  # "MEMORA"
TICK_SECONDS = 20


async def _periodic(app, name: str, interval: float, job: Callable[[], Awaitable[None]],  # noqa: ANN001
                    align: bool = False) -> None:
    """Run ``job`` forever. Failures are logged + reported, never fatal to the loop."""
    while True:
        started = time.monotonic()
        try:
            await job()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("periodic job crashed", extra={"job": name})
            await app.notifier.alert_admin(f"Fon vazifasi xatosi: {name}", exc=exc)
        if align:  # fire on wall-clock boundaries (:00, :20, :40) for minute-accurate alerts
            delay = interval - (time.time() % interval)
        else:
            delay = max(1.0, interval - (time.monotonic() - started))
        await asyncio.sleep(delay)


async def run_worker(app) -> None:  # noqa: ANN001
    """Acquire the scheduler leadership lock, then run all loops until cancelled.

    A session-level Postgres advisory lock guarantees a single active scheduler even if
    two worker containers are started by mistake; the standby simply waits. The lock
    lives on a dedicated connection — if that connection dies, this process exits and the
    orchestrator restarts it (the dispatch ledger covers the hand-over window).
    """
    engine = get_engine()
    async with engine.connect() as lock_conn:
        lock_conn = await lock_conn.execution_options(isolation_level="AUTOCOMMIT")
        while True:
            acquired = (await lock_conn.execute(
                text("SELECT pg_try_advisory_lock(:k)"), {"k": ADVISORY_LOCK_KEY})).scalar()
            if acquired:
                break
            log.info("another worker holds the scheduler lock; standing by")
            await asyncio.sleep(15)
        log.info("worker started as scheduler leader")

        async def lock_watchdog() -> None:
            while True:
                await asyncio.sleep(30)
                await lock_conn.execute(text("SELECT 1"))  # raises if the lock connection is gone

        settings = app.settings
        loops = [
            asyncio.create_task(_periodic(app, "tick", TICK_SECONDS, lambda: jobs.tick(app), align=True), name="tick"),
            asyncio.create_task(_periodic(app, "timetable_sync", settings.timetable_sync_minutes * 60,
                                          lambda: _sync(app)), name="timetable"),
            asyncio.create_task(_periodic(app, "google_sync", settings.google_sync_minutes * 60,
                                          lambda: jobs.google_sync(app)), name="google"),
            asyncio.create_task(_periodic(app, "maintenance", 3600, lambda: jobs.maintenance(app)), name="maintenance"),
            asyncio.create_task(lock_watchdog(), name="lock-watchdog"),
        ]
        try:
            done, _ = await asyncio.wait(loops, return_when=asyncio.FIRST_EXCEPTION)
            for finished in done:
                finished.result()  # re-raise (only the watchdog can fail) -> process exits -> restart
        finally:
            for loop_task in loops:
                loop_task.cancel()
            await asyncio.gather(*loops, return_exceptions=True)


async def _sync(app) -> None:  # noqa: ANN001
    await sync_timetable(app, announce=True)
