"""
Unified configuration for signalbot.
All settings are configurable via environment variables with sensible defaults.
"""

import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ──────────────────────────────────────────────
# Database
# ──────────────────────────────────────────────
DB_CONFIG = {
    'host': os.getenv('DB_HOST', 'localhost'),
    'user': os.getenv('DB_USER', 'signalbot'),
    'password': os.getenv('DB_PASSWORD', ''),
    'database': os.getenv('DB_NAME', 'messages_db'),
}

# ──────────────────────────────────────────────
# Signal REST API
# ──────────────────────────────────────────────
SIGNAL_API_BASE = os.getenv('SIGNAL_API_BASE', 'http://localhost:8080')
SIGNAL_PHONE_NUMBER = os.getenv('SIGNAL_PHONE_NUMBER', '')

TARGET_GROUP_IDS = set(
    filter(None, os.getenv('TARGET_GROUP_IDS', '').split(','))
)

# ──────────────────────────────────────────────
# Device Activity Tracker (opt-in, surveillance feature)
#
# Measures target-device state (active / standby / offline) by sending a
# reaction to a message the target authored, immediately removing it, and
# timing the delivery receipt that comes back. Off by default.
# ──────────────────────────────────────────────
ACTIVITY_TRACKER_ENABLED   = os.getenv('ACTIVITY_TRACKER_ENABLED', '0') == '1'
ACTIVITY_PROBE_INTERVAL    = int(os.getenv('ACTIVITY_PROBE_INTERVAL', '180'))   # seconds per target
ACTIVITY_PROBE_JITTER      = int(os.getenv('ACTIVITY_PROBE_JITTER', '30'))      # ± seconds
ACTIVITY_ACK_TIMEOUT       = int(os.getenv('ACTIVITY_ACK_TIMEOUT', '20'))       # seconds pending→timeout
ACTIVITY_PROBE_EMOJI       = os.getenv('ACTIVITY_PROBE_EMOJI', '🫥')             # less conspicuous than 👀
ACTIVITY_PROBE_SELF_REMOVE = os.getenv('ACTIVITY_PROBE_SELF_REMOVE', '1') == '1'
ACTIVITY_MAX_ENROLLED      = int(os.getenv('ACTIVITY_MAX_ENROLLED', '10'))
ACTIVITY_PROBE_ERROR_BACKOFF    = int(os.getenv('ACTIVITY_PROBE_ERROR_BACKOFF', '3600'))   # s
ACTIVITY_PROBE_ERROR_THRESHOLD  = int(os.getenv('ACTIVITY_PROBE_ERROR_THRESHOLD', '5'))    # consecutive errors
ACTIVITY_SAMPLE_RETENTION_DAYS  = int(os.getenv('ACTIVITY_SAMPLE_RETENTION_DAYS', '90'))
ACTIVITY_PROBE_TARGET_MAX_AGE_DAYS = int(os.getenv('ACTIVITY_PROBE_TARGET_MAX_AGE_DAYS', '30'))

# ──────────────────────────────────────────────
# Telegram connector (Bot API only — read-only, low-profile)
# ──────────────────────────────────────────────
TELEGRAM_ENABLED            = os.getenv('TELEGRAM_ENABLED', '0') == '1'
TG_CONNECTOR_BASE           = os.getenv('TG_CONNECTOR_BASE', 'http://tg-connector:8081')
TG_CONNECTOR_TOKEN          = os.getenv('TG_CONNECTOR_TOKEN', '')
TG_BOT_TOKEN                = os.getenv('TG_BOT_TOKEN', '')
TG_BOT_API_BASE             = os.getenv('TG_BOT_API_BASE', 'https://api.telegram.org')
TG_TARGET_CHAT_IDS          = set(filter(None, os.getenv('TG_TARGET_CHAT_IDS', '').split(',')))
TG_POLL_INTERVAL            = int(os.getenv('TG_POLL_INTERVAL', '5'))
# Bot API has no delivery/read receipts or presence for other users, so the
# RTT/presence activity probe is a no-op on Telegram; this only enables outbound
# actions (send/react/delete). Off by default — stealth is the priority.
TG_ACTIVITY_TRACKER_ENABLED = os.getenv('TG_ACTIVITY_TRACKER_ENABLED', '0') == '1'

