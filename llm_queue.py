"""
MySQL-backed LLM task queue with background worker thread.

All LLM calls are enqueued as tasks and processed asynchronously by a single
worker thread. Results are persisted to the database so they survive restarts.
"""

import json
import logging
import re
import threading
import time
from datetime import datetime, timedelta

import mysql.connector

import config

logger = logging.getLogger(__name__)

# Patterns that indicate `summarize_fn` produced an error-stub instead of a real
# summary (typically: Ollama returned `content=''` because the thinking model
# burned the entire `num_predict` budget on `thinking`). Detected so we don't
# overwrite a previously-good `daily_summaries` row with the stub and instead
# raise to trigger `_mark_error`'s retry-up-to-max_attempts machinery.
_STUB_PATTERNS = re.compile(
    r"Error generating summary|No response content from LLM|Invalid JSON from LLM",
    re.IGNORECASE,
)

# A subset of stub failures that are *deterministic* for the current model +
# input: re-running the identical request will fail identically, so retrying
# (default 3×, each a multi-minute Ollama call) only starves the caption and
# sentiment workers behind it. These are parked as 'error' on the first hit.
# Transient stubs (HTTP 5xx, timeouts, malformed JSON) keep the retry path.
_TERMINAL_STUB_PATTERNS = re.compile(
    r"No response content from LLM",
    re.IGNORECASE,
)


class TerminalTaskError(RuntimeError):
    """A task failure that must NOT be retried (deterministic for this input).

    `_process_task` routes this straight to a terminal `_mark_error`, bypassing
    the attempt-counter requeue so the worker moves on to lower-priority work
    (captions/sentiment) instead of re-burning GPU time on a guaranteed failure.
    """

