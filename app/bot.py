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

import asyncio
import contextlib
import html
import re
import time
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatAction, ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.auth import AllowedUserMiddleware
from app.chunker import chunk_text, is_too_long_for_messages
from app.claude_session import ChatSession, StreamedReply
from app.config import settings
from app.logging import get_logger
from app.md_to_tg import to_html as md_to_html
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


# ─────────── Heartbeat / typing keep-alive ───────────

# How long a turn must run before we surface a "still working…" status
# message. Below this we just rely on the typing-action animation —
# a status message for a 5-second turn is more noise than signal.
_STATUS_AFTER_S = 25.0
# Cadence at which we edit the status message. Telegram rate-limits
# edit_message_text to about one edit per second per chat — 30s is
# safe and matches the user's mental model of "it's checking in".
_STATUS_INTERVAL_S = 30.0
# Telegram's chat-action ("typing…") expires after ~5s. We refresh on
# this cadence to keep it visible for the whole turn.
_TYPING_REFRESH_S = 4.0


async def _run_query_with_status(
    bot: Bot,
    chat_id: int,
    sess: ChatSession,
    prompt: str,
) -> StreamedReply:
    """Run ``sess.query(prompt)`` while keeping the user informed.

    Two background tasks run for the duration of the query:

    * **typing-keepalive** — re-sends ``ChatAction.TYPING`` every few
      seconds so the native "печатает…" indicator stays on.
    * **heartbeat** — after ``_STATUS_AFTER_S`` of work, sends one
      status message and edits it every ``_STATUS_INTERVAL_S`` with
      elapsed seconds + the most recent tool call. Deletes the message
      when the turn ends so the chat stays clean.

    Tool labels arrive via ``on_progress`` callback wired into the
    SDK stream loop in ``claude_session._collect_reply``.
    """
    started = time.monotonic()
    last_tool: dict[str, str | None] = {"label": None}

    async def on_progress(label: str) -> None:
        last_tool["label"] = label

    async def typing_keepalive() -> None:
        try:
            while True:
                with contextlib.suppress(Exception):
                    await bot.send_chat_action(chat_id, ChatAction.TYPING)
                await asyncio.sleep(_TYPING_REFRESH_S)
        except asyncio.CancelledError:
            raise

    async def heartbeat() -> None:
        try:
            await asyncio.sleep(_STATUS_AFTER_S)
            tool = last_tool["label"] or "думаю"
            text = f"⏳ работаю… <i>{html.escape(tool)}</i>"
            try:
                msg = await bot.send_message(
                    chat_id, text, parse_mode=ParseMode.HTML
                )
            except Exception as e:  # noqa: BLE001 — heartbeat is best-effort
                log.debug("heartbeat_send_failed", error=str(e))
                return
            try:
                while True:
                    await asyncio.sleep(_STATUS_INTERVAL_S)
                    elapsed = int(time.monotonic() - started)
                    tool = last_tool["label"] or "думаю"
                    new_text = (
                        f"⏳ {elapsed}с — <i>{html.escape(tool)}</i>"
                    )
                    try:
                        await bot.edit_message_text(
                            new_text,
                            chat_id=chat_id,
                            message_id=msg.message_id,
                            parse_mode=ParseMode.HTML,
                        )
                    except Exception as e:  # noqa: BLE001
                        log.debug("heartbeat_edit_failed", error=str(e))
                        return
            except asyncio.CancelledError:
                # On clean turn end, drop the status message — we don't
                # want a forever-stale "⏳" lingering above the answer.
                with contextlib.suppress(Exception):
                    await bot.delete_message(chat_id, msg.message_id)
                raise
        except asyncio.CancelledError:
            raise

    typing_task = asyncio.create_task(typing_keepalive())
    hb_task = asyncio.create_task(heartbeat())
    try:
        return await sess.query(prompt, on_progress=on_progress)
    finally:
        for task in (typing_task, hb_task):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


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
    """Full operator manual. Splits across multiple TG messages because
    the limit is 4096 chars and we have a lot to say.

    Each section is self-contained — sending them as separate messages
    means the user can scroll back to one section without losing the
    others off-screen.
    """
    bot = message.bot
    assert bot is not None
    chat_id = message.chat.id

    inputs = _HELP_INPUTS.replace(
        "{voice_limit}", str(settings.voice_max_duration_sec)
    )
    sections = [
        _HELP_INTRO,
        inputs,
        _HELP_COMMANDS,
        _HELP_PERMISSIONS,
        _HELP_CONTEXT,
    ]
    if settings.playwright_mcp_enabled:
        sections.append(_HELP_BROWSER)
    sections.append(_HELP_TIPS)
    for section in sections:
        await bot.send_message(chat_id, section, parse_mode=ParseMode.HTML)


