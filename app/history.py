"""Conversation history — persist summaries before /reset wipes them.

Why this exists
---------------
Without this, ``/reset`` (and any time the user starts fresh) is
amnesia: the previous thread evaporates and you can't go back to
"that bug we discussed last week". The bot now keeps a short summary
of every reset session so the user can browse and resume.

Storage shape
-------------
::

    state_dir/
      └── history/
          └── chat_<chat_id>/
              ├── 2026-05-01T20-30-00Z.md     # summary of one session
              └── ...

Each ``.md`` file is plain markdown. First line is a one-line title
(generated from Claude's ``/compact`` output or fallback "Сессия
DD.MM"). The rest is whatever ``/compact`` produced — usually a few
paragraphs.

Lifecycle
---------
- Triggered from ``ChatSession.reset()`` and ``cmd_compact`` (when the
  user explicitly compacts). Both paths fall back gracefully on errors:
  the history saving must never block ``/reset``, even if Claude fails.
- ``list_history(chat_id)`` walks the directory, returns most-recent
  first. Files are auto-deleted after 30 days to keep things tidy.
- ``/resume <id>`` reads a summary and feeds it as a system note into
  the next session, so Claude knows "вот что обсуждали раньше".

What we DO NOT store
--------------------
- Full transcripts. The summary captures intent, not every keystroke.
- Code blocks Claude generated. Those live in git already.
- Anything older than 30 days (auto-pruned on each save).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.config import settings
from app.logging import get_logger

log = get_logger(__name__)

# Auto-prune entries older than this. 30 days = a month of memory is
# more than the user actually consults; older sessions just clutter.
RETENTION_DAYS = 30

# Hard cap on the number of entries we list. Beyond ~50 the chat
# becomes a wall of text with no value.
LIST_LIMIT = 30

# How many bytes of summary content we cap at. ``/compact`` output is
# usually under 4 KB, but a runaway response shouldn't blow up the
# state directory.
MAX_BYTES_PER_FILE = 32 * 1024


@dataclass(frozen=True)
class HistoryEntry:
    """One archived session.

    Attributes:
        entry_id: ``YYYYMMDDTHHMMSSZ`` timestamp — both the filename
            stem and the user-visible id (``/resume <id>``).
        path: Absolute path to the ``.md`` file.
        title: First non-empty line of the summary, truncated to 80
            chars. Used in ``/history`` list.
        created_at: Parsed from the filename. Used for sorting + age.
    """

    entry_id: str
    path: Path
    title: str
    created_at: datetime


# ─────────── Public API ───────────


def save_summary(chat_id: int, summary: str) -> HistoryEntry | None:
    """Persist ``summary`` for ``chat_id``. Returns the new entry, or
    None if the summary was empty / save failed. Auto-prunes old files.

    Caller is responsible for getting the summary text — usually by
    asking Claude to ``/compact`` and capturing the assistant text.
    """
    summary = (summary or "").strip()
    if not summary:
        log.debug("history_skip_empty", chat_id=chat_id)
        return None

    # Truncate runaway summaries — better than failing the save.
    if len(summary.encode("utf-8")) > MAX_BYTES_PER_FILE:
        summary = summary.encode("utf-8")[:MAX_BYTES_PER_FILE].decode(
            "utf-8", errors="ignore"
        )

    chat_dir = _chat_dir(chat_id)
    chat_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(UTC)
    entry_id = now.strftime("%Y%m%dT%H%M%SZ")
    path = chat_dir / f"{entry_id}.md"

    # Write atomically — never leave a half-written summary if interrupted.
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(summary, encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        log.warning("history_save_failed", chat_id=chat_id, error=str(e))
        return None

    log.info("history_saved", chat_id=chat_id, entry_id=entry_id, bytes=len(summary))
    _prune_old(chat_dir)

    return HistoryEntry(
        entry_id=entry_id,
        path=path,
        title=_extract_title(summary),
        created_at=now,
    )


def list_history(chat_id: int) -> list[HistoryEntry]:
    """Return all saved entries for ``chat_id``, most recent first.

    Returns ``[]`` if the directory doesn't exist (no history yet).
    """
    chat_dir = _chat_dir(chat_id)
    if not chat_dir.exists():
        return []

    entries = list(_iter_entries(chat_dir))
    entries.sort(key=lambda e: e.created_at, reverse=True)
    return entries[:LIST_LIMIT]


def load_summary(chat_id: int, entry_id: str) -> str | None:
    """Read the full summary text for ``entry_id``. Returns None if
    not found (id typo, file deleted)."""
    if not _ID_PATTERN.fullmatch(entry_id):
        return None  # reject anything that's not our timestamp format
    path = _chat_dir(chat_id) / f"{entry_id}.md"
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError as e:
        log.warning("history_load_failed", entry_id=entry_id, error=str(e))
        return None


# ─────────── Internals ───────────


# YYYYMMDDTHHMMSSZ  — exactly what we generate.
_ID_PATTERN = re.compile(r"^\d{8}T\d{6}Z$")


def _chat_dir(chat_id: int) -> Path:
    return settings.state_dir / "history" / f"chat_{chat_id}"


def _iter_entries(chat_dir: Path) -> Iterator[HistoryEntry]:
    for path in chat_dir.iterdir():
        if path.suffix != ".md":
            continue
        stem = path.stem
        if not _ID_PATTERN.fullmatch(stem):
            continue
        try:
            created = datetime.strptime(stem, "%Y%m%dT%H%M%SZ").replace(
                tzinfo=UTC
            )
        except ValueError:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        yield HistoryEntry(
            entry_id=stem,
            path=path,
            title=_extract_title(text),
            created_at=created,
        )


def _extract_title(summary: str) -> str:
    """First non-empty / non-marker line, trimmed to 80 chars.

    Markdown headings (``#``), emphasis (``*``), block quotes (``>``)
    are stripped at the front for cleaner display in chip-row UIs.
    """
    for raw in summary.splitlines():
        line = raw.strip()
        if not line:
            continue
        # Strip leading markdown syntax.
        line = re.sub(r"^[#>*\-]+\s*", "", line)
        if line:
            return line[:80] + ("…" if len(line) > 80 else "")
    return "(пусто)"


def _prune_old(chat_dir: Path) -> None:
    """Delete entries older than ``RETENTION_DAYS``."""
    cutoff = datetime.now(UTC) - timedelta(days=RETENTION_DAYS)
    for entry in _iter_entries(chat_dir):
        if entry.created_at < cutoff:
            try:
                entry.path.unlink()
                log.debug("history_pruned", entry_id=entry.entry_id)
            except OSError:
                pass
