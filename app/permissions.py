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
from claude_agent_sdk.types import (
    PermissionResult,
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

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


# ─────────── Playwright MCP browser tools ───────────
#
# When the Playwright MCP server is enabled, Claude gets ``mcp__playwright__*``
# tools (navigate, click, type, screenshot, etc.). MCP tool names are
# ``mcp__<server>__<name>`` — they DON'T match ``allowed_tools`` literal
# strings, so we filter them in ``can_use_tool`` below.
#
# Two-tier policy:
# - **Auto**: read-only browser ops (snapshots, screenshots, console, network)
#   AND interaction ops (click, type, fill, drag) — the whole point of
#   M3 is autonomous testing, asking on every click defeats it.
# - **Ask**: anything that runs arbitrary JS (``evaluate``,
#   ``run_code_unsafe``) — those are the equivalent of Bash and need a
#   second pair of eyes.
# - **Deny**: nothing currently. ``run_code_unsafe`` is gated behind a
#   manual approval; Claude has the freedom to ask for it but it won't
#   slip through.

_BROWSER_AUTO_TOOLS = frozenset(
    {
        # Inspection — read-only by definition.
        "browser_snapshot",
        "browser_take_screenshot",
        "browser_console_messages",
        "browser_network_requests",
        "browser_network_request",
        "browser_get_page_text",
        "browser_read_page",
        "browser_inspect",
        "browser_find",
        # Navigation — observable, recoverable.
        "browser_navigate",
        "browser_navigate_back",
        "browser_resize",
        "browser_wait_for",
        "browser_tabs",
        # Interaction — needed for any autonomous test scenario.
        # Mini App is the user's own dev environment; click-to-test is
        # the primary use case, asking on each click would break the flow.
        "browser_click",
        "browser_type",
        "browser_fill",
        "browser_fill_form",
        "browser_select_option",
        "browser_press_key",
        "browser_hover",
        "browser_drag",
        "browser_drop",
        "browser_file_upload",
        "browser_handle_dialog",
        "browser_close",
        # Inspection helpers and harmless misc.
        "browser_screenshot",  # Claude_Preview alias
        "browser_resize_window",
    }
)

# These ALWAYS require user approval, even if browser auto is on.
# - evaluate runs arbitrary user-defined JS in the page context;
# - run_code_unsafe runs JS in the Playwright Node server (RCE-grade).
_BROWSER_ALWAYS_ASK = frozenset(
    {
        "browser_evaluate",
        "browser_run_code_unsafe",
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
        # File move / create — safe-ish: ``mv`` and ``cp`` can clobber,
        # but in our flow they're used for moving artifacts (logs,
        # patches, screenshots) not for blowing away source. ``rm`` is
        # NOT included — destructive deletes go through the prompt.
        "mkdir", "rmdir", "touch", "cp", "mv", "ln", "chmod", "chown",
        # Archives — read or create is safe; extract can clobber but
        # ``tar -xf foo.tar -C dir`` is normal post-deploy flow.
        "tar", "gzip", "gunzip", "bzip2", "bunzip2", "zip", "unzip",
        # Text inspection
        "grep", "rg", "ag", "sort", "uniq", "diff",
        "echo", "printf", "true", "false",
        # Git read-only + commit/amend/restore (filtered by subcommand below)
        "git",
        # Python tooling read-only
        "python", "python3", "uv",  # uv has destructive subcommands; filtered
        "pytest", "ruff", "mypy", "pyright",
        # Node tooling read-only
        "node", "npm", "npx",  # filtered by sub
        # Build runner — `make <target>` is whatever Makefile defines.
        # In our repos targets are read/lint/test/format/dev-up — all safe;
        # if someone adds a destructive target, that's a Makefile policy,
        # not a bash policy. Pragmatic call: skip the prompt on `make`.
        "make",
        # System inspect
        "ps", "top", "free", "uptime", "whoami", "id", "uname",
        "env", "which", "type", "command",
        "date", "hostname",
        # Docker inspect
        "docker",  # filtered by sub
        "systemctl",  # filtered by sub — incl. restart/reload
        "journalctl",
        "curl", "wget",  # technically network — but read-only by default
    }
)

# Subcommand allowlists for tools that have both safe and destructive
# operations. Only the listed subcommands auto-approve — anything else
# falls through to the manual prompt.
#
# Policy for the day-to-day flow (commit / amend / restart / sync deps):
# auto-approve everything that's reversible from local history. The line
# we don't cross without explicit consent: ``push`` (touches remote, can
# be force-pushed elsewhere later), ``rebase`` / ``merge --no-ff`` /
# anything that rewrites history beyond the most recent commit.
SAFE_GIT_SUBCOMMANDS = frozenset(
    {
        # Read-only inspection.
        "status", "log", "diff", "show", "blame",
        "branch", "tag", "remote", "config",
        "ls-files", "ls-tree", "rev-parse", "rev-list",
        "describe", "shortlog", "reflog",
        # Mutate but cheap-to-recover (worktree only, no remote, no
        # history rewrite).
        "fetch", "pull", "stash",
        "add", "restore", "checkout",
        "commit",  # incl. --amend; amending unpushed commits is normal flow
        "mv", "rm",  # tracked-file ops; bash-level rm is filtered separately
        "format-patch", "am", "apply", "bundle",
        "cherry-pick", "revert",
        "init", "clone",
    }
)
SAFE_DOCKER_SUBCOMMANDS = frozenset(
    {"ps", "logs", "inspect", "images", "volume", "network", "stats", "top", "version", "info"}
)
# systemctl: status/inspect AND restart/reload/start/stop. The bot
# routinely restarts itself / slot-hunter services after deploys; we
# can't have it ask on every cycle. Disabling / masking units is still
# manual — those persist beyond the current process and shouldn't be
# silent.
SAFE_SYSTEMCTL_SUBCOMMANDS = frozenset(
    {
        "status", "is-active", "is-enabled", "list-units",
        "list-unit-files", "show", "cat",
        "restart", "reload", "start", "stop", "try-restart",
        "daemon-reload",
    }
)
# npm: install/uninstall are routine for dev-bot deps; "rm/uninstall"
# remove a package which is recoverable via package-lock + reinstall.
SAFE_NPM_SUBCOMMANDS = frozenset(
    {
        "list", "ls", "view", "outdated", "audit", "version", "help",
        "install", "i", "ci", "update",
    }
)
# uv: sync / add / remove are how dependencies get touched. lock writes
# uv.lock — recoverable from git. Everything destructive (clean cache)
# stays manual.
SAFE_UV_SUBCOMMANDS = frozenset(
    {
        "run", "tree", "version", "help",
        "sync", "add", "remove", "lock", "pip", "tool",
    }
)
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


# ─────────── Catastrophic patterns — NEVER auto-approved, even in YOLO ──
#
# Broker-level YOLO (``yolo_provider`` returning True) lets Claude run
# anything without asking. The user explicitly opted into that. But
# there's a tiny "pull the plug" list we still gate: things that brick
# the VPS, exfiltrate the entire disk, or are textbook attacker
# payloads. Claude has no business running these even in autopilot.
#
# Each match falls back to the broker prompt, so the user can still
# explicitly OK it if they really meant to.
CATASTROPHIC_BASH_PATTERNS = (
    "rm -rf /",
    "rm -rf /*",
    "rm -rf ~",
    "rm -rf $HOME",
    "rm -rf /etc",
    "rm -rf /var",
    "rm -rf /root",
    "rm -rf /home",
    "dd if=/dev/zero of=/dev/",
    "dd if=/dev/random of=/dev/",
    "mkfs",
    "fdisk",
    "wipefs",
    ":(){:|:&};:",  # forkbomb (compact form)
    ": ( ) { :|:& };:",  # forkbomb (spaced)
    "shutdown",
    "reboot",
    "halt",
    "poweroff",
    "iptables -F",
    "ufw disable",
    "userdel",
    "passwd root",
    "chmod -R 000 /",
    "chmod 000 /",
)


def is_catastrophic_bash(command: str) -> bool:
    """True if the command matches a "never auto-run" pattern.

    Used by the YOLO path: we trust Claude on everything *except* this.
    Substring match — paranoid by design, false positives just mean an
    extra approval tap.
    """
    cmd = command.strip()
    if not cmd:
        return False
    return any(p in cmd for p in CATASTROPHIC_BASH_PATTERNS)


CanUseToolCallback = Callable[
    [str, dict[str, Any], ToolPermissionContext],
    Awaitable[PermissionResult],
]
YoloProvider = Callable[[], bool]


def make_can_use_tool(
    broker: PermissionBroker,
    *,
    yolo: YoloProvider | None = None,
) -> CanUseToolCallback:
    """Return a ``can_use_tool`` async callback bound to this broker.

    Signature comes from ``claude_agent_sdk``: receives the tool name,
    input dict, and a ``ToolPermissionContext``; must return a
    ``PermissionResultAllow`` or ``PermissionResultDeny`` instance.

    YOLO mode (``yolo()`` returns True) — broker-level bypass:
    we return Allow for every tool **without** asking. This works
    under root, where the SDK's own ``permission_mode="bypassPermissions"``
    refuses to run (Claude CLI hardcodes a no-bypass-as-root sanity check).
    Catastrophic bash patterns (``rm -rf /``, ``dd``, forkbomb, etc.)
    still go through the broker — even in autopilot the user gets one
    last chance to bail.

    The provider is called on every tool invocation, so toggling
    ``/yolo on|off`` takes effect on the very next tool call without a
    client rebuild.
    """

    async def callback(
        tool_name: str,
        tool_input: dict[str, Any],
        context: ToolPermissionContext,  # noqa: ARG001 — not currently inspected
    ) -> PermissionResult:
        # AUTO_TOOLS shouldn't reach here (pre-allowed via allowed_tools)
        # but we double-belt-check.
        if tool_name in AUTO_TOOLS:
            return PermissionResultAllow(updated_input=tool_input)

        # MCP tool names look like ``mcp__<server>__<name>``. Strip the
        # prefix to match against our local whitelists. We don't bother
        # validating the server name — having Playwright MCP enabled is
        # already opt-in via env var.
        local_name = tool_name
        if tool_name.startswith("mcp__"):
            parts = tool_name.split("__", 2)
            if len(parts) == 3:
                local_name = parts[2]

        # YOLO short-circuit. Order: catastrophic-check, then auto-allow.
        # Browser MCP "always ask" patterns also bypass YOLO — those are
        # Claude-running-arbitrary-JS, dangerous regardless of mode.
        is_yolo = yolo() if yolo is not None else False
        if is_yolo:
            if tool_name == "Bash":
                cmd = str(tool_input.get("command", ""))
                if is_catastrophic_bash(cmd):
                    log.warning("yolo_catastrophic_blocked", command=cmd[:120])
                    allowed, reason = await broker.request(tool_name, tool_input)
                    if allowed:
                        return PermissionResultAllow(updated_input=tool_input)
                    return PermissionResultDeny(
                        message=reason or "Отклонено пользователем",
                        interrupt=False,
                    )
            if local_name in _BROWSER_ALWAYS_ASK:
                # browser_evaluate / run_code_unsafe — same risk as
                # arbitrary JS, ask even in YOLO.
                allowed, reason = await broker.request(tool_name, tool_input)
                if allowed:
                    return PermissionResultAllow(updated_input=tool_input)
                return PermissionResultDeny(
                    message=reason or "Отклонено пользователем",
                    interrupt=False,
                )
            log.info("yolo_auto_approved", tool=tool_name)
            return PermissionResultAllow(updated_input=tool_input)

        # Browser tool policy (non-YOLO).
        if local_name in _BROWSER_AUTO_TOOLS:
            log.info("browser_auto_approved", tool=tool_name)
            return PermissionResultAllow(updated_input=tool_input)
        if local_name in _BROWSER_ALWAYS_ASK:
            allowed, reason = await broker.request(tool_name, tool_input)
            if allowed:
                return PermissionResultAllow(updated_input=tool_input)
            return PermissionResultDeny(
                message=reason or "Отклонено пользователем",
                interrupt=False,
            )

        # Smart Bash: read-only / inspect commands skip the prompt.
        # Cuts ~80% of approval taps when working over Telegram.
        if tool_name == "Bash":
            cmd = str(tool_input.get("command", ""))
            if is_safe_bash(cmd):
                log.info("bash_auto_approved", command=cmd[:120])
                return PermissionResultAllow(updated_input=tool_input)

        allowed, reason = await broker.request(tool_name, tool_input)
        if allowed:
            return PermissionResultAllow(updated_input=tool_input)
        return PermissionResultDeny(
            message=reason or "Отклонено пользователем",
            interrupt=False,
        )

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
