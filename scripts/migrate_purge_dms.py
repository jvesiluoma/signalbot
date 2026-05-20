#!/usr/bin/env python3
"""
Hard-delete pre-existing 1:1 / DM rows from the signalbot database.

Why: the product is a *group monitor*. Pre-fix-up the WhatsApp connector
let 9 direct-message rows leak into `messages` (Tero Hakola's chat-id was
his own `@s.whatsapp.net` JID, not a `@g.us` group). After this fix-up the
ingest gates reject DMs at the door (`app._event_is_group()` and
`connectors/whatsapp/connector.js:272-273`); this script cleans the
historical leakage.

Usage:
    # Print expected delete counts per table; NO destructive action.
    python scripts/migrate_purge_dms.py --dry-run

    # Actually delete (defense-in-depth: requires the operator flag too).
    python scripts/migrate_purge_dms.py --commit --i-understand-this-deletes-data

Behavior:
  - Default is --dry-run; never deletes anything unless --commit is passed.
  - Per-table count logs (before / after / deleted) and a final summary.
  - All non-trivial deletes wrapped in transactions; on any error the script
    rolls back THAT step but continues with the next (so a missing legacy
    table doesn't abort the whole run).
  - `--exclude-chats` lets you whitelist a chat_id that the JID heuristic
    misclassifies as a DM (defensive flag — not expected to be needed for
    the present-day Baileys version, but available).

Order of operations (children → parents → orphan cleanup):
  1. message_attachments      WHERE message_id IN <dm_ids>
  2. message_entities         "
  3. message_mentions         "
  4. message_quotes           "
  5. url_observations         "
  6. watchlist_hits           "
  7. reactions                WHERE platform='whatsapp' AND group_id IS NULL
  8. remote_deletes           same
  9. messages                 the DMs themselves
 10. tracked_urls             WHERE NOT EXISTS in messages.url AND NOT EXISTS in url_observations
 11. identity_links           orphans (no remaining backing messages row)

NEVER deleted: `identities` (operator labels may be attached).
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time

# Repo root → sys.path so we can `import config`.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

import mysql.connector  # noqa: E402
import config  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("migrate_purge_dms")


# The set of (platform, predicate) clauses that uniquely identify DM rows in
# `messages`. Build the IN-list of dm_ids once at the start of the run.
_DM_PREDICATE = (
    "platform='whatsapp' "
    "AND (platform_chat_id IS NULL OR platform_chat_id NOT LIKE '%@g.us')"
)


def _connect():
    return mysql.connector.connect(**config.DB_CONFIG)


def _exists(cur, table):
    """Return True if a table exists in the current database."""
    cur.execute(
        "SELECT 1 FROM information_schema.TABLES "
        "WHERE table_schema=DATABASE() AND table_name=%s LIMIT 1",
        (table,),
    )
    return cur.fetchone() is not None


def _count(cur, sql, params=()):
    cur.execute(sql, params)
    row = cur.fetchone()
    if not row:
        return 0
    return int(row[0] or 0)


def _summary_query(cur, exclude_chats):
    """SELECT one row per (platform, chat-id suffix). Useful pre-commit sanity
    check so the operator can confirm "those 9 WhatsApp rows" before --commit."""
    cur.execute(
        f"""
        SELECT platform,
               COALESCE(RIGHT(platform_chat_id, 8), '<null>') AS suffix,
               COUNT(*) AS c,
               GROUP_CONCAT(DISTINCT COALESCE(group_name,'<null>') SEPARATOR ' | ') AS group_names
          FROM messages
         WHERE {_DM_PREDICATE}
        GROUP BY platform, suffix
        ORDER BY c DESC
        """
    )
    rows = cur.fetchall()
    if not rows:
        log.info("Pre-commit summary: NO DM-shaped messages found.")
        return
    log.info("Pre-commit summary of DM-shaped messages:")
    for platform, suffix, c, group_names in rows:
        marker = " (excluded via --exclude-chats)" if (group_names and group_names in exclude_chats) else ""
        log.info("  %s  chat_id…%s  count=%d  groups=%s%s",
                 platform, suffix, c, (group_names or '<null>')[:80], marker)


def _collect_dm_ids(cur, exclude_chats):
    """Return the list of `messages.id` to purge. Filters `exclude_chats`."""
    sql = f"SELECT id FROM messages WHERE {_DM_PREDICATE}"
    params: list = []
    if exclude_chats:
        placeholders = ",".join(["%s"] * len(exclude_chats))
        sql += f" AND COALESCE(platform_chat_id,'') NOT IN ({placeholders})"
        params.extend(exclude_chats)
    cur.execute(sql, tuple(params))
    return [r[0] for r in cur.fetchall()]


def _delete_with_ids(cur, table, id_col, dm_ids, dry_run):
    """DELETE FROM <table> WHERE <id_col> IN (<dm_ids>)."""
    if not dm_ids:
        return 0
    if not _exists(cur, table):
        log.info("  %s: table absent — skipping", table)
        return 0
    # Batched: MySQL has a 1MB query length default; chunks of 1000 ids fit.
    deleted = 0
    BATCH = 1000
    for i in range(0, len(dm_ids), BATCH):
        chunk = dm_ids[i:i + BATCH]
        placeholders = ",".join(["%s"] * len(chunk))
        before = _count(cur, f"SELECT COUNT(*) FROM {table} WHERE {id_col} IN ({placeholders})", tuple(chunk))
        if dry_run:
            deleted += before
            continue
        cur.execute(f"DELETE FROM {table} WHERE {id_col} IN ({placeholders})", tuple(chunk))
        deleted += cur.rowcount or 0
    return deleted


def _delete_with_predicate(cur, table, predicate, params, dry_run):
    if not _exists(cur, table):
        log.info("  %s: table absent — skipping", table)
        return 0
    before = _count(cur, f"SELECT COUNT(*) FROM {table} WHERE {predicate}", params)
    if dry_run:
        return before
    cur.execute(f"DELETE FROM {table} WHERE {predicate}", params)
    return cur.rowcount or 0


def _orphan_identity_links(cur, dry_run):
    """Drop `identity_links` rows whose (platform, platform_user_id) has no
    surviving row in `messages.account_key`. Never deletes `identities` —
    operator-assigned labels survive."""
    if not _exists(cur, "identity_links"):
        return 0
    cur.execute(
        """
        SELECT il.id
          FROM identity_links il
         WHERE NOT EXISTS (
            SELECT 1 FROM messages m
             WHERE m.platform = il.platform
               AND m.account_key = il.platform_user_id
         )
        """
    )
    ids = [r[0] for r in cur.fetchall()]
    if not ids:
        return 0
    if dry_run:
        return len(ids)
    placeholders = ",".join(["%s"] * len(ids))
    cur.execute(f"DELETE FROM identity_links WHERE id IN ({placeholders})", tuple(ids))
    return cur.rowcount or 0


def _orphan_tracked_urls(cur, dry_run):
    """Drop tracked_urls rows whose URL has no surviving reference anywhere."""
    if not _exists(cur, "tracked_urls"):
        return 0
    # The schema has tracked_urls.url plus optional INDEX on url(255). We require
    # the URL to be referenced by neither messages.url nor url_observations.normalized_url.
    cur.execute(
        """
        SELECT tu.id, tu.url FROM tracked_urls tu
         WHERE NOT EXISTS (SELECT 1 FROM messages m
                            WHERE m.url IS NOT NULL AND m.url LIKE CONCAT('%', tu.url, '%'))
           AND NOT EXISTS (SELECT 1 FROM url_observations uo
                            WHERE uo.normalized_url = tu.url)
        """
    )
    rows = cur.fetchall()
    if not rows:
        return 0
    if dry_run:
        return len(rows)
    ids = [r[0] for r in rows]
    placeholders = ",".join(["%s"] * len(ids))
    cur.execute(f"DELETE FROM tracked_urls WHERE id IN ({placeholders})", tuple(ids))
    return cur.rowcount or 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", default=True,
                    help="(default) print expected delete counts; no destructive action.")
    ap.add_argument("--commit", action="store_true",
                    help="Actually delete. Requires --i-understand-this-deletes-data.")
    ap.add_argument("--i-understand-this-deletes-data", action="store_true",
                    help="Second explicit confirmation flag for --commit.")
    ap.add_argument("--exclude-chats", default="",
                    help="Comma-separated platform_chat_id values to exclude from "
                         "the DM predicate (defense against false positives).")
    args = ap.parse_args()

    if args.commit and not args.i_understand_this_deletes_data:
        ap.error("--commit requires --i-understand-this-deletes-data (defense-in-depth).")

    dry_run = not args.commit
    exclude_chats = [c.strip() for c in args.exclude_chats.split(",") if c.strip()]

    log.warning("Mode: %s%s", "DRY-RUN (no writes)" if dry_run else "COMMIT (deleting)",
                f", excluding chats={exclude_chats!r}" if exclude_chats else "")

    conn = _connect()
    conn.autocommit = False
    cur = conn.cursor()

    _summary_query(cur, exclude_chats)

    dm_ids = _collect_dm_ids(cur, exclude_chats)
    log.warning("DM messages.id count to purge: %d", len(dm_ids))
    if not dm_ids:
        log.info("No DM rows match the predicate. Nothing to do.")
        cur.close()
        conn.close()
        return 0

    totals = {}

    # Each step in its own transaction.
    steps_with_ids = [
        ("message_attachments", "message_id"),
        ("message_entities",    "message_id"),
        ("message_mentions",    "message_id"),
        ("message_quotes",      "message_id"),
        ("url_observations",    "message_id"),
        ("watchlist_hits",      "message_id"),
    ]
    for table, id_col in steps_with_ids:
        try:
            t0 = time.monotonic()
            n = _delete_with_ids(cur, table, id_col, dm_ids, dry_run)
            if not dry_run:
                conn.commit()
            log.info("%s: %s %d rows (%.2fs)", table,
                     "would delete" if dry_run else "deleted",
                     n, time.monotonic() - t0)
            totals[table] = n
        except Exception:
            log.exception("step %s failed; rolling back this step only", table)
            try: conn.rollback()
            except Exception: pass

    # reactions / remote_deletes: scoped by predicate, not by message_id (some
    # rows don't have a referenced message in `messages`).
    for table, predicate in [
        ("reactions",
         "platform='whatsapp' AND (group_id IS NULL OR group_id='')"),
        ("remote_deletes",
         "platform='whatsapp' AND (group_id IS NULL OR group_id='')"),
    ]:
        try:
            t0 = time.monotonic()
            n = _delete_with_predicate(cur, table, predicate, (), dry_run)
            if not dry_run:
                conn.commit()
            log.info("%s: %s %d rows (%.2fs)", table,
                     "would delete" if dry_run else "deleted",
                     n, time.monotonic() - t0)
            totals[table] = n
        except Exception:
            log.exception("step %s failed; rolling back this step only", table)
            try: conn.rollback()
            except Exception: pass

    # The parent table.
    try:
        t0 = time.monotonic()
        if exclude_chats:
            placeholders = ",".join(["%s"] * len(exclude_chats))
            n = _delete_with_predicate(
                cur, "messages",
                f"{_DM_PREDICATE} AND COALESCE(platform_chat_id,'') NOT IN ({placeholders})",
                tuple(exclude_chats), dry_run,
            )
        else:
            n = _delete_with_predicate(cur, "messages", _DM_PREDICATE, (), dry_run)
        if not dry_run:
            conn.commit()
        log.warning("messages: %s %d rows (%.2fs)",
                    "would delete" if dry_run else "deleted",
                    n, time.monotonic() - t0)
        totals["messages"] = n
    except Exception:
        log.exception("messages delete failed; rolling back this step only")
        try: conn.rollback()
        except Exception: pass

    # Now that messages are gone, the orphan-cleanup steps run.
    try:
        n = _orphan_tracked_urls(cur, dry_run)
        if not dry_run:
            conn.commit()
        log.info("tracked_urls (orphan): %s %d rows",
                 "would delete" if dry_run else "deleted", n)
        totals["tracked_urls"] = n
    except Exception:
        log.exception("tracked_urls orphan cleanup failed; rolling back this step only")
        try: conn.rollback()
        except Exception: pass

    try:
        n = _orphan_identity_links(cur, dry_run)
        if not dry_run:
            conn.commit()
        log.info("identity_links (orphan): %s %d rows (NOTE: identities table untouched — labels preserved)",
                 "would delete" if dry_run else "deleted", n)
        totals["identity_links"] = n
    except Exception:
        log.exception("identity_links orphan cleanup failed; rolling back this step only")
        try: conn.rollback()
        except Exception: pass

    cur.close()
    conn.close()

    log.warning("─" * 50)
    log.warning("SUMMARY (%s):", "dry-run" if dry_run else "committed")
    for k, v in totals.items():
        log.warning("  %-22s %5d rows", k, v)
    if dry_run:
        log.warning("No data was modified. Re-run with --commit "
                    "--i-understand-this-deletes-data to apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
