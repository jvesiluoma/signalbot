"""Reaction-target identifier classifier.

`reaction.targetAuthor` envelope values fall into four shapes:
   - E.164 phone:        `+358501234567`               → reactions.target_author_phone
   - Signal ACI UUID:    `bef7c33f-728b-4357-a559-…`   → reactions.target_author_uuid
   - WhatsApp JID:       `<num>@s.whatsapp.net|@g.us|@c.us|@lid`
                                                       → reactions.target_platform_user_id
   - Anything else:                                    → target_platform_user_id (verbatim)
                                                         + a one-shot per-shape WARN log

This helper lives in its own module (instead of inside `poller.py`) so the
unit test can import it without pulling in Playwright / YouTubeTranscriptApi.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger("poller")  # share the poller logger so existing log filters apply

# Signal ACI / PNI canonical UUID format.
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

# WhatsApp / cross-platform JID suffixes (catch-all "@something").
_JID_SUFFIX_RE = re.compile(r"@(s\.whatsapp\.net|g\.us|c\.us|lid)$", re.IGNORECASE)

# Track which "unrecognized JID shape" warnings we've already emitted so we
# don't flood logs (the unparseable-targetAuthor warning fired thousands of
# times pre-fix for the same dozen accounts).
_unparseable_target_warned: set[str] = set()


def is_uuid(value) -> bool:
    return bool(value) and bool(_UUID_RE.match(str(value)))


def classify_reaction_target(target_raw, target_uuid_hint):
    """Map raw `reaction.targetAuthor` → (phone, uuid, platform_user_id).

    Rules, in order:
      1. Starts with "+": E.164 phone.
      2. Matches UUID shape: ACI UUID.
      3. Endswith a known WhatsApp JID suffix: store in platform_user_id verbatim
         (the bot's WhatsApp connector sometimes wraps these as "whatsapp:<jid>"
         in the raw envelope — strip that prefix on the way in).
      4. Anything else (future shapes, unknown platforms): store in
         platform_user_id verbatim, warn once per shape so we know what's
         arriving without flooding the log.

    `target_uuid_hint` is the optional `reaction.targetAuthorUuid` already on
    the envelope — if present, it always wins for the UUID slot.
    """
    phone = None
    uuid = target_uuid_hint or None
    platform_user_id = None

    if not target_raw:
        return phone, uuid, platform_user_id

    raw = str(target_raw)

    # "whatsapp:<jid>" prefix from older connector payloads — strip it.
    if raw.lower().startswith("whatsapp:"):
        raw = raw[len("whatsapp:"):]

    if raw.startswith("+"):
        phone = raw
    elif is_uuid(raw):
        uuid = uuid or raw
    elif _JID_SUFFIX_RE.search(raw):
        platform_user_id = raw
    else:
        suffix = raw.rsplit("@", 1)[-1] if "@" in raw else f"<len{len(raw)}>"
        if suffix not in _unparseable_target_warned:
            _unparseable_target_warned.add(suffix)
            logger.warning(
                "insert_reaction: new target shape suffix=%r raw=%r (storing in "
                "target_platform_user_id verbatim; will not log this shape again)",
                suffix, raw,
            )
        platform_user_id = raw

    return phone, uuid, platform_user_id
