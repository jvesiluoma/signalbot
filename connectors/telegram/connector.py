"""
Telegram connector — Bot API only, read-only, low-profile.

Runs as its own container (it holds the bot token, nothing else does). It
long-polls `getUpdates`, normalizes each update into the project's CanonicalEvent
JSON shape (the same dict `connectors.base.CanonicalEvent.to_dict()` produces),
and exposes a tiny HTTP API the main app polls:

    GET  /healthz                    -> { ok, bot, offset, chats }
    GET  /v1/me                      -> getMe result
    GET  /v1/events?since=<update_id>&limit=N  -> { events: [...], next_cursor }
    GET  /v1/chats                   -> [ { id, title, kind, is_public, members_count } ]
    GET  /v1/chats/<id>/members      -> [ { platform_user_id, display_name, username } ]  (admins; Bot API can't list all members)
    GET  /v1/files/<file_id>         -> the raw file bytes

All /v1/* endpoints require `Authorization: Bearer <TG_CONNECTOR_TOKEN>` when
that env var is set. If `INGEST_URL` is set the connector ALSO pushes each event
there (POST, `Authorization: Bearer <INGEST_WEBHOOK_TOKEN>`); by default it's
pull-only.

Stealth notes / Bot API limitations: the bot is a *visible* group member; it
sees no messages from before it joined; it can't see other users' DMs; it needs
to be a channel admin to read channel posts; and Bot API exposes no
delivery/read receipts or presence for other users (so the activity probe is a
no-op on Telegram). Disable BotFather privacy mode (`/setprivacy` → Disable) so
the bot sees all group messages.
"""

from __future__ import annotations

import collections
import logging
import mimetypes
import os
import re
import threading
import time

import requests
from flask import Flask, jsonify, request, abort, Response

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("tg-connector")

# ── config ──
TG_BOT_TOKEN       = os.getenv("TG_BOT_TOKEN", "").strip()
TG_BOT_API_BASE    = os.getenv("TG_BOT_API_BASE", "https://api.telegram.org").rstrip("/")
TG_TARGET_CHAT_IDS = set(filter(None, (s.strip() for s in os.getenv("TG_TARGET_CHAT_IDS", "").split(","))))
CONNECTOR_TOKEN    = os.getenv("TG_CONNECTOR_TOKEN", "").strip()
CONNECTOR_ID       = os.getenv("CONNECTOR_ID", "tg-1")
INGEST_URL         = os.getenv("INGEST_URL", "").strip()
INGEST_TOKEN       = os.getenv("INGEST_WEBHOOK_TOKEN", "").strip()
PORT               = int(os.getenv("CONNECTOR_PORT", "8081"))
LONGPOLL_TIMEOUT   = int(os.getenv("TG_LONGPOLL_TIMEOUT", "25"))
STATE_DIR          = os.getenv("STATE_DIR", "/data")
EVENT_BUFFER       = int(os.getenv("TG_EVENT_BUFFER", "5000"))

API = f"{TG_BOT_API_BASE}/bot{TG_BOT_TOKEN}"
_OFFSET_PATH = os.path.join(STATE_DIR, "offset.txt")

_URL_RE = re.compile(r'https?://\S+')

app = Flask(__name__)
_events: "collections.deque[tuple[int, dict]]" = collections.deque(maxlen=EVENT_BUFFER)
_chats: dict[str, dict] = {}
_lock = threading.Lock()
_bot_info: dict = {}
_offset = 0


# ──────────────────────────────────────────────
# Telegram API helper
# ──────────────────────────────────────────────

def _tg(method: str, **params):
    if not TG_BOT_TOKEN:
        raise RuntimeError("TG_BOT_TOKEN not set")
    r = requests.post(f"{API}/{method}", json=params, timeout=(5, LONGPOLL_TIMEOUT + 15))
    r.raise_for_status()
    d = r.json()
    if not d.get("ok"):
        raise RuntimeError(f"telegram {method} failed: {d.get('description')}")
    return d.get("result")


def _load_offset() -> int:
    try:
        with open(_OFFSET_PATH) as f:
            return int(f.read().strip() or "0")
    except Exception:
        return 0


