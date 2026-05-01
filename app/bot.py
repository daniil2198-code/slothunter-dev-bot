"""aiogram bot — handlers for / commands and free-form messages.

Single chat per user (single-user bot — only ``ALLOWED_USER_ID`` reaches
us). The chat_id is keyed off the message's ``chat.id``; a single
``ChatSession`` is held per chat_id in ``_sessions``.

Commands:
    /start        — say hello, show working directory
    /status       — current cwd, session id, pending approval
    /reset        — drop the conversation; next message starts fresh
    /compact      — compress conversation history into a summary
    /cancel       — best-effort interrupt of the in-flight Claude turn
    /cd <path>    — switch Claude's working directory (must exist)
    /menu         — quick-tap inline keyboard for common dev actions
    /help         — list of commands

Anything else (text, photos with captions) is forwarded to Claude.
"""

from __future__ import annotations

import html
import time
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatAction, ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

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
        "Перед каждой задачей делаю <code>git pull</code> автоматически.\n\n"
        "<b>Как работать</b>\n"
        "• Просто пиши задачу — я передам Claude Code\n"
        "• Картинки тоже принимаю (скриншот бага → опиши проблему)\n"
        "• <code>/menu</code> — кнопки быстрых действий\n"
        "• <b>Новая задача?</b> начни сообщение со «<i>новая задача:</i>» — "
        "Claude сам поймёт сегментацию. История полезна как контекст.\n"
        "• Контекст разросся → <code>/compact</code>. "
        "Хочешь полный чистый старт → <code>/reset</code>.\n\n"
        "Все команды: /help"
    )
    await message.answer(text, parse_mode=ParseMode.HTML)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    text = (
        "<b>Команды</b>\n\n"
        "<code>/menu</code>    — inline-клавиатура быстрых действий\n"
        "<code>/digest</code>  — утренний дайджест (commits, ROADMAP, прод, ошибки)\n"
        "<code>/status</code>  — текущая папка, session id, модель, betas\n"
        "<code>/reset</code>   — забыть разговор; перед стиранием сохранит summary\n"
        "<code>/compact</code> — ужать историю в summary, контекст продолжается\n"
        "<code>/history</code> — список сохранённых сессий\n"
        "<code>/show &lt;id&gt;</code> — открыть сохранённый summary\n"
        "<code>/resume &lt;id&gt;</code> — стартовать с этим контекстом\n"
        "<code>/cancel</code>  — прервать текущий ход (best-effort)\n"
        "<code>/cd &lt;path&gt;</code> — сменить рабочую директорию Claude\n"
        "<code>/help</code>    — это сообщение\n\n"
        "<b>Сообщения</b>\n"
        "• Текст — задача для Claude\n"
        "• Фото (с подписью или без) — Claude увидит и проанализирует\n"
        "• Голос/видео — не поддерживается, используй TG-транскрипцию\n\n"
        "<b>Новая задача в той же сессии</b>\n"
        "Просто напиши «<i>новая задача: …</i>» или похожее. Claude поймёт "
        "переключение, прошлый контекст останется как фон.\n\n"
        "<b>Permissions</b>\n"
        "Read / Edit / Write / Grep — авто.\n"
        "Безопасные bash (<code>git status / log / diff</code>, "
        "<code>ls</code>, <code>pytest</code>, <code>uv</code>) — авто.\n"
        "Опасное (<code>rm</code>, <code>git push</code>, <code>deploy</code>) — "
        "спросит inline-кнопкой."
    )
    await message.answer(text, parse_mode=ParseMode.HTML)


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    sess = get_or_create_session(message.chat.id, message.bot)  # type: ignore[arg-type]
    sid = sess.state.session_id or "<i>(новая сессия)</i>"
    pending = "есть" if _brokers[message.chat.id]._pending else "нет"  # noqa: SLF001
    # ``settings.model`` is the requested id; the actual model the SDK
    # connects to should match unless Claude Code falls back. The user
    # can verify by asking the bot directly: "какая ты модель?".
    model = settings.model or "<i>(default — Claude Code chooses)</i>"
    betas = ", ".join(settings.betas) if settings.betas else "<i>(none)</i>"
    text = (
        f"📍 cwd: <code>{html.escape(str(sess.state.cwd))}</code>\n"
        f"🆔 session: <code>{html.escape(sid)}</code>\n"
        f"🤖 model: <code>{html.escape(model)}</code>\n"
        f"🧪 betas: <code>{betas}</code>\n"
        f"⏳ ожидание подтверждения: {pending}"
    )
    await message.answer(text, parse_mode=ParseMode.HTML)


@router.message(Command("reset"))
async def cmd_reset(message: Message) -> None:
    bot = message.bot
    assert bot is not None
    sess = get_or_create_session(message.chat.id, bot)
    # Tell the user we're working — the compact-before-wipe takes a few seconds.
    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    saved_title = await sess.reset()
    if saved_title:
        text = (
            "🔄 Сессия сброшена.\n"
            f"📚 Сохранил summary: <i>{html.escape(saved_title)}</i>\n"
            "Вернуться: <code>/history</code>"
        )
    else:
        text = "🔄 Сессия сброшена."
    await message.answer(text, parse_mode=ParseMode.HTML)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message) -> None:
    sess = get_or_create_session(message.chat.id, message.bot)  # type: ignore[arg-type]
    sess.request_cancel()
    await message.answer("⏹ Прерываю — придёт результат того, что успел.")


