"""Tests for reaction_target.classify_reaction_target — Phase 2.2 fix-up.

Goal: WhatsApp `@lid` / `@s.whatsapp.net` JIDs that the legacy poller stuffed
into `reactions.target_author_phone` (corrupting 97% of WA reactions) now go
into the new `target_platform_user_id` column. E.164 phones and Signal UUIDs
keep going to their original columns.

The classifier lives in a leaf module (`reaction_target.py`) — not in
`poller.py` — precisely so this test runs without importing Playwright or the
YouTube transcript SDK.
"""

import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from app_core.reaction_target import (  # noqa: E402
    classify_reaction_target,
    _unparseable_target_warned,
)


@pytest.mark.parametrize("raw,uuid_hint,expected", [
    # E.164 phone — goes to target_phone, NOT to platform_user_id.
    ("+358501234567", None, ("+358501234567", None, None)),
    # Signal ACI UUID — goes to target_uuid.
    ("bef7c33f-728b-4357-a559-6ca71a4c19cf", None,
     (None, "bef7c33f-728b-4357-a559-6ca71a4c19cf", None)),
    # Envelope-provided UUID hint always wins for the UUID slot.
    ("bef7c33f-728b-4357-a559-6ca71a4c19cf", "deadbeef-dead-beef-dead-beefdeadbeef",
     (None, "deadbeef-dead-beef-dead-beefdeadbeef", None)),
    # WhatsApp regular user JID → platform_user_id.
    ("358501234567@s.whatsapp.net", None,
     (None, None, "358501234567@s.whatsapp.net")),
    # WhatsApp Linked-Device JID — the previously-unparseable case.
    ("118846174277734@lid", None,
     (None, None, "118846174277734@lid")),
    # WhatsApp group JID (defensive — shouldn't appear in targetAuthor).
    ("abc123@g.us", None, (None, None, "abc123@g.us")),
    # WhatsApp community JID.
    ("abc123@c.us", None, (None, None, "abc123@c.us")),
    # Connector-prefixed form "whatsapp:NNN@lid" — prefix stripped.
    ("whatsapp:118846174277734@lid", None,
     (None, None, "118846174277734@lid")),
    ("whatsapp:358501234567@s.whatsapp.net", None,
     (None, None, "358501234567@s.whatsapp.net")),
    # Empty / None — all None, no warning.
    ("", None, (None, None, None)),
    (None, None, (None, None, None)),
    # Truly unrecognized shape — stored verbatim.
    ("opaque-token", None, (None, None, "opaque-token")),
])
def test_classify(raw, uuid_hint, expected):
    assert classify_reaction_target(raw, uuid_hint) == expected


def test_uuid_hint_used_when_raw_is_phone():
    """Envelope with both targetAuthor (phone) and targetAuthorUuid populates
    both slots; phone wins the routing column, UUID gets persisted alongside."""
    raw = "+358501234567"
    hint = "bef7c33f-728b-4357-a559-6ca71a4c19cf"
    phone, uuid, pid = classify_reaction_target(raw, hint)
    assert phone == raw
    assert uuid == hint
    assert pid is None


def test_warning_dedup(caplog):
    """The 'new target shape' warning must fire at most once per suffix to
    avoid the pre-fix log flood (where the same dozen accounts produced
    thousands of identical warnings)."""
    import logging

    _unparseable_target_warned.clear()
    with caplog.at_level(logging.WARNING, logger="poller"):
        classify_reaction_target("alpha-beta-7777", None)   # shape: "<len15>"
        classify_reaction_target("opaque@new.proto", None)  # shape: "new.proto"
    new_shape_warns = [r for r in caplog.records if "new target shape" in r.getMessage()]
    assert len(new_shape_warns) == 2  # two distinct shapes

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="poller"):
        classify_reaction_target("alpha-beta-9999", None)  # same shape "<len15>"
        classify_reaction_target("other@new.proto", None)  # same shape "new.proto"
    repeat_warns = [r for r in caplog.records if "new target shape" in r.getMessage()]
    assert len(repeat_warns) == 0
