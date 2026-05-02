"""File-based trigger queue — let other processes ask the bot to do things.

Why files (not HTTP):
- Zero new ports / surface area — only ``root`` on the VPS can drop a
  file into ``state_dir/triggers/`` anyway.
- Survives bot restarts — pending files are still there when the bot
  comes back up.
- Trivially audit-able: ``ls /var/lib/slothunter-dev-bot/triggers/``
  shows what's been queued.

Format:
- Drop ``<anything>.txt`` into ``state_dir/triggers/``. The first line
  is the prompt to feed to Claude. Empty / whitespace-only lines are
  skipped.
- The scheduler invokes ``process_pending_triggers(bot)`` once a minute.
  Each .txt is processed in lexicographic order, then renamed to
  ``.done`` (auditable trail). Errors → ``.failed`` so we can retry by
  hand if needed.

Currently the only producer is ``slot-hunter/scripts/deploy.sh`` which
writes ``/test mini-app-home-loads`` after a successful prod deploy.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import TYPE_CHECKING

from app.bot import _send_reply, build_e2e_prompt, get_or_create_session
from app.config import settings
from app.logging import get_logger

if TYPE_CHECKING:
    from aiogram import Bot

log = get_logger(__name__)


def _trigger_dir() -> Path:
    return settings.state_dir / "triggers"


def ensure_trigger_dir() -> Path:
    """Create the trigger dir if missing. Idempotent.

    Called on bot startup so external producers (deploy.sh) can rely
    on the dir existing without each one mkdir'ing it themselves. Also
    chmods to 0o700 since the dir contains queued prompts that may
    reference dev-tokens / project paths.
    """
    d = _trigger_dir()
    d.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        d.chmod(0o700)
    return d


async def process_pending_triggers(bot: Bot) -> None:
    """Drain the trigger directory.

    Each ``.txt`` file → one Claude turn. Side effects:
    - Bot sends a heads-up message to ``settings.allowed_user_id`` so
      the user knows an automated test is starting (otherwise a deploy
      reply with no preamble is confusing).
    - Result is sent as a normal bot reply via ``_send_reply``.
    - File is renamed to ``.done`` (success) or ``.failed`` (exception).
    """
    trig_dir = _trigger_dir()
    if not trig_dir.is_dir():
        return

    files = sorted(p for p in trig_dir.iterdir() if p.suffix == ".txt")
    if not files:
        return

    chat_id = settings.allowed_user_id

    for path in files:
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError as e:
            log.warning("trigger_read_failed", path=str(path), error=str(e))
            _mark_failed(path)
            continue

        # First non-empty line is the prompt. Anything after is context
        # (we still feed it all but the line is what we log).
        first_line = next((ln for ln in text.splitlines() if ln.strip()), "")
        if not first_line:
            log.warning("trigger_empty", path=str(path))
            _mark_done(path)
            continue

        log.info("trigger_processing", path=str(path), prompt_head=first_line[:80])
        try:
            await bot.send_message(
                chat_id,
                f"🤖 <i>авто-триггер: {first_line[:200]}</i>",
                parse_mode="HTML",
            )
            sess = get_or_create_session(chat_id, bot)
            # If the trigger is a slash-command we know how to handle
            # natively (e.g. ``/test mini-app-home-loads``), translate
            # it to the same prompt our Aiogram handler would build.
            # Plain ``sess.query("/test foo")`` would otherwise reach
            # Claude Code as a builtin slash-command attempt and fail
            # with "Unknown command: /test".
            prompt = _translate_slash_command(first_line) or text
            reply = await sess.query(prompt)
            await _send_reply(bot, chat_id, reply)
        except Exception as e:  # noqa: BLE001 — never crash the scheduler
            log.exception("trigger_failed", path=str(path), error=str(e))
            _mark_failed(path)
            with contextlib.suppress(Exception):
                await bot.send_message(
                    chat_id,
                    f"❌ Авто-триггер упал ({type(e).__name__}): "
                    f"<code>{str(e)[:200]}</code>",
                    parse_mode="HTML",
                )
            continue

        _mark_done(path)


def _translate_slash_command(line: str) -> str | None:
    """Map a leading slash-command in a trigger to a Claude prompt.

    Triggers are dropped as plain text by other processes (deploy.sh
    writes ``/test mini-app-home-loads``). When the file content
    starts with a slash-command we recognize, we want to invoke the
    same logic the Aiogram handler would — but those handlers expect
    a ``Message`` object we don't have here. So we re-derive the
    prompt directly via shared helpers in ``app.bot``.

    Returns the translated prompt, or ``None`` if the line is just
    plain text (in which case the caller forwards it as-is to Claude).
    """
    line = line.strip()
    if not line.startswith("/"):
        return None

    # ``/test <scenario>`` — re-use the e2e prompt builder.
    if line.startswith("/test "):
        name = line.split(maxsplit=1)[1].strip().split()[0]
        prompt, err = build_e2e_prompt(name)
        if prompt is not None:
            return prompt
        log.warning("trigger_test_unknown_scenario", name=name, error=err)
        return f"Не нашёл e2e-сценария «{name}». Скажи это в TG чтобы юзер увидел."

    # Unknown slash-command — fall back to plain text. Claude will
    # likely complain about it but at least we logged the case.
    log.warning("trigger_unknown_slash", line=line[:80])
    return None


def _mark_done(path: Path) -> None:
    target = path.with_suffix(".done")
    try:
        path.rename(target)
    except OSError as e:
        log.warning("trigger_rename_done_failed", path=str(path), error=str(e))


def _mark_failed(path: Path) -> None:
    target = path.with_suffix(".failed")
    try:
        path.rename(target)
    except OSError as e:
        log.warning("trigger_rename_failed_failed", path=str(path), error=str(e))