def _save_offset(o: int) -> None:
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(_OFFSET_PATH, "w") as f:
            f.write(str(int(o)))
    except Exception as e:
        logger.debug("save offset failed: %s", e)


# ──────────────────────────────────────────────
# Update → CanonicalEvent dict
# ──────────────────────────────────────────────

_CHAT_KIND = {"private": "dm", "group": "group", "supergroup": "group", "channel": "channel"}


def _kind(chat: dict) -> str:
    return _CHAT_KIND.get(chat.get("type", ""), "group")


def _full_name(u: dict) -> str:
    if not u:
        return ""
    parts = [u.get("first_name"), u.get("last_name")]
    return " ".join(p for p in parts if p) or (u.get("username") or "")


def _remember_chat(chat: dict, members_count=None):
    if not chat or "id" not in chat:
        return
    cid = str(chat["id"])
    rec = _chats.setdefault(cid, {})
    rec.update({
        "id": cid,
        "title": chat.get("title") or chat.get("username") or _full_name(chat),
        "kind": _kind(chat),
        "is_public": bool(chat.get("username")),
    })
    if members_count is not None:
        rec["members_count"] = members_count


def _extract_urls(text: str, entities) -> list[str]:
    out: list[str] = []
    if entities and text:
        # Telegram entity offsets are in UTF-16 code units; for plain ASCII URLs
        # this matches Python slicing closely enough for our purposes.
        for e in entities:
            t = e.get("type")
            if t == "text_link" and e.get("url"):
                out.append(e["url"])
            elif t == "url":
                try:
                    out.append(text[e["offset"]:e["offset"] + e["length"]])
                except Exception:
                    pass
    for m in _URL_RE.findall(text or ""):
        if m not in out:
            out.append(m)
    # de-dup, preserve order
    seen = set()
    return [u for u in out if not (u in seen or seen.add(u))]


def _attachments_from(msg: dict) -> list[dict]:
    out = []
    photos = msg.get("photo")
    if photos:  # list of sizes; take the largest
        big = max(photos, key=lambda p: p.get("file_size") or p.get("width", 0))
        out.append({"id": big["file_id"], "content_type": "image/jpeg",
                    "file_name": None, "size": big.get("file_size"),
                    "fetch_url": f"/v1/files/{big['file_id']}"})
    for key, default_ct in (("document", None), ("video", "video/mp4"), ("audio", "audio/mpeg"),
                            ("voice", "audio/ogg"), ("video_note", "video/mp4"), ("animation", "video/mp4"),
                            ("sticker", "image/webp")):
        a = msg.get(key)
        if not a:
            continue
        ct = a.get("mime_type") or default_ct
        fn = a.get("file_name")
        if not ct and fn:
            ct = mimetypes.guess_type(fn)[0]
        out.append({"id": a["file_id"], "content_type": ct, "file_name": fn,
                    "size": a.get("file_size"), "fetch_url": f"/v1/files/{a['file_id']}"})
    return out


def _mentions_from(text: str, entities) -> list[dict]:
    out = []
    for e in (entities or []):
        if e.get("type") == "text_mention" and e.get("user"):
            out.append({"platform_user_id": str(e["user"]["id"]), "username": e["user"].get("username")})
        elif e.get("type") == "mention" and text:
            try:
                handle = text[e["offset"]:e["offset"] + e["length"]].lstrip("@")
                out.append({"platform_user_id": None, "username": handle})
            except Exception:
                pass
    return out


