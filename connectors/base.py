"""
Canonical event schema + the Adapter interface every platform connector implements.

A *connector* is a sidecar service that holds one platform's credentials/session
(like `signal-cli-rest-api` does for Signal) and exposes a small HTTP API. An
*adapter* (this app's side) talks to that service and converts its native
payloads into `CanonicalEvent`s. `ingest.py` is the single place that writes
`CanonicalEvent`s into the database, so the rest of the app never has to know
which platform a message came from beyond its `platform` tag.

This module is intentionally dependency-free (stdlib only) and imports nothing
from the app, so `app.py` / `poller.py` / connector code can all import it
without circular-import worries.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

# ──────────────────────────────────────────────
# Platform identifiers
# ──────────────────────────────────────────────

PLATFORM_SIGNAL = "signal"
PLATFORM_TELEGRAM = "telegram"
PLATFORM_WHATSAPP = "whatsapp"
PLATFORMS = frozenset({PLATFORM_SIGNAL, PLATFORM_TELEGRAM, PLATFORM_WHATSAPP})

# Short UI badge codes (see `.platform-badge` in static/style.css).
PLATFORM_BADGE = {
    PLATFORM_SIGNAL: "SG",
    PLATFORM_TELEGRAM: "TG",
    PLATFORM_WHATSAPP: "WA",
}

# ──────────────────────────────────────────────
# Event types
# ──────────────────────────────────────────────

EV_MESSAGE = "message"
EV_EDIT = "edit"
EV_DELETE = "delete"
EV_REACTION = "reaction"
EV_REACTION_REMOVE = "reaction_remove"
EV_JOIN = "join"
EV_LEAVE = "leave"
EV_ADMIN_GRANT = "admin_grant"
EV_ADMIN_REVOKE = "admin_revoke"
EV_CHAT_RENAME = "chat_rename"
EV_CHAT_META = "chat_meta"
EV_ACTIVITY = "activity"  # device-activity probe result (see Part F)

EVENT_TYPES = frozenset({
    EV_MESSAGE, EV_EDIT, EV_DELETE, EV_REACTION, EV_REACTION_REMOVE,
    EV_JOIN, EV_LEAVE, EV_ADMIN_GRANT, EV_ADMIN_REVOKE,
    EV_CHAT_RENAME, EV_CHAT_META, EV_ACTIVITY,
})

# Chat kinds.
CHAT_GROUP = "group"
CHAT_CHANNEL = "channel"
CHAT_DM = "dm"

# ──────────────────────────────────────────────
# Sub-structures
# ──────────────────────────────────────────────


@dataclass
class ChatRef:
    platform_chat_id: str
    title: Optional[str] = None
    kind: str = CHAT_GROUP            # group | channel | dm
    is_public: bool = False
    members_count: Optional[int] = None


@dataclass
class SenderRef:
    platform_user_id: Optional[str] = None
    display_name: Optional[str] = None
    username: Optional[str] = None    # e.g. Telegram @handle
    phone: Optional[str] = None       # E.164 ("+...") when known


@dataclass
class Attachment:
    id: str
    content_type: Optional[str] = None
    file_name: Optional[str] = None
    size: Optional[int] = None
    # Path on the connector to fetch the bytes, e.g. "/v1/files/<id>".
    fetch_url: Optional[str] = None


@dataclass
class ReactionRef:
    emoji: str = ""
    target_msg_id: Optional[str] = None
    target_author_id: Optional[str] = None
    is_remove: bool = False


@dataclass
class ReplyRef:
    platform_msg_id: Optional[str] = None
    author_user_id: Optional[str] = None
    text: Optional[str] = None


@dataclass
class Mention:
    platform_user_id: Optional[str] = None
    username: Optional[str] = None


@dataclass
class MsgRef:
    platform_msg_id: Optional[str] = None


# ──────────────────────────────────────────────
# The canonical event
# ──────────────────────────────────────────────


@dataclass
class CanonicalEvent:
    platform: str
    connector_id: str
    event_type: str = EV_MESSAGE

    platform_msg_id: Optional[str] = None
    timestamp_ms: Optional[int] = None

    chat: Optional[ChatRef] = None
    sender: Optional[SenderRef] = None

    text: Optional[str] = None
    urls: list[str] = field(default_factory=list)

    reply_to: Optional[ReplyRef] = None
    mentions: list[Mention] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    reaction: Optional[ReactionRef] = None

    edit_of: Optional[MsgRef] = None
    delete_of: Optional[MsgRef] = None

    # Original platform payload, stored verbatim in messages.raw_envelope.
    raw: Optional[dict] = None

    schema: int = 1

    # ── Derived helpers ──

    @property
    def platform_chat_id(self) -> Optional[str]:
        return self.chat.platform_chat_id if self.chat else None

    @property
    def platform_user_id(self) -> Optional[str]:
        return self.sender.platform_user_id if self.sender else None

    @property
    def legacy_group_id(self) -> Optional[str]:
        """Value to put in the long-standing `messages.group_id` column.

        Signal keeps its bare base64 group id (so every existing query/index
        keeps working unchanged); other platforms get a namespaced
        ``"<platform>:<chat_id>"`` so they can't collide with a Signal id.
        """
        cid = self.platform_chat_id
        if cid is None:
            return None
        if self.platform == PLATFORM_SIGNAL:
            return cid
        return f"{self.platform}:{cid}"

    @property
    def legacy_sender_phone(self) -> Optional[str]:
        """Value to put in the long-standing `messages.sender_phone` column.

        Prefer a real phone; fall back to a namespaced ``"<platform>:<user_id>"``
        so dedup / per-sender aggregation still has a non-null key for
        phone-less accounts (Telegram users, UUID-only Signal users, …).
        """
        if not self.sender:
            return None
        if self.sender.phone:
            return self.sender.phone
        if self.sender.platform_user_id:
            if self.platform == PLATFORM_SIGNAL:
                return self.sender.platform_user_id
            return f"{self.platform}:{self.sender.platform_user_id}"
        return None

    # ── (de)serialization for the webhook transport ──

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CanonicalEvent":
        d = dict(d or {})
        chat = d.get("chat")
        sender = d.get("sender")
        reply_to = d.get("reply_to")
        reaction = d.get("reaction")
        edit_of = d.get("edit_of")
        delete_of = d.get("delete_of")
        return cls(
            platform=d.get("platform") or "",
            connector_id=d.get("connector_id") or "",
            event_type=d.get("event_type") or EV_MESSAGE,
            platform_msg_id=d.get("platform_msg_id"),
            timestamp_ms=d.get("timestamp_ms"),
            chat=ChatRef(**chat) if isinstance(chat, dict) else None,
            sender=SenderRef(**sender) if isinstance(sender, dict) else None,
            text=d.get("text"),
            urls=list(d.get("urls") or []),
            reply_to=ReplyRef(**reply_to) if isinstance(reply_to, dict) else None,
            mentions=[Mention(**m) for m in (d.get("mentions") or []) if isinstance(m, dict)],
            attachments=[Attachment(**a) for a in (d.get("attachments") or []) if isinstance(a, dict)],
            reaction=ReactionRef(**reaction) if isinstance(reaction, dict) else None,
            edit_of=MsgRef(**edit_of) if isinstance(edit_of, dict) else None,
            delete_of=MsgRef(**delete_of) if isinstance(delete_of, dict) else None,
            raw=d.get("raw"),
            schema=int(d.get("schema", 1) or 1),
        )

    # ── validation ──

    def validate(self) -> None:
        """Raise ValueError if the event is structurally unusable for ingest."""
        if self.platform not in PLATFORMS:
            raise ValueError(f"unknown platform: {self.platform!r}")
        if self.event_type not in EVENT_TYPES:
            raise ValueError(f"unknown event_type: {self.event_type!r}")
        if not self.connector_id:
            raise ValueError("connector_id is required")
        # message-ish events need a chat to land in
        if self.event_type in (EV_MESSAGE, EV_EDIT, EV_DELETE, EV_REACTION,
                                EV_REACTION_REMOVE) and not self.platform_chat_id:
            raise ValueError(f"{self.event_type} event missing chat.platform_chat_id")


# ──────────────────────────────────────────────
# Phone normalization
# ──────────────────────────────────────────────

_DIGITS_RE = re.compile(r"\d+")


def normalize_phone(raw) -> Optional[str]:
    """Best-effort normalize a phone identifier to E.164 ("+<digits>"), or None.

    Accepts ``"+358501234567"``, ``"358501234567"`` (WhatsApp's bare-digits
    form), ``"358501234567@s.whatsapp.net"`` (a WhatsApp JID), or arbitrary
    formatting like ``"+358 50 123 4567"``. Anything that doesn't yield a
    plausible 7–15 digit number returns None.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    # Strip a WhatsApp JID suffix if present.
    if "@" in s:
        s = s.split("@", 1)[0]
    # A WhatsApp group JID like "123-456" is not a phone.
    if "-" in s:
        return None
    digits = "".join(_DIGITS_RE.findall(s))
    if not (7 <= len(digits) <= 15):
        return None
    return "+" + digits


