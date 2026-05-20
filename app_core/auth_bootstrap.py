"""
Auth-secret bootstrap.

Goal: never let the dashboard come up unauthenticated by accident.

`config.AUTH_SECRET` is read by `app.py` at module-init time (line 61) and again
by the per-request gate (`@app.before_request check_auth`). When empty, the
whole API is LAN-readable. This module's `ensure_auth_secret()` runs as the very
first thing in `app.py` (right after `import config`) so all downstream readers
see a populated value:

  1. If `config.AUTH_SECRET` is already non-empty (set via env), return.
  2. Else, if `./.auth-secret` exists and is readable, load it and assign to
     `config.AUTH_SECRET`. **Never overwrite** an operator-provided file —
     this lets a Docker bind-mount or sealed secret stay authoritative.
  3. Else, generate a 48-byte URL-safe random token, write it to `.auth-secret`
     with mode 0600, and assign to `config.AUTH_SECRET`. Idempotent: the next
     boot hits branch (2) and reuses the same value.

The file path defaults to `.auth-secret` in the current working directory (which
is the project root when launched via `python3 app.py`). In Docker that's
`/app/.auth-secret`, which lives on a writable volume so it persists across
container restarts.
"""

from __future__ import annotations

import logging
import os
import secrets
import stat

import config

logger = logging.getLogger("auth_bootstrap")

_SECRET_FILE = os.environ.get("AUTH_SECRET_FILE", ".auth-secret")


def _read_secret_file(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            value = f.read().strip()
        return value or None
    except FileNotFoundError:
        return None
    except OSError as e:
        logger.warning("auth_bootstrap: cannot read %s: %s", path, e)
        return None


def _write_secret_file(path: str, value: str) -> bool:
    """Write the secret with mode 0600. Returns True on success."""
    try:
        # umask covers the case where the file already exists with a wider mode.
        prev_umask = os.umask(0o077)
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(value + "\n")
        finally:
            os.umask(prev_umask)
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        except OSError:
            pass
        return True
    except OSError as e:
        logger.error("auth_bootstrap: cannot write %s: %s", path, e)
        return False


def ensure_auth_secret(path: str | None = None) -> str:
    """Populate `config.AUTH_SECRET` (and return the value). Call BEFORE any other
    module reads `config.AUTH_SECRET`."""
    secret_path = path or _SECRET_FILE

    # 1. Env-provided wins.
    if config.AUTH_SECRET:
        logger.info("auth_bootstrap: AUTH_SECRET provided via env (len=%d)",
                    len(config.AUTH_SECRET))
        return config.AUTH_SECRET

    # 2. File on disk (operator-mounted or previously generated).
    existing = _read_secret_file(secret_path)
    if existing:
        config.AUTH_SECRET = existing
        logger.info("auth_bootstrap: AUTH_SECRET loaded from %s (len=%d, prefix=%s…)",
                    secret_path, len(existing), existing[:4])
        return existing

    # 3. Generate + persist. token_urlsafe(48) yields a 64-char string.
    new_secret = secrets.token_urlsafe(48)
    if _write_secret_file(secret_path, new_secret):
        config.AUTH_SECRET = new_secret
        logger.warning(
            "auth_bootstrap: generated new AUTH_SECRET and persisted to %s "
            "(len=%d, prefix=%s…). Auth is now ENABLED.",
            secret_path, len(new_secret), new_secret[:4],
        )
    else:
        # Filesystem write failed (read-only mount?). Fall back to in-memory
        # only — better an ephemeral secret than no auth at all. The operator
        # will see the warning and either fix the mount or set the env var.
        config.AUTH_SECRET = new_secret
        logger.error(
            "auth_bootstrap: generated AUTH_SECRET could NOT be persisted to %s — "
            "auth is enabled for this process only. Set AUTH_SECRET env var or "
            "make the file writable.",
            secret_path,
        )
    return config.AUTH_SECRET