# ─────────── /help section text ───────────
# Kept as module-level constants so they stay readable as one block —
# editing UX copy gets messy when buried inside a function.

_HELP_INTRO = (
    "📖 <b>Slot Hunter dev-bot — справка</b>\n\n"
    "Это твой Claude Code в Telegram. Бот сидит на VPS рядом с прод-кодом "
    "Slot Hunter и выполняет задачи как локальный Claude — читает / правит "
    "файлы, гоняет тесты, коммитит, деплоит. Полностью автономный single-user "
    "интерфейс: пиши текст, кидай скрины, получай результат."
)

_HELP_INPUTS = (
    "💬 <b>Что можно отправлять</b>\n\n"
    "• <b>Текст</b> — обычная задача для Claude. Можно длинно, можно одной "
    "строкой («что в логах сегодня?»).\n"
    "• <b>Фото</b> — скриншот бага, ref-дизайн, любая картинка. Бот сохранит "
    "её на VPS, передаст Claude путь, и тот прочитает её через Read tool. "
    "Подпись к фото = промпт; без подписи бот сам спросит «что ты видишь, "
    "что не так».\n"
    "• <b>Голос / аудио</b> — бот транскрибирует через Groq Whisper "
    "(<code>whisper-large-v3-turbo</code>) и сразу передаёт текст Claude. "
    "Сначала покажет тебе что услышал ("
    "🎤 <i>… ваш текст …</i>), чтобы ты мог поймать ошибки распознавания. "
    "Лимит длины — {voice_limit}s.\n"
    "• <b>Видео / документы / стикеры</b> — пока не подключены."
)

_HELP_COMMANDS = (
    "⚙️ <b>Команды</b>\n\n"
    "<b>Контекст разговора</b>\n"
    "<code>/status</code>  — папка, session id, модель, betas, "
    "режим разрешений, thinking-вкл/выкл\n"
    "<code>/reset</code>   — забыть разговор; перед стиранием сохранит summary\n"
    "<code>/compact</code> — ужать историю в summary; контекст продолжается\n"
    "<code>/cancel</code>  — прервать текущий ход (best-effort)\n"
    "<code>/cd &lt;path&gt;</code> — сменить рабочую директорию Claude\n\n"
    "<b>Поведение</b>\n"
    "<code>/yolo on|off</code> — включить bypass-режим (Claude перестаёт "
    "спрашивать на bash / edits / MCP). Без аргумента — статус.\n"
    "<code>/thinking on|off</code> — показывать рассуждения Claude "
    "отдельным «💭»-сообщением. Без аргумента — статус.\n\n"
    "<b>Журнал разговоров</b>\n"
    "<code>/history</code> — список сохранённых сессий (id, время, заголовок)\n"
    "<code>/show &lt;id&gt;</code> — открыть сохранённый summary\n"
    "<code>/resume &lt;id&gt;</code> — новая сессия с этим контекстом как фон\n\n"
    "<b>Быстрые действия</b>\n"
    "<code>/menu</code>    — inline-клавиатура (Status / Diff / Tests / "
    "Deploy / Roadmap / Logs)\n"
    "<code>/digest</code>  — утренний дайджест по запросу (commits, ROADMAP, "
    "прод, ошибки). Сам приходит каждый день в 09:00 Минск.\n"
    "<code>/test</code>    — без аргументов список e2e-сценариев, "
    "<code>/test &lt;name&gt;</code> прогон через Playwright + Mini App "
    "с dev-токеном.\n\n"
    "<code>/help</code>    — этот текст"
)

