"""
Signal adapter — thin wrapper over the bbernhard/signal-cli-rest-api daemon for
the *non-message* concerns (chat list, members, file fetch) used by the
generalized chat-sync loop and the identity engine.

Signal message ingest stays where it has always been: `poller.poll_messages()`
already polls `/v1/receive` and writes equivalent rows (now platform-tagged), so
we don't route Signal through `ingest.ingest_event()` — that path is for the
Telegram and WhatsApp connectors. This module exists so all three platforms
expose a uniform `list_chats()` / `list_members()` surface.
"""

from __future__ import annotations

import logging

import config
import poller
from connectors.base import ChatRef, SenderRef, PLATFORM_SIGNAL, CHAT_GROUP

logger = logging.getLogger("signal_adapter")


class SignalAdapter:
    platform = PLATFORM_SIGNAL

    def __init__(self, connector_id: str = "signal-1"):
        self.connector_id = connector_id

    # ── chat / membership sync ──

    def list_chats(self) -> list[ChatRef]:
        groups = poller._fetch_groups_list(debug=False) or []
        out: list[ChatRef] = []
        for g in groups:
            if not isinstance(g, dict):
                continue
            cid = g.get("internal_id") or g.get("id")
            if not cid:
                continue
            members = g.get("members") or []
            out.append(ChatRef(
                platform_chat_id=str(cid),
                title=g.get("name"),
                kind=CHAT_GROUP,
                is_public=bool(g.get("invite_link")),
                members_count=len(members) if isinstance(members, list) else None,
            ))
        return out

    def list_members(self, platform_chat_id: str) -> list[SenderRef]:
        groups = poller._fetch_groups_list(debug=False) or []
        for g in groups:
            if not isinstance(g, dict):
                continue
            cid = g.get("internal_id") or g.get("id")
            if str(cid) != str(platform_chat_id):
                continue
            out: list[SenderRef] = []
            for m in (g.get("members") or []):
                phone, uuid = poller._normalize_member(m)
                out.append(SenderRef(platform_user_id=uuid or phone, phone=phone if phone and str(phone).startswith("+") else None))
            return out
        return []

    # ── media ──

    def fetch_file(self, fetch_url: str) -> bytes:
        import requests
        # `fetch_url` is either a full URL or a "/v1/attachments/<name>" path.
        url = fetch_url if fetch_url.startswith("http") else f"{config.SIGNAL_API_BASE.rstrip('/')}{fetch_url}"
        resp = requests.get(url, timeout=(5, 30))
        resp.raise_for_status()
        return resp.content
