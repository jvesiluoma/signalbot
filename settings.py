"""
DB-backed runtime settings overlay.

`config.py` holds the static defaults sourced from environment variables at
process start. This module adds a small key/value layer on top of it, stored in
the `app_settings` MySQL table, that the Settings web page can write to and that
hot paths re-read each cycle. Reads are cached for a few seconds so this stays
cheap to call inside loops.

Keys are plain strings; values are stored as TEXT. The helper accessors coerce
to bool / int / list / set. When a key is absent the caller's default (usually a
value from `config.py`) is returned, so deleting a row simply reverts to the env
default.

Anything written here that the poller / connector threads cannot pick up live
(connector tokens, enable flags) is still useful — it persists across restarts
and is the single place the Settings page reads/writes — but it only takes
effect on the next process start. See `docs/SETTINGS_PAGE_DESIGN.md`.
"""

from __future__ import annotations

import logging
import threading
import time

import mysql.connector

import config

logger = logging.getLogger("settings")

# How long a loaded snapshot is reused before re-reading the table.
_CACHE_TTL = 5.0  # seconds

_lock = threading.Lock()
_cache: dict[str, str] = {}
_cache_at: float = 0.0
_table_ready = False

# Keys the Settings page is allowed to write. The POST handler whitelists
# against this; anything else is rejected so a stray field can't poison config.
KNOWN_KEYS = {
    "save_own_messages",          # bool  — store the bot account's own outgoing messages
    "signal_target_group_ids",    # csv   — monitored Signal group ids (overrides TARGET_GROUP_IDS)
    "telegram_enabled",           # bool  — (restart required to (de)activate connector threads)
    "telegram_bot_token",         # str   — (restart required; the tg-connector reads it)
    "telegram_target_chat_ids",   # csv
    "whatsapp_enabled",           # bool  — (restart required)
    "whatsapp_target_chat_ids",   # csv
    "poll_interval",              # int   seconds between Signal poll cycles
    "image_caption_enabled",      # bool  — generate AI captions for image attachments
    "video_caption_enabled",      # bool  — generate AI captions for video attachments
    # AI / LLM model config (DB overlay on config.py env defaults). Live ≤5s.
    "ai_enabled",                 # bool  — global LLM master switch (default on)
    "ollama_summary_model",       # str   — '__none__' disables the role
    "ollama_summary_thinking",    # bool
    "ollama_summary_num_ctx",     # int
    "ollama_summary_num_predict", # int
    "ollama_analysis_model",      # str
    "ollama_analysis_thinking",   # bool
    "ollama_analysis_num_ctx",    # int
    "ollama_analysis_num_predict",# int
    "ollama_sentiment_model",     # str
    "ollama_sentiment_thinking",  # bool
    "ollama_sentiment_num_ctx",   # int
    "ollama_sentiment_num_predict",# int
    "ollama_vision_model",        # str
    "ollama_vision_thinking",     # bool
    "ollama_vision_num_ctx",      # int
    "ollama_vision_num_predict",  # int
}

_DDL = """
CREATE TABLE IF NOT EXISTS app_settings (
    setting_key   VARCHAR(128) NOT NULL,
    setting_value TEXT,
    updated_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (setting_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


# ──────────────────────────────────────────────
# Internals
# ──────────────────────────────────────────────

def _connect():
    return mysql.connector.connect(**config.DB_CONFIG)


def _ensure_table(conn):
    global _table_ready
    if _table_ready:
        return
    try:
        cur = conn.cursor()
        cur.execute(_DDL)
        conn.commit()
        _table_ready = True
    except mysql.connector.Error as e:
        logger.debug("app_settings DDL failed (will retry): %s", e)


def _load_locked():
    """Reload the cache. Caller must hold `_lock`."""
    global _cache, _cache_at
    conn = None
    try:
        conn = _connect()
        _ensure_table(conn)
        cur = conn.cursor()
        cur.execute("SELECT setting_key, setting_value FROM app_settings")
        _cache = {str(k): str(v) for (k, v) in cur.fetchall() if v is not None}
        _cache_at = time.monotonic()
    except mysql.connector.Error as e:
        # Keep whatever we had; bump the timestamp so we don't hammer a down DB.
        logger.debug("settings load failed: %s", e)
        _cache_at = time.monotonic()
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _maybe_refresh(force=False):
    with _lock:
        if force or not _cache_at or (time.monotonic() - _cache_at) > _CACHE_TTL:
            _load_locked()


# ──────────────────────────────────────────────
# Readers
# ──────────────────────────────────────────────

def get(key, default=None):
    _maybe_refresh()
    v = _cache.get(key)
    return default if v is None else v


def get_bool(key, default=False):
    v = get(key, None)
    if v is None:
        return bool(default)
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def get_int(key, default=0):
    v = get(key, None)
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        try:
            return int(default)
        except (TypeError, ValueError):
            return 0


def get_list(key, default=None):
    """Return a comma-separated value as a stripped, non-empty list."""
    v = get(key, None)
    if v is None:
        return list(default) if default else []
    return [s.strip() for s in str(v).split(",") if s.strip()]


def get_set(key, default=None):
    return set(get_list(key, default))


def all_settings():
    """Return a copy of every stored key/value (cached)."""
    _maybe_refresh()
    return dict(_cache)


def is_set(key):
    _maybe_refresh()
    return _cache.get(key) is not None


# ──────────────────────────────────────────────
# Writers
# ──────────────────────────────────────────────

def save(key, value):
    save_many({key: value})


def save_many(mapping):
    """Upsert each key. A value of None deletes the key (reverts to env default)."""
    if not mapping:
        return
    conn = None
    try:
        conn = _connect()
        _ensure_table(conn)
        cur = conn.cursor()
        for k, v in mapping.items():
            if v is None:
                cur.execute("DELETE FROM app_settings WHERE setting_key=%s", (str(k),))
            else:
                cur.execute(
                    "INSERT INTO app_settings (setting_key, setting_value) VALUES (%s, %s) "
                    "ON DUPLICATE KEY UPDATE setting_value=VALUES(setting_value)",
                    (str(k), str(v)),
                )
        conn.commit()
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    _maybe_refresh(force=True)


# ──────────────────────────────────────────────
# Convenience accessors for things the running process reads each cycle
# ──────────────────────────────────────────────

def signal_target_group_ids():
    """Monitored Signal group ids — DB value if present, else config.TARGET_GROUP_IDS."""
    if is_set("signal_target_group_ids"):
        return get_set("signal_target_group_ids")
    return set(config.TARGET_GROUP_IDS)


def save_own_messages_enabled():
    """Whether the bot account's own outgoing messages should be archived. Default True."""
    return get_bool("save_own_messages", True)