def _msg_event(msg: dict, *, edited: bool, channel_post: bool) -> dict | None:
    chat = msg.get("chat") or {}
    if "id" not in chat:
        return None
    _remember_chat(chat)
    text = msg.get("text") or msg.get("caption") or ""
    entities = msg.get("entities") or msg.get("caption_entities") or []
    urls = _extract_urls(text, entities)

    frm = msg.get("from") or {}
    sender_chat = msg.get("sender_chat")
    if frm.get("id"):
        sender = {"platform_user_id": str(frm["id"]), "display_name": _full_name(frm),
                  "username": frm.get("username"), "phone": None}
    elif sender_chat:
        sender = {"platform_user_id": f"chat:{sender_chat['id']}",
                  "display_name": sender_chat.get("title") or sender_chat.get("username"),
                  "username": sender_chat.get("username"), "phone": None}
    else:  # bare channel post
        sender = {"platform_user_id": f"channel:{chat['id']}",
                  "display_name": chat.get("title"), "username": chat.get("username"), "phone": None}

    reply = msg.get("reply_to_message")
    reply_to = None
    if reply and reply.get("message_id"):
        rfrm = reply.get("from") or {}
        reply_to = {"platform_msg_id": f"{chat['id']}:{reply['message_id']}",
                    "author_user_id": str(rfrm["id"]) if rfrm.get("id") else None,
                    "text": (reply.get("text") or reply.get("caption") or "")[:2048] or None}

    ts_ms = (msg.get("edit_date") or msg.get("date") or 0) * 1000

    return {
        "schema": 1,
        "platform": "telegram",
        "connector_id": CONNECTOR_ID,
        "event_type": "edit" if edited else "message",
        "platform_msg_id": f"{chat['id']}:{msg['message_id']}",
        "timestamp_ms": ts_ms,
        "chat": {"platform_chat_id": str(chat["id"]),
                 "title": chat.get("title") or chat.get("username") or _full_name(chat),
                 "kind": _kind(chat), "is_public": bool(chat.get("username")),
                 "members_count": None},
        "sender": sender,
        "text": text,
        "urls": urls,
        "reply_to": reply_to,
        "mentions": _mentions_from(text, entities),
        "attachments": _attachments_from(msg),
        "reaction": None,
        "edit_of": {"platform_msg_id": f"{chat['id']}:{msg['message_id']}"} if edited else None,
        "delete_of": None,
        "raw": {"telegram_update_kind": ("edited_channel_post" if (edited and channel_post)
                                         else "channel_post" if channel_post
                                         else "edited_message" if edited else "message"),
                "message": msg},
    }


def _reaction_event(mr: dict) -> dict | None:
    chat = mr.get("chat") or {}
    if "id" not in chat or "message_id" not in mr:
        return None
    _remember_chat(chat)
    old = {r.get("emoji") for r in (mr.get("old_reaction") or []) if r.get("type") == "emoji"}
    new = {r.get("emoji") for r in (mr.get("new_reaction") or []) if r.get("type") == "emoji"}
    added = new - old
    removed = old - new
    is_remove = bool(removed) and not added
    emoji = (next(iter(added)) if added else (next(iter(removed)) if removed else "")) or ""
    actor = mr.get("user") or {}
    actor_chat = mr.get("actor_chat") or {}
    if actor.get("id"):
        sender = {"platform_user_id": str(actor["id"]), "display_name": _full_name(actor),
                  "username": actor.get("username"), "phone": None}
    elif actor_chat.get("id"):
        sender = {"platform_user_id": f"chat:{actor_chat['id']}", "display_name": actor_chat.get("title"),
                  "username": actor_chat.get("username"), "phone": None}
    else:
        sender = {"platform_user_id": None, "display_name": None, "username": None, "phone": None}
    return {
        "schema": 1, "platform": "telegram", "connector_id": CONNECTOR_ID,
        "event_type": "reaction_remove" if is_remove else "reaction",
        "platform_msg_id": None,
        "timestamp_ms": (mr.get("date") or 0) * 1000,
        "chat": {"platform_chat_id": str(chat["id"]), "title": chat.get("title"),
                 "kind": _kind(chat), "is_public": bool(chat.get("username")), "members_count": None},
        "sender": sender, "text": None, "urls": [], "reply_to": None, "mentions": [],
        "attachments": [],
        "reaction": {"emoji": emoji, "target_msg_id": f"{chat['id']}:{mr['message_id']}",
                     "target_author_id": None, "is_remove": is_remove},
        "edit_of": None, "delete_of": None,
        "raw": {"telegram_update_kind": "message_reaction", "message_reaction": mr},
    }


