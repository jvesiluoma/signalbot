"""
Jinja2 customizations: context processor + template filters.

These need a Flask `app` to register against, so the public entry point is
`register(app)`. Called from `app.py` right after `app = Flask(__name__)`.

Extracted from app.py during Phase 7. The filters / context constants stay
exposed at module level for ad-hoc imports by other code that wants to render
the same badges (e.g. CLI tools, future blueprints).
"""

from __future__ import annotations

import re
from datetime import datetime

from markupsafe import Markup

import config

_PLATFORM_BADGE_CODES = {'signal': 'SG', 'telegram': 'TG', 'whatsapp': 'WA'}
_PLATFORM_LABELS = {'signal': 'Signal', 'telegram': 'Telegram', 'whatsapp': 'WhatsApp'}
_ENABLED_PLATFORMS = ['signal'] + (
    (['telegram'] if getattr(config, 'TELEGRAM_ENABLED', False) else [])
    + (['whatsapp'] if getattr(config, 'WHATSAPP_ENABLED', False) else [])
)


def _platform_badge(platform):
    """Render the small SG/TG/WA origin chip."""
    p = (str(platform).lower() if platform else 'signal')
    if p not in _PLATFORM_BADGE_CODES:
        p = 'unknown'
        code, label = '??', 'Unknown'
    else:
        code, label = _PLATFORM_BADGE_CODES[p], _PLATFORM_LABELS.get(p, p.title())
    return Markup(f'<span class="platform-badge platform-{p}" title="{Markup.escape(label)}">{code}</span>')


def _platform_label(platform):
    p = (str(platform).lower() if platform else 'signal')
    return _PLATFORM_LABELS.get(p, p.title() if p else 'Signal')


def _datefmt(value, fmt='%Y-%m-%d %H:%M'):
    if isinstance(value, datetime):
        return value.strftime(fmt)
    return str(value) if value else ''


def _domain(url_string):
    if not url_string:
        return ''
    first_url = url_string.split('|')[0].strip()
    try:
        from urllib.parse import urlparse
        return urlparse(first_url).netloc or first_url
    except Exception:
        return first_url


def _highlight(text, query):
    if not text or not query:
        return Markup(Markup.escape(text)) if text else Markup('')
    escaped_text = str(Markup.escape(text))
    escaped_query = re.escape(query)
    highlighted = re.sub(
        f'({escaped_query})', r'<mark>\1</mark>', escaped_text, flags=re.IGNORECASE
    )
    return Markup(highlighted)


def register(app):
    """Wire context processor + template filters into the given Flask app.
    Idempotent only by Flask's own re-registration semantics; intended to be
    called exactly once from `create_app()` / module init in `app.py`."""

    @app.context_processor
    def inject_globals():
        return {
            'config_db_host': config.DB_CONFIG.get('host'),
            'enabled_platforms': _ENABLED_PLATFORMS,
            'platform_labels': _PLATFORM_LABELS,
        }

    app.add_template_filter(_platform_badge, name='platform_badge')
    app.add_template_filter(_platform_label, name='platform_label')
    app.add_template_filter(_datefmt, name='datefmt')
    app.add_template_filter(_domain, name='domain')
    app.add_template_filter(_highlight, name='highlight')
