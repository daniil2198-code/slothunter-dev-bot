"""Entrypoint — long-poll Telegram, dispatch to handlers, run scheduler."""

from __future__ import annotations

import asyncio
import contextlib

from app.bot import build_bot, build_dispatcher
from app.logging import configure_logging, get_logger
from app.scheduler import build_scheduler
from app.triggers import ensure_trigger_dir


async def amain() -> None:
    configure_logging()
    log = get_logger(__name__)

    bot = build_bot()
    dp = build_dispatcher()
    scheduler = build_scheduler(bot)

    # Make sure the trigger queue dir exists before either the
    # scheduler watcher or any external producer (deploy.sh) tries to
    # use it.
    ensure_trigger_dir()

    me = await bot.get_me()
    log.info("bot_started", username=me.username, id=me.id)

    scheduler.start()
    try:
        # ``handle_as_tasks=True`` runs every update in its own asyncio
        # task instead of awaiting them sequentially. Without this, a
        # long-running Claude turn (3+ minutes is normal for browser
        # tests) would block ``/reset`` and ``/cancel`` updates from
        # ever reaching their handlers — the user is locked out until
        # the turn finishes. With the flag, control commands always
        # respond even mid-turn.
        await dp.start_polling(
            bot,
            allowed_updates=["message", "callback_query"],
            handle_as_tasks=True,
        )
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()


def main() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(amain())


if __name__ == "__main__":
    main()