_STATUS_EVENT = {
    "member": "join", "creator": "join", "administrator": "admin_grant",
    "restricted": "join", "left": "leave", "kicked": "leave",
}


def _chat_member_event(cm: dict, *, is_my: bool) -> dict | None:
    chat = cm.get("chat") or {}
    if "id" not in chat:
        return None
    _remember_chat(chat)
    old_st = (cm.get("old_chat_member") or {}).get("status")
    new = cm.get("new_chat_member") or {}
    new_st = new.get("status")
    if not new_st:
        return None
    et = _STATUS_EVENT.get(new_st)
    if not et:
        return None
    # admin → member is a revoke, not a join
    if et == "join" and old_st == "administrator" and new_st not in ("administrator", "creator"):
        et = "admin_revoke"
    # no real status change → nothing to record
    if old_st == new_st:
        return None
    user = new.get("user") or {}
    sender = {"platform_user_id": str(user["id"]) if user.get("id") else None,
              "display_name": _full_name(user), "username": user.get("username"), "phone": None}
    return {
        "schema": 1, "platform": "telegram", "connector_id": CONNECTOR_ID,
        "event_type": et, "platform_msg_id": None,
        "timestamp_ms": (cm.get("date") or 0) * 1000,
        "chat": {"platform_chat_id": str(chat["id"]), "title": chat.get("title"),
                 "kind": _kind(chat), "is_public": bool(chat.get("username")), "members_count": None},
        "sender": sender, "text": f"{old_st or '?'} → {new_st}", "urls": [], "reply_to": None,
        "mentions": [], "attachments": [], "reaction": None, "edit_of": None, "delete_of": None,
        "raw": {"telegram_update_kind": "my_chat_member" if is_my else "chat_member", "chat_member": cm},
    }


def _build_event(update: dict) -> dict | None:
    if "message" in update:
        return _msg_event(update["message"], edited=False, channel_post=False)
    if "edited_message" in update:
        return _msg_event(update["edited_message"], edited=True, channel_post=False)
    if "channel_post" in update:
        return _msg_event(update["channel_post"], edited=False, channel_post=True)
    if "edited_channel_post" in update:
        return _msg_event(update["edited_channel_post"], edited=True, channel_post=True)
    if "message_reaction" in update:
        return _reaction_event(update["message_reaction"])
    if "chat_member" in update:
        return _chat_member_event(update["chat_member"], is_my=False)
    if "my_chat_member" in update:
        return _chat_member_event(update["my_chat_member"], is_my=True)
    return None


# ──────────────────────────────────────────────
# Long-poll loop
# ──────────────────────────────────────────────

_ALLOWED_UPDATES = ["message", "edited_message", "channel_post", "edited_channel_post",
                    "message_reaction", "chat_member", "my_chat_member"]


def _poll_loop():
    global _offset, _bot_info
    # Wait for a token; this thread is started unconditionally.
    while not TG_BOT_TOKEN:
        logger.warning("TG_BOT_TOKEN not set; idling")
        time.sleep(30)
    try:
        _bot_info = _tg("getMe") or {}
        logger.info("Telegram bot: @%s (id=%s)", _bot_info.get("username"), _bot_info.get("id"))
    except Exception as e:
        logger.warning("getMe failed: %s", e)
    _offset = _load_offset()
    logger.info("starting long-poll from offset %d", _offset)
    while True:
        try:
            updates = _tg("getUpdates", offset=_offset, timeout=LONGPOLL_TIMEOUT,
                          allowed_updates=_ALLOWED_UPDATES) or []
        except requests.RequestException as e:
            logger.warning("getUpdates network error: %s", e)
            time.sleep(5)
            continue
        except Exception as e:
            logger.warning("getUpdates error: %s", e)
            time.sleep(10)
            continue
        for u in updates:
            uid = u.get("update_id", 0)
            _offset = max(_offset, uid + 1)
            try:
                ev = _build_event(u)
            except Exception:
                logger.exception("build_event failed for update %s", uid)
                ev = None
            if ev is None:
                continue
            cid = ev.get("chat", {}).get("platform_chat_id")
            if TG_TARGET_CHAT_IDS and str(cid) not in TG_TARGET_CHAT_IDS:
                continue
            with _lock:
                _events.append((uid, ev))
            if INGEST_URL:
                try:
                    requests.post(INGEST_URL, json=ev,
                                  headers={"Authorization": f"Bearer {INGEST_TOKEN}"} if INGEST_TOKEN else {},
                                  timeout=(5, 20))
                except Exception as e:
                    logger.warning("ingest push failed: %s", e)
        if updates:
            _save_offset(_offset)