@router.message(Command("history"))
async def cmd_history(message: Message) -> None:
    """List archived session summaries — most recent first.

    Each entry shows: ``• DD.MM HH:MM — title`` plus a tap-to-resume
    inline button. Tapping opens that summary; an explicit /resume
    starts a new session pre-loaded with it.
    """
    from app.history import list_history

    entries = list_history(message.chat.id)
    if not entries:
        await message.answer(
            "📚 История пуста — она наполняется при <code>/reset</code>.",
            parse_mode=ParseMode.HTML,
        )
        return

    lines = ["📚 <b>История сессий</b> (новые сверху):", ""]
    for entry in entries:
        # Use the user's local timezone for display (Europe/Minsk in our case).
        local = entry.created_at.astimezone()
        when = local.strftime("%d.%m %H:%M")
        lines.append(
            f"• <code>{entry.entry_id}</code> · {when}\n"
            f"  <i>{html.escape(entry.title)}</i>"
        )
    lines.append("")
    lines.append(
        "Открыть: <code>/show &lt;id&gt;</code>\n"
        "Возобновить как контекст: <code>/resume &lt;id&gt;</code>"
    )
    await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)


@router.message(Command("show"))
async def cmd_show(message: Message, command: CommandObject) -> None:
    """Print a saved summary in full."""
    from app.history import load_summary

    if not command.args:
        await message.answer(
            "Usage: <code>/show 20260501T203000Z</code> "
            "(id из <code>/history</code>)",
            parse_mode=ParseMode.HTML,
        )
        return
    entry_id = command.args.strip().split()[0]
    text = load_summary(message.chat.id, entry_id)
    if text is None:
        await message.answer(
            f"❌ Не нашёл запись <code>{html.escape(entry_id)}</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    # Fits in a TG message in 99% of cases (we cap summaries at 32KB).
    # If somehow doesn't — let chunker handle it.
    chunks = chunk_text(text)
    for chunk in chunks:
        await message.answer(chunk)


@router.message(Command("resume"))
async def cmd_resume(message: Message, command: CommandObject) -> None:
    """Start a fresh session seeded with a saved summary as context.

    Differs from /reset+manual: Claude starts with the summary already
    loaded, so you can pick up "from where we left off" without copying
    text yourself.
    """
    from app.history import load_summary

    if not command.args:
        await message.answer(
            "Usage: <code>/resume 20260501T203000Z</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    bot = message.bot
    assert bot is not None
    entry_id = command.args.strip().split()[0]
    summary = load_summary(message.chat.id, entry_id)
    if summary is None:
        await message.answer(
            f"❌ Не нашёл запись <code>{html.escape(entry_id)}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    sess = get_or_create_session(message.chat.id, bot)
    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    try:
        await sess.seed_with_summary(summary)
    except Exception as e:  # noqa: BLE001
        await message.answer(f"⚠️ Не получилось зарядить контекст: {html.escape(str(e))}")
        return
    await message.answer(
        f"♻️ Возобновил контекст из <code>{html.escape(entry_id)}</code>.\n"
        "Можно продолжать с этого места.",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("digest"))
async def cmd_digest(message: Message) -> None:
    """Trigger the morning digest on demand. Same renderer as the cron
    job — useful for verifying things look right without waiting for
    9 AM."""
    bot = message.bot
    assert bot is not None
    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    try:
        from app.digest import build_digest_html

        text = await build_digest_html()
    except Exception as e:  # noqa: BLE001
        await message.answer(f"⚠️ Дайджест упал: {html.escape(str(e))}")
        return
    await message.answer(text, parse_mode=ParseMode.HTML)


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


# ─────────── Inline quick-action menu ───────────
#
# Quick-tap macros for common dev operations. Each button maps to a
# canned prompt the bot sends to Claude on the user's behalf — no
# typing required when on the move.
#
# Picked actions on the principle of "I do this multiple times a day":
#   - Status   → "git status, what's the project state?"
#   - Diff     → "git diff against origin/main, summarize"
#   - Tests    → run pytest
#   - Deploy   → run scripts/deploy.sh on slot-hunter
#   - Roadmap  → show the top of notes/ROADMAP.md
#   - Logs     → tail prod logs

_MENU_PROMPTS: dict[str, str] = {
    "status": (
        "Покажи git status (uncommitted), последний коммит и кратко состояние проекта. "
        "Не редактируй ничего."
    ),
    "diff": (
        "Покажи git diff против origin/main кратко: какие файлы изменились "
        "и в двух предложениях что меняем. Если ничего не изменилось — так и скажи."
    ),
    "tests": "Запусти uv run pytest и покажи итоговый счёт + первую упавшую если есть.",
    "deploy": (
        "Закоммить текущие изменения если есть (придумай осмысленное conventional-commit "
        "сообщение), запушь на origin/main и задеплой на прод через "
        "expect+ssh root@104.152.48.210 'cd /opt/slot-hunter && bash scripts/deploy.sh'. "
        "Сначала спроси у меня подтверждения через Bash — я разрешу."
    ),
    "roadmap": (
        "Покажи notes/ROADMAP.md — секции In progress, Planned next и Done за последние 7 дней."
    ),
    "logs": (
        "Покажи последние 30 строк journalctl -u slot-hunter-api на проде. "
        "Если есть ERROR/WARN — выдели."
    ),
}


def _menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📊 Status", callback_data="menu:status"),
                InlineKeyboardButton(text="🧾 Diff", callback_data="menu:diff"),
            ],
            [
                InlineKeyboardButton(text="🧪 Tests", callback_data="menu:tests"),
                InlineKeyboardButton(text="🚀 Deploy", callback_data="menu:deploy"),
            ],
            [
                InlineKeyboardButton(text="🗺 Roadmap", callback_data="menu:roadmap"),
                InlineKeyboardButton(text="📜 Logs", callback_data="menu:logs"),
            ],
        ]
    )


