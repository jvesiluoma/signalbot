"""
Database helpers shared across the dashboard, poller, identity engine, llm_queue.

This is currently a single thin function — kept in its own module so the
forthcoming Flask blueprints can `from app_core.db import get_db_connection`
without pulling in the rest of `app.py`. Future expansion: connection pooling,
read-only replica routing, query-time metrics.
"""

from __future__ import annotations

import logging
import time

import mysql.connector

import config

logger = logging.getLogger("app_core.db")


def get_db_connection():
    """Return a fresh MySQL connection or None on error.

    Logs the round-trip time so connection-spam regressions show up in the logs.
    Caller owns the connection lifecycle (`conn.close()` in finally).
    """
    try:
        t0 = time.monotonic()
        conn = mysql.connector.connect(**config.DB_CONFIG)
        elapsed_ms = (time.monotonic() - t0) * 1000
        logger.info("DB connection established in %.1f ms", elapsed_ms)
        return conn
    except mysql.connector.Error as err:
        logger.exception("Error connecting to database: %s", err)
        return None
