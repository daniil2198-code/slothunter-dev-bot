"""Entrypoint — long-poll Telegram, dispatch to handlers, run scheduler."""

from __future__ import annotations

import asyncio
import contextlib

from app.bot import build_bot, build_dispatcher
from app.logging import configure_logging, get_logger
from app.scheduler import build_scheduler


async def amain() -> None:
    configure_logging()
    log = get_logger(__name__)

    bot = build_bot()
    dp = build_dispatcher()
    scheduler = build_scheduler(bot)

    me = await bot.get_me()
    log.info("bot_started", username=me.username, id=me.id)

    scheduler.start()
    try:
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()


def main() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(amain())


if __name__ == "__main__":
    main()