@router.message(Command("menu"))
async def cmd_menu(message: Message) -> None:
    await message.answer("Быстрые действия:", reply_markup=_menu_keyboard())


@router.callback_query(F.data.startswith("menu:"))
async def cb_menu(callback: CallbackQuery) -> None:
    if callback.message is None or callback.data is None:
        await callback.answer()
        return
    action = callback.data.split(":", 1)[1]
    prompt = _MENU_PROMPTS.get(action)
    if prompt is None:
        await callback.answer("неизвестное действие", show_alert=True)
        return
    await callback.answer(f"⏳ {action}…")
    bot = callback.bot
    assert bot is not None
    chat_id = callback.message.chat.id
    sess = get_or_create_session(chat_id, bot)
    await bot.send_chat_action(chat_id, ChatAction.TYPING)
    reply = await sess.query(prompt)
    await _send_reply(bot, chat_id, reply)


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

    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
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


# ─────────── Image input ───────────
#
# Photos are downloaded to ``state_dir/incoming/<chat>/<ts>.jpg`` and the
# absolute path is appended to the prompt. Claude Code's Read tool
# supports images natively — when the prompt mentions a path, Claude
# decides whether to Read it, and can describe what's in the picture.
#
# Captions are forwarded as the prompt text. Without a caption we
# default to "Что ты видишь на этой картинке? Если это скриншот — что
# в нём не так / нужно исправить?".


@router.message(F.photo)
async def on_photo(message: Message) -> None:
    bot = message.bot
    assert bot is not None
    chat_id = message.chat.id
    sess = get_or_create_session(chat_id, bot)

    # Telegram resamples photos at multiple sizes; the last entry is the
    # largest. That's what Claude wants.
    if not message.photo:
        return
    photo = message.photo[-1]

    incoming = settings.state_dir / "incoming" / str(chat_id)
    incoming.mkdir(parents=True, exist_ok=True)
    fname = f"{int(time.time())}_{photo.file_unique_id}.jpg"
    path = incoming / fname

    file = await bot.get_file(photo.file_id)
    if file.file_path is None:
        await message.answer("❌ Не получилось забрать картинку у Telegram")
        return
    await bot.download_file(file.file_path, destination=str(path))

    caption = (message.caption or "").strip()
    default_q = (
        "Что ты видишь на этой картинке? Если это скриншот UI — "
        "что в нём может быть не так / что нужно исправить в коде?"
    )
    prompt = (
        f"{caption or default_q}\n\n"
        f"📎 Приложена картинка: {path}\n"
        f"Прочитай её через Read tool, чтобы увидеть содержимое."
    )

    await bot.send_chat_action(chat_id, ChatAction.TYPING)
    log.info("photo_received", chat_id=chat_id, path=str(path), has_caption=bool(caption))
    reply = await sess.query(prompt)
    await _send_reply(bot, chat_id, reply)


# Voice / video / documents — give a friendly hint instead of silently
# dropping. Voice transcription is on the roadmap; documents can be
# uploaded via Claude's Read tool with a path.
@router.message(F.voice | F.video | F.video_note | F.audio | F.document)
async def on_unsupported_media(message: Message) -> None:
    if message.voice or message.audio or message.video_note:
        await message.answer(
            "🎤 Голос/видео пока не поддерживается. "
            "Используй Telegram-транскрипцию (через тапе по сообщению) "
            "и пришли текст."
        )
    else:
        await message.answer(
            "📄 Документы напрямую не подключены. Если файл нужен в проекте — "
            "попроси меня создать/обновить его через Write/Edit."
        )


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

    # Auto-pull breadcrumb — sits above tools/answer when the cwd updated.
    if reply.pre_note:
        body = f"<i>↻ {html.escape(reply.pre_note)}</i>\n\n" + body

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