# ──────────────────────────────────────────────
# The Adapter interface
# ──────────────────────────────────────────────


class Adapter(ABC):
    """One adapter per connector instance. App-side; talks HTTP to the sidecar."""

    #: Stable identifier for this connector instance, e.g. "signal-1", "tg-1".
    connector_id: str
    #: One of PLATFORM_*.
    platform: str

    # ── ingest (pull transport) ──

    @abstractmethod
    def fetch_events(self, cursor: Optional[str]) -> tuple[list[CanonicalEvent], Optional[str]]:
        """Return (events, next_cursor). `cursor` is whatever was returned last
        time (opaque to the caller); pass None on first call."""
        raise NotImplementedError

    # ── chat / membership sync ──

    @abstractmethod
    def list_chats(self) -> list[ChatRef]:
        raise NotImplementedError

    @abstractmethod
    def list_members(self, platform_chat_id: str) -> list[SenderRef]:
        raise NotImplementedError

    # ── media ──

    @abstractmethod
    def fetch_file(self, fetch_url: str) -> bytes:
        """Download an attachment's bytes given the `Attachment.fetch_url`."""
        raise NotImplementedError

    # ── outbound (optional; default: not supported) ──

    def send_action(self, *_args, **_kwargs):  # pragma: no cover - optional
        """Optional outbound capability (send/react/delete/probe). Connectors
        that don't support it leave this raising."""
        raise NotImplementedError(f"{type(self).__name__} does not support outbound actions")
