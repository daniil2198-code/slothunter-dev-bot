"""APScheduler bootstrap — currently runs a single daily digest job.

Lives in the bot process: same event loop, no extra container/cron
required. We pin the timezone to Europe/Minsk because the user is
local. The scheduler is started/stopped by ``app.main`` next to the
aiogram dispatcher.

If ``settings.digest_time`` is empty the scheduler still spins up but
without any jobs — keeps the wiring simple and lets the user enable
the digest later by setting the env var and restarting.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.config import settings
from app.digest import send_digest
from app.logging import get_logger
from app.triggers import process_pending_triggers

if TYPE_CHECKING:
    from aiogram import Bot

log = get_logger(__name__)

TZ = "Europe/Minsk"


def build_scheduler(bot: Bot) -> AsyncIOScheduler:
    """Construct (but don't start) the scheduler.

    Caller starts it after the dispatcher is ready and stops it on
    shutdown — see ``app.main``.
    """
    sched = AsyncIOScheduler(timezone=TZ)

    digest_time = settings.digest_time.strip()
    if not digest_time:
        log.info("scheduler_no_digest", reason="DIGEST_TIME unset")
        return sched

    try:
        hh, mm = (int(p) for p in digest_time.split(":", 1))
    except ValueError:
        log.error("scheduler_bad_digest_time", value=digest_time)
        return sched

    async def _fire() -> None:
        log.info("digest_fire")
        try:
            await send_digest(_make_sender(bot))
        except Exception as e:  # noqa: BLE001 — never crash the scheduler
            log.exception("digest_failed", error=str(e))
            # Last-ditch: tell the user it failed instead of going silent.
            with contextlib.suppress(Exception):
                await bot.send_message(
                    settings.allowed_user_id,
                    f"⚠️ Утренний дайджест упал: {e}",
                )

    sched.add_job(
        _fire,
        CronTrigger(hour=hh, minute=mm, timezone=TZ),
        id="daily-digest",
        name=f"daily digest @{hh:02d}:{mm:02d} {TZ}",
        replace_existing=True,
    )
    log.info("scheduler_digest_armed", hour=hh, minute=mm, tz=TZ)

    # ─────────── File-based triggers (M3.4b) ───────────
    # Poll state_dir/triggers/ once a minute. Other processes
    # (currently slot-hunter's deploy.sh) drop *.txt files there to
    # ask the bot to run a Claude turn. See app/triggers.py.
    async def _process_triggers() -> None:
        try:
            await process_pending_triggers(bot)
        except Exception:  # noqa: BLE001
            log.exception("trigger_loop_crashed")

    sched.add_job(
        _process_triggers,
        IntervalTrigger(minutes=1),
        id="trigger-watcher",
        name="trigger-watcher: drain state_dir/triggers/",
        replace_existing=True,
        # Skip a tick if the previous run is still going — avoids
        # piling up runs when a turn takes longer than the interval.
        max_instances=1,
        coalesce=True,
    )
    log.info("scheduler_trigger_watcher_armed")

    return sched


def _make_sender(bot: Bot):  # type: ignore[no-untyped-def]  # closure factory
    async def _send(text: str) -> None:
        # parse_mode="HTML" matches the format build_digest_html produces.
        await bot.send_message(settings.allowed_user_id, text, parse_mode="HTML")

    return _send