_HELP_PERMISSIONS = (
    "🔐 <b>Разрешения tools</b>\n\n"
    "Чтобы тебя не мучали кнопки на каждый чих, разделил по риску:\n\n"
    "<b>Авто (без подтверждения)</b>\n"
    "• Read, Glob, Grep, Edit, Write, MultiEdit, NotebookRead, TodoWrite\n"
    "• Безопасные bash: <code>git status</code> / <code>log</code> / "
    "<code>diff</code> / <code>show</code> / <code>fetch</code> / "
    "<code>pull</code>, <code>ls</code>, <code>cat</code>, <code>head</code>, "
    "<code>grep</code>, <code>pytest</code>, <code>ruff</code>, <code>mypy</code>, "
    "<code>uv run</code>, <code>docker ps/logs/inspect</code>, "
    "<code>systemctl status</code>, <code>journalctl</code>, "
    "<code>curl/wget</code>\n\n"
    "<b>Спрашивает inline-кнопкой</b>\n"
    "• Любой Bash, который не в auto-списке\n"
    "• WebFetch, WebSearch, Task, MCP-tools\n"
    "• Любая команда с pipe / chain / substitution "
    "(<code>|</code> <code>&amp;&amp;</code> <code>$(...)</code>)\n\n"
    "<b>Громкое предупреждение ⚠️</b>\n"
    "• <code>rm -rf</code>, <code>git push --force</code>, "
    "<code>git reset --hard</code>, <code>git clean</code>, "
    "<code>DROP TABLE</code>, <code>TRUNCATE</code>, "
    "<code>docker system prune</code>, <code>shutdown</code> / <code>reboot</code>\n\n"
    "Таймаут на ответ — 120 сек. Молчание = deny."
)

_HELP_CONTEXT = (
    "🧠 <b>Контекст и продолжительность сессии</b>\n\n"
    "<b>Один чат = одна непрерывная сессия Claude.</b> Прошлый контекст помогает "
    "в новых задачах, history переживает рестарты бота (resume через "
    "<code>session_id</code>).\n\n"
    "<b>Новая задача в той же сессии</b>\n"
    "Начни сообщение с «<i>новая задача: …</i>» или похожее — Claude поймёт "
    "переключение, старая ниточка остаётся фоном.\n\n"
    "<b>Контекст разросся</b>\n"
    "<code>/compact</code> — Claude ужмёт историю в summary, продолжишь "
    "работать в том же session_id.\n\n"
    "<b>Полный чистый старт</b>\n"
    "<code>/reset</code> — перед стиранием бот сам сохранит summary в "
    "<code>/history</code>, потом стартует с нуля. Если потом захочешь "
    "вернуться — <code>/resume &lt;id&gt;</code>.\n\n"
    "<b>Auto-pull</b>\n"
    "Перед каждой задачей бот делает <code>git fetch &amp;&amp; git pull</code> "
    "в рабочей папке. Если ты пушнул что-то с ноута — Claude увидит. "
    "В ответе появится строчка «↻ git pull: a1b → c2d (N коммитов)»."
)

_HELP_BROWSER = (
    "🌐 <b>Браузер для Claude (M3)</b>\n\n"
    "Когда включен Playwright MCP (<code>PLAYWRIGHT_MCP_ENABLED=true</code> "
    "в .env), Claude умеет:\n"
    "• ходить по URL, читать DOM, делать скриншоты\n"
    "• кликать элементы, заполнять формы\n"
    "• читать console / network логи страницы\n\n"
    "Auto-approve: navigate, screenshot, snapshot, click, type, "
    "fill, console_messages, network_requests.\n"
    "Спрашивает: <code>browser_evaluate</code> и "
    "<code>browser_run_code_unsafe</code> (произвольный JS).\n\n"
    "Скриншоты бот сам подхватывает из <code>/tmp/</code>, "
    "<code>~/.cache/playwright-mcp/</code> и "
    "<code>.playwright-mcp/</code> внутри проекта — присылает как фото.\n\n"
    "<b>Готовые e2e-сценарии</b>\n"
    "Лежат в <code>slot-hunter/notes/e2e/*.md</code> — markdown-описание "
    "(Goal / Steps / Pass / Report). Запуск через <code>/test &lt;name&gt;</code>; "
    "<code>/test</code> без аргументов — список всех. Claude читает сценарий, "
    "прогоняет через Playwright, выдаёт PASS/FAIL + скриншоты.\n\n"
    "<b>Авто-тест после деплоя</b>\n"
    "Slot-hunter <code>scripts/deploy.sh</code> после успеха кладёт триггер "
    "в <code>/var/lib/slothunter-dev-bot/triggers/*.txt</code> — бот в "
    "течение ~60 сек прогоняет <code>/test mini-app-home-loads</code> "
    "и шлёт результат сам.\n\n"
    "<b>Свободные запросы тоже работают</b>\n"
    "• <i>«сделай скриншот slothunter.space»</i>\n"
    "• <i>«зайди с dev_token и покажи список алертов»</i>\n"
    "• <i>«пробеги wizard Минск→Брест и проверь что нет console errors»</i>"
)

