"""
Telegram adapter (app side) — HTTP client for the `tg-connector` sidecar.

Pull transport: `fetch_events(cursor)` GETs `/v1/events?since=<cursor>` and turns
the returned dicts into `CanonicalEvent`s for `ingest.ingest_event()`. The
connector keeps an in-memory ring buffer, so the cursor (a Telegram update_id)
gives at-least-once replay across restarts when paired with the idempotent
`idx_msg_platform_dedup`.
"""

from __future__ import annotations

import logging
from typing import Optional

import requests

import config
from connectors.base import CanonicalEvent, ChatRef, SenderRef, PLATFORM_TELEGRAM

logger = logging.getLogger("telegram_adapter")


class TelegramAdapter:
    platform = PLATFORM_TELEGRAM

    def __init__(self, base: Optional[str] = None, token: Optional[str] = None,
                 connector_id: str = "tg-1", timeout: tuple = (5, 35)):
        self.base = (base or config.TG_CONNECTOR_BASE).rstrip("/")
        self.token = token if token is not None else config.TG_CONNECTOR_TOKEN
        self.connector_id = connector_id
        self.timeout = timeout

    # ── internal ──
    def _headers(self):
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def _get(self, path: str, **params):
        r = requests.get(f"{self.base}{path}", params=params or None,
                         headers=self._headers(), timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    # ── ingest (pull) ──
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
                logger.exception("telegram_adapter: bad event payload")
        next_cursor = str(data.get("next_cursor") if data.get("next_cursor") is not None else since)
        return evts, next_cursor

    # ── chat / membership sync ──
    def list_chats(self) -> list[ChatRef]:
        out = []
        for c in (self._get("/v1/chats") or []):
            if not isinstance(c, dict) or not c.get("id"):
                continue
            out.append(ChatRef(platform_chat_id=str(c["id"]), title=c.get("title"),
                               kind=c.get("kind") or "group", is_public=bool(c.get("is_public")),
                               members_count=c.get("members_count")))
        return out

    def list_members(self, platform_chat_id: str) -> list[SenderRef]:
        try:
            data = self._get(f"/v1/chats/{platform_chat_id}/members")
        except Exception:
            return []
        rows = data if isinstance(data, list) else (data.get("members") or [])
        out = []
        for m in rows:
            if not isinstance(m, dict):
                continue
            out.append(SenderRef(platform_user_id=m.get("platform_user_id"),
                                 display_name=m.get("display_name"), username=m.get("username")))
        return out

    # ── media ──
    def fetch_file(self, fetch_url: str) -> bytes:
        url = fetch_url if fetch_url.startswith("http") else f"{self.base}{fetch_url}"
        r = requests.get(url, headers=self._headers(), timeout=(5, 60))
        r.raise_for_status()
        return r.content