# ──────────────────────────────────────────────
# WhatsApp connector (custom Baileys service)
# ──────────────────────────────────────────────
WHATSAPP_ENABLED            = os.getenv('WHATSAPP_ENABLED', '0') == '1'
WA_CONNECTOR_BASE           = os.getenv('WA_CONNECTOR_BASE', 'http://wa-connector:8082')
WA_API_KEY                  = os.getenv('WA_API_KEY', '')
WA_TARGET_CHAT_IDS          = set(filter(None, os.getenv('WA_TARGET_CHAT_IDS', '').split(',')))
WA_POLL_INTERVAL            = int(os.getenv('WA_POLL_INTERVAL', '5'))
WA_ACTIVITY_TRACKER_ENABLED = os.getenv('WA_ACTIVITY_TRACKER_ENABLED', '0') == '1'

# ──────────────────────────────────────────────
# Connector ingest webhook
# ──────────────────────────────────────────────
# Shared secret connectors present in `Authorization: Bearer …` when POSTing
# events to /ingest/<platform>. If empty, the /ingest endpoint is disabled.
INGEST_WEBHOOK_TOKEN        = os.getenv('INGEST_WEBHOOK_TOKEN', '')

# ──────────────────────────────────────────────
# Cross-platform identity engine
# ──────────────────────────────────────────────
IDENTITY_LINK_INTERVAL              = int(os.getenv('IDENTITY_LINK_INTERVAL', '1800'))
IDENTITY_AUTOCONFIRM_THRESHOLD      = float(os.getenv('IDENTITY_AUTOCONFIRM_THRESHOLD', '0.97'))
IDENTITY_URL_COOCCURRENCE_WINDOW_S  = int(os.getenv('IDENTITY_URL_COOCCURRENCE_WINDOW_S', '600'))

# ──────────────────────────────────────────────
# Ollama LLM
# ──────────────────────────────────────────────
OLLAMA_API_URL = os.getenv('OLLAMA_API_URL', 'http://localhost:11434/api/generate')

# Dashboard uses a larger model for group summaries
OLLAMA_SUMMARY_MODEL = os.getenv('OLLAMA_SUMMARY_MODEL', 'qwen3:4b-instruct-2507-q8_0')

# Poller uses a smaller/faster model for per-URL AI analysis
OLLAMA_ANALYSIS_MODEL = os.getenv('OLLAMA_ANALYSIS_MODEL', 'llama3.2:3b-instruct-q4_1')

OLLAMA_MAX_CONCURRENCY = int(os.getenv('OLLAMA_MAX_CONCURRENCY', '1'))
OLLAMA_CONNECT_TIMEOUT = float(os.getenv('OLLAMA_CONNECT_TIMEOUT', '10'))
OLLAMA_READ_TIMEOUT = float(os.getenv('OLLAMA_READ_TIMEOUT', '180'))
# 16384 (≈8× the typical group summary) is the new default — qwen3:4b is
# nominally a non-thinking instruct, but ships with the reasoning template
# enabled by default, so 4096 was being burned on thinking tokens before any
# content was emitted (logged as "OLLAMA content empty but thinking present").
# Combined with `think:false` in OllamaClient.default_options, this gives a
# 2× safety margin even when thinking sneaks back on.
OLLAMA_NUM_PREDICT = int(os.getenv('OLLAMA_NUM_PREDICT', '16384'))
OLLAMA_ANALYSIS_NUM_PREDICT = int(os.getenv('OLLAMA_ANALYSIS_NUM_PREDICT', '256'))
OLLAMA_SENTIMENT_NUM_PREDICT = int(os.getenv('OLLAMA_SENTIMENT_NUM_PREDICT', '10'))
# Context-window sizes per call site. Defaults preserve previous hardcoded values:
# 8192 for group summaries / intel briefs, 4096 for URL analysis, 2048 for sentiment.
OLLAMA_SUMMARY_NUM_CTX = int(os.getenv('OLLAMA_SUMMARY_NUM_CTX', '8192'))
OLLAMA_ANALYSIS_NUM_CTX = int(os.getenv('OLLAMA_ANALYSIS_NUM_CTX', '4096'))
OLLAMA_SENTIMENT_NUM_CTX = int(os.getenv('OLLAMA_SENTIMENT_NUM_CTX', '2048'))
OLLAMA_RETRY_ATTEMPTS = int(os.getenv('OLLAMA_RETRY_ATTEMPTS', '5'))

