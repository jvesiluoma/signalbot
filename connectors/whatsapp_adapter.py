"""
WhatsApp adapter (app side) — HTTP client for the `wa-connector` (Baileys) sidecar.

The WhatsApp connector is push-first: it POSTs CanonicalEvent dicts to the app's
`/ingest/whatsapp` endpoint. This adapter therefore exists mainly for the
chat-sync loop (`list_chats` / `list_members`) and media fetch; `fetch_events`
is provided too (the connector also keeps a small ring buffer) so the
`connector-poller` can run it in pull mode if push is disabled.
"""

from __future__ import annotations

import logging
from typing import Optional

import requests

import config
from connectors.base import CanonicalEvent, ChatRef, SenderRef, PLATFORM_WHATSAPP, normalize_phone

logger = logging.getLogger("whatsapp_adapter")


class WhatsAppAdapter:
    platform = PLATFORM_WHATSAPP

    def __init__(self, base: Optional[str] = None, api_key: Optional[str] = None,
                 connector_id: str = "wa-1", timeout: tuple = (5, 30)):
        self.base = (base or config.WA_CONNECTOR_BASE).rstrip("/")
        self.api_key = api_key if api_key is not None else config.WA_API_KEY
        self.connector_id = connector_id
        self.timeout = timeout

    def _headers(self):
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def _get(self, path: str, **params):
        r = requests.get(f"{self.base}{path}", params=params or None,
                         headers=self._headers(), timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    # ── ingest (pull fallback) ──
    def fetch_events(self, cursor: Optional[str]):
        try:
            since = int(cursor) if cursor not in (None, "") else 0
        except (TypeError, ValueError):
            since = 0
        data = self._get("/v1/events", since=since, limit=1000)
        evts = []
        for d in (data.get("events") or []):
            try:
                ev = CanonicalEvent.from_dict(d)
                ev.connector_id = ev.connector_id or self.connector_id
                evts.append(ev)
            except Exception:
                logger.exception("whatsapp_adapter: bad event payload")
        next_cursor = str(data.get("next_cursor") if data.get("next_cursor") is not None else since)
        return evts, next_cursor

    # ── chat / membership sync ──
    def list_chats(self) -> list[ChatRef]:
        out = []
        for c in (self._get("/v1/chats") or []):
            if not isinstance(c, dict) or not c.get("id"):
                continue
            out.append(ChatRef(platform_chat_id=str(c["id"]), title=c.get("title") or c.get("name"),
                               kind=c.get("kind") or ("group" if str(c["id"]).endswith("@g.us") else "dm"),
                               is_public=bool(c.get("is_public")),
                               members_count=c.get("members_count") or c.get("participantsCount")))
        return out

    def list_members(self, platform_chat_id: str) -> list[SenderRef]:
        try:
            data = self._get(f"/v1/chats/{platform_chat_id}/participants")
        except Exception:
            return []
        rows = data if isinstance(data, list) else (data.get("participants") or data.get("members") or [])
        out = []
        for m in rows:
            if not isinstance(m, dict):
                jid = str(m) if m else None
                out.append(SenderRef(platform_user_id=jid, phone=normalize_phone(jid)))
                continue
            jid = m.get("id") or m.get("jid") or m.get("platform_user_id")
            out.append(SenderRef(platform_user_id=jid, display_name=m.get("name") or m.get("display_name"),
                                 phone=normalize_phone(m.get("phone") or jid)))
        return out

    # ── media ──
    def fetch_file(self, fetch_url: str) -> bytes:
        url = fetch_url if fetch_url.startswith("http") else f"{self.base}{fetch_url}"
        r = requests.get(url, headers=self._headers(), timeout=(5, 60))
        r.raise_for_status()
        return r.content