# ──────────────────────────────────────────────
# HTTP API
# ──────────────────────────────────────────────

@app.before_request
def _auth():
    if request.path.startswith("/v1/") and CONNECTOR_TOKEN:
        hdr = request.headers.get("Authorization", "")
        if hdr != f"Bearer {CONNECTOR_TOKEN}":
            abort(401)


@app.get("/healthz")
def healthz():
    with _lock:
        n = len(_events)
        chats = len(_chats)
    return jsonify(ok=bool(TG_BOT_TOKEN), bot=_bot_info.get("username"), offset=_offset,
                   buffered_events=n, chats=chats)


@app.get("/v1/me")
def v1_me():
    try:
        return jsonify(_tg("getMe"))
    except Exception as e:
        return jsonify(error=str(e)), 502


@app.get("/v1/events")
def v1_events():
    since = request.args.get("since", type=int) or 0
    limit = min(request.args.get("limit", default=500, type=int), 2000)
    with _lock:
        items = [(uid, ev) for (uid, ev) in _events if uid > since]
    items.sort(key=lambda t: t[0])
    items = items[:limit]
    next_cursor = items[-1][0] if items else since
    return jsonify(events=[ev for (_uid, ev) in items], next_cursor=next_cursor)


@app.get("/v1/chats")
def v1_chats():
    with _lock:
        return jsonify(list(_chats.values()))


@app.get("/v1/chats/<path:cid>/members")
def v1_chat_members(cid):
    # Bot API can't enumerate all members; return the administrators we can see.
    try:
        admins = _tg("getChatAdministrators", chat_id=cid) or []
    except Exception as e:
        return jsonify(error=str(e), members=[]), 200
    out = []
    for a in admins:
        u = a.get("user") or {}
        out.append({"platform_user_id": str(u.get("id")) if u.get("id") else None,
                    "display_name": _full_name(u), "username": u.get("username"),
                    "phone": None, "role": "admin"})
    return jsonify(out)


@app.get("/v1/files/<path:file_id>")
def v1_file(file_id):
    try:
        f = _tg("getFile", file_id=file_id) or {}
        fp = f.get("file_path")
        if not fp:
            return jsonify(error="no file_path"), 404
        # The Bot API server serves files at /file/bot<token>/<file_path>
        url = f"{TG_BOT_API_BASE}/file/bot{TG_BOT_TOKEN}/{fp}"
        r = requests.get(url, timeout=(5, 60))
        r.raise_for_status()
        ct = r.headers.get("Content-Type") or mimetypes.guess_type(fp)[0] or "application/octet-stream"
        return Response(r.content, content_type=ct)
    except Exception as e:
        logger.warning("file fetch failed for %s: %s", file_id, e)
        return jsonify(error=str(e)), 502


@app.get("/")
@app.get("/qr")
def index():
    # Telegram uses a bot token (no QR pairing). This page just reports status.
    ok = bool(TG_BOT_TOKEN)
    bot = _bot_info.get("username") or "(unknown — set TG_BOT_TOKEN, then check /healthz)"
    return Response(
        f"<h2>Telegram connector</h2><p>token configured: <b>{ok}</b></p>"
        f"<p>bot: <b>@{bot}</b></p>"
        f"<p>Add the bot to your target groups, disable BotFather privacy mode "
        f"(/setprivacy → Disable), and set TG_TARGET_CHAT_IDS. See /healthz for status.</p>",
        content_type="text/html",
    )


_poller_thread = threading.Thread(target=_poll_loop, daemon=True, name="tg-longpoll")
_poller_thread.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
