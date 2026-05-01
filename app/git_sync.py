"""Auto-pull the working directory before a Claude turn.

Why: the user might have pushed commits from their laptop while the
bot's clone is stale. Without an auto-pull the bot might commit on top
of an outdated tree, producing merge conflicts on the next manual pull.

Strategy: best-effort. If anything fails (not a git repo, dirty
working tree, network blip, conflict) we just log and continue —
Claude will see whatever's on disk and the user can resolve manually.
We never throw out of this module.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.logging import get_logger

log = get_logger(__name__)


# Git takes its sweet time on bad networks; cap the pull so a stuck
# command doesn't block the bot from responding.
PULL_TIMEOUT_S = 20.0


async def maybe_pull_workdir(cwd: Path) -> str | None:
    """Run ``git fetch && git pull --ff-only`` in ``cwd``.

    Returns:
        - ``None`` if the directory is not a git repo or pull was a no-op.
        - A short human-readable string describing the update otherwise
          (e.g. ``"updated: a1b2c3d → e4f5g6h (3 commits)"``). Bot can
          surface this to the user as a status note.
    """
    if not (cwd / ".git").exists():
        return None  # not a repo — silent skip

    try:
        # Capture the SHA before pulling so we can describe the diff.
        before = await _git(cwd, "rev-parse", "HEAD")
        await _git(cwd, "fetch", "--quiet", "origin")
        # ``--ff-only`` refuses to merge — if local diverged we abort
        # and let the user resolve. Better than auto-merging.
        await _git(cwd, "pull", "--ff-only", "--quiet")
        after = await _git(cwd, "rev-parse", "HEAD")
    except _GitFailed as e:
        log.info("auto_pull_skipped", cwd=str(cwd), reason=e.detail)
        return None
    except TimeoutError:
        log.warning("auto_pull_timeout", cwd=str(cwd))
        return None

    if before == after:
        return None

    try:
        # Count commits between the old and new HEAD for a friendlier note.
        cnt_str = await _git(cwd, "rev-list", "--count", f"{before}..{after}")
        cnt = int(cnt_str.strip() or "0")
    except (_GitFailed, ValueError, TimeoutError):
        cnt = 0

    short_before = before[:7]
    short_after = after[:7]
    suffix = f" ({cnt} коммит)" if cnt == 1 else f" ({cnt} коммитов)" if cnt else ""
    msg = f"git pull: {short_before} → {short_after}{suffix}"
    log.info("auto_pull_applied", cwd=str(cwd), before=short_before, after=short_after, count=cnt)
    return msg


# ─────────── internals ───────────


class _GitFailed(Exception):
    def __init__(self, detail: str) -> None:
        self.detail = detail


async def _git(cwd: Path, *args: str) -> str:
    """Run ``git <args>`` in ``cwd``, return stripped stdout.

    Raises ``_GitFailed`` on non-zero exit (with a short tail of stderr
    so the log entry stays grep-able).
    """
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=PULL_TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise

    if proc.returncode != 0:
        tail = (stderr.decode("utf-8", errors="replace") or "").strip().splitlines()
        raise _GitFailed(tail[-1] if tail else f"git {args[0]} exit={proc.returncode}")
    return stdout.decode("utf-8", errors="replace")
