-- ─────────────────────────────────────────────────────────────────────────────
-- dedup-messages.sql — remove duplicate rows from `messages` so the unique
-- dedup index `idx_msg_dedup (sender_phone(20), group_id(64), sent_timestamp)`
-- can be created.
--
-- Why this exists: a database that ran for a while *before* `idx_msg_dedup`
-- existed (e.g. a pre-multi-platform install, or one migrated from an external
-- MySQL) can have several rows with the same (sender_phone, group_id,
-- sent_timestamp). On startup the app's schema migration then logs:
--     ADD UNIQUE INDEX idx_msg_dedup ... 1062 Duplicate entry '...'
-- and skips the index. Inserts use `INSERT IGNORE`, so once the index exists no
-- new duplicates can appear — you just need to clean up the old ones once.
--
-- HOW TO RUN (bundled compose; mysql has no published host port):
--   # 1. back up the table first:
--   docker compose exec -T mysql sh -lc \
--     'exec mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE" messages' \
--     > messages-backup-$(date +%F-%H%M).sql
--   # 2. run this script:
--   docker compose exec -T mysql sh -lc \
--     'exec mysql -uroot -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE"' < scripts/dedup-messages.sql
--   # 3. restart so the schema migration creates the index (or run the ALTER below):
--   docker compose up -d
--
-- Requires MySQL 8.0+ (window functions). Restore from the backup with:
--   docker compose exec -T mysql sh -lc \
--     'exec mysql -uroot -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE"' < messages-backup-XXXX.sql
-- ─────────────────────────────────────────────────────────────────────────────

-- 1. How many rows will be removed (informational; safe to run anytime).
SELECT COUNT(*) AS dup_groups, COALESCE(SUM(c) - COUNT(*), 0) AS rows_to_delete
FROM (
    SELECT COUNT(*) AS c
    FROM messages
    GROUP BY LEFT(sender_phone, 20), LEFT(group_id, 64), sent_timestamp
    HAVING COUNT(*) > 1
) t;

-- 2. Delete duplicates, keeping ONE row per (sender_phone(20), group_id(64),
--    sent_timestamp) — the same expression the unique index covers — so exactly
--    one row per partition survives. "Best" survivor: has a screenshot, then has
--    AI analysis, then the lowest id (the original). NULL sent_timestamp is
--    bucketed via COALESCE (PARTITION BY can't use the <=> operator).
DELETE m
FROM messages AS m
JOIN (
    SELECT id
    FROM (
        SELECT
            id,
            ROW_NUMBER() OVER (
                PARTITION BY LEFT(sender_phone, 20), LEFT(group_id, 64),
                             COALESCE(sent_timestamp, '1000-01-01 00:00:00')
                ORDER BY (screenshot IS NOT NULL) DESC,
                         (`ai-analysis` IS NOT NULL AND `ai-analysis` <> '') DESC,
                         id ASC
            ) AS rn
        FROM messages
    ) ranked
    WHERE rn > 1
) AS losers ON losers.id = m.id;

-- 3. Verify: this must return zero rows.
SELECT LEFT(sender_phone, 20) AS sp, LEFT(group_id, 64) AS gid, sent_timestamp, COUNT(*) AS c
FROM messages
GROUP BY sp, gid, sent_timestamp
HAVING c > 1;

-- 4. (optional) Create the index now instead of waiting for the app's next
--    startup schema migration to do it:
-- ALTER TABLE messages
--   ADD UNIQUE INDEX idx_msg_dedup (sender_phone(20), group_id(64), sent_timestamp),
--   ALGORITHM=INPLACE, LOCK=NONE;
