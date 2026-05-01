"""aiogram bot — handlers for / commands and free-form messages.

Single chat per user (single-user bot — only ``ALLOWED_USER_ID`` reaches
us). The chat_id is keyed off the message's ``chat.id``; a single
``ChatSession`` is held per chat_id in ``_sessions``.

Commands:
    /start        — say hello, show working directory
    /status       — current cwd, session id, pending approval
    /reset        — drop the conversation; next message starts fresh
    /cancel       — best-effort interrupt of the in-flight Claude turn
    /cd <path>    — switch Claude's working directory (must exist)
    /help         — list of commands

Anything else is forwarded to Claude as a user message.
"""

from __future__ import annotations

import html
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatAction, ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from app.auth import AllowedUserMiddleware
from app.chunker import chunk_text, is_too_long_for_messages
from app.claude_session import ChatSession, StreamedReply
from app.config import settings
from app.logging import get_logger
from app.permissions import PermissionBroker

log = get_logger(__name__)

router = Router()
_sessions: dict[int, ChatSession] = {}
_brokers: dict[int, PermissionBroker] = {}


def get_or_create_session(chat_id: int, bot: Bot) -> ChatSession:
    sess = _sessions.get(chat_id)
    if sess is None:
        broker = PermissionBroker(bot=bot, chat_id=chat_id)
        sess = ChatSession(chat_id=chat_id, broker=broker)
        _sessions[chat_id] = sess
        _brokers[chat_id] = broker
    return sess


# ─────────── Commands ───────────


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    sess = get_or_create_session(message.chat.id, message.bot)  # type: ignore[arg-type]
    text = (
        "👋 <b>Slot Hunter dev-bot</b>\n\n"
        f"Рабочая директория: <code>{html.escape(str(sess.state.cwd))}</code>\n"
        "Просто пиши задачу обычным сообщением — я её передам Claude Code.\n\n"
        "Команды: /status /reset /cancel /cd /help"
    )
    await message.answer(text, parse_mode=ParseMode.HTML)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    text = (
        "<b>Команды</b>\n\n"
        "<code>/status</code>  — текущая папка, session id, активность\n"
        "<code>/reset</code>   — забыть разговор, следующее сообщение начнёт с чистого\n"
        "<code>/compact</code> — ужать историю в summary, контекст продолжается\n"
        "<code>/cancel</code>  — прервать текущий ход (best-effort)\n"
        "<code>/cd &lt;path&gt;</code> — сменить рабочую директорию Claude\n"
        "<code>/help</code>    — это сообщение\n\n"
        "Любое другое сообщение — задача для Claude."
    )
    await message.answer(text, parse_mode=ParseMode.HTML)


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    sess = get_or_create_session(message.chat.id, message.bot)  # type: ignore[arg-type]
    sid = sess.state.session_id or "<i>(новая сессия)</i>"
    pending = "есть" if _brokers[message.chat.id]._pending else "нет"  # noqa: SLF001
    text = (
        f"📍 cwd: <code>{html.escape(str(sess.state.cwd))}</code>\n"
        f"🆔 session: <code>{html.escape(sid)}</code>\n"
        f"⏳ ожидание подтверждения: {pending}"
    )
    await message.answer(text, parse_mode=ParseMode.HTML)


@router.message(Command("reset"))
async def cmd_reset(message: Message) -> None:
    sess = get_or_create_session(message.chat.id, message.bot)  # type: ignore[arg-type]
    await sess.reset()
    await message.answer("🔄 Сессия сброшена.")


@router.message(Command("cancel"))
async def cmd_cancel(message: Message) -> None:
    sess = get_or_create_session(message.chat.id, message.bot)  # type: ignore[arg-type]
    sess.request_cancel()
    await message.answer("⏹ Прерываю — придёт результат того, что успел.")


@router.message(Command("compact"))
async def cmd_compact(message: Message) -> None:
    """Ask Claude to compress the conversation in place — keeps the same
    session_id but condenses earlier turns into a summary, freeing up the
    context window. Effectively the same as typing ``/compact`` in the
    Claude Code CLI."""
    sess = get_or_create_session(message.chat.id, message.bot)  # type: ignore[arg-type]
    bot = message.bot
    assert bot is not None
    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    reply = await sess.query("/compact")
    await _send_reply(bot, message.chat.id, reply)