_HELP_TIPS = (
    "💡 <b>Подсказки</b>\n\n"
    "• <b>Долгий ответ</b> — режется на куски по 3500 символов. Если &gt; 14 KB "
    "— приходит файлом.\n"
    "• <b>Подписка Claude</b>: бот использует OAuth-токен из <code>claude /login</code> "
    "на VPS. Никаких API-ключей — твоя подписка.\n"
    "• <b>Утренний дайджест</b> приходит сам в 09:00 Минск. Содержит "
    "коммиты за сутки, статусы ROADMAP, что можно взять дальше, что "
    "заблокировано, статус прода, число ошибок в логах.\n"
    "• <b>Деплой через бота</b>: попроси «закоммить и задеплой» — Claude "
    "сделает <code>git commit</code> + <code>push</code> + ssh-deploy. "
    "Каждое действие спросит подтверждения.\n"
    "• <b>Если запутался</b>: <code>/status</code> покажет где находишься, "
    "<code>/cancel</code> прервёт залипший ход, <code>/reset</code> начнёт "
    "с чистого."
)


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
    perm_label = _format_permission_mode(sess.state.permission_mode)
    thinking_label = "вкл" if sess.state.thinking_visible else "выкл"
    text = (
        f"📍 cwd: <code>{html.escape(str(sess.state.cwd))}</code>\n"
        f"🆔 session: <code>{html.escape(sid)}</code>\n"
        f"🤖 model: <code>{html.escape(model)}</code>\n"
        f"🧪 betas: <code>{betas}</code>\n"
        f"🔐 разрешения: {perm_label}\n"
        f"💭 thinking в чате: {thinking_label}\n"
        f"⏳ ожидание подтверждения: {pending}"
    )
    await message.answer(text, parse_mode=ParseMode.HTML)


def _format_permission_mode(mode: str) -> str:
    """Human label for the /status / /yolo replies."""
    return {
        "default": "🔒 default (спрашиваю)",
        "bypassPermissions": "🚀 bypass — YOLO (без вопросов)",
        "acceptEdits": "✏️ acceptEdits (правки авто, bash спрашиваю)",
        "plan": "📖 plan (read-only)",
    }.get(mode, f"<code>{html.escape(mode)}</code>")


# ─────────── /yolo + /thinking — runtime toggles ───────────
#
# These two commands let the user flip behaviour mid-session without
# editing config / restarting the bot. State is per-chat and persists
# across bot restarts (saved in ``state_dir/chat_<id>.json``).


@router.message(Command("yolo"))
async def cmd_yolo(message: Message, command: CommandObject) -> None:
    """Toggle Claude's permission_mode between ``default`` and
    ``bypassPermissions``. The latter skips the inline-button prompt
    entirely — Claude can run any tool, edit any file, execute any
    bash command. Useful when the user trusts the run and is tired
    of tapping Allow.

    Usage: ``/yolo on`` / ``/yolo off`` / ``/yolo`` (status)
    """
    sess = get_or_create_session(message.chat.id, message.bot)  # type: ignore[arg-type]
    arg = (command.args or "").strip().lower()

    if not arg or arg == "status":
        await message.answer(
            f"🔐 Сейчас: {_format_permission_mode(sess.state.permission_mode)}\n\n"
            "Переключить:\n"
            "<code>/yolo on</code> — без вопросов (YOLO)\n"
            "<code>/yolo off</code> — спрашивать как обычно",
            parse_mode=ParseMode.HTML,
        )
        return

    if arg in {"on", "yolo", "true", "1", "yes"}:
        # Broker-level YOLO: we don't propagate ``bypassPermissions`` to
        # the SDK (Claude CLI hard-blocks that combo under root). Instead
        # the broker's can_use_tool callback returns Allow for everything
        # while we're in this mode. Catastrophic patterns (rm -rf /, dd,
        # forkbomb, etc.) still go through the prompt as a final safety
        # net.
        await sess.set_permission_mode("bypassPermissions")
        await message.answer(
            "🚀 <b>YOLO включён.</b> Claude больше не спрашивает — bash, "
            "edits, MCP пройдут без подтверждения. Останется фильтр на "
            "катастрофические команды (<code>rm -rf /</code>, "
            "<code>dd if=/dev/zero of=/dev/sda</code>, форкбомбу, "
            "<code>shutdown</code>, <code>iptables -F</code>) — на них всё "
            "равно спросит, на случай галлюцинаций.\n\n"
            "Выключить: <code>/yolo off</code>",
            parse_mode=ParseMode.HTML,
        )
    elif arg in {"off", "default", "false", "0", "no"}:
        await sess.set_permission_mode("default")
        await message.answer(
            "🔒 Вернулся в обычный режим — спрашиваю на всё, что не "
            "в auto-списке.",
            parse_mode=ParseMode.HTML,
        )
    else:
        await message.answer(
            "Не понял. <code>/yolo on</code>, <code>/yolo off</code>, "
            "или <code>/yolo</code> (статус).",
            parse_mode=ParseMode.HTML,
        )


