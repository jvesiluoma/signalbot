"""
Periodic sync of signal-cli's `recipient` table into MySQL.

signal-cli stores its contact registry as a SQLite database inside the
bbernhard/signal-cli-rest-api docker container. The bbernhard REST API does
not expose the `recipient` rows over HTTP, so we copy the SQLite file out
via `docker cp` and mirror the relevant columns into a MySQL table named
`signal_recipients`. The sync is the only mechanism that lets the dashboard
resolve names for UUID-only Signal users (newer accounts that have not
shared a phone number).

The sync is read-only on the signal-cli side: we never write back to the
container. `docker cp` produces a consistent snapshot even while signal-cli
is writing (SQLite WAL is checkpointed by the cp).
"""

from __future__ import annotations

import datetime
import logging
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time

import mysql.connector

import config

logger = logging.getLogger("signal_recipients_sync")


_SELECT_SQL = """
SELECT aci, pni, number, username,
       profile_given_name, profile_family_name,
       given_name, family_name, nick_name, profile_about,
       unregistered_timestamp
  FROM recipient
 WHERE aci IS NOT NULL
"""

_UPSERT_SQL = """
INSERT INTO signal_recipients
    (aci, pni, number, username,
     profile_given_name, profile_family_name,
     given_name, family_name, nick_name, profile_about,
     unregistered_ts, last_synced)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
    pni                 = VALUES(pni),
    number              = VALUES(number),
    username            = VALUES(username),
    profile_given_name  = VALUES(profile_given_name),
    profile_family_name = VALUES(profile_family_name),
    given_name          = VALUES(given_name),
    family_name         = VALUES(family_name),
    nick_name           = VALUES(nick_name),
    profile_about       = VALUES(profile_about),
    unregistered_ts     = VALUES(unregistered_ts),
    last_synced         = VALUES(last_synced)
"""


def _copy_db_snapshot(container: str, src_in_container: str, dst_on_host: str) -> None:
    """Run `docker cp <container>:<src> <dst>`. Raises CalledProcessError on failure."""
    subprocess.run(
        ["docker", "cp", f"{container}:{src_in_container}", dst_on_host],
        check=True,
        timeout=30,
        capture_output=True,
    )


def _copy_local_snapshot(src_path: str, dst_path: str) -> None:
    """Copy account.db (plus its -wal/-shm siblings, if any) from a local path.

    Used when SIGNAL_CLI_DB_LOCAL_PATH is set — e.g. the app runs in a container
    that mounts signal-cli's data dir read-only and therefore can't `docker cp`.
    Copying the WAL/SHM siblings alongside the main file lets SQLite replay any
    not-yet-checkpointed pages when the snapshot is opened.
    """
    if not os.path.isfile(src_path):
        raise FileNotFoundError(f"SIGNAL_CLI_DB_LOCAL_PATH does not point at a file: {src_path}")
    shutil.copy2(src_path, dst_path)
    for suffix in ("-wal", "-shm"):
        sib = src_path + suffix
        if os.path.isfile(sib):
            try:
                shutil.copy2(sib, dst_path + suffix)
            except OSError:
                pass


def sync_once() -> int:
    """Pull a fresh snapshot of signal-cli's recipient table into MySQL.

    Returns the number of rows upserted (which is also the number of recipients
    in the snapshot, since UPSERT touches every row)."""
    local_path = config.SIGNAL_CLI_DB_LOCAL_PATH

    workdir = tempfile.mkdtemp(prefix="sigcli-")
    try:
        snapshot_path = os.path.join(workdir, "account.db")
        if local_path:
            _copy_local_snapshot(local_path, snapshot_path)
        else:
            _copy_db_snapshot(config.SIGNAL_CLI_CONTAINER, config.SIGNAL_CLI_DB_PATH, snapshot_path)
        sconn = sqlite3.connect(f"file:{snapshot_path}?mode=ro", uri=True)
        try:
            rows = sconn.execute(_SELECT_SQL).fetchall()
        finally:
            sconn.close()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    now = datetime.datetime.now()
    payload = [(*row, now) for row in rows]

    mconn = mysql.connector.connect(**config.DB_CONFIG)
    cur = mconn.cursor()
    try:
        cur.executemany(_UPSERT_SQL, payload)
        mconn.commit()
        n = len(payload)
        logger.info("signal_recipients sync: upserted %d rows", n)
        return n
    finally:
        try:
            cur.close()
        except Exception:
            pass
        mconn.close()


def run_loop(interval_seconds: int) -> None:
    """Long-running worker: sync now, then every `interval_seconds`."""
    logger.info("recipient sync worker started (interval=%ds)", interval_seconds)
    warned_unavailable = False
    while True:
        try:
            sync_once()
        except FileNotFoundError as exc:
            # Either `docker` isn't on PATH (docker-cp mode in a socket-less
            # container) or SIGNAL_CLI_DB_LOCAL_PATH points at a missing file.
            # Log once with the fix, then stay quiet until it recovers.
            if not warned_unavailable:
                logger.warning(
                    "recipient sync unavailable: %s. Fix: set SIGNAL_CLI_DB_LOCAL_PATH to "
                    "account.db inside the mounted signal-cli data dir (see .env.example), "
                    "or set SIGNAL_RECIPIENTS_SYNC_ENABLED=0 if you don't need name resolution.",
                    exc,
                )
                warned_unavailable = True
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or b"").decode("utf-8", errors="replace")[:300]
            logger.warning("docker cp failed (rc=%s): %s", exc.returncode, stderr.strip())
        except subprocess.TimeoutExpired:
            logger.warning("docker cp timed out after 30s")
        except sqlite3.DatabaseError:
            logger.exception("snapshot SQLite read failed")
        except mysql.connector.Error:
            logger.exception("MySQL upsert failed")
        except Exception:
            logger.exception("recipient sync error")
        else:
            warned_unavailable = False  # recovered — allow the warning again next time
        time.sleep(max(60, interval_seconds))


if __name__ == "__main__":
    # Allow `python3 signal_recipients_sync.py` for one-shot manual runs.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    n = sync_once()
    print(f"upserted {n} rows")
