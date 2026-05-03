"""CommonMark → Telegram-flavored HTML.

Claude emits CommonMark (``**bold**``, fenced code blocks, headings,
links). Telegram's HTML parse mode only understands a small subset:
``<b>``, ``<i>``, ``<u>``, ``<s>``, ``<code>``, ``<pre>``, ``<a>``,
``<blockquote>``, ``<tg-spoiler>``. Without conversion users see raw
asterisks and backticks. This module bridges the gap.

Supported:

- ``**bold**`` and ``__bold__`` → ``<b>``
- ``*italic*`` and ``_italic_`` → ``<i>``
- ``` `code` ``` → ``<code>``
- fenced ```` ```lang\\n...\\n``` ```` → ``<pre><code class="language-lang">``
- ``# Heading`` … ``###### Heading`` → ``<b>`` (TG has no headings)
- ``[text](url)`` → ``<a href="url">``
- GitHub-flavored Markdown tables → flattened bullet-list (TG has no
  ``<table>``; raw pipes/dashes leak through and word-wrap mangles them).

Images and HRs pass through as plain text. The output is always valid
Telegram HTML — every untrusted character is ``html.escape``'d before
being inserted.
"""

from __future__ import annotations

import html
import re

# Order matters: extract code first so its content isn't touched by
# bold/italic substitutions, then escape, then apply markup.
_FENCED_RE = re.compile(
    r"```([\w+-]*)\n(.*?)```",
    re.DOTALL,
)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
# ``\S(?:.*?\S)?`` — content can't start or end with whitespace, so
# ``** **`` doesn't match. Non-greedy interior keeps shortest pair.
_BOLD_AST_RE = re.compile(r"\*\*(\S(?:.*?\S)?)\*\*", re.DOTALL)
_BOLD_UND_RE = re.compile(r"(?<!\w)__(\S(?:.*?\S)?)__(?!\w)", re.DOTALL)
# Italic: single * not adjacent to another *, single _ not adjacent to
# a word char (so ``snake_case`` stays intact). Content forbids
# leading/trailing whitespace AND ``*`` so ``** **`` doesn't get
# mis-parsed as ``<i>* *</i>``.
_ITAL_AST_RE = re.compile(
    r"(?<![\w*])\*([^*\s](?:.*?[^*\s])?)\*(?![\w*])",
    re.DOTALL,
)
_ITAL_UND_RE = re.compile(r"(?<!\w)_(\S(?:.*?\S)?)_(?!\w)", re.DOTALL)
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*$", re.MULTILINE)
_HR_RE = re.compile(r"^\s*[-*_]{3,}\s*$", re.MULTILINE)
# Safe-list of HTML tags Telegram accepts in HTML parse mode. We let
# Claude emit these directly (e.g. ``<b>...</b>``) — without this the
# pipeline ``html.escape``s them into ``&lt;b&gt;`` and they show up
# as literal text in the chat.
_TG_SAFE_TAG_NAMES = (
    "b|strong|i|em|u|ins|s|strike|del|code|pre|blockquote|tg-spoiler|a"
)
_SAFE_TAG_RE = re.compile(
    r"</?(?:" + _TG_SAFE_TAG_NAMES + r")(?:\s+[^>]*)?\s*/?>",
    re.IGNORECASE,
)
# Table separator row, e.g. ``|---|:--:|---:|``. ``-+`` is the actual
# rule chars; optional ``:`` on either side is column-alignment, which
# we ignore (TG has no table).
_TABLE_SEP_CELL_RE = re.compile(r"^:?-+:?$")


def _is_table_row(line: str) -> bool:
    """Bare-minimum table-row sniff: starts and ends with ``|``."""
    s = line.strip()
    return len(s) >= 2 and s.startswith("|") and s.endswith("|")


def _is_table_separator(line: str) -> bool:
    """Detect the ``|---|---|`` rule row that follows a table header."""
    if not _is_table_row(line):
        return False
    cells = [c.strip() for c in line.strip()[1:-1].split("|")]
    return len(cells) >= 1 and all(_TABLE_SEP_CELL_RE.match(c) for c in cells if c)


def _parse_table_row(line: str) -> list[str]:
    return [c.strip() for c in line.strip()[1:-1].split("|")]


def _convert_tables(text: str) -> str:
    """Flatten GFM tables into bullet-lists.

    Telegram HTML has no ``<table>``, and shipping the raw
    ``|col|col|`` text means TG word-wraps cell contents at random
    boundaries — unreadable. Heuristic: detect a header row + ``---``
    separator + zero-or-more data rows, replace with bullets where
    each row becomes::

        • **<first cell>** — **Header2**: cell2 · **Header3**: cell3

    First cell of each data row is the lead label (usually most
    informative — name, id, scenario). Empty cells (``—`` / ``-`` /
    blank) are skipped to avoid clutter.
    """
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        if (
            i + 1 < n
            and _is_table_row(lines[i])
            and _is_table_separator(lines[i + 1])
        ):
            header = _parse_table_row(lines[i])
            i += 2
            rows: list[list[str]] = []
            while i < n and _is_table_row(lines[i]) and not _is_table_separator(
                lines[i]
            ):
                rows.append(_parse_table_row(lines[i]))
                i += 1
            out.append(_render_table_as_bullets(header, rows))
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