@router.message(Command("thinking"))
async def cmd_thinking(message: Message, command: CommandObject) -> None:
    """Toggle whether ThinkingBlocks from Claude get surfaced in TG.

    Off (default) — рассуждения отбрасываются ещё до отправки в чат.
    On — каждый ход пушит отдельным сообщением «💭 …» (truncated).
    """
    sess = get_or_create_session(message.chat.id, message.bot)  # type: ignore[arg-type]
    arg = (command.args or "").strip().lower()

    if not arg or arg == "status":
        state = "вкл" if sess.state.thinking_visible else "выкл"
        await message.answer(
            f"💭 Thinking в чате: <b>{state}</b>\n\n"
            "Переключить:\n"
            "<code>/thinking on</code> — показывать рассуждения\n"
            "<code>/thinking off</code> — прятать (default)",
            parse_mode=ParseMode.HTML,
        )
        return

    if arg in {"on", "true", "1", "yes"}:
        await sess.set_thinking_visible(True)
        await message.answer(
            "💭 Thinking теперь видно. Каждый ответ может прийти двумя "
            "сообщениями: сначала рассуждения, потом сам ответ. Если "
            "становится шумно — <code>/thinking off</code>.",
            parse_mode=ParseMode.HTML,
        )
    elif arg in {"off", "false", "0", "no"}:
        await sess.set_thinking_visible(False)
        await message.answer("💭 Thinking скрыт. Возвращаюсь к лаконичным ответам.")
    else:
        await message.answer(
            "Не понял. <code>/thinking on</code>, <code>/thinking off</code>, "
            "или <code>/thinking</code> (статус).",
            parse_mode=ParseMode.HTML,
        )


@router.message(Command("reset"))
async def cmd_reset(message: Message) -> None:
    bot = message.bot
    assert bot is not None
    sess = get_or_create_session(message.chat.id, bot)
    # If there's an active Claude turn, ask it to cancel. Without this,
    # `await sess.reset()` would queue behind the turn's lock and the
    # user perceives the bot as frozen until the turn finishes
    # (sometimes minutes for runaway browser tests).
    sess.request_cancel()
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


def _list_e2e_scenarios() -> tuple[Path, list[str]]:
    """Return ``(scenarios_dir, names)``. ``names`` empty if dir missing."""
    scenarios_dir = settings.default_workdir / "notes" / "e2e"
    if not scenarios_dir.is_dir():
        return scenarios_dir, []
    names = sorted(
        p.stem for p in scenarios_dir.glob("*.md") if p.stem != "README"
    )
    return scenarios_dir, names


def build_e2e_prompt(name: str) -> tuple[str | None, str | None]:
    """Build the Claude prompt for running an e2e scenario.

    Returns ``(prompt, None)`` on success or ``(None, error_message)``
    when the scenario file isn't there. Used both by the ``/test``
    Telegram command and the file-trigger queue (``triggers.py``) —
    the trigger queue can't go through Aiogram's command dispatcher
    because the inbound text is a write-from-disk, not a TG ``Message``.
    """
    scenarios_dir, _ = _list_e2e_scenarios()
    target = scenarios_dir / f"{name}.md"
    if not target.is_file():
        return None, f"Сценарий «{name}» не найден ({target})"

    prompt = (
        f"Прогон e2e-сценария.\n\n"
        f"1. Прочитай файл: {target}\n"
        f"2. Выполни его шаги через Playwright MCP.\n"
        f"3. По итогу дай вердикт: **PASS** или **FAIL** одной строкой "
        f"вверху ответа, потом краткие наблюдения и ссылки на скриншоты.\n\n"
        f"Если что-то пошло не по плану и ты не уверен в трактовке — "
        f"всё равно вынеси PASS/FAIL и объясни сомнения."
    )
    return prompt, None


