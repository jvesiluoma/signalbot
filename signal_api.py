"""
Thin outbound wrapper over the bbernhard/signal-cli-rest-api daemon.

This module exists only to centralize the Signal REST calls that the
device-activity tracker needs. All the rest of the bot talks to signal-cli-rest-api
inline via requests.get in poller.py — no existing helper to reuse.

Endpoints used:
    POST   /v1/reactions/{bot_number}   — send a reaction to an existing message
    DELETE /v1/reactions/{bot_number}   — remove a reaction previously sent

Both endpoints return HTTP 204 on success with no body, so we can't learn the
Signal-protocol send timestamp from the response. The caller records a local
time just before the POST and matches incoming delivery receipts against the
target UUID + recency window in poller.handle_receipt().

No state; import-safe; never raises at import time.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.parse
from typing import Optional

import requests

import config

logger = logging.getLogger("signal_api")

# ──────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────

# Tuple is (connect_timeout_seconds, read_timeout_seconds).
_DEFAULT_TIMEOUT = (5, 10)
_RETRY_BACKOFF_SECONDS = 1.0
_REACTION_PATH = "/v1/reactions/"


# ──────────────────────────────────────────────
# Exceptions
# ──────────────────────────────────────────────

class SignalAPIError(Exception):
    """Any non-success response or transport failure from signal-cli-rest-api."""

    def __init__(self, message: str, status_code: Optional[int] = None, body: Optional[str] = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class SignalRateLimited(SignalAPIError):
    """HTTP 429. Respect the Retry-After header when present."""

    def __init__(self, message: str, retry_after: Optional[float] = None, body: Optional[str] = None):
        super().__init__(message, status_code=429, body=body)
        self.retry_after = retry_after


class SignalClientError(SignalAPIError):
    """4xx other than 429 — e.g. target not in group, invalid group id, bad emoji."""


class SignalServerError(SignalAPIError):
    """5xx from the daemon or signal-cli itself."""


# ──────────────────────────────────────────────
# Public helpers
# ──────────────────────────────────────────────

def send_reaction(
    recipient: str,
    reaction: str,
    target_author: str,
    target_timestamp: int,
    bot_number: Optional[str] = None,
) -> None:
    """Send `reaction` to the message identified by (target_author, target_timestamp).

    `recipient` is either a phone number (DM) or `group.<base64id>` (group).
    `target_timestamp` is the Signal message timestamp of the message being
    reacted to (milliseconds since epoch).

    Raises SignalClientError / SignalServerError / SignalRateLimited on failure.
    Returns None on success (the API returns 204 with no body).
    """
    body = {
        "recipient": recipient,
        "reaction": reaction,
        "target_author": target_author,
        "timestamp": int(target_timestamp),
    }
    _call("POST", _reaction_url(bot_number), body)


def remove_reaction(
    recipient: str,
    target_author: str,
    target_timestamp: int,
    reaction: str = "",
    bot_number: Optional[str] = None,
) -> None:
    """Remove a reaction previously sent by the bot to (target_author, target_timestamp).

    `reaction` is optional; the API accepts an empty string and removes any
    reaction the bot sent to that target message.
    """
    body = {
        "recipient": recipient,
        "reaction": reaction or "",
        "target_author": target_author,
        "timestamp": int(target_timestamp),
    }
    _call("DELETE", _reaction_url(bot_number), body)


# ──────────────────────────────────────────────
# Internals
# ──────────────────────────────────────────────

def _reaction_url(bot_number: Optional[str]) -> str:
    number = bot_number or config.SIGNAL_PHONE_NUMBER
    if not number:
        raise SignalAPIError("SIGNAL_PHONE_NUMBER not configured")
    return (
        config.SIGNAL_API_BASE.rstrip("/")
        + _REACTION_PATH
        + urllib.parse.quote(number, safe="")
    )


def _call(method: str, url: str, body: dict) -> None:
    """Issue the request with one retry on 429/5xx."""
    payload = json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}

    last_exc: Optional[Exception] = None
    for attempt in (1, 2):
        try:
            resp = requests.request(
                method,
                url,
                data=payload,
                headers=headers,
                timeout=_DEFAULT_TIMEOUT,
            )
        except requests.RequestException as exc:
            last_exc = exc
            logger.warning("signal_api %s transport error (attempt %d): %s",
                           method, attempt, exc)
            if attempt == 1:
                time.sleep(_RETRY_BACKOFF_SECONDS)
                continue
            raise SignalAPIError(f"transport error: {exc}") from exc

        if 200 <= resp.status_code < 300:
            logger.debug("signal_api %s %s -> %d", method, url, resp.status_code)
            return

        body_snip = (resp.text or "")[:500]

        if resp.status_code == 429:
            retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
            if attempt == 1:
                delay = retry_after if retry_after is not None else _RETRY_BACKOFF_SECONDS
                logger.warning("signal_api 429 from daemon; sleeping %.2fs then retrying", delay)
                time.sleep(min(delay, 10.0))
                continue
            raise SignalRateLimited(
                f"rate limited: {body_snip}", retry_after=retry_after, body=body_snip,
            )

        if 500 <= resp.status_code < 600:
            if attempt == 1:
                time.sleep(_RETRY_BACKOFF_SECONDS)
                continue
            raise SignalServerError(
                f"daemon 5xx: {resp.status_code} body={body_snip}",
                status_code=resp.status_code, body=body_snip,
            )

        # 4xx other than 429 — don't retry, permanent.
        raise SignalClientError(
            f"daemon 4xx: {resp.status_code} body={body_snip}",
            status_code=resp.status_code, body=body_snip,
        )

    # Loop exhausted with no explicit return/raise — defensive.
    raise SignalAPIError(f"unexpected retry exhaustion: {last_exc}")


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