# ──────────────────────────────────────────────
# Image / video captioning (vision model)
# ──────────────────────────────────────────────
# A short one-sentence caption is generated per image/video attachment by a
# vision-language model (the deployment already runs qwen3-vl:8b). These are
# defaults; the IMAGE_/VIDEO_CAPTION_ENABLED toggles can be overridden live from
# the /settings page (see settings.py).
OLLAMA_VISION_MODEL = os.getenv('OLLAMA_VISION_MODEL', 'qwen3-vl:8b')
# Must NOT be tiny: qwen3-vl is a thinking model and ignores think:false, so it
# spends a chunk of the budget on hidden reasoning before emitting the caption
# (at 64 it returned done_reason=length with empty content; ~2048 yields the
# sentence with comfortable headroom). The caption itself is still ~1 sentence.
OLLAMA_VISION_NUM_PREDICT = int(os.getenv('OLLAMA_VISION_NUM_PREDICT', '2048'))
OLLAMA_VISION_NUM_CTX = int(os.getenv('OLLAMA_VISION_NUM_CTX', '8192'))
IMAGE_CAPTION_ENABLED = os.getenv('IMAGE_CAPTION_ENABLED', '1') == '1'
VIDEO_CAPTION_ENABLED = os.getenv('VIDEO_CAPTION_ENABLED', '1') == '1'
# Raw bytes above this are skipped (never captioned). Images are downscaled so
# the long edge is at most CAPTION_MAX_PIXELS_LONG_EDGE px before being sent.
CAPTION_MAX_IMAGE_BYTES = int(os.getenv('CAPTION_MAX_IMAGE_BYTES', '20000000'))
CAPTION_MAX_PIXELS_LONG_EDGE = int(os.getenv('CAPTION_MAX_PIXELS_LONG_EDGE', '1024'))
CAPTION_VIDEO_FRAMES = int(os.getenv('CAPTION_VIDEO_FRAMES', '3'))
CAPTION_VIDEO_MAX_BYTES = int(os.getenv('CAPTION_VIDEO_MAX_BYTES', '100000000'))
# When on, image/video bytes are captured during ingest (while the connector's
# media cache is still warm) and persisted into `attachments` for all platforms.
CAPTION_INGEST_PERSIST = os.getenv('CAPTION_INGEST_PERSIST', '1') == '1'

# ──────────────────────────────────────────────
# Polling intervals
# ──────────────────────────────────────────────
POLL_INTERVAL = int(os.getenv('POLL_INTERVAL', '10'))         # seconds between poll cycles
SUMMARY_INTERVAL = int(os.getenv('SUMMARY_INTERVAL', '3600'))  # seconds between summary refreshes
PAGE_TRACK_INTERVAL = int(os.getenv('PAGE_TRACK_INTERVAL', '3600'))  # seconds between page tracking cycles
PAGE_TRACK_CHANGE_THRESHOLD = float(os.getenv('PAGE_TRACK_CHANGE_THRESHOLD', '0.05'))  # 5% change threshold

# ──────────────────────────────────────────────
# Flask / Web
# ──────────────────────────────────────────────
FLASK_HOST = os.getenv('HOST', '0.0.0.0')
FLASK_PORT = int(os.getenv('PORT', '5581'))
FLASK_DEBUG = os.getenv('FLASK_DEBUG', '0') == '1'

# ──────────────────────────────────────────────
# Authentication
# ──────────────────────────────────────────────
AUTH_SECRET = os.getenv('AUTH_SECRET', '')  # If empty, authentication is disabled

# ──────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────
LOG_LEVEL = os.getenv('LOG_LEVEL', 'DEBUG').upper()
LOG_FORMAT = (
    "%(asctime)s | %(levelname)s | %(process)d:%(threadName)s | %(name)s | "
    "%(funcName)s:%(lineno)d | %(message)s"
)

# ──────────────────────────────────────────────
# Intel page
# ──────────────────────────────────────────────
INTEL_BURST_WINDOW_MINUTES = int(os.getenv('INTEL_BURST_WINDOW_MINUTES', '5'))
INTEL_BURST_MIN_SENDERS = int(os.getenv('INTEL_BURST_MIN_SENDERS', '3'))
INTEL_BEHAVIORAL_INTERVAL = int(os.getenv('INTEL_BEHAVIORAL_INTERVAL', '3600'))
INTEL_NER_ENABLED = os.getenv('INTEL_NER_ENABLED', '0') == '1'
INTEL_BRIEF_AUTO_GENERATE = os.getenv('INTEL_BRIEF_AUTO_GENERATE', '0') == '1'

# Group metadata sync (Phase 2)
GROUP_SYNC_ENABLED = os.getenv('GROUP_SYNC_ENABLED', '1') == '1'
GROUP_SYNC_INTERVAL = int(os.getenv('GROUP_SYNC_INTERVAL', '900'))  # seconds between /v1/groups polls
GROUP_SYNC_TIMEOUT = int(os.getenv('GROUP_SYNC_TIMEOUT', '15'))     # HTTP timeout per group fetch