@router.message(Command("test"))
async def cmd_test(message: Message, command: CommandObject) -> None:
    """Run an E2E scenario from ``notes/e2e/`` against the prod Mini App.

    ``/test`` (no args) — list available scenarios.
    ``/test <name>`` — Claude reads ``notes/e2e/<name>.md`` and executes
    it via Playwright MCP, then reports PASS/FAIL with evidence.

    Sets a 5-minute soft budget by request — browser scenarios that run
    longer are almost always stuck; user can /cancel to bail. Multi-step
    flows like full wizard runs sit comfortably under 2 min.
    """
    bot = message.bot
    assert bot is not None
    chat_id = message.chat.id

    scenarios_dir, scenarios = _list_e2e_scenarios()
    if not scenarios_dir.is_dir():
        await message.answer(
            f"❌ Не нашёл сценариев: <code>{html.escape(str(scenarios_dir))}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    if not command.args:
        if not scenarios:
            await message.answer("Сценариев нет — добавь файлы в notes/e2e/")
            return
        listing = "\n".join(f"• <code>/test {s}</code>" for s in scenarios)
        await message.answer(
            f"🧪 <b>Доступные сценарии</b>\n\n{listing}\n\n"
            "Без аргументов — этот список. С именем — прогон.",
            parse_mode=ParseMode.HTML,
        )
        return

    name = command.args.strip().split()[0]
    prompt, err = build_e2e_prompt(name)
    if err is not None or prompt is None:
        await message.answer(
            f"❌ Сценарий <code>{html.escape(name)}</code> не найден. "
            "Список — <code>/test</code>.",
            parse_mode=ParseMode.HTML,
        )
        return

    sess = get_or_create_session(chat_id, bot)
    log.info("e2e_test_start", chat_id=chat_id, scenario=name)
    reply = await _run_query_with_status(bot, chat_id, sess, prompt)
    log.info(
        "e2e_test_done",
        chat_id=chat_id,
        scenario=name,
        cancelled=reply.cancelled,
        error=bool(reply.error),
    )
    await _send_reply(bot, chat_id, reply)


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
    reply = await _run_query_with_status(bot, message.chat.id, sess, "/compact")
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
    reply = await _run_query_with_status(bot, chat_id, sess, prompt)
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

    log.info("query_start", chat_id=message.chat.id, len=len(message.text))

    reply = await _run_query_with_status(bot, message.chat.id, sess, message.text)

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

    log.info("photo_received", chat_id=chat_id, path=str(path), has_caption=bool(caption))
    reply = await _run_query_with_status(bot, chat_id, sess, prompt)
    await _send_reply(bot, chat_id, reply)


# ─────────── Voice / audio → Whisper → Claude ───────────
#
# Telegram voice notes (`voice`) and uploaded audio files (`audio`)
# are downloaded and transcribed via Groq Whisper, then forwarded to
# Claude as an ordinary text turn. We DON'T persist the audio bytes
# anywhere — the file lives in memory for the round-trip and gets GC'd.
#
# Long voice messages (over `voice_max_duration_sec`) are rejected
# with a friendly note rather than burning Whisper budget on what's
# almost certainly a mistap.


@router.message(F.voice | F.audio)
async def on_voice(message: Message) -> None:
    bot = message.bot
    assert bot is not None
    chat_id = message.chat.id

    # If the user hasn't configured Groq, fall back to the old hint
    # so the bot doesn't appear broken.
    if not settings.groq_api_key:
        await message.answer(
            "🎤 Голос пока не подключён (нет GROQ_API_KEY). "
            "Используй TG-транскрипцию (long-tap по voice → "
            "«Транскрибировать») и пришли текстом."
        )
        return

    media = message.voice or message.audio
    assert media is not None
    duration = getattr(media, "duration", 0) or 0
    if duration > settings.voice_max_duration_sec:
        await message.answer(
            f"🎤 Слишком длинное ({duration}s) — лимит "
            f"{settings.voice_max_duration_sec}s. Разбей на части или "
            "перешли текстом."
        )
        return

    # Download the audio bytes into memory. Bot API gives us a path
    # under api.telegram.org/file/bot<token>/<path>; aiogram's
    # download_file streams it for us.
    file = await bot.get_file(media.file_id)
    if file.file_path is None:
        await message.answer("❌ Не получилось забрать аудио у Telegram")
        return
    buf = await bot.download_file(file.file_path)
    if buf is None:
        await message.answer("❌ Telegram вернул пустой ответ")
        return
    audio_bytes = buf.read()

    # Hint the format via filename suffix — Groq picks a decoder by it.
    # voice → ogg/opus; audio → whatever extension TG kept.
    ext = "ogg" if message.voice else (
        Path(getattr(media, "file_name", "") or "audio").suffix.lstrip(".") or "mp3"
    )
    filename = f"voice.{ext}"

    await bot.send_chat_action(chat_id, ChatAction.TYPING)
    log.info("voice_received", chat_id=chat_id, duration_sec=duration, ext=ext)

    from app.transcribe import TranscriptionError, transcribe_audio

    try:
        transcript = await transcribe_audio(audio_bytes, filename=filename)
    except TranscriptionError as e:
        log.warning("voice_transcribe_failed", error=str(e))
        await message.answer(
            f"❌ Не получилось расшифровать голос: "
            f"<code>{html.escape(str(e)[:200])}</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    except Exception as e:  # noqa: BLE001 — surface unexpected to user
        log.exception("voice_transcribe_crash")
        await message.answer(
            f"❌ Сбой расшифровки ({type(e).__name__}): "
            f"<code>{html.escape(str(e)[:200])}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    text = transcript.text.strip()
    if not text:
        await message.answer("🎤 Whisper вернул пустой текст — попробуй ещё раз")
        return

    # Show the user what we heard (so they can spot misreads early).
    # Then act on it as a normal user message — Claude doesn't need to
    # know whether the input came from text or speech.
    await message.answer(
        f"🎤 <i>{html.escape(text[:1500])}</i>",
        parse_mode=ParseMode.HTML,
    )

    sess = get_or_create_session(chat_id, bot)
    reply = await _run_query_with_status(bot, chat_id, sess, text)
    await _send_reply(bot, chat_id, reply)


# Video / video-notes / documents — friendly hint instead of silent drop.
# (Voice now handled above; this branch is what's left.)
@router.message(F.video | F.video_note | F.document)
async def on_unsupported_media(message: Message) -> None:
    if message.video_note or message.video:
        await message.answer(
            "🎥 Видео пока не подключено. Если нужен анализ кадра — "
            "сделай скриншот и пришли как фото."
        )
    else:
        await message.answer(
            "📄 Документы напрямую не подключены. Если файл нужен в "
            "проекте — попроси меня создать/обновить через Write/Edit."
        )


async def _send_reply(bot: Bot, chat_id: int, reply: StreamedReply) -> None:
    """Render a StreamedReply into one or more TG messages."""
    # Pre-message: thinking text (if /thinking on for this chat). Sent
    # FIRST so the user sees Claude's reasoning above the answer in
    # the scroll. Truncated to keep TG happy and the chat readable —
    # the full text would often blow past the 3500-char chunk anyway.
    if reply.thinking:
        thinking = reply.thinking
        max_len = 3000
        if len(thinking) > max_len:
            thinking = thinking[:max_len] + "\n\n…[обрезано]"
        try:
            await bot.send_message(
                chat_id,
                f"💭 <i>{html.escape(thinking)}</i>",
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:  # noqa: BLE001 — never let thinking break the answer
            log.warning("send_thinking_failed", error=str(e))

    if reply.error:
        await bot.send_message(
            chat_id,
            f"❌ <b>Ошибка</b>\n<pre>{html.escape(reply.error[:1500])}</pre>",
            parse_mode=ParseMode.HTML,
        )
        return

    # Claude emits CommonMark (``**bold**``, fenced code, headings, links).
    # Telegram's HTML mode doesn't render ``**bold**`` — convert before
    # sending so the user sees actual formatting instead of asterisks.
    body = md_to_html(reply.text.strip())
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
        # Send EVERY chunk with HTML mode. Earlier we only HTML-parsed
        # the first chunk on the theory that mid-tag splits would crash
        # subsequent ones, but the chunker only splits on ``\n\n`` /
        # ``\n``, which means tags are never broken in practice — and
        # the upside is huge: long answers stop showing raw ``<b>…</b>``
        # in chunks #2+. If TG ever does reject (malformed HTML), we
        # catch and re-send as plain text below.
        try:
            await bot.send_message(chat_id, chunk, parse_mode=ParseMode.HTML)
        except Exception as e:  # noqa: BLE001
            log.warning("send_chunk_failed", error=str(e), chunk_idx=i)
            # Fall back to plain text on parse error.
            await bot.send_message(chat_id, chunk[: 4096])

    # Auto-attach any image files Claude mentions in the reply. Common
    # case: Playwright took a screenshot, saved to /tmp/foo.png, Claude
    # tells the user "see /tmp/foo.png". We pick those up and ship.
    for path in _extract_image_paths(reply.text):
        try:
            await bot.send_photo(chat_id, FSInputFile(str(path)))
        except Exception as e:  # noqa: BLE001
            log.debug("auto_send_image_failed", path=str(path), error=str(e))


# Image-path regex. Three flavors:
# 1) absolute paths under common screenshot roots (/tmp, ~/.cache/...);
# 2) ``.playwright-mcp/<file>`` — Playwright MCP's default output dir;
# 3) bare ``screenshot-<name>.png`` or ``./screenshot-...`` — what
#    Claude often writes when asking for a "filename" with no path.
#    Restricted to the ``screenshot-`` prefix so we don't accidentally
#    pick up arbitrary png mentions in code blocks ("button.png" etc).
#
# All relative forms get resolved against known project roots before
# we check existence on disk.
_ABS_IMAGE_PATH_RE = re.compile(
    r"(?<![\w/])"  # no leading word char or slash before
    r"(/(?:tmp|var/lib/slothunter-dev-bot|root/\.cache/playwright-mcp|"
    r"home/[^/\s]+/screenshots|opt/[^/\s]+/\.playwright-mcp|opt/[^/\s]+)"
    r"/[^\s\"'<>`]+\.(?:png|jpg|jpeg|webp))",
    re.IGNORECASE,
)
# Relative path: ``.playwright-mcp/<filename>`` — Playwright MCP's
# default output dir, relative to its cwd.
_REL_PLAYWRIGHT_RE = re.compile(
    r"(?<![\w/])"
    r"(\.playwright-mcp/[^\s\"'<>`]+\.(?:png|jpg|jpeg|webp))",
    re.IGNORECASE,
)
# Bare or ./-prefixed screenshot file. Conservative: require the
# ``screenshot-`` prefix so we don't suck in random ``logo.png``
# mentions Claude might quote from code.
_BARE_SCREENSHOT_RE = re.compile(
    r"(?<![\w/])"
    r"\.?/?(screenshot-[^\s\"'<>`/]+\.(?:png|jpg|jpeg|webp))",
    re.IGNORECASE,
)

# Search roots for resolving relative paths. We try the project root
# first (where the bot was launched), then a couple of common
# alternates. Order matters: first hit wins.
_RELATIVE_SEARCH_ROOTS = (
    Path("/opt/slot-hunter"),
    Path("/opt/slothunter-dev-bot"),
    Path.cwd(),
)


def _extract_image_paths(text: str) -> list[Path]:
    """Find screenshot paths Claude mentioned and that exist on disk.

    Recognises absolute paths, ``.playwright-mcp/...`` relatives, and
    bare ``screenshot-*.png`` mentions. Relative forms are resolved
    against known project roots.

    Caps at 5 to prevent runaway output from spamming the chat.
    """
    if not text:
        return []
    seen: set[str] = set()
    out: list[Path] = []

    def _push(p: Path) -> bool:
        """Append if file exists; return True if we should stop."""
        if not p.is_file():
            return False
        out.append(p)
        return len(out) >= 5

    for match in _ABS_IMAGE_PATH_RE.findall(text):
        if match in seen:
            continue
        seen.add(match)
        if _push(Path(match)):
            return out

    for match in _REL_PLAYWRIGHT_RE.findall(text):
        if match in seen:
            continue
        seen.add(match)
        for root in _RELATIVE_SEARCH_ROOTS:
            if _push(root / match):
                return out
            if (root / match).is_file():
                break  # already pushed inside _push, no need to try other roots

    for match in _BARE_SCREENSHOT_RE.findall(text):
        # Skip if this filename was already captured as part of an
        # absolute / .playwright-mcp match above.
        if any(match in s for s in seen):
            continue
        if match in seen:
            continue
        seen.add(match)
        for root in _RELATIVE_SEARCH_ROOTS:
            if _push(root / match):
                return out
            if (root / match).is_file():
                break

    return out


# ─────────── Dispatcher factory ───────────


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.update.middleware(AllowedUserMiddleware())
    dp.include_router(router)
    return dp


def build_bot() -> Bot:
    return Bot(token=settings.telegram_bot_token)
