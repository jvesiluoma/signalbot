"""Smoke test for ingest.ingest_event() without a real DB or the real poller.

We stub `poller` in sys.modules (so `import poller` inside ingest_event picks up
the mock) and pass a MagicMock connection. The point is to verify the canonical
event is routed and the legacy fields are mapped, not to test SQL.
"""
import os, sys, types
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Stub a minimal `poller` module BEFORE importing ingest (ingest imports it lazily).
fake_poller = types.ModuleType("poller")
fake_poller.take_screenshot = lambda url, debug=False: (None, None)
fake_poller.insert_message = MagicMock(return_value=123)
fake_poller.insert_quote = MagicMock()
fake_poller.insert_mentions = MagicMock()
fake_poller.insert_message_attachments = MagicMock()
fake_poller.insert_page_snapshot = MagicMock()
fake_poller.insert_reaction = MagicMock(return_value=1)
fake_poller._record_membership_event = MagicMock()
sys.modules["poller"] = fake_poller

import ingest  # noqa: E402
from connectors.base import CanonicalEvent, ChatRef, SenderRef, ReactionRef  # noqa: E402


def _conn():
    c = MagicMock()
    c.cursor.return_value = MagicMock()
    return c


def test_ingest_message_calls_insert_message_with_platform():
    fake_poller.insert_message.reset_mock()
    conn = _conn()
    ev = CanonicalEvent(
        platform="whatsapp", connector_id="wa-1", event_type="message",
        platform_msg_id="123@g.us:ABC", timestamp_ms=1_716_000_000_000,
        chat=ChatRef(platform_chat_id="123@g.us", title="Group X", kind="group"),
        sender=SenderRef(platform_user_id="358501234567@s.whatsapp.net", display_name="Alice",
                         phone="+358501234567"),
        text="hello https://example.org/a", urls=["https://example.org/a"],
    )
    mid = ingest.ingest_event(conn, ev, do_screenshot=False, debug=False)
    assert mid == 123
    assert fake_poller.insert_message.called
    _args, kwargs = fake_poller.insert_message.call_args
    assert kwargs.get("platform") == "whatsapp"
    assert kwargs.get("connector_id") == "wa-1"
    # legacy group_id / sender_phone mapping (positional args of insert_message)
    pos = fake_poller.insert_message.call_args[0]
    # signature: (conn, sender_name, sender_phone, message_text, request_url, group_name, group_id, sent_timestamp, ...)
    assert pos[2] == "+358501234567"            # sender_phone (real phone wins)
    assert pos[5] == "Group X"                  # group_name
    assert pos[6] == "whatsapp:123@g.us"        # legacy group_id is namespaced for non-Signal


def test_ingest_reaction_routes_to_insert_reaction():
    fake_poller.insert_reaction.reset_mock()
    conn = _conn()
    ev = CanonicalEvent(
        platform="telegram", connector_id="tg-1", event_type="reaction",
        timestamp_ms=1_716_000_000_000,
        chat=ChatRef(platform_chat_id="-100123", title="TG"),
        sender=SenderRef(platform_user_id="42", display_name="Bob"),
        reaction=ReactionRef(emoji="👍", target_msg_id="-100123:9", target_author_id="55", is_remove=False),
    )
    ingest.ingest_event(conn, ev, debug=False)
    assert fake_poller.insert_reaction.called
    # platform passed through as kwarg
    assert fake_poller.insert_reaction.call_args.kwargs.get("platform") == "telegram"


def test_ingest_drops_invalid_event():
    conn = _conn()
    bad = CanonicalEvent(platform="nope", connector_id="x")
    assert ingest.ingest_event(conn, bad) is None
