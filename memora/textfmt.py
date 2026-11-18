"""Safe Telegram-HTML rendering.

LLMs are asked for a small Markdown subset; this module converts it deterministically
into Telegram's HTML dialect. Because every piece of free text is escaped and tags are
only ever emitted by this code, a model can never produce markup that breaks the parser.
Long answers are split below Telegram's 4096-character limit without cutting through a
code block.
"""
from __future__ import annotations

import html
import re

TG_LIMIT = 4096
_CHUNK_BUDGET = 3500  # markdown characters per chunk; escaping/tags may expand the text

_FENCE_RE = re.compile(r"```[ \t]*([\w+#.-]*)[ \t]*\n(.*?)(?:\n```|\Z)", re.S)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_BOLD_RE = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", re.S)
_ITALIC_RE = re.compile(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])")
_HEADING_RE = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(.+?)[ \t#]*$", re.M)
_BULLET_RE = re.compile(r"^([ \t]*)[-*][ \t]+", re.M)
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_ALLOWED_TAG_RE = re.compile(r"&lt;(/?)(b|i|u|s|code|pre)&gt;")
_TAG_RE = re.compile(r"<[^>]+>")


def esc(value: object) -> str:
    """Escape arbitrary text for inclusion in a Telegram HTML message."""
    return html.escape(str(value), quote=False)


def strip_tags(text: str) -> str:
    """Plain-text fallback used if Telegram ever rejects a message's entities."""
    return html.unescape(_TAG_RE.sub("", text))


def _restore_balanced_tags(text: str) -> str:
    """Un-escape simple tags the model wrote literally, but only when they are balanced."""
    for tag in ("b", "i", "u", "s", "code", "pre"):
        opens = text.count(f"&lt;{tag}&gt;")
        closes = text.count(f"&lt;/{tag}&gt;")
        if opens and opens == closes:
            text = text.replace(f"&lt;{tag}&gt;", f"<{tag}>").replace(f"&lt;/{tag}&gt;", f"</{tag}>")
    return text


def _render_inline(segment: str) -> str:
    """Convert a markdown segment that contains no fenced code blocks."""
    stash: list[str] = []

    def keep(rendered: str) -> str:
        stash.append(rendered)
        return f"\x00{len(stash) - 1}\x00"

    segment = _INLINE_CODE_RE.sub(lambda m: keep(f"<code>{esc(m.group(1))}</code>"), segment)
    segment = _LINK_RE.sub(lambda m: keep(f'<a href="{html.escape(m.group(2), quote=True)}">{esc(m.group(1))}</a>'), segment)
    segment = esc(segment)
    segment = _HEADING_RE.sub(lambda m: f"<b>{m.group(1)}</b>", segment)
    segment = _BOLD_RE.sub(lambda m: f"<b>{m.group(1)}</b>", segment)
    segment = _ITALIC_RE.sub(lambda m: f"<i>{m.group(1)}</i>", segment)
    segment = _BULLET_RE.sub(lambda m: f"{m.group(1)}• ", segment)
    segment = _restore_balanced_tags(segment)
    return re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], segment)


def _render_code(lang: str, code: str) -> str:
    cls = f' class="language-{html.escape(lang, quote=True)}"' if lang else ""
    return f"<pre><code{cls}>{esc(code)}</code></pre>"


def md_to_html(markdown: str) -> str:
    """Markdown subset -> Telegram HTML (no length handling)."""
    out: list[str] = []
    pos = 0
    for match in _FENCE_RE.finditer(markdown):
        out.append(_render_inline(markdown[pos:match.start()]))
        out.append(_render_code(match.group(1), match.group(2).strip("\n")))
        pos = match.end()
    out.append(_render_inline(markdown[pos:]))
    return "".join(out).strip()


def _split_text(text: str, budget: int) -> list[str]:
    """Split prose on paragraph, then line, then hard boundaries."""
    chunks: list[str] = []
    current = ""
    for para in re.split(r"(\n\s*\n)", text):
        if len(current) + len(para) <= budget:
            current += para
            continue
        if current.strip():
            chunks.append(current)
        current = ""
        while len(para) > budget:
            cut = para.rfind("\n", 0, budget)
            cut = cut if cut > budget // 2 else budget
            chunks.append(para[:cut])
            para = para[cut:]
        current = para
    if current.strip():
        chunks.append(current)
    return chunks


def _split_code(lang: str, code: str, budget: int) -> list[str]:
    """A fenced block larger than one message is re-fenced piece by piece."""
    pieces: list[str] = []
    current: list[str] = []
    size = 0
    for line in code.split("\n"):
        # Budget is measured on the *escaped* length: "<" becomes "&lt;" in the final HTML.
        while len(esc(line)) > budget:
            if current:
                pieces.append("\n".join(current))
                current, size = [], 0
            cut = budget // 5
            pieces.append(line[:cut])
            line = line[cut:]
        cost = len(esc(line)) + 1
        if size + cost > budget and current:
            pieces.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += cost
    if current:
        pieces.append("\n".join(current))
    return [f"```{lang}\n{piece}\n```" for piece in pieces]


def render_markdown(markdown: str) -> list[str]:
    """Markdown -> list of Telegram-HTML messages, each safely under the size limit."""
    markdown = markdown.strip()
    if not markdown:
        return []
    units: list[str] = []
    pos = 0
    for match in _FENCE_RE.finditer(markdown):
        units.extend(_split_text(markdown[pos:match.start()], _CHUNK_BUDGET))
        units.extend(_split_code(match.group(1), match.group(2).strip("\n"), _CHUNK_BUDGET))
        pos = match.end()
    units.extend(_split_text(markdown[pos:], _CHUNK_BUDGET))

    messages: list[str] = []
    current = ""
    for unit in units:
        if current and len(esc(current)) + len(esc(unit)) > _CHUNK_BUDGET:
            messages.append(current)
            current = ""
        current += unit if not current or unit.startswith("\n") or current.endswith("\n") else "\n" + unit
    if current.strip():
        messages.append(current)

    rendered: list[str] = []
    for message in messages:
        html_text = md_to_html(message)
        if len(html_text) <= TG_LIMIT:
            if html_text:
                rendered.append(html_text)
        else:  # pathological escaping expansion: fall back to plain, hard-wrapped text
            plain = esc(strip_tags(html_text))
            rendered.extend(plain[i:i + TG_LIMIT - 96] for i in range(0, len(plain), TG_LIMIT - 96))
    return rendered


def split_html(text: str) -> list[str]:
    """Split already-rendered (code-generated) HTML on line boundaries only."""
    if len(text) <= TG_LIMIT:
        return [text]
    parts: list[str] = []
    current = ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > TG_LIMIT - 64 and current:
            parts.append(current)
            current = ""
        current += ("\n" if current else "") + line
    if current:
        parts.append(current)
    return parts


def truncate(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: max(limit - 1, 0)].rstrip() + "…"
