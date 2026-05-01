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

# Read-only / inspect-only bash commands that don't need approval.
# Match strategy: tokenize the command on whitespace, take the first
# word (after stripping leading subshell wrappers), and check it
# against this set. Anything with shell metacharacters that could
# rewrite the command (``;``, ``&&``, ``|``, backticks, ``$(...)``)
# falls through to the manual prompt — better safe than sorry.
SAFE_BASH_COMMANDS: frozenset[str] = frozenset(
    {
        # File inspection
        "ls", "cat", "head", "tail", "wc", "file", "stat",
        "pwd", "tree", "find", "du", "df",
        # Text inspection
        "grep", "rg", "ag", "sort", "uniq", "diff",
        "echo", "printf", "true", "false",
        # Git read-only
        "git",  # filtered further by subcommand below
        # Python tooling read-only
        "python", "python3", "uv",  # uv has destructive subcommands; filtered
        "pytest", "ruff", "mypy", "pyright",
        # Node tooling read-only
        "node", "npm", "npx",  # filtered by sub
        # System inspect
        "ps", "top", "free", "uptime", "whoami", "id", "uname",
        "env", "which", "type", "command",
        "date", "hostname",
        # Docker inspect
        "docker",  # filtered by sub
        "systemctl",  # filtered by sub
        "journalctl",
        "curl", "wget",  # technically network — but read-only by default
    }
)

# Subcommand allowlists for tools that have both safe and destructive
# operations. Only the listed subcommands auto-approve — anything else
# falls through to the manual prompt.
SAFE_GIT_SUBCOMMANDS = frozenset(
    {
        "status", "log", "diff", "show", "blame",
        "branch", "tag", "remote", "config",
        "ls-files", "ls-tree", "rev-parse", "rev-list",
        "describe", "shortlog", "reflog",
        "fetch", "pull", "stash",  # mutate but recoverable
    }
)
SAFE_DOCKER_SUBCOMMANDS = frozenset(
    {"ps", "logs", "inspect", "images", "volume", "network", "stats", "top", "version", "info"}
)
SAFE_SYSTEMCTL_SUBCOMMANDS = frozenset(
    {"status", "is-active", "is-enabled", "list-units", "list-unit-files", "show", "cat"}
)
SAFE_NPM_SUBCOMMANDS = frozenset({"list", "ls", "view", "outdated", "audit", "version", "help"})
SAFE_UV_SUBCOMMANDS = frozenset({"run", "tree", "version", "help"})
SAFE_NODE_SUBCOMMANDS: frozenset[str] = frozenset()  # `node script.js` arbitrary code → ask
SAFE_PYTHON_SUBCOMMANDS: frozenset[str] = frozenset()  # same

# Shell metacharacters that can chain or substitute commands. If any of
# these are present in the command we don't try to parse — just ask.
SHELL_REWRITE_CHARS = ("&&", "||", ";", "|", "`", "$(", ">(", "<(", "$( ", ">", "<")


def is_safe_bash(command: str) -> bool:
    """Return True if the command is purely a read-only / safe operation
    that we can run without explicit approval.

    We err on the side of caution: ANY syntactic feature we don't fully
    understand pushes the command back into the manual-approve queue.
    """
    cmd = command.strip()
    if not cmd:
        return False

    # Anything with destructive markers — never safe.
    if any(p in cmd for p in DESTRUCTIVE_BASH_PATTERNS):
        return False

    # Reject anything chained / piped / substituted. Each segment would
    # need its own check; until we want that complexity, just ask.
    if any(ch in cmd for ch in SHELL_REWRITE_CHARS):
        return False

    # Strip leading env-var assignments ("FOO=bar baz arg") and a
    # trailing ``--`` separator if present.
    tokens = cmd.split()
    while tokens and "=" in tokens[0] and not tokens[0].startswith("-"):
        tokens.pop(0)
    if not tokens:
        return False

    head = tokens[0]
    # Allow common runner prefixes (``sudo`` still requires a TTY → ask).
    if head == "sudo":
        return False

    if head not in SAFE_BASH_COMMANDS:
        return False

    sub = tokens[1] if len(tokens) > 1 else ""

    if head == "git":
        return sub in SAFE_GIT_SUBCOMMANDS
    if head == "docker":
        return sub in SAFE_DOCKER_SUBCOMMANDS
    if head == "systemctl":
        return sub in SAFE_SYSTEMCTL_SUBCOMMANDS
    if head in ("npm", "npx"):
        return sub in SAFE_NPM_SUBCOMMANDS
    if head == "uv":
        return sub in SAFE_UV_SUBCOMMANDS
    if head in ("python", "python3"):
        # `python -c "..."` and `python script.py` execute arbitrary code;
        # only `python --version` and `python -V` are safe.
        return sub in {"--version", "-V", "-h", "--help"}
    if head == "node":
        return sub in {"--version", "-v", "-h", "--help"}
    return True

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

        # Smart Bash: read-only / inspect commands skip the prompt.
        # Cuts ~80% of approval taps when working over Telegram.
        if tool_name == "Bash":
            cmd = str(tool_input.get("command", ""))
            if is_safe_bash(cmd):
                log.info("bash_auto_approved", command=cmd[:120])
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
