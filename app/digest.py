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
from collections.abc import Awaitable, Callable, Iterator
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
    next_up: list[str]  # tasks under "Planned next" — what to take on
    blocked: list[str]  # tasks under "Blocked"
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
    """Parse ``notes/ROADMAP.md`` and ``git log -p`` over the last 24h.

    Surfaces:
    - count of currently-in-progress tasks
    - tasks moved into Done (deduped by task id, not full line)
    - what's at the top of "Planned next" — what to take on
    - what's "Blocked" — needs unblocking before progress
    """
    roadmap = repo / "notes" / "ROADMAP.md"
    if not roadmap.exists():
        return RoadmapBlock(0, [], [], [], error="ROADMAP.md missing")

    try:
        text = roadmap.read_text(encoding="utf-8")
    except OSError as e:
        return RoadmapBlock(0, [], [], [], error=str(e))

    sections = _split_sections(text)
    in_progress = sum(1 for _ in _iter_table_rows(sections.get("in_progress", "")))
    # "Можно взять дальше" — Planned next first, then fall back to Backlog.
    # Often `Planned next` is empty as a roadmap convention, the real
    # candidates list lives in Backlog.
    next_up_rows = list(_iter_table_rows(sections.get("planned", "")))
    if not next_up_rows:
        next_up_rows = list(_iter_table_rows(sections.get("backlog", "")))
    next_up = [_format_row(row) for row in next_up_rows][:5]
    blocked = [_format_row(row) for row in _iter_table_rows(sections.get("blocked", ""))][:5]

    # Newly-Done tasks via git diff for ROADMAP.md over the window.
    # Dedup by task id (#NNNN) — repeated edits to the same row in the
    # window must not produce duplicate entries.
    by_id: dict[str, str] = {}
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
        for line in diff.splitlines():
            if not line.startswith("+") or line.startswith("+++"):
                continue
            m = re.search(r"\[(\d{4})\]\([^)]+\)\s*\|\s*([^|]+?)\s*\|", line)
            if not m:
                continue
            tid, title = m.group(1), m.group(2).strip()
            # Keep the FIRST occurrence per task id — usually the most
            # complete title; later edits often shorten or rephrase.
            by_id.setdefault(tid, f"#{tid} {title}")
    except _ProbeFailed as e:
        return RoadmapBlock(in_progress, [], next_up, blocked, error=e.detail)

    return RoadmapBlock(in_progress, list(by_id.values())[:5], next_up, blocked)


def _split_sections(text: str) -> dict[str, str]:
    """Group ROADMAP lines by section keyword.

    Keys: ``in_progress``, ``planned``, ``backlog``, ``blocked``, ``done``.
    Other sections are dropped (we don't surface them).
    """
    out: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("##"):
            low = s.lower()
            if "in progress" in low or "🔥" in s:
                current = "in_progress"
            elif "planned" in low or "📋" in s:
                current = "planned"
            elif "blocked" in low or "🚧" in s:
                current = "blocked"
            elif "done" in low or "✅" in s:
                current = "done"
            elif "backlog" in low or "💤" in s:
                current = "backlog"
            else:
                current = None
            continue
        if current is None:
            continue
        out.setdefault(current, []).append(line)
    return {k: "\n".join(v) for k, v in out.items()}


def _iter_table_rows(section: str) -> Iterator[str]:
    """Yield raw markdown rows that look like a task table row."""
    for line in section.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        if "---" in s:  # table separator
            continue
        if "tasks/" not in s:  # not a task row
            continue
        yield s


def _format_row(row: str) -> str:
    """Pull "#NNNN Title" out of a markdown table row."""
    m = re.search(r"\[(\d{4})\]\([^)]+\)\s*\|\s*([^|]+?)\s*\|", row)
    if m:
        return f"#{m.group(1)} {m.group(2).strip()}"
    # Fallback: trim the leading | and take first cell.
    parts = [p.strip() for p in row.strip("|").split("|") if p.strip()]
    return parts[0] if parts else "?"


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
        # Headline: how the day looks at a glance.
        if roadmap.in_progress == 0 and not roadmap.new_done and not roadmap.next_up:
            out.append("🗺 ROADMAP: пусто — добавь задач")
        else:
            head_parts = []
            head_parts.append(
                f"{roadmap.in_progress} в работе"
                if roadmap.in_progress
                else "в работе пусто"
            )
            if roadmap.new_done:
                head_parts.append(f"закрыто {len(roadmap.new_done)}")
            out.append(f"🗺 ROADMAP: {', '.join(head_parts)}")

        for d in roadmap.new_done:
            out.append(f"  ✅ {escape(d)}")

        if roadmap.next_up:
            out.append("")
            out.append("🎯 Можно взять дальше:")
            for n in roadmap.next_up:
                out.append(f"  · {escape(n)}")

        if roadmap.blocked:
            out.append("")
            out.append("🚧 Заблокировано:")
            for b in roadmap.blocked:
                out.append(f"  · {escape(b)}")

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
