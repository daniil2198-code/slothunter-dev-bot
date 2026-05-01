"""Single-user whitelist middleware.

The whole security model: if the Telegram ``user_id`` doesn't match
``settings.allowed_user_id``, the update is dropped silently — no error
message, no acknowledgement. From a stranger's POV the bot looks dead.

Why ``user_id`` and not ``username``: usernames can be transferred or
deleted; numeric ids are stable for the lifetime of the account. Anyone
who steals the bot token still can't actually use the bot — they'd need
to make ``ALLOWED_USER_ID`` think they're the right user, which Telegram
won't let them do.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.config import settings
from app.logging import get_logger

log = get_logger(__name__)


class AllowedUserMiddleware(BaseMiddleware):
    """Drops any update whose ``from_user.id`` isn't the whitelisted one.

    Applies to both ``Message`` and ``CallbackQuery`` updates (the two
    things this bot processes). Anything else is passed through — channel
    posts, my_chat_member, etc. shouldn't reach our handlers anyway.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user_id = _user_id_of(event)
        if user_id is None:
            # Unknown event shape — let it through; downstream handlers
            # will either consume it or ignore it.
            return await handler(event, data)

        if user_id != settings.allowed_user_id:
            # Silent drop — no reply, no log spam unless we're debugging.
            log.debug(
                "auth_drop",
                user_id=user_id,
                event_type=type(event).__name__,
            )
            return None

        return await handler(event, data)


def _user_id_of(event: TelegramObject) -> int | None:
    user = getattr(event, "from_user", None)
    if user is None and isinstance(event, CallbackQuery):
        user = event.from_user
    if user is None and isinstance(event, Message):
        user = event.from_user
    return user.id if user else None