# Poll-loop hang guards. The poller is single-threaded — a blocking call in any
# stage (screenshot/URL fetch, attachment download) stalls all message polling.
# These caps bound each stage so a wedged Chromium process or a hung HTTP socket
# can never silence the poller indefinitely.
PW_JOB_TIMEOUT = int(os.getenv('PW_JOB_TIMEOUT', '90'))             # hard cap (s) per Playwright browser op
ATTACHMENT_FETCH_TIMEOUT = int(os.getenv('ATTACHMENT_FETCH_TIMEOUT', '30'))  # HTTP timeout per attachment download

# Poller liveness watchdog. The poll loop emits a heartbeat at every bounded
# sub-step (after each /v1/receive, per URL, per Ollama attempt). If that beat
# goes stale the poller is wedged and no Signal messages are being fetched.
# Thresholds must clear the slowest *legitimate* single step — an Ollama attempt
# (OLLAMA_READ_TIMEOUT, default 180s) — so they sit well above it.
POLLER_HUNG_SECONDS    = int(os.getenv('POLLER_HUNG_SECONDS', '300'))    # UI/health flags "hung" past this
WATCHDOG_ENABLED       = os.getenv('WATCHDOG_ENABLED', '1') == '1'
WATCHDOG_INTERVAL      = int(os.getenv('WATCHDOG_INTERVAL', '30'))       # seconds between watchdog checks
WATCHDOG_RECYCLE_SECONDS = int(os.getenv('WATCHDOG_RECYCLE_SECONDS', '300'))   # stale > this → recycle browser
WATCHDOG_RESTART_SECONDS = int(os.getenv('WATCHDOG_RESTART_SECONDS', '600'))   # stale > this → restart process

# ──────────────────────────────────────────────
# signal-cli recipient registry sync
#
# Periodically copies signal-cli's account.db out of the signal-api docker
# container and mirrors the `recipient` table (UUID/phone/profile-name) into
# MySQL. Required to resolve names for UUID-only Signal users (newer accounts
# that don't share a phone number) in the dashboard.
# ──────────────────────────────────────────────
SIGNAL_RECIPIENTS_SYNC_ENABLED  = os.getenv('SIGNAL_RECIPIENTS_SYNC_ENABLED', '1') == '1'
SIGNAL_RECIPIENTS_SYNC_INTERVAL = int(os.getenv('SIGNAL_RECIPIENTS_SYNC_INTERVAL', '3600'))  # 1h
SIGNAL_CLI_CONTAINER            = os.getenv('SIGNAL_CLI_CONTAINER', 'signal-api')
SIGNAL_CLI_DB_PATH              = os.getenv(
    'SIGNAL_CLI_DB_PATH',
    '/home/.local/share/signal-cli/data/+CHANGEME.d/account.db',
)
# Optional: read account.db directly from a local filesystem path (e.g. a
# read-only bind/volume mount of the signal-cli data dir, as set up by the
# bundled docker-compose at /signal-cli-data — see SIGNAL_CLI_DATA_DIR below).
# When non-empty this takes precedence over the `docker cp` path above — and is
# the right choice for the default image, which has no Docker socket.
SIGNAL_CLI_DB_LOCAL_PATH        = os.getenv('SIGNAL_CLI_DB_LOCAL_PATH', '')
# Host path of the signal-cli data dir, bind-mounted read-only into the app at
# /signal-cli-data by docker-compose.yml. Leave unset to use the bundled
# `signal-cli-data` named volume (matches the bundled `signal` profile); set to
# an absolute host path (e.g. /root/.local/share/signal-api) when the signal-api
# container is external. Consumed by docker-compose.yml, not by Python — it's
# surfaced here so all the signal-cli wiring is documented in one place.
SIGNAL_CLI_DATA_DIR             = os.getenv('SIGNAL_CLI_DATA_DIR', '')

# ──────────────────────────────────────────────
# Rate-limiting
# ──────────────────────────────────────────────
# Minimum gap in seconds between /debug/force_refresh (Regenerate button) invocations.
FORCE_REFRESH_COOLDOWN = int(os.getenv('FORCE_REFRESH_COOLDOWN', '1800'))  # 30 minutes

# ──────────────────────────────────────────────
# Rolled-up summaries
# ──────────────────────────────────────────────
# Seconds between monthly/yearly aggregation passes (default 6 hours).
ROLLUP_INTERVAL = int(os.getenv('ROLLUP_INTERVAL', '21600'))
