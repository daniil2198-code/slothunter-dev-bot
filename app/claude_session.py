"""Per-chat Claude SDK session.

One ``ClaudeSDKClient`` lives per Telegram chat for the whole bot
lifetime. Sequential interaction model: a new user message waits if a
previous one is still streaming. That mirrors how Claude Code behaves
in a real terminal — no interleaved replies.

Session state on disk:

    state_dir/
      └── chat_<chat_id>.json        # {"session_id": "...", "cwd": "..."}

The ``session_id`` from each ``ResultMessage`` is persisted so a bot
restart can resume the conversation via ``ClaudeAgentOptions.resume``.
``cwd`` lets the user switch projects via ``/cd`` (future).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from app.config import settings
from app.git_sync import maybe_pull_workdir
from app.logging import get_logger
from app.permissions import AUTO_TOOLS, PermissionBroker, make_can_use_tool

log = get_logger(__name__)


# ─────────── Persistent state ───────────


@dataclass
class ChatState:
    """What we remember about a chat across restarts."""

    chat_id: int
    session_id: str | None = None
    cwd: Path = field(default_factory=lambda: settings.default_workdir)

    @classmethod
    def load(cls, chat_id: int) -> ChatState:
        path = _state_path(chat_id)
        if not path.exists():
            return cls(chat_id=chat_id)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            log.warning("state_load_failed", chat_id=chat_id, error=str(e))
            return cls(chat_id=chat_id)
        return cls(
            chat_id=chat_id,
            session_id=data.get("session_id"),
            cwd=Path(data.get("cwd") or settings.default_workdir),
        )

    def save(self) -> None:
        path = _state_path(self.chat_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {"session_id": self.session_id, "cwd": str(self.cwd)},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        tmp.replace(path)


def _state_path(chat_id: int) -> Path:
    return settings.state_dir / f"chat_{chat_id}.json"


# ─────────── Session wrapper ───────────


@dataclass
class StreamedReply:
    """What we send back to the bot after a turn completes.

    Fields are concatenated text the user should see, plus tool-use
    activity (we surface tool names and short summaries, not raw input).
    """

    text: str = ""
    tool_calls: list[str] = field(default_factory=list)
    session_id: str | None = None
    error: str | None = None
    cancelled: bool = False
    # Optional pre-message (rendered above the answer) — currently only
    # used by the auto-pull feature to surface "git pull: a1b → c2d".
    pre_note: str | None = None


class ChatSession:
    """One Claude conversation per chat.

    The SDK client is created lazily on the first user message and
    reused thereafter. ``query()`` is the only public coroutine.
    """

    def __init__(self, chat_id: int, broker: PermissionBroker) -> None:
        self.chat_id = chat_id
        self.broker = broker
        self.state = ChatState.load(chat_id)
        self._client: ClaudeSDKClient | None = None
        self._lock = asyncio.Lock()
        # Set when the user asks to cancel the current turn (/cancel).
        self._cancel_event = asyncio.Event()

    async def reset(self) -> str | None:
        """Drop any cached client + erase session_id (Claude starts fresh).

        Before wiping, asks Claude for a ``/compact`` summary and stores
        it via ``app.history``. Returns the title of the saved entry
        (so the caller can confirm "сохранил X") or ``None`` if there
        was nothing to save (no active session) or saving failed.

        Saving is best-effort — a /reset must always succeed even if
        Claude or the filesystem cooperate poorly.
        """
        from app.history import save_summary  # local import to avoid cycle

        saved_title: str | None = None

        async with self._lock:
            if self.state.session_id and self._client is not None:
                # Ask Claude to summarize before we drop the session.
                # /compact is a builtin Claude Code slash-command — the
                # response goes through the regular message stream.
                try:
                    await self._client.query(
                        "/compact опиши коротко (3-6 предложений) о чём была сессия "
                        "и какие итоги, чтобы я мог потом вернуться к этому контексту."
                    )
                    summary_reply = await self._collect_reply()
                except Exception as e:  # noqa: BLE001
                    log.warning("compact_before_reset_failed", error=str(e))
                else:
                    entry = save_summary(self.chat_id, summary_reply.text)
                    if entry is not None:
                        saved_title = entry.title

            await self._close_client()
            self.state.session_id = None
            self.state.save()

        return saved_title

    def request_cancel(self) -> None:
        """Signal the active turn to stop. Best-effort — Claude finishes
        the current tool use before bailing."""
        self._cancel_event.set()

    async def query(self, user_text: str) -> StreamedReply:
        """Send a message; return when Claude finishes the turn."""
        async with self._lock:
            self._cancel_event.clear()
            try:
                # Sync cwd with origin so the bot doesn't write on top of
                # commits the user pushed from their laptop. Best-effort —
                # never blocks the turn on git errors.
                pull_note = await maybe_pull_workdir(self.state.cwd)
                await self._ensure_client()
                assert self._client is not None
                await self._client.query(user_text)
                reply = await self._collect_reply()
                if pull_note:
                    reply.pre_note = pull_note
                return reply
            except Exception as e:  # noqa: BLE001 — surface anything to TG
                log.exception("query_failed", chat_id=self.chat_id)
                return StreamedReply(error=f"{type(e).__name__}: {e}")

    async def aclose(self) -> None:
        async with self._lock:
            await self._close_client()

    async def seed_with_summary(self, summary: str) -> None:
        """Start a fresh session pre-loaded with a previous-session
        summary so Claude has the context.

        Implementation: drop any current session (without saving — the
        caller is doing this on purpose), then send the summary as the
        opening user message wrapped in a context block. The next
        regular ``query()`` then continues normally.
        """
        async with self._lock:
            await self._close_client()
            self.state.session_id = None
            self.state.save()

            await self._ensure_client()
            assert self._client is not None
            seed = (
                "Контекст из предыдущей сессии (для справки, не отвечай "
                "на это сообщение, просто запомни):\n\n"
                f"{summary}\n\n"
                "Готов к новым задачам в этом контексте."
            )
            await self._client.query(seed)
            # Drain the response so the session_id gets saved + the
            # acknowledgement doesn't leak into the user's first turn.
            await self._collect_reply()

    # ─────────── internals ───────────

    async def _ensure_client(self) -> None:
        if self._client is not None:
            return
        opts: dict[str, object] = {
            "cwd": str(self.state.cwd),
            "allowed_tools": sorted(AUTO_TOOLS),
            "permission_mode": "default",  # ask for everything not in allowed_tools
            "can_use_tool": make_can_use_tool(self.broker),
            "resume": self.state.session_id,  # None on first run; SDK ignores
            "system_prompt": _system_prompt(),
        }
        if settings.model:
            opts["model"] = settings.model
        if settings.betas:
            opts["betas"] = settings.betas
        options = ClaudeAgentOptions(**opts)  # type: ignore[arg-type]
        self._client = ClaudeSDKClient(options=options)
        await self._client.connect()

    async def _close_client(self) -> None:
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.disconnect()
        self._client = None

    async def _collect_reply(self) -> StreamedReply:
        """Drain the SDK's message stream until ResultMessage."""
        assert self._client is not None
        out = StreamedReply()
        text_chunks: list[str] = []

        async for msg in self._client.receive_response():
            if self._cancel_event.is_set():
                out.cancelled = True
                # Best-effort cancel: just return what we have. The SDK
                # will keep streaming silently in the background until
                # the turn ends; the next query() call will see it
                # finished.
                break

            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        text_chunks.append(block.text)
                    elif isinstance(block, ThinkingBlock):
                        # Don't surface thinking in TG — too noisy.
                        pass
                    elif isinstance(block, ToolUseBlock):
                        out.tool_calls.append(_format_tool_call(block))
            elif isinstance(msg, UserMessage):
                # Tool results come back as UserMessage(content=[ToolResultBlock])
                # — already echoed by Claude in its next AssistantMessage.
                # Surface only as a debug breadcrumb.
                for block in msg.content if isinstance(msg.content, list) else []:
                    if isinstance(block, ToolResultBlock):
                        log.debug("tool_result", tool_use_id=block.tool_use_id)
            elif isinstance(msg, SystemMessage):
                log.debug("system_msg", subtype=getattr(msg, "subtype", None))
            elif isinstance(msg, ResultMessage):
                out.session_id = msg.session_id
                if out.session_id and out.session_id != self.state.session_id:
                    self.state.session_id = out.session_id
                    self.state.save()
                if msg.is_error:
                    out.error = (
                        f"{getattr(msg, 'subtype', 'error')}: "
                        f"{getattr(msg, 'result', None) or 'see logs'}"
                    )
                break

        out.text = "\n\n".join(t for t in text_chunks if t).strip()
        return out


