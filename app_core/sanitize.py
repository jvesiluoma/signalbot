"""
HTML sanitization for LLM-generated content.

The dashboard renders LLM-derived markdown (group summaries, intel briefs,
narrative descriptions, etc.) in the same DOM as user-trusted content. The LLM
input is attacker-controllable (group messages, scraped pages), so any HTML
emitted by the model MUST be sanitized fail-closed: if bleach is unavailable or
sanitization throws, render as escaped plain text rather than emit raw HTML.

Extracted from app.py during Phase 7. Also re-exports `strip_think_tags` —
removes the `<think>…</think>` reasoning prologue some local LLMs emit.
"""

from __future__ import annotations

import logging
import re

import markdown
from markupsafe import Markup

logger = logging.getLogger("app_core.sanitize")

# bleach is optional — when unavailable, render() falls back to escaped text.
try:
    import bleach
    BLEACH_AVAILABLE = True
except ImportError:
    bleach = None
    BLEACH_AVAILABLE = False
    logger.warning(
        "Bleach not installed — LLM HTML will render as escaped plain text "
        "(fail-closed). Install 'bleach' to restore rich rendering."
    )

ALLOWED_TAGS = [
    'p', 'ul', 'ol', 'li', 'strong', 'em', 'b', 'i', 'br', 'hr',
    'blockquote', 'code', 'pre', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'
]

ALLOWED_ATTRIBUTES = {
    '*': ['class'],
    'blockquote': ['cite'],
    'code': ['class'],
    'pre': ['class']
}


def _escaped_plaintext(text):
    """Fail-closed rendering: show the text verbatim, fully HTML-escaped, in a <pre>."""
    return Markup('<pre style="white-space:pre-wrap;word-break:break-word;margin:0">{}</pre>').format(text or "")


def render_markdown_to_safe_html(markdown_text):
    """Convert markdown to HTML and sanitize it for XSS prevention.

    The input is untrusted (LLM output derived from attacker-controlled
    message/page content), so this MUST fail closed: if bleach is unavailable
    or sanitisation throws, we fall back to escaped plain text rather than
    emitting unsanitised HTML.
    """
    if not markdown_text or not isinstance(markdown_text, str):
        return ""
    if not BLEACH_AVAILABLE or bleach is None:
        return _escaped_plaintext(markdown_text)
    try:
        html_content = markdown.markdown(markdown_text, extensions=['nl2br'])
    except Exception as e:
        logger.error("Failed to convert markdown: %s", e)
        return _escaped_plaintext(markdown_text)
    try:
        return bleach.clean(html_content, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRIBUTES, strip=True)
    except Exception:
        logger.exception("bleach.clean failed; falling back to escaped plain text")
        return _escaped_plaintext(markdown_text)


def strip_think_tags(text):
    """Remove <think>...</think> tags and plain-text reasoning from LLM output.

    Used when the model emits a chain-of-thought prologue before the actual
    summary (qwen3 reasoning template, deepseek-r1, etc.). The XML stripping
    is exact; the plain-text stripping uses a phrase list and is conservative."""
    if not text or not isinstance(text, str):
        return text
    cleaned = re.sub(r'<think>.*?</think>', '', text, flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r'</?think[^>]*>', '', cleaned, flags=re.IGNORECASE)
    reasoning_prefixes = (
        "we need to", "let me", "let's", "i need to", "i should",
        "first,", "okay,", "ok,", "so,", "now,", "step ",
        "my task is", "the task is", "i will", "i'll",
    )
    lines = cleaned.strip().splitlines()
    summary_start = 0
    for i, line in enumerate(lines):
        stripped = line.strip().lower()
        if any(stripped.startswith(p) for p in reasoning_prefixes):
            summary_start = i + 1
    if summary_start > 0 and summary_start < len(lines):
        cleaned = "\n".join(lines[summary_start:])
    else:
        cleaned = "\n".join(lines)
    return cleaned.strip()
