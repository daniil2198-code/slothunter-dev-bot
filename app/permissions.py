"""Tool-use permission flow over Telegram inline buttons.

Permission policy (matches "Read+Write+lint auto, dangerous → ask"):

* ``Read``, ``Glob``, ``Grep``, ``Edit``, ``Write``, ``NotebookRead``
  → auto-approve. The user explicitly opted into "easy mode".
* ``Bash`` → ALWAYS ask. Bash is where prod gets nuked.
  ↳ Some bash commands carry an extra "destructive" flag: ``rm -rf``,
    ``git push --force``, ``git reset --hard`` (without origin/main),
    ``DROP``, ``TRUNCATE``. Same flow, just the message is louder.
* Everything else (``WebFetch``, ``Task``, MCP tools, …) → ask.

How the round-trip works:
  1. Claude asks to use a tool → ``can_use_tool`` callback fires.
  2. We post an inline-keyboard message in the active chat:
        ✅ Разрешить   ❌ Отклонить
  3. ``callback_query`` from the user resolves an ``asyncio.Future`` we
     parked, the callback returns ``allow`` / ``deny``.
  4. If the user ignores it for >2 minutes the future times out → deny.

We can only run one "ask" at a time per chat (Claude is sequential
inside a session), so a single ``_pending`` slot per chat is enough.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.logging import get_logger

log = get_logger(__name__)


# ─────────── Policy ───────────

# Tools auto-approved; never reach the can_use_tool callback because we
# pre-allow them via ``ClaudeAgentOptions.allowed_tools``. Listed here for
# clarity / future audit.
AUTO_TOOLS = frozenset(
    {
        "Read",
        "Glob",
        "Grep",
        "Edit",
        "Write",
        "MultiEdit",
        "NotebookRead",
        "TodoWrite",
    }
)

# Bash commands that warrant a louder warning. Substring match,
# case-sensitive — these are command-line tokens, not English.
DESTRUCTIVE_BASH_PATTERNS = (
    "rm -rf",
    "rm -fr",
    "git push --force",
    "git push -f",
    "git reset --hard",
    "git clean -",
    "DROP TABLE",
    "TRUNCATE",
    "docker system prune",
    "shutdown",
    "reboot",
)

ASK_TIMEOUT_S = 120.0


# ─────────── Pending-approval state ───────────


@dataclass
class _Pending:
    future: asyncio.Future[bool]
    tool_name: str
    tool_input: dict[str, Any]
    summary: str  # short human-readable description, used in callback msg
    message_id: int | None = None


@dataclass
class PermissionBroker:
    """One-pending-approval-per-chat queue.

    Owned by the bot's lifetime; injected into the per-chat
    ``ClaudeAgentOptions.can_use_tool`` callback via a closure. The
    permissions module knows nothing about aiogram dispatchers — the
    bot module wires callback_query handlers that call ``resolve``.
    """

    bot: Bot
    chat_id: int
    _pending: _Pending | None = field(default=None, init=False)

    async def request(
        self, tool_name: str, tool_input: dict[str, Any]
    ) -> tuple[bool, str | None]:
        """Ask the user via TG; return ``(allowed, deny_reason)``.

        deny_reason is set on rejection or timeout — Claude shows it
        verbatim. Acceptance returns ``(True, None)``.
        """
        if self._pending is not None:
            # Shouldn't happen with a single Claude session per chat, but
            # be defensive — refuse rather than silently overwrite.
            return False, "Уже жду подтверждения по другому действию"

        summary = _summarize(tool_name, tool_input)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[bool] = loop.create_future()
        self._pending = _Pending(
            future=fut, tool_name=tool_name, tool_input=tool_input, summary=summary
        )

        text = _format_ask(tool_name, tool_input, summary)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Разрешить", callback_data="perm:allow"),
                    InlineKeyboardButton(text="❌ Отклонить", callback_data="perm:deny"),
                ]
            ]
        )

        try:
            msg = await self.bot.send_message(
                chat_id=self.chat_id,
                text=text,
                reply_markup=kb,
                parse_mode="HTML",
            )
            self._pending.message_id = msg.message_id
        except Exception as e:  # noqa: BLE001 — TG can fail in many ways
            log.warning("perm_send_failed", error=str(e), tool=tool_name)
            self._pending = None
            return False, f"Не смог отправить запрос на разрешение: {e}"

        try:
            allowed = await asyncio.wait_for(fut, timeout=ASK_TIMEOUT_S)
            reason = None if allowed else "Отклонено"
        except TimeoutError:
            allowed = False
            reason = f"Таймаут — нет ответа за {int(ASK_TIMEOUT_S)} сек"
            await self._strip_keyboard("⌛ Таймаут — отклонено")
        finally:
            self._pending = None
        return allowed, reason

    async def resolve(self, *, allow: bool) -> bool:
        """Called by the bot when the user taps a button.

        Returns True if there was a pending request to resolve.
        """
        if self._pending is None or self._pending.future.done():
            return False
        self._pending.future.set_result(allow)
        verb = "разрешено" if allow else "отклонено"
        await self._strip_keyboard(f"{'✅' if allow else '❌'} {verb}")
        return True

    async def _strip_keyboard(self, suffix: str) -> None:
        """Remove inline buttons after a decision/timeout to avoid double-tap."""
        if self._pending is None or self._pending.message_id is None:
            return
        try:
            with contextlib.suppress(Exception):
                await self.bot.edit_message_reply_markup(
                    chat_id=self.chat_id,
                    message_id=self._pending.message_id,
                    reply_markup=None,
                )
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=f"<i>{html.escape(suffix)}</i>",
                parse_mode="HTML",
            )
        except Exception as e:  # noqa: BLE001
            log.debug("perm_strip_failed", error=str(e))


# ─────────── can_use_tool callback factory ───────────


CanUseToolCallback = Callable[[str, dict[str, Any], Any], Awaitable[dict[str, Any]]]


def make_can_use_tool(broker: PermissionBroker) -> CanUseToolCallback:
    """Return a ``can_use_tool`` async callback bound to this broker.

    Signature comes from ``claude_agent_sdk``: receives the tool name,
    input dict, and a context object; returns a dict with ``behavior``
    set to either ``allow`` or ``deny`` (with optional ``message``).
    """

    async def callback(
        tool_name: str,
        tool_input: dict[str, Any],
        context: Any,  # noqa: ARG001 — not currently inspected
    ) -> dict[str, Any]:
        # AUTO_TOOLS shouldn't reach here (pre-allowed via allowed_tools)
        # but we double-belt-check.
        if tool_name in AUTO_TOOLS:
            return {"behavior": "allow", "updatedInput": tool_input}

        allowed, reason = await broker.request(tool_name, tool_input)
        if allowed:
            return {"behavior": "allow", "updatedInput": tool_input}
        return {
            "behavior": "deny",
            "message": reason or "Отклонено пользователем",
        }

    return callback


# ─────────── Formatting helpers ───────────


def _summarize(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Single-line human description of what Claude wants to do."""
    if tool_name == "Bash":
        cmd = str(tool_input.get("command", "")).strip()
        if len(cmd) > 200:
            cmd = cmd[:200] + "…"
        return f"$ {cmd}"
    if tool_name in ("WebFetch", "WebSearch"):
        return str(tool_input.get("url") or tool_input.get("query") or "")
    return json.dumps(tool_input, ensure_ascii=False)[:300]


def _format_ask(tool_name: str, tool_input: dict[str, Any], summary: str) -> str:
    """HTML-formatted approval prompt."""
    lines = [f"🔧 <b>{html.escape(tool_name)}</b>"]
    if tool_name == "Bash":
        cmd = str(tool_input.get("command", ""))
        is_destructive = any(p in cmd for p in DESTRUCTIVE_BASH_PATTERNS)
        if is_destructive:
            lines.insert(0, "⚠️ <b>ВНИМАНИЕ — деструктивная команда</b>")
        lines.append(f"<pre>{html.escape(summary[:1500])}</pre>")
    else:
        lines.append(f"<code>{html.escape(summary[:1500])}</code>")
    lines.append("")
    lines.append(f"Таймаут: {int(ASK_TIMEOUT_S)} сек.")
    return "\n".join(lines)