def _format_tool_call(block: ToolUseBlock) -> str:
    """Short one-line label for the tool-call breadcrumb in the bot reply."""
    name = block.name
    inp = block.input
    if not isinstance(inp, dict):
        return name
    if name == "Bash":
        cmd = str(inp.get("command", ""))[:120]
        return f"🔧 Bash: {cmd}"
    if name in ("Read", "Edit", "Write", "MultiEdit"):
        path = str(inp.get("file_path") or inp.get("path") or "")
        return f"🔧 {name}: {path}"
    if name == "Grep":
        pat = str(inp.get("pattern", ""))[:80]
        return f"🔧 Grep: /{pat}/"
    if name == "Glob":
        return f"🔧 Glob: {inp.get('pattern')}"
    return f"🔧 {name}"


def _system_prompt() -> str:
    """Append a short note on top of Claude Code's preset.

    The default preset already pulls in CLAUDE.md / notes/* from cwd,
    so we don't repeat conventions here — just nudge the assistant
    about the unusual transport (Telegram).
    """
    return (
        "Ты работаешь на VPS, общаешься с пользователем через Telegram-бота."
        " Длинные простыни выводятся плохо — дави на лаконичность,"
        " а большие куски кода пиши в файлы, не цитируй обратно в чат."
        " Когда работа завершена и есть, что показать — дай короткое"
        " резюме того, что сделал, и предложи следующий шаг."
    )