def poll_interval():
    return get_int("poll_interval", config.POLL_INTERVAL)


def image_caption_enabled():
    """Whether image attachments get an AI caption. Default config.IMAGE_CAPTION_ENABLED."""
    return get_bool("image_caption_enabled", config.IMAGE_CAPTION_ENABLED)


def video_caption_enabled():
    """Whether video attachments get an AI caption. Default config.VIDEO_CAPTION_ENABLED."""
    return get_bool("video_caption_enabled", config.VIDEO_CAPTION_ENABLED)


# ──────────────────────────────────────────────
# AI / LLM model config (Settings page overlays config.py env defaults)
# ──────────────────────────────────────────────

# Stored model value meaning "operator explicitly disabled this role" — distinct
# from "key never set" (→ fall back to the config.py / .env default).
NONE_SENTINEL = "__none__"

# When a model is flagged "thinking", its hidden reasoning must fit *plus* the
# visible answer or message.content comes back empty (the qwen3-vl / analysis
# regression: 256-token budget fully consumed by reasoning). This per-role floor
# is the real structural guard — applied at read time regardless of what the
# operator typed. Non-thinking roles keep their small env defaults.
_THINKING_NUM_PREDICT_FLOOR = {
    "summary": 16384,
    "analysis": 2048,
    "sentiment": 1024,
    "vision": 2048,
}


def ai_enabled():
    """Global LLM master switch. Default True so existing deployments are
    unaffected; when False every LLM feature degrades cleanly."""
    return get_bool("ai_enabled", True)


def _model_or_none(key, config_default):
    """DB value, else the config.py/.env default. NONE_SENTINEL (or empty) →
    None, meaning the operator disabled this role entirely."""
    v = get(key, None)
    if v is None:
        return config_default or None
    v = str(v).strip()
    if not v or v == NONE_SENTINEL:
        return None
    return v


def _role_num_predict(key, config_default, role, thinking):
    """User/env num_predict, but never below the thinking floor when the model
    is flagged thinking (prevents the empty-content regression)."""
    val = get_int(key, config_default)
    if thinking:
        val = max(val, _THINKING_NUM_PREDICT_FLOOR.get(role, 2048))
    return val


# Summary (group/rollup summaries, intel brief, cross-group)
def summary_model():       return _model_or_none("ollama_summary_model", config.OLLAMA_SUMMARY_MODEL)
def summary_is_thinking(): return get_bool("ollama_summary_thinking", False)
def summary_num_ctx():     return get_int("ollama_summary_num_ctx", config.OLLAMA_SUMMARY_NUM_CTX)
def summary_num_predict(): return _role_num_predict("ollama_summary_num_predict", config.OLLAMA_NUM_PREDICT, "summary", summary_is_thinking())


# Per-URL analysis (poller)
def analysis_model():       return _model_or_none("ollama_analysis_model", config.OLLAMA_ANALYSIS_MODEL)
def analysis_is_thinking(): return get_bool("ollama_analysis_thinking", False)
def analysis_num_ctx():     return get_int("ollama_analysis_num_ctx", config.OLLAMA_ANALYSIS_NUM_CTX)
def analysis_num_predict(): return _role_num_predict("ollama_analysis_num_predict", config.OLLAMA_ANALYSIS_NUM_PREDICT, "analysis", analysis_is_thinking())


# Message sentiment (shares the analysis model's env default, as today)
def sentiment_model():       return _model_or_none("ollama_sentiment_model", config.OLLAMA_ANALYSIS_MODEL)
def sentiment_is_thinking(): return get_bool("ollama_sentiment_thinking", False)
def sentiment_num_ctx():     return get_int("ollama_sentiment_num_ctx", config.OLLAMA_SENTIMENT_NUM_CTX)
def sentiment_num_predict(): return _role_num_predict("ollama_sentiment_num_predict", config.OLLAMA_SENTIMENT_NUM_PREDICT, "sentiment", sentiment_is_thinking())


# Image / video captioning (vision model)
def vision_model():       return _model_or_none("ollama_vision_model", config.OLLAMA_VISION_MODEL)
def vision_is_thinking(): return get_bool("ollama_vision_thinking", False)
def vision_num_ctx():     return get_int("ollama_vision_num_ctx", config.OLLAMA_VISION_NUM_CTX)
def vision_num_predict(): return _role_num_predict("ollama_vision_num_predict", config.OLLAMA_VISION_NUM_PREDICT, "vision", vision_is_thinking())
