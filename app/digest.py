"""Daily morning digest — what happened in the project over the last 24h.

Composed entirely from cheap local probes:
- ``git log`` against the slot-hunter repo (commit count + first lines)
- diff in ``notes/ROADMAP.md`` (which task lines changed)
- HTTP GET on the prod healthz endpoint
- ``journalctl --since=yesterday`` grep for ERROR / WARN

All probes are best-effort: a missing tool or a network blip turns into
"data unavailable" line, not a crash. The whole digest must always send,
because if it doesn't fire on time the user has no signal that the bot
is alive.

Output is a single Telegram-safe HTML string. No file attachments.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from app.config import settings
from app.logging import get_logger

log = get_logger(__name__)

# Awaitable callable that the scheduler / /digest command pass in to
# decouple this module from aiogram.
Send = Callable[[str], Awaitable[None]]

# Probes share this timeout — we never want a single hang to block the
# whole morning send.
_PROBE_TIMEOUT_S = 8.0


@dataclass(frozen=True)
class CommitsBlock:
    count: int
    lines: list[str]
    error: str | None = None


@dataclass(frozen=True)
class RoadmapBlock:
    in_progress: int
    new_done: list[str]
    error: str | None = None


@dataclass(frozen=True)
class HealthBlock:
    ok: bool
    detail: str
    error: str | None = None


@dataclass(frozen=True)
class LogBlock:
    error_count: int
    warn_count: int
    sample: list[str]
    error: str | None = None


# ─────────── Public ───────────


async def build_digest_html(now: datetime | None = None) -> str:
    """Run all probes in parallel and render an HTML digest message."""
    repo = settings.digest_repo
    now = now or datetime.now()
    since = now - timedelta(days=1)

    # Kick off all probes concurrently so a slow probe doesn't block.
    commits, roadmap, health, logs = await asyncio.gather(
        _probe_commits(repo, since),
        _probe_roadmap(repo, since),
        _probe_health(),
        _probe_logs(since),
    )

    return _render(commits, roadmap, health, logs, now)


# ─────────── Probes ───────────


async def _probe_commits(repo: Path, since: datetime) -> CommitsBlock:
    """Count commits and grab their subjects since ``since``."""
    if not (repo / ".git").exists():
        return CommitsBlock(0, [], error="repo missing")
    try:
        out = await _run(
            "git",
            "-C",
            str(repo),
            "log",
            f"--since={since.strftime('%Y-%m-%d %H:%M:%S')}",
            "--pretty=%h %s",
        )
    except _ProbeFailed as e:
        return CommitsBlock(0, [], error=e.detail)
    lines = [ln for ln in out.splitlines() if ln.strip()]
    return CommitsBlock(len(lines), lines[:6])


async def _probe_roadmap(repo: Path, since: datetime) -> RoadmapBlock:
    """Look at ``notes/ROADMAP.md`` — count "In progress" entries now,
    and find tasks moved into Done in the last 24h via ``git log -p``."""
    roadmap = repo / "notes" / "ROADMAP.md"
    if not roadmap.exists():
        return RoadmapBlock(0, [], error="ROADMAP.md missing")

    # Count current in-progress lines: rows in the In-progress section
    # that look like a task table row (start with `|` and have an md link).
    try:
        text = roadmap.read_text(encoding="utf-8")
    except OSError as e:
        return RoadmapBlock(0, [], error=str(e))

    in_progress = 0
    in_section = False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("##"):
            in_section = "in progress" in s.lower() or "🔥" in s
            continue
        if in_section and s.startswith("|") and "tasks/" in s and "---" not in s:
            in_progress += 1

    # Find newly-Done tasks via git diff for ROADMAP.md over the window.
    new_done: list[str] = []
    try:
        diff = await _run(
            "git",
            "-C",
            str(repo),
            "log",
            f"--since={since.strftime('%Y-%m-%d %H:%M:%S')}",
            "-p",
            "--no-color",
            "--",
            "notes/ROADMAP.md",
        )
        # Naive scan: ``+ | [0017](tasks/0017-foo.md) | Title | 2026-... |``
        # represents a Done-table addition. We don't care about exact
        # column positions, just want to surface task numbers / titles.
        for line in diff.splitlines():
            if not line.startswith("+") or line.startswith("+++"):
                continue
            m = re.search(r"\[(\d{4})\]\([^)]+\)\s*\|\s*([^|]+?)\s*\|", line)
            if m:
                new_done.append(f"#{m.group(1)} {m.group(2).strip()}")
    except _ProbeFailed as e:
        return RoadmapBlock(in_progress, [], error=e.detail)

    # Dedup preserving order.
    seen = set()
    deduped: list[str] = []
    for item in new_done:
        if item not in seen:
            seen.add(item)
            deduped.append(item)
    return RoadmapBlock(in_progress, deduped[:5])


async def _probe_health() -> HealthBlock:
    """GET the configured healthz URL; surface the response shape."""
    url = settings.digest_healthz.strip()
    if not url:
        return HealthBlock(False, "skipped (DIGEST_HEALTHZ unset)")
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_S) as client:
            resp = await client.get(url)
    except (httpx.HTTPError, OSError) as e:
        return HealthBlock(False, "unreachable", error=str(e))
    detail = f"HTTP {resp.status_code}"
    return HealthBlock(resp.status_code == 200, detail)


async def _probe_logs(since: datetime) -> LogBlock:
    """Run ``journalctl --since=... -p warning -u <units>`` and count rows."""
    units = settings.digest_log_units_list
    if not units:
        return LogBlock(0, 0, [], error="skipped (DIGEST_LOG_UNITS unset)")

    args = [
        "journalctl",
        f"--since={since.strftime('%Y-%m-%d %H:%M:%S')}",
        "-p",
        "warning",
        "--no-pager",
        "--output=cat",
    ]
    for u in units:
        args += ["-u", u]
    try:
        out = await _run(*args)
    except _ProbeFailed as e:
        return LogBlock(0, 0, [], error=e.detail)

    lines = [ln for ln in out.splitlines() if ln.strip()]
    err = sum(1 for ln in lines if _looks_like_error(ln))
    warn = len(lines) - err
    sample = [ln for ln in lines if _looks_like_error(ln)][:3]
    return LogBlock(err, warn, sample)


_ERROR_RE = re.compile(r"\b(?:ERROR|CRITICAL|FATAL|Exception|Traceback)\b", re.IGNORECASE)


def _looks_like_error(line: str) -> bool:
    return bool(_ERROR_RE.search(line))


# ─────────── Rendering ───────────


def _render(
    commits: CommitsBlock,
    roadmap: RoadmapBlock,
    health: HealthBlock,
    logs: LogBlock,
    now: datetime,
) -> str:
    from html import escape

    date_str = now.strftime("%d %b").lstrip("0")
    out = [f"☀️ <b>Доброе утро · {escape(date_str)}</b>", ""]

    # ── Commits
    if commits.error:
        out.append(f"📦 коммиты: <i>{escape(commits.error)}</i>")
    elif commits.count == 0:
        out.append("📦 коммиты: вчера ничего не пушили")
    else:
        suffix = _ru_plural(commits.count, "коммит", "коммита", "коммитов")
        out.append(f"📦 <b>{commits.count}</b> {suffix} за сутки:")
        for ln in commits.lines:
            out.append(f"  · <code>{escape(ln[:90])}</code>")

    # ── Roadmap
    out.append("")
    if roadmap.error:
        out.append(f"🗺 ROADMAP: <i>{escape(roadmap.error)}</i>")
    else:
        line = f"🗺 ROADMAP: {roadmap.in_progress} в работе"
        if roadmap.new_done:
            line += f", закрыто {len(roadmap.new_done)}"
        out.append(line)
        for d in roadmap.new_done:
            out.append(f"  ✅ {escape(d)}")

    # ── Health
    out.append("")
    icon = "✅" if health.ok else "⚠️"
    if health.error:
        out.append(f"{icon} прод: {escape(health.detail)} <i>({escape(health.error)})</i>")
    else:
        out.append(f"{icon} прод: {escape(health.detail)}")

    # ── Logs
    if logs.error:
        out.append(f"📜 логи: <i>{escape(logs.error)}</i>")
    elif logs.error_count == 0 and logs.warn_count == 0:
        out.append("📜 логи: чисто за сутки")
    else:
        out.append(
            f"📜 логи: <b>{logs.error_count}</b> ошибок, {logs.warn_count} warn"
        )
        for s in logs.sample:
            out.append(f"  ⚠️ <code>{escape(s[:120])}</code>")

    return "\n".join(out)


def _ru_plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not (12 <= n % 100 <= 14):
        return few
    return many


# ─────────── Subprocess helper ───────────


class _ProbeFailed(Exception):
    def __init__(self, detail: str) -> None:
        self.detail = detail


async def _run(*args: str) -> str:
    """Spawn a subprocess, return stdout. Raises ``_ProbeFailed`` on failure."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=_PROBE_TIMEOUT_S
        )
    except TimeoutError as e:
        proc.kill()
        await proc.wait()
        raise _ProbeFailed(f"{args[0]} timed out") from e

    if proc.returncode != 0:
        tail = (stderr.decode("utf-8", errors="replace") or "").strip().splitlines()
        raise _ProbeFailed(tail[-1] if tail else f"exit={proc.returncode}")
    return stdout.decode("utf-8", errors="replace")


# ─────────── Test seam (used by /digest command + scheduler) ───────────


async def send_digest(send: Send) -> None:
    """Build and send a digest. ``send`` is an awaitable taking ``(text:str)``.

    Wrapper exists so the scheduler can call this with the bot's
    ``send_message`` partial, and ``/digest`` command can use the same
    code-path without coupling to aiogram inside this module.
    """
    text = await build_digest_html()
    await send(text)