def _render_table_as_bullets(header: list[str], rows: list[list[str]]) -> str:
    """Build a bullet-list rendering of a table, in raw Markdown.

    The output goes through the rest of the pipeline (escape, bold,
    etc.) like any other text. We emit ``**bold**`` for emphasis;
    those become ``<b>`` later.
    """
    if not rows:
        # Header-only table: degenerate, just emit the header cells.
        return " · ".join(f"**{h}**" for h in header if h)

    lines: list[str] = []
    for row in rows:
        # Pad/trim to header length so we never index out of range.
        cells = (row + [""] * len(header))[: len(header)]
        lead = cells[0].strip()
        tail_parts: list[str] = []
        for h, c in zip(header[1:], cells[1:], strict=False):
            v = c.strip()
            if not v or v in {"—", "-", "–"}:
                continue
            h_clean = h.strip()
            if h_clean:
                tail_parts.append(f"**{h_clean}**: {v}")
            else:
                tail_parts.append(v)
        if lead and tail_parts:
            lines.append(f"• **{lead}** — " + " · ".join(tail_parts))
        elif lead:
            lines.append(f"• **{lead}**")
        elif tail_parts:
            lines.append("• " + " · ".join(tail_parts))
        # all-empty rows are dropped silently
    return "\n".join(lines)


def to_html(text: str) -> str:
    """Convert CommonMark to Telegram HTML.

    Returns an empty string for falsy input. The result is safe to send
    with ``parse_mode=HTML`` — all literal ``<``, ``>``, ``&`` from the
    original are escaped.
    """
    if not text:
        return ""

    # 0. Flatten GFM tables into bullets BEFORE anything else. Tables
    #    with ``**bold**``-decorated headers want to round-trip through
    #    the rest of the pipeline, so we emit raw Markdown here and let
    #    the bold/italic passes pick it up downstream.
    text = _convert_tables(text)

    # 0b. Stash any TG-safe HTML tags Claude already emitted (``<b>``,
    #     ``<code>``, ``<a href="...">``, etc.). We restore them as-is
    #     after html.escape so they stay live tags instead of becoming
    #     ``&lt;b&gt;`` text. Whitelist is strict — anything else gets
    #     escaped like normal.
    safe_tags: list[str] = []

    def _stash_safe_tag(m: re.Match[str]) -> str:
        idx = len(safe_tags)
        safe_tags.append(m.group(0))
        return f"\x00TAG{idx}\x00"

    text = _SAFE_TAG_RE.sub(_stash_safe_tag, text)

    # 1. Stash fenced code blocks so their interior survives untouched.
    code_blocks: list[tuple[str, str]] = []

    def _stash_fenced(m: re.Match[str]) -> str:
        idx = len(code_blocks)
        code_blocks.append((m.group(1), m.group(2)))
        return f"\x00FENCE{idx}\x00"

    text = _FENCED_RE.sub(_stash_fenced, text)

    # 2. Stash inline code likewise.
    inline_codes: list[str] = []

    def _stash_inline(m: re.Match[str]) -> str:
        idx = len(inline_codes)
        inline_codes.append(m.group(1))
        return f"\x00INLINE{idx}\x00"

    text = _INLINE_CODE_RE.sub(_stash_inline, text)

    # 3. Escape everything left. Placeholders are pure ASCII + NULs so
    #    they survive the escape unchanged.
    text = html.escape(text)

    # 4. Apply Markdown → HTML substitutions on the escaped text.
    #    Bold/italic before headings: a heading containing ``**x**`` ends
    #    up as ``<b>... <b>x</b> ...</b>``. Telegram tolerates nested
    #    ``<b>`` and renders it as plain bold, which is what we want.
    text = _BOLD_AST_RE.sub(r"<b>\1</b>", text)
    text = _BOLD_UND_RE.sub(r"<b>\1</b>", text)
    text = _ITAL_AST_RE.sub(r"<i>\1</i>", text)
    text = _ITAL_UND_RE.sub(r"<i>\1</i>", text)
    text = _HEADING_RE.sub(r"<b>\2</b>", text)
    text = _HR_RE.sub("─" * 10, text)
    text = _LINK_RE.sub(r'<a href="\2">\1</a>', text)

    # 5. Restore inline code with HTML-escaped content.
    for idx, code in enumerate(inline_codes):
        text = text.replace(
            f"\x00INLINE{idx}\x00",
            f"<code>{html.escape(code)}</code>",
            1,
        )

    # 6. Restore fenced code blocks.
    for idx, (lang, code) in enumerate(code_blocks):
        attr = f' class="language-{html.escape(lang)}"' if lang else ""
        text = text.replace(
            f"\x00FENCE{idx}\x00",
            f"<pre><code{attr}>{html.escape(code)}</code></pre>",
            1,
        )

    # 7. Restore TG-safe HTML tags that Claude emitted directly.
    #    They go back AS-IS (no re-escape) so the browser/TG sees live
    #    ``<b>`` instead of ``&lt;b&gt;``.
    for idx, tag in enumerate(safe_tags):
        text = text.replace(f"\x00TAG{idx}\x00", tag, 1)

    return text