@router.message(Command("cd"))
async def cmd_cd(message: Message, command: CommandObject) -> None:
    if not command.args:
        await message.answer("Usage: <code>/cd /path/to/project</code>", parse_mode=ParseMode.HTML)
        return
    path = Path(command.args.strip()).expanduser()
    if not path.is_dir():
        await message.answer(
            f"❌ Не директория: <code>{html.escape(str(path))}</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    sess = get_or_create_session(message.chat.id, message.bot)  # type: ignore[arg-type]
    # Switching cwd requires a fresh client (ClaudeAgentOptions is immutable
    # after connect). Treat /cd as an implicit /reset.
    sess.state.cwd = path
    sess.state.save()
    await sess.reset()
    await message.answer(
        f"📁 cwd → <code>{html.escape(str(path))}</code>\nСессия сброшена.",
        parse_mode=ParseMode.HTML,
    )


# ─────────── Permission button callbacks ───────────


@router.callback_query(F.data.startswith("perm:"))
async def cb_permission(callback: CallbackQuery) -> None:
    if callback.message is None or callback.data is None:
        await callback.answer()
        return
    chat_id = callback.message.chat.id
    broker = _brokers.get(chat_id)
    if broker is None:
        await callback.answer("Нет активной сессии", show_alert=True)
        return
    decision = callback.data.split(":", 1)[1]
    resolved = await broker.resolve(allow=(decision == "allow"))
    if resolved:
        await callback.answer("✅" if decision == "allow" else "❌")
    else:
        await callback.answer("Запрос уже обработан")


# ─────────── Free-form message → Claude ───────────


@router.message(F.text)
async def on_text(message: Message) -> None:
    if message.text is None or message.text.startswith("/"):
        return
    bot = message.bot
    assert bot is not None
    sess = get_or_create_session(message.chat.id, bot)

    typing_task = await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    log.info("query_start", chat_id=message.chat.id, len=len(message.text))

    reply = await sess.query(message.text)

    await _send_reply(bot, message.chat.id, reply)
    log.info(
        "query_done",
        chat_id=message.chat.id,
        text_chars=len(reply.text),
        tools=len(reply.tool_calls),
        cancelled=reply.cancelled,
        error=bool(reply.error),
    )
    del typing_task  # silence unused warning — typing indicator is fire-and-forget


async def _send_reply(bot: Bot, chat_id: int, reply: StreamedReply) -> None:
    """Render a StreamedReply into one or more TG messages."""
    if reply.error:
        await bot.send_message(
            chat_id,
            f"❌ <b>Ошибка</b>\n<pre>{html.escape(reply.error[:1500])}</pre>",
            parse_mode=ParseMode.HTML,
        )
        return

    body = reply.text.strip()
    if reply.tool_calls:
        # Brief activity log above the answer body.
        head = "\n".join(reply.tool_calls[-12:])  # last 12 are enough
        body = f"<i>{html.escape(head)}</i>\n\n{body}" if body else f"<i>{html.escape(head)}</i>"

    if not body:
        body = "<i>(пустой ответ)</i>"

    if reply.cancelled:
        body = "<i>⏹ прервано</i>\n\n" + body

    if is_too_long_for_messages(body):
        # Upload the reply as a file; chat gets a short pointer.
        file = BufferedInputFile(body.encode("utf-8"), filename="reply.txt")
        await bot.send_document(
            chat_id,
            document=file,
            caption="📎 Длинный ответ — приложил файлом.",
        )
        return

    chunks = chunk_text(body)
    for i, chunk in enumerate(chunks):
        # First chunk gets HTML; subsequent chunks plain (could contain
        # broken tags if we split mid-tag). Trade-off: occasional ugly
        # chunk vs. crash on malformed HTML.
        try:
            await bot.send_message(
                chat_id,
                chunk,
                parse_mode=ParseMode.HTML if i == 0 else None,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("send_chunk_failed", error=str(e), chunk_idx=i)
            # Fall back to plain text on parse error.
            await bot.send_message(chat_id, chunk[: 4096])


# ─────────── Dispatcher factory ───────────


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.update.middleware(AllowedUserMiddleware())
    dp.include_router(router)
    return dp


def build_bot() -> Bot:
    return Bot(token=settings.telegram_bot_token)
