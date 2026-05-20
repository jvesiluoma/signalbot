import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from connectors.base import (
    CanonicalEvent, ChatRef, SenderRef, Attachment, ReactionRef, ReplyRef, Mention,
    normalize_phone, PLATFORM_SIGNAL, PLATFORM_TELEGRAM, PLATFORM_WHATSAPP, EV_MESSAGE,
)


def _tg_event():
    return CanonicalEvent(
        platform=PLATFORM_TELEGRAM, connector_id="tg-1", event_type=EV_MESSAGE,
        platform_msg_id="-1001:42", timestamp_ms=1_716_000_000_000,
        chat=ChatRef(platform_chat_id="-1001", title="Sec Chat", kind="group"),
        sender=SenderRef(platform_user_id="777", display_name="Bob", username="bobby"),
        text="check this https://a.com/x", urls=["https://a.com/x"],
        reply_to=ReplyRef(platform_msg_id="-1001:40", author_user_id="555", text="prev"),
        mentions=[Mention(platform_user_id="999", username="carol")],
        attachments=[Attachment(id="f1", content_type="image/jpeg", file_name=None, size=10, fetch_url="/v1/files/f1")],
    )


def test_roundtrip_dict():
    e = _tg_event()
    d = e.to_dict()
    e2 = CanonicalEvent.from_dict(d)
    assert e2.platform == PLATFORM_TELEGRAM
    assert e2.platform_chat_id == "-1001"
    assert e2.platform_user_id == "777"
    assert e2.urls == ["https://a.com/x"]
    assert e2.reply_to.platform_msg_id == "-1001:40"
    assert e2.attachments[0].id == "f1"
    assert e2.mentions[0].username == "carol"


def test_legacy_field_mapping_non_signal():
    e = _tg_event()
    assert e.legacy_group_id == "telegram:-1001"
    assert e.legacy_sender_phone == "telegram:777"   # no phone known


def test_legacy_field_mapping_signal_keeps_bare_ids():
    e = CanonicalEvent(platform=PLATFORM_SIGNAL, connector_id="signal-1",
                       chat=ChatRef(platform_chat_id="abc123base64="),
                       sender=SenderRef(platform_user_id="+358501234567", phone="+358501234567"))
    assert e.legacy_group_id == "abc123base64="
    assert e.legacy_sender_phone == "+358501234567"


def test_validate_rejects_unknown_platform():
    e = CanonicalEvent(platform="myspace", connector_id="x", chat=ChatRef(platform_chat_id="1"))
    with pytest.raises(ValueError):
        e.validate()


def test_validate_requires_chat_for_message():
    e = CanonicalEvent(platform=PLATFORM_SIGNAL, connector_id="signal-1", event_type=EV_MESSAGE)
    with pytest.raises(ValueError):
        e.validate()


@pytest.mark.parametrize("raw,expected", [
    ("+358 50 123 4567", "+358501234567"),
    ("358501234567", "+358501234567"),
    ("358501234567@s.whatsapp.net", "+358501234567"),
    ("123-456@g.us", None),       # group jid, not a phone
    ("abc", None),
    (None, None),
])
def test_normalize_phone(raw, expected):
    assert normalize_phone(raw) == expected
