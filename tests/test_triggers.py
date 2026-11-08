"""Задание из очереди не сгорает, пока Telegram лежит.

08.11 12:19–12:21 Bot API отвечал Bad Gateway; рутина «аудит за сутки»
(slot-hunter #0158) пришла в 12:20, приветствие не отправилось — и файл
ушёл в .failed без единой повторной попытки. Рутину за день пропустили.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

import pytest

from app import triggers
from app.config import settings


class _Bot:
    def __init__(self, fail_first: int) -> None:
        self.fail_first = fail_first
        self.sent: list[str] = []

    async def send_message(self, chat_id: int, text: str, **kw: Any) -> None:
        if self.fail_first > 0:
            self.fail_first -= 1
            raise RuntimeError("Telegram server says - Bad Gateway")
        self.sent.append(text)


class _Session:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def query(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return "отчёт"


@pytest.fixture
def queue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setattr(settings, "state_dir", tmp_path)
    triggers.ensure_trigger_dir()
    sess = _Session()
    replies: list[Any] = []

    async def fake_send_reply(bot: Any, chat_id: int, reply: Any) -> None:
        replies.append(reply)

    monkeypatch.setattr(triggers, "get_or_create_session", lambda chat_id, bot: sess)
    monkeypatch.setattr(triggers, "_send_reply", fake_send_reply)
    path = tmp_path / "triggers" / "daily-audit-2026-11-08.txt"
    path.write_text("Ежедневная рутина «аудит за сутки»\n", encoding="utf-8")
    return {"dir": tmp_path / "triggers", "path": path, "sess": sess, "replies": replies}


def test_telegram_down_defers_then_runs(queue: dict[str, Any]) -> None:
    bot = _Bot(fail_first=1)
    asyncio.run(triggers.process_pending_triggers(bot))  # type: ignore[arg-type]
    assert queue["path"].exists(), "задание сгорело на первом же сбое Telegram"
    assert queue["sess"].prompts == [], "Claude запущен, хотя ответ некуда доставить"

    asyncio.run(triggers.process_pending_triggers(bot))  # type: ignore[arg-type]
    assert not queue["path"].exists()
    assert (queue["dir"] / "daily-audit-2026-11-08.done").exists()
    assert len(queue["sess"].prompts) == 1
    assert queue["replies"] == ["отчёт"]


def test_gives_up_after_max_age(queue: dict[str, Any]) -> None:
    old = time.time() - triggers._DEFER_MAX_AGE_S - 60
    os.utime(queue["path"], (old, old))
    asyncio.run(triggers.process_pending_triggers(_Bot(fail_first=1)))  # type: ignore[arg-type]
    assert (queue["dir"] / "daily-audit-2026-11-08.failed").exists()


def test_reply_retried_after_run(queue: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """Claude отработал — минутный сбой при отправке отчёт не теряет."""
    monkeypatch.setattr(triggers, "_REPLY_RETRY_DELAYS_S", (0.0, 0.0))
    calls = 0

    async def flaky_send_reply(bot: Any, chat_id: int, reply: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("Bad Gateway")
        queue["replies"].append(reply)

    monkeypatch.setattr(triggers, "_send_reply", flaky_send_reply)
    asyncio.run(triggers.process_pending_triggers(_Bot(fail_first=0)))  # type: ignore[arg-type]
    assert queue["replies"] == ["отчёт"]
    assert (queue["dir"] / "daily-audit-2026-11-08.done").exists()
