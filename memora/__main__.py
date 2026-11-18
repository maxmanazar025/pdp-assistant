"""CLI entry point:  python -m memora <bot|worker|all|migrate|sync|healthcheck>."""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

from memora import timeutil
from memora.config import get_settings
from memora.logging import setup_logging

log = logging.getLogger("memora")


def _migrate() -> None:
    from alembic import command
    from alembic.config import Config

    root = Path(__file__).resolve().parent.parent
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    command.upgrade(config, "head")


async def _bot_heartbeat() -> None:
    from memora.db import kv_set, session_scope

    while True:
        try:
            async with session_scope() as session:
                await kv_set(session, "bot:heartbeat", timeutil.iso(timeutil.now()))
        except Exception:  # noqa: BLE001
            log.warning("bot heartbeat failed", exc_info=True)
        await asyncio.sleep(30)


async def _run(mode: str) -> None:
    from memora.app import build_app
    from memora.bot.runner import run_bot
    from memora.db import dispose_engine
    from memora.worker.runner import run_worker

    app = build_app()
    tasks: list[asyncio.Task] = []
    if mode in ("bot", "all"):
        tasks += [asyncio.create_task(run_bot(app), name="bot"), asyncio.create_task(_bot_heartbeat(), name="hb")]
    if mode in ("worker", "all"):
        tasks.append(asyncio.create_task(run_worker(app), name="worker"))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    stopper = asyncio.create_task(stop.wait(), name="stop")
    exit_code = 0
    try:
        done, _ = await asyncio.wait([*tasks, stopper], return_when=asyncio.FIRST_COMPLETED)
        for finished in done:
            if finished is stopper or finished.cancelled():
                continue
            # A component returning or raising is always abnormal: report it and let the
            # container restart policy bring the process back.
            exit_code = 1
            error = finished.exception()
            log.error("component stopped unexpectedly", extra={"component": finished.get_name()}, exc_info=error)
            await app.notifier.alert_admin(f"{finished.get_name()} to'xtab qoldi", exc=error)
    finally:
        log.info("shutting down")
        for task in [*tasks, stopper]:
            task.cancel()
        await asyncio.gather(*tasks, stopper, return_exceptions=True)
        await app.aclose()
        await dispose_engine()
    if exit_code:
        raise SystemExit(exit_code)


async def _sync_once() -> None:
    from memora.app import build_app
    from memora.db import dispose_engine
    from memora.timetable.sync import sync_timetable

    app = build_app()
    try:
        report = await sync_timetable(app, announce=False)
        print(f"active={report.total_active} added={len(report.added)} removed={len(report.removed)} "
              f"changed={len(report.changed)} errors={report.errors} missing={report.missing_groups}")
    finally:
        await app.aclose()
        await dispose_engine()


async def _healthcheck(component: str) -> int:
    from memora.db import dispose_engine, kv_get, session_scope

    try:
        async with session_scope() as session:
            beat = await kv_get(session, f"{component}:heartbeat")
        if not beat:
            return 1
        age = (timeutil.now() - timeutil.parse_iso_local(beat)).total_seconds()
        return 0 if age < 150 else 1
    except Exception:  # noqa: BLE001
        return 1
    finally:
        await dispose_engine()


def main() -> None:
    parser = argparse.ArgumentParser(prog="memora", description="Memora OS")
    parser.add_argument("mode", choices=["bot", "worker", "all", "migrate", "sync", "healthcheck"])
    parser.add_argument("component", nargs="?", default="worker", choices=["bot", "worker"],
                        help="for healthcheck: which heartbeat to verify")
    args = parser.parse_args()
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_json)

    if args.mode == "migrate":
        _migrate()
    elif args.mode == "sync":
        asyncio.run(_sync_once())
    elif args.mode == "healthcheck":
        sys.exit(asyncio.run(_healthcheck(args.component)))
    else:
        asyncio.run(_run(args.mode))


if __name__ == "__main__":
    main()