_LLM_TASKS_DDL = """
CREATE TABLE IF NOT EXISTS llm_tasks (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    task_type     VARCHAR(50)  NOT NULL,
    task_key      VARCHAR(255) NOT NULL,
    status        ENUM('pending','running','done','error') NOT NULL DEFAULT 'pending',
    priority      INT NOT NULL DEFAULT 5,
    input_data    LONGTEXT DEFAULT NULL,
    result        LONGTEXT DEFAULT NULL,
    error_msg     TEXT DEFAULT NULL,
    attempts      INT NOT NULL DEFAULT 0,
    max_attempts  INT NOT NULL DEFAULT 3,
    created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at    DATETIME DEFAULT NULL,
    completed_at  DATETIME DEFAULT NULL,
    expires_at    DATETIME DEFAULT NULL,
    INDEX idx_status_priority (status, priority, created_at),
    INDEX idx_type_key (task_type, task_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


def _get_conn():
    """Get a fresh DB connection."""
    return mysql.connector.connect(**config.DB_CONFIG)


class LLMTaskQueue:
    """MySQL-backed LLM task queue with a single background worker thread."""

    def __init__(self, ollama_sem, shutdown_event, summarize_fn):
        self.ollama_sem = ollama_sem
        self.shutdown_event = shutdown_event
        self.summarize_fn = summarize_fn  # summarize_messages_for_group(group_name, text)
        self.sentiment_fn = None  # set by app.py after init
        self.cross_group_fn = None  # set by app.py after init
        self.monthly_summarize_fn = None  # set by app.py after init; (group, month_start, daily_text) -> str
        self.yearly_summarize_fn = None   # set by app.py after init; (group, year_start, monthly_text) -> str
        self._wake = threading.Event()
        self._worker = None

    # ── Table setup ──────────────────────────────

    def ensure_table(self, conn):
        """Create llm_tasks table if it doesn't exist."""
        cursor = conn.cursor()
        try:
            cursor.execute(_LLM_TASKS_DDL)
            conn.commit()
            logger.info("llm_tasks table ensured")
        except Exception:
            logger.exception("Failed to create llm_tasks table")
        finally:
            cursor.close()

    # ── Enqueue ──────────────────────────────────

    def enqueue_summary(self, group_name, messages_text, priority=5, ttl_seconds=3600):
        """
        Enqueue a summary task. Returns task_id (new or existing).
        Deduplicates: won't create a new task if one is already pending/running
        for the same group.
        """
        conn = _get_conn()
        cursor = conn.cursor(dictionary=True)
        try:
            # Check for existing active task
            cursor.execute(
                "SELECT id FROM llm_tasks "
                "WHERE task_type='summary' AND task_key=%s AND status IN ('pending','running') "
                "LIMIT 1",
                (group_name,)
            )
            row = cursor.fetchone()
            if row:
                logger.debug("Summary task already active for %r (id=%d)", group_name, row['id'])
                return row['id']

            expires = datetime.now() + timedelta(seconds=ttl_seconds)
            cursor.execute(
                "INSERT INTO llm_tasks "
                "(task_type, task_key, status, priority, input_data, expires_at) "
                "VALUES ('summary', %s, 'pending', %s, %s, %s)",
                (group_name, priority, json.dumps({"messages_text": messages_text}), expires)
            )
            conn.commit()
            task_id = cursor.lastrowid
            logger.info("Enqueued summary task id=%d for group=%r priority=%d", task_id, group_name, priority)
            self._wake.set()
            return task_id
        except Exception:
            logger.exception("Failed to enqueue summary for %r", group_name)
            conn.rollback()
            return None
        finally:
            cursor.close()
            conn.close()

    def enqueue_sentiment(self, message_id, message_text, priority=8):
        """Enqueue a sentiment analysis task. Returns task_id."""
        task_key = str(message_id)
        conn = _get_conn()
        cursor = conn.cursor(dictionary=True)
        try:
            cursor.execute(
                "SELECT id FROM llm_tasks "
                "WHERE task_type='sentiment' AND task_key=%s AND status IN ('pending','running') "
                "LIMIT 1",
                (task_key,)
            )
            if cursor.fetchone():
                return None
            cursor.execute(
                "INSERT INTO llm_tasks "
                "(task_type, task_key, status, priority, input_data, max_attempts) "
                "VALUES ('sentiment', %s, 'pending', %s, %s, 1)",
                (task_key, priority, json.dumps({"message_text": message_text}))
            )
            conn.commit()
            self._wake.set()
            return cursor.lastrowid
        except Exception:
            logger.exception("Failed to enqueue sentiment for msg %s", message_id)
            conn.rollback()
            return None
        finally:
            cursor.close()
            conn.close()

    def enqueue_caption(self, md5sum, media_type, priority=9):
        """Enqueue an image/video caption task, keyed by attachment md5.

        Keying on md5 means a forwarded/identical attachment is captioned once
        and the result fans out to every message_attachments row sharing it.
        Priority 9 (lowest) so captioning never preempts summaries (3-8).
        """
        if not md5sum:
            return None
        conn = _get_conn()
        cursor = conn.cursor(dictionary=True)
        try:
            cursor.execute(
                "SELECT id FROM llm_tasks "
                "WHERE task_type='caption' AND task_key=%s AND status IN ('pending','running') "
                "LIMIT 1",
                (md5sum,)
            )
            if cursor.fetchone():
                return None
            cursor.execute(
                "INSERT INTO llm_tasks "
                "(task_type, task_key, status, priority, input_data, max_attempts) "
                "VALUES ('caption', %s, 'pending', %s, %s, 2)",
                (md5sum, priority, json.dumps({"md5": md5sum, "media_type": media_type}))
            )
            conn.commit()
            self._wake.set()
            return cursor.lastrowid
        except Exception:
            logger.exception("Failed to enqueue caption for md5 %s", md5sum)
            conn.rollback()
            return None
        finally:
            cursor.close()
            conn.close()

    def enqueue_monthly_summary(self, group_name, month_start, daily_text, priority=8, ttl_seconds=86400):
        """Enqueue a monthly summary task. task_key = f"{group}|{YYYY-MM-01}".
        Deduplicates: one pending/running task per (group, month)."""
        month_str = month_start.isoformat() if hasattr(month_start, 'isoformat') else str(month_start)
        task_key = f"{group_name}|{month_str}"
        conn = _get_conn()
        cursor = conn.cursor(dictionary=True)
        try:
            cursor.execute(
                "SELECT id FROM llm_tasks "
                "WHERE task_type='monthly_summary' AND task_key=%s AND status IN ('pending','running') "
                "LIMIT 1",
                (task_key,)
            )
            row = cursor.fetchone()
            if row:
                return row['id']
            expires = datetime.now() + timedelta(seconds=ttl_seconds)
            payload = json.dumps({
                "group_name": group_name,
                "month_start": month_str,
                "daily_text": daily_text,
            })
            cursor.execute(
                "INSERT INTO llm_tasks "
                "(task_type, task_key, status, priority, input_data, expires_at, max_attempts) "
                "VALUES ('monthly_summary', %s, 'pending', %s, %s, %s, 2)",
                (task_key, priority, payload, expires)
            )
            conn.commit()
            self._wake.set()
            return cursor.lastrowid
        except Exception:
            logger.exception("Failed to enqueue monthly summary for %r/%s", group_name, month_str)
            conn.rollback()
            return None
        finally:
            cursor.close()
            conn.close()

    def enqueue_yearly_summary(self, group_name, year_start, monthly_text, priority=9, ttl_seconds=86400):
        """Enqueue a yearly summary task. task_key = f"{group}|{YYYY-01-01}"."""
        year_str = year_start.isoformat() if hasattr(year_start, 'isoformat') else str(year_start)
        task_key = f"{group_name}|{year_str}"
        conn = _get_conn()
        cursor = conn.cursor(dictionary=True)
        try:
            cursor.execute(
                "SELECT id FROM llm_tasks "
                "WHERE task_type='yearly_summary' AND task_key=%s AND status IN ('pending','running') "
                "LIMIT 1",
                (task_key,)
            )
            row = cursor.fetchone()
            if row:
                return row['id']
            expires = datetime.now() + timedelta(seconds=ttl_seconds)
            payload = json.dumps({
                "group_name": group_name,
                "year_start": year_str,
                "monthly_text": monthly_text,
            })
            cursor.execute(
                "INSERT INTO llm_tasks "
                "(task_type, task_key, status, priority, input_data, expires_at, max_attempts) "
                "VALUES ('yearly_summary', %s, 'pending', %s, %s, %s, 2)",
                (task_key, priority, payload, expires)
            )
            conn.commit()
            self._wake.set()
            return cursor.lastrowid
        except Exception:
            logger.exception("Failed to enqueue yearly summary for %r/%s", group_name, year_str)
            conn.rollback()
            return None
        finally:
            cursor.close()
            conn.close()

    def enqueue_cross_group(self, summaries_text, priority=3, ttl_seconds=3600):
        """Enqueue a cross-group topic analysis. Returns task_id."""
        conn = _get_conn()
        cursor = conn.cursor(dictionary=True)
        try:
            cursor.execute(
                "SELECT id FROM llm_tasks "
                "WHERE task_type='cross_group' AND status IN ('pending','running') "
                "LIMIT 1"
            )
            if cursor.fetchone():
                return None
            expires = datetime.now() + timedelta(seconds=ttl_seconds)
            cursor.execute(
                "INSERT INTO llm_tasks "
                "(task_type, task_key, status, priority, input_data, expires_at) "
                "VALUES ('cross_group', 'all', 'pending', %s, %s, %s)",
                (priority, json.dumps({"summaries_text": summaries_text}), expires)
            )
            conn.commit()
            self._wake.set()
            return cursor.lastrowid
        except Exception:
            logger.exception("Failed to enqueue cross-group summary")
            conn.rollback()
            return None
        finally:
            cursor.close()
            conn.close()

    # ── Query results ────────────────────────────

    def get_all_summaries(self):
        """
        Get the latest summary for each group. Returns dict:
        {group_name: {"summary": str, "status": str, "completed_at": datetime|None}}

        For each group, returns the most recent task (done > running > pending > error).
        """
        conn = _get_conn()
        cursor = conn.cursor(dictionary=True)
        try:
            # Get the best task per group: prefer done, then running/pending, then error
            cursor.execute("""
                SELECT t.task_key, t.status, t.result, t.completed_at, t.error_msg
                FROM llm_tasks t
                INNER JOIN (
                    SELECT task_key, MAX(id) AS max_id
                    FROM llm_tasks
                    WHERE task_type = 'summary'
                    GROUP BY task_key
                ) latest ON t.task_key = latest.task_key AND t.id = latest.max_id
                WHERE t.task_type = 'summary'
            """)
            rows = cursor.fetchall()
            result = {}
            for row in rows:
                result[row['task_key']] = {
                    "summary": row['result'] or '',
                    "status": row['status'],
                    "completed_at": row['completed_at'],
                    "error_msg": row['error_msg'],
                }
            return result
        except Exception:
            logger.exception("Failed to get summaries")
            return {}
        finally:
            cursor.close()
            conn.close()

    def get_cross_group_summary(self):
        """Get the latest cross-group summary. Returns dict or None."""
        conn = _get_conn()
        cursor = conn.cursor(dictionary=True)
        try:
            cursor.execute(
                "SELECT status, result, completed_at, error_msg FROM llm_tasks "
                "WHERE task_type='cross_group' ORDER BY id DESC LIMIT 1"
            )
            row = cursor.fetchone()
            if row:
                return {
                    'status': row['status'],
                    'result': row['result'] or '',
                    'completed_at': row['completed_at'],
                    'error_msg': row['error_msg'],
                }
            return None
        except Exception:
            logger.exception("Failed to get cross-group summary")
            return None
        finally:
            cursor.close()
            conn.close()

    def get_pending_count(self):
        """Count pending/running summary tasks."""
        conn = _get_conn()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT COUNT(*) FROM llm_tasks "
                "WHERE task_type='summary' AND status IN ('pending','running')"
            )
            return cursor.fetchone()[0]
        except Exception:
            return 0
        finally:
            cursor.close()
            conn.close()

    # ── Worker thread ────────────────────────────

    def start_worker(self):
        """Start the background LLM worker thread."""
        self._worker = threading.Thread(target=self._worker_loop, daemon=True, name="llm-worker")
        self._worker.start()
        logger.info("LLM worker thread started")

    def _worker_loop(self):
        """Main loop: claim tasks, process them, repeat."""
        while not self.shutdown_event.is_set():
            try:
                self._expire_stale_running()
                task = self._claim_next_task()
                if task:
                    self._process_task(task)
                else:
                    self._wake.wait(timeout=30)
                    self._wake.clear()
            except Exception:
                logger.exception("Unexpected error in LLM worker loop")
                time.sleep(5)

    def _claim_next_task(self):
        """Atomically claim the next pending task."""
        conn = _get_conn()
        cursor = conn.cursor(dictionary=True)
        try:
            conn.start_transaction()
            cursor.execute(
                "SELECT * FROM llm_tasks "
                "WHERE status='pending' "
                "ORDER BY priority ASC, created_at ASC "
                "LIMIT 1 "
                "FOR UPDATE"
            )
            task = cursor.fetchone()
            if not task:
                conn.commit()
                return None

            cursor.execute(
                "UPDATE llm_tasks SET status='running', started_at=NOW(), attempts=attempts+1 "
                "WHERE id=%s",
                (task['id'],)
            )
            conn.commit()
            logger.info("Claimed task id=%d type=%s key=%r", task['id'], task['task_type'], task['task_key'])
            return task
        except Exception:
            logger.exception("Failed to claim task")
            conn.rollback()
            return None
        finally:
            cursor.close()
            conn.close()

    def _process_task(self, task):
        """Dispatch task to the right handler."""
        task_id = task['id']
        try:
            if task['task_type'] == 'summary':
                result = self._process_summary(task)
                self._mark_done(task_id, result)
            elif task['task_type'] == 'sentiment':
                result = self._process_sentiment(task)
                self._mark_done(task_id, result)
            elif task['task_type'] == 'caption':
                result = self._process_caption(task)
                self._mark_done(task_id, result)
            elif task['task_type'] == 'cross_group':
                result = self._process_cross_group(task)
                self._mark_done(task_id, result)
            elif task['task_type'] == 'monthly_summary':
                result = self._process_monthly_summary(task)
                self._mark_done(task_id, result)
            elif task['task_type'] == 'yearly_summary':
                result = self._process_yearly_summary(task)
                self._mark_done(task_id, result)
            else:
                self._mark_error(task_id, f"Unknown task type: {task['task_type']}")
        except TerminalTaskError as e:
            logger.error("Task id=%d permanently failed (no retry): %s", task_id, e)
            self._mark_error(task_id, str(e), terminal=True)
        except Exception as e:
            logger.exception("Task id=%d failed", task_id)
            self._mark_error(task_id, str(e))

    def _process_summary(self, task):
        """Generate a group summary using the existing summarization pipeline.
        Note: ollama_sem is acquired inside chat_json(), not here.
        On success also persists to `daily_summaries` (UPSERT on date+group)."""
        input_data = json.loads(task['input_data']) if task['input_data'] else {}
        group_name = task['task_key']
        messages_text = input_data.get('messages_text', '')

        if not messages_text:
            return ''

        import settings as _settings
        if not _settings.ai_enabled() or _settings.summary_model() is None:
            logger.info("Summary skipped (LLM disabled) for group=%r", group_name)
            return ''

        logger.info("Generating summary for group=%r (%d chars)", group_name, len(messages_text))
        result = self.summarize_fn(group_name, messages_text)
        result_str = result or ''
        logger.info("Summary done for group=%r (%d chars result)", group_name, len(result_str))

        # Reject error-stub responses: don't overwrite a good prior summary with
        # "❌ Error generating summary…" markdown. Raising here routes through
        # `_process_task`'s except → `_mark_error`, which re-queues until
        # `max_attempts` is exhausted (default 3). The next attempt may succeed
        # if the Ollama hiccup was transient.
        if result_str.strip() and _STUB_PATTERNS.search(result_str):
            attempts = task.get('attempts', 0)
            max_attempts = task.get('max_attempts', 3)
            snippet = result_str[:200].replace('\n', ' ')
            if _TERMINAL_STUB_PATTERNS.search(result_str):
                # Deterministic for this model+input (think-budget exhaust that
                # already survived ollama.py's salvage + hardened retry). Park
                # it now instead of re-burning two more multi-minute attempts.
                logger.warning(
                    "Parking summary for group=%r as terminal error "
                    "(no-retry, attempt %s/%s): %s",
                    group_name, attempts, max_attempts, snippet,
                )
                raise TerminalTaskError(
                    f"summary_fn returned non-retryable stub "
                    f"(Ollama think-budget exhaust, salvage+retry failed): {snippet}"
                )
            logger.warning(
                "Rejecting stub summary for group=%r (attempt %s/%s): %s",
                group_name, attempts, max_attempts, snippet,
            )
            raise RuntimeError(
                f"summary_fn returned error stub (likely Ollama think-budget exhaust): "
                f"{result_str[:200]}"
            )

        if result_str.strip():
            # rough message count: lines containing "|" (the marker between sender and message)
            message_count = sum(1 for ln in messages_text.splitlines() if '|' in ln) or None
            self._upsert_daily_summary(
                group_name=group_name,
                summary_text=result_str,
                model_used=getattr(config, 'OLLAMA_SUMMARY_MODEL', None),
                char_count=len(messages_text),
                message_count=message_count,
            )
        return result

    def _upsert_daily_summary(self, group_name, summary_text, model_used, char_count, message_count):
        """UPSERT today's summary into daily_summaries, keyed by (date, group_name).

        Failures are logged but do not abort the task — the primary record still
        lives in llm_tasks, so the dashboard keeps working even if this table
        is unavailable for some reason."""
        try:
            conn = _get_conn()
        except Exception:
            logger.exception("daily_summaries: DB connection failed")
            return
        cursor = conn.cursor()
        try:
            cursor.execute(
                "INSERT INTO daily_summaries "
                "(summary_date, group_name, summary_text, model_used, char_count, message_count) "
                "VALUES (CURDATE(), %s, %s, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE "
                "  summary_text = VALUES(summary_text), "
                "  model_used   = VALUES(model_used), "
                "  char_count   = VALUES(char_count), "
                "  message_count= VALUES(message_count), "
                "  generated_at = CURRENT_TIMESTAMP",
                (group_name, summary_text, model_used, char_count, message_count)
            )
            conn.commit()
            logger.info("daily_summaries UPSERT ok for group=%r", group_name)
        except Exception:
            logger.exception("daily_summaries UPSERT failed for group=%r", group_name)
            try:
                conn.rollback()
            except Exception:
                pass
        finally:
            try:
                cursor.close()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass

    def _process_sentiment(self, task):
        """Classify message sentiment using Ollama analysis model."""
        input_data = json.loads(task['input_data']) if task['input_data'] else {}
        message_text = input_data.get('message_text', '')
        message_id = int(task['task_key'])

        if not message_text or len(message_text) < 20:
            sentiment = 'neutral'
        elif self.sentiment_fn:
            sentiment = self.sentiment_fn(message_text)
        else:
            sentiment = 'neutral'

        # Write sentiment directly to messages table
        conn = _get_conn()
        cursor = conn.cursor()
        try:
            cursor.execute("UPDATE messages SET sentiment = %s WHERE id = %s", (sentiment, message_id))
            conn.commit()
        except Exception:
            logger.exception("Failed to update sentiment for message %d", message_id)
        finally:
            cursor.close()
            conn.close()

        return sentiment

    def _process_caption(self, task):
        """Caption one image/video (by md5) and fan the result out to every
        message_attachments row that shares that attachment.

        Returns a short status string (stored as the task result). Raises on a
        transient failure so `_process_task` re-queues until max_attempts; on
        the final attempt it instead marks the rows 'error' and returns, so a
        permanently-failing attachment is never re-scanned forever.
        """
        import image_caption

        input_data = json.loads(task['input_data']) if task['input_data'] else {}
        md5 = input_data.get('md5') or task['task_key']
        media_type = input_data.get('media_type') or 'image'
        # task['attempts'] is the pre-increment value; this run is +1.
        final_attempt = (task.get('attempts', 0) + 1) >= task.get('max_attempts', 2)

        import settings as _settings
        if not _settings.ai_enabled() or _settings.vision_model() is None:
            # Clean skip: return a string → _mark_done (no _set_status), so the
            # rows keep caption_status NULL and recover once vision is re-enabled.
            logger.info("Caption skipped (vision LLM disabled) md5=%s", md5)
            return 'skipped-llm-disabled'

        def _set_status(status, caption=None):
            model = getattr(config, 'OLLAMA_VISION_MODEL', None)
            conn = _get_conn()
            cur = conn.cursor()
            try:
                cur.execute(
                    "UPDATE message_attachments ma "
                    "JOIN attachments a ON (a.file_name = ma.attachment_id "
                    "                       OR a.file_name = ma.file_name) "
                    "SET ma.ai_caption=%s, ma.caption_status=%s, "
                    "    ma.caption_model=%s, ma.captioned_at=NOW() "
                    "WHERE a.md5sum=%s",
                    (caption, status, model, md5),
                )
                conn.commit()
            except Exception:
                logger.exception("caption status update failed (md5=%s)", md5)
            # Denormalized md5-keyed cache on `attachments` itself. Always
            # written (md5sum is UNIQUE there): for orphan blobs — no joinable
            # message_attachments row — this is the ONLY place the caption can
            # land and is what gates the lazy orphan worker; for linked rows
            # it's a harmless cache the files tab can COALESCE onto. Separate
            # try/except so it can never break the path above.
            try:
                cur.execute(
                    "UPDATE attachments "
                    "SET ai_caption=%s, caption_status=%s, "
                    "    caption_model=%s, captioned_at=NOW() "
                    "WHERE md5sum=%s",
                    (caption, status, model, md5),
                )
                conn.commit()
            except Exception:
                logger.exception("attachments caption cache update failed (md5=%s)", md5)
            finally:
                cur.close()
                conn.close()

        # Load the stored bytes (persisted by Signal poll_attachments or the
        # ingest-time capture for WhatsApp/Telegram).
        conn = _get_conn()
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT file_content FROM attachments WHERE md5sum=%s LIMIT 1", (md5,)
            )
            row = cur.fetchone()
        finally:
            cur.close()
            conn.close()
        raw = row[0] if row else None
        if not raw:
            if final_attempt:
                _set_status('error')
                return 'no-bytes'
            raise RuntimeError(f"caption: no bytes yet for md5={md5}")

        caption, status = image_caption.caption_media(
            bytes(raw), media_type, ollama_sem=self.ollama_sem
        )
        if status == 'done':
            _set_status('done', caption)
            logger.info("caption done md5=%s (%d chars)", md5, len(caption))
            return caption
        if status == 'skipped':
            _set_status('skipped')
            return 'skipped'
        # status == 'error'
        if final_attempt:
            _set_status('error')
            return 'error'
        raise RuntimeError(f"caption generation failed for md5={md5}")

    def _process_cross_group(self, task):
        """Generate cross-group topic summary."""
        input_data = json.loads(task['input_data']) if task['input_data'] else {}
        summaries_text = input_data.get('summaries_text', '')

        if not summaries_text:
            return ''

        import settings as _settings
        if not _settings.ai_enabled() or _settings.summary_model() is None:
            logger.info("Cross-group summary skipped (LLM disabled)")
            return ''

        if self.cross_group_fn:
            return self.cross_group_fn(summaries_text)
        return ''

    def _process_monthly_summary(self, task):
        """Aggregate a month of daily summaries into a single monthly summary,
        and UPSERT into `monthly_summaries`."""
        input_data = json.loads(task['input_data']) if task['input_data'] else {}
        group_name = input_data.get('group_name') or task['task_key'].split('|', 1)[0]
        month_str  = input_data.get('month_start') or task['task_key'].split('|', 1)[-1]
        daily_text = input_data.get('daily_text', '')
        daily_count = input_data.get('daily_count') or daily_text.count('\n--- ')

        if not daily_text or not self.monthly_summarize_fn:
            return ''

        import settings as _settings
        if not _settings.ai_enabled() or _settings.summary_model() is None:
            logger.info("Monthly summary skipped (LLM disabled) group=%r", group_name)
            return ''

        try:
            month_start = datetime.strptime(month_str, '%Y-%m-%d').date()
        except Exception:
            logger.warning("monthly_summary: bad month_start=%r", month_str)
            return ''

        logger.info("Generating monthly summary for group=%r month=%s (%d chars in)",
                    group_name, month_str, len(daily_text))
        result = self.monthly_summarize_fn(group_name, month_start, daily_text)
        result_str = result or ''
        logger.info("Monthly summary done group=%r month=%s (%d chars out)",
                    group_name, month_str, len(result_str))

        if result_str.strip():
            self._upsert_monthly_summary(
                group_name=group_name,
                month_start=month_start,
                summary_text=result_str,
                daily_count=int(daily_count or 0),
                model_used=getattr(config, 'OLLAMA_SUMMARY_MODEL', None),
            )
        return result

    def _process_yearly_summary(self, task):
        """Aggregate a year of monthly summaries into a single yearly summary,
        and UPSERT into `yearly_summaries`."""
        input_data = json.loads(task['input_data']) if task['input_data'] else {}
        group_name   = input_data.get('group_name') or task['task_key'].split('|', 1)[0]
        year_str     = input_data.get('year_start')  or task['task_key'].split('|', 1)[-1]
        monthly_text = input_data.get('monthly_text', '')
        monthly_count = input_data.get('monthly_count') or monthly_text.count('\n--- ')

        if not monthly_text or not self.yearly_summarize_fn:
            return ''

        import settings as _settings
        if not _settings.ai_enabled() or _settings.summary_model() is None:
            logger.info("Yearly summary skipped (LLM disabled) group=%r", group_name)
            return ''

        try:
            year_start = datetime.strptime(year_str, '%Y-%m-%d').date()
        except Exception:
            logger.warning("yearly_summary: bad year_start=%r", year_str)
            return ''

        logger.info("Generating yearly summary for group=%r year=%s (%d chars in)",
                    group_name, year_str, len(monthly_text))
        result = self.yearly_summarize_fn(group_name, year_start, monthly_text)
        result_str = result or ''
        logger.info("Yearly summary done group=%r year=%s (%d chars out)",
                    group_name, year_str, len(result_str))

        if result_str.strip():
            self._upsert_yearly_summary(
                group_name=group_name,
                year_start=year_start,
                summary_text=result_str,
                monthly_count=int(monthly_count or 0),
                model_used=getattr(config, 'OLLAMA_SUMMARY_MODEL', None),
            )
        return result

    def _upsert_monthly_summary(self, group_name, month_start, summary_text, daily_count, model_used):
        try:
            conn = _get_conn()
        except Exception:
            logger.exception("monthly_summaries: DB connect failed")
            return
        cursor = conn.cursor()
        try:
            cursor.execute(
                "INSERT INTO monthly_summaries "
                "(month_start, group_name, summary_text, daily_count, model_used) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE "
                "  summary_text = VALUES(summary_text), "
                "  daily_count  = VALUES(daily_count), "
                "  model_used   = VALUES(model_used), "
                "  generated_at = CURRENT_TIMESTAMP",
                (month_start, group_name, summary_text, daily_count, model_used)
            )
            conn.commit()
            logger.info("monthly_summaries UPSERT ok for %r/%s", group_name, month_start)
        except Exception:
            logger.exception("monthly_summaries UPSERT failed for %r/%s", group_name, month_start)
            try:
                conn.rollback()
            except Exception:
                pass
        finally:
            try:
                cursor.close()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass

    def _upsert_yearly_summary(self, group_name, year_start, summary_text, monthly_count, model_used):
        try:
            conn = _get_conn()
        except Exception:
            logger.exception("yearly_summaries: DB connect failed")
            return
        cursor = conn.cursor()
        try:
            cursor.execute(
                "INSERT INTO yearly_summaries "
                "(year_start, group_name, summary_text, monthly_count, model_used) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE "
                "  summary_text  = VALUES(summary_text), "
                "  monthly_count = VALUES(monthly_count), "
                "  model_used    = VALUES(model_used), "
                "  generated_at  = CURRENT_TIMESTAMP",
                (year_start, group_name, summary_text, monthly_count, model_used)
            )
            conn.commit()
            logger.info("yearly_summaries UPSERT ok for %r/%s", group_name, year_start)
        except Exception:
            logger.exception("yearly_summaries UPSERT failed for %r/%s", group_name, year_start)
            try:
                conn.rollback()
            except Exception:
                pass
        finally:
            try:
                cursor.close()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass

    def _mark_done(self, task_id, result):
        """Mark task as completed with result."""
        conn = _get_conn()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "UPDATE llm_tasks SET status='done', result=%s, completed_at=NOW() "
                "WHERE id=%s",
                (result, task_id)
            )
            conn.commit()
            logger.info("Task id=%d marked done", task_id)
        except Exception:
            logger.exception("Failed to mark task %d done", task_id)
        finally:
            cursor.close()
            conn.close()

    def _mark_error(self, task_id, error_msg, terminal=False):
        """Mark task as error. Retry if under max_attempts.

        ``terminal=True`` parks the task as 'error' immediately regardless of
        the attempt counter — used for deterministic failures that would fail
        identically on retry (see TerminalTaskError)."""
        conn = _get_conn()
        cursor = conn.cursor(dictionary=True)
        try:
            cursor.execute("SELECT attempts, max_attempts FROM llm_tasks WHERE id=%s", (task_id,))
            row = cursor.fetchone()
            if row and not terminal and row['attempts'] < row['max_attempts']:
                cursor.execute(
                    "UPDATE llm_tasks SET status='pending', error_msg=%s WHERE id=%s",
                    (error_msg, task_id)
                )
                logger.warning("Task id=%d failed (attempt %d/%d), re-queued: %s",
                               task_id, row['attempts'], row['max_attempts'], error_msg)
            else:
                cursor.execute(
                    "UPDATE llm_tasks SET status='error', error_msg=%s, completed_at=NOW() WHERE id=%s",
                    (error_msg, task_id)
                )
                logger.error("Task id=%d permanently failed: %s", task_id, error_msg)
            conn.commit()
        except Exception:
            logger.exception("Failed to mark task %d as error", task_id)
        finally:
            cursor.close()
            conn.close()

    def _expire_stale_running(self):
        """Reset tasks stuck in 'running' for too long back to 'pending'."""
        stale_threshold = config.OLLAMA_READ_TIMEOUT * 2
        conn = _get_conn()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "UPDATE llm_tasks SET status='pending', error_msg='Timed out (stale running)' "
                "WHERE status='running' AND started_at < NOW() - INTERVAL %s SECOND",
                (stale_threshold,)
            )
            if cursor.rowcount > 0:
                logger.warning("Reset %d stale running tasks", cursor.rowcount)
            conn.commit()
        except Exception:
            logger.exception("Failed to expire stale tasks")
        finally:
            cursor.close()
            conn.close()

    def invalidate_summaries(self):
        """Delete old done/error summary tasks so they get re-enqueued."""
        conn = _get_conn()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "DELETE FROM llm_tasks "
                "WHERE task_type='summary' AND status IN ('done','error')"
            )
            conn.commit()
            deleted = cursor.rowcount
            logger.info("Invalidated %d old summary tasks", deleted)
            return deleted
        except Exception:
            logger.exception("Failed to invalidate summaries")
            return 0
        finally:
            cursor.close()
            conn.close()
