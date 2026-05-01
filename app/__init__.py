"""Slot Hunter dev-bot — single-user Telegram wrapper around Claude Code.

Architecture overview
---------------------
- ``app.config``           — pydantic-settings: bot token, allowed user id, workdir.
- ``app.auth``             — aiogram middleware that silently drops any update
                             not originating from ``ALLOWED_USER_ID``.
- ``app.claude_session``   — long-lived ``ClaudeSDKClient`` per chat with
                             stateful follow-ups; session id persisted to
                             disk so the bot survives restarts.
- ``app.permissions``      — ``can_use_tool`` callback that asks for approval
                             via Telegram inline buttons before risky tools
                             (Bash, NotebookEdit, MCP writes …) execute.
- ``app.chunker``          — splits long Claude responses into TG-safe
                             chunks (4096-char limit) and uploads anything
                             bigger as files.
- ``app.bot`` / ``app.main`` — aiogram setup, command handlers, entrypoint.
"""

__all__: list[str] = []
