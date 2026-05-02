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

Lists, tables, images, and HRs pass through as plain text. The
output is always valid Telegram HTML — every untrusted character is
``html.escape``'d before being inserted.
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


def to_html(text: str) -> str:
    """Convert CommonMark to Telegram HTML.

    Returns an empty string for falsy input. The result is safe to send
    with ``parse_mode=HTML`` — all literal ``<``, ``>``, ``&`` from the
    original are escaped.
    """
    if not text:
        return ""

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

    return text
