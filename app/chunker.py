"""Telegram-message chunking — keep long Claude output readable.

Telegram's hard limit per message is 4096 chars. Beyond that we either
split or upload. The split logic is kept naive on purpose:

* messages ≤ ``SOFT_LIMIT`` go through as a single text message
* messages between ``SOFT_LIMIT`` and ``HARD_LIMIT`` are split on
  paragraph boundaries (double newlines), then on lines, and only as a
  last resort mid-line
* anything beyond ``HARD_LIMIT * 4`` is uploaded as a ``response.txt``
  document — protects the chat from a 50KB code-review wall

Why not stream chunks as they arrive? aiogram's HTML/MD parser would have
to keep state across messages, and we'd need to re-emit fences on every
chunk. Buffering until the assistant turn ends is much simpler and the
user doesn't notice — Claude's final reply lands within seconds.
"""

from __future__ import annotations

# Aim a bit under Telegram's 4096 ceiling so we have room for HTML tags.
SOFT_LIMIT = 3500
# A long single message we still send as text (split into multiple sends).
HARD_LIMIT = 3500
# Anything larger than this becomes a file attachment.
FILE_THRESHOLD = HARD_LIMIT * 4


def chunk_text(text: str, *, soft: int = SOFT_LIMIT) -> list[str]:
    """Split ``text`` into TG-safe chunks.

    Splits prefer paragraph (``\\n\\n``) → line (``\\n``) → hard cut at
    ``soft``. Each chunk is at most ``soft`` chars. Empty input returns
    an empty list (caller decides whether to send a placeholder).
    """
    text = text or ""
    if not text:
        return []
    if len(text) <= soft:
        return [text]

    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= soft:
            chunks.append(remaining)
            break

        # Try paragraph break within budget.
        cut = remaining.rfind("\n\n", 0, soft)
        if cut == -1 or cut < soft // 4:
            cut = remaining.rfind("\n", 0, soft)
        if cut == -1 or cut < soft // 4:
            cut = soft  # hard cut, last resort

        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip("\n")
    return chunks


def is_too_long_for_messages(text: str) -> bool:
    """Beyond this size we shouldn't spam the chat — upload as a file."""
    return len(text) > FILE_THRESHOLD
