#!/usr/bin/env python3
"""
Gmail-to-local email repository importer.

The local repository is authoritative. Gmail is only an import source.

Repository ownership model:
- Every active message has a canonical record in the messages table.
- ALL EMAIL is a normal persisted folder in folders, with a folder_messages
  relationship to every active message.
- Every message is stored exactly once, as raw RFC 2822 bytes returned
  by Gmail's API.
- Local folders and Gmail labels are organisational references to
  canonical message records, never separate physical message copies.
- Archiving removes INBOX membership, not ALL EMAIL membership.
- Gmail label changes update memberships in local folders mapped to Gmail
  labels. They never remove ALL EMAIL membership.
- Gmail deletions never delete local data.
- Only explicit local deletion removes a message and all its folder memberships.
- Locally deleted Gmail messages are suppressed by a tombstone.

Authentication supports both Google Workspace service accounts with
domain-wide delegation and personal Gmail OAuth 2.0. Personal OAuth is
authorised on a computer with a browser; its token is then transferred
to the headless Pi.

Configuration is supplied through environment variables. Run with
--help for the required variables and usage information.
"""

from __future__ import annotations

import argparse
import errno
import base64
import hashlib
import json
import logging
import os
import sqlite3
import shutil
import random
import time
import stat
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional
from urllib.parse import quote
from requests import Session

from google.auth.transport.requests import Request
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


APP_NAME = "Gmail Local Repository Importer"
SCHEMA_VERSION = 3
ALL_EMAIL_FOLDER_NAME = "ALL EMAIL"
MAX_FILES_PER_SHARD = 500
DEFAULT_DB_LOCATION = "./data"
REPOSITORY_MARKER = "repository.json"
DATABASE_FILENAME = "repository.db"
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
API_PAGE_SIZE = 500
DEFAULT_MIN_FREE_BYTES = 64 * 1024 * 1024
MAX_API_RETRIES = 8
DEFAULT_HTTP_TIMEOUT_SECONDS = 30
INITIAL_BACKOFF_SECONDS = 2.0
MAX_BACKOFF_SECONDS = 120.0
# Keep request volume below bursty per-user limits. The interval adapts upward
# after quota responses and gradually recovers after sustained success.
INITIAL_API_REQUEST_INTERVAL_SECONDS = 0.25
MIN_API_REQUEST_INTERVAL_SECONDS = 0.15
MAX_API_REQUEST_INTERVAL_SECONDS = 5.0
API_INTERVAL_RECOVERY_SUCCESSES = 40


class TimeoutSession(Session):
    """Requests session that applies a finite timeout to every HTTP request."""

    def request(self, method, url, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = DEFAULT_HTTP_TIMEOUT_SECONDS

        return super().request(method, url, **kwargs)


class DiskSpaceExhausted(RuntimeError):
    """Fatal repository storage condition; stop the import immediately."""


class GmailRateLimitExhausted(RuntimeError):
    """Gmail continued rejecting requests after bounded exponential backoff."""


# The importer is single-threaded, so a small module-level pacing state is
# sufficient. It applies across all Gmail API operations in this process.
_api_request_interval = INITIAL_API_REQUEST_INTERVAL_SECONDS
_api_last_request_started = 0.0
_api_successes_at_current_interval = 0


def interruptible_wait(seconds: float) -> None:
    """Wait without masking Ctrl-C; check in short intervals as a safeguard."""
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        # time.sleep in the main thread is interrupted by SIGINT. Short slices
        # also ensure the loop never commits to one long, opaque wait.
        time.sleep(min(0.25, remaining))


def pace_google_request() -> None:
    """Wait until the adaptive minimum interval has elapsed."""
    global _api_last_request_started
    now = time.monotonic()
    elapsed = now - _api_last_request_started
    wait = _api_request_interval - elapsed
    if wait > 0:
        interruptible_wait(wait)
    _api_last_request_started = time.monotonic()


def note_google_request_success() -> None:
    """Gradually recover request speed after sustained successful calls."""
    global _api_request_interval, _api_successes_at_current_interval
    _api_successes_at_current_interval += 1
    if _api_successes_at_current_interval >= API_INTERVAL_RECOVERY_SUCCESSES:
        _api_request_interval = max(
            MIN_API_REQUEST_INTERVAL_SECONDS,
            _api_request_interval * 0.9,
        )
        _api_successes_at_current_interval = 0


def note_google_rate_limit() -> None:
    """Back off proactively after a quota response, not just for one retry."""
    global _api_request_interval, _api_successes_at_current_interval
    _api_request_interval = min(
        MAX_API_REQUEST_INTERVAL_SECONDS,
        max(INITIAL_API_REQUEST_INTERVAL_SECONDS, _api_request_interval * 2),
    )
    _api_successes_at_current_interval = 0


def server_retry_delay(exc: HttpError) -> Optional[float]:
    """Extract Retry-After or Google RPC RetryInfo delay, if supplied."""
    response = getattr(exc, "resp", None)
    headers = getattr(response, "headers", {}) or {}
    retry_after = None
    try:
        retry_after = next(
            (value for key, value in headers.items()
             if str(key).casefold() == "retry-after"),
            None,
        )
    except AttributeError:
        pass

    if retry_after is not None:
        try:
            return max(0.0, float(retry_after))
        except (TypeError, ValueError):
            try:
                retry_date = parsedate_to_datetime(str(retry_after))
                if retry_date.tzinfo is None:
                    retry_date = retry_date.replace(tzinfo=timezone.utc)
                return max(0.0, (retry_date - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                pass

    try:
        payload = json.loads(exc.content.decode("utf-8"))
        details = payload.get("error", {}).get("details", [])
        for detail in details:
            value = detail.get("retryDelay") or detail.get("retry_delay")
            if value is None:
                continue
            # Google RPC Duration values are strings such as "1.500s".
            text_value = str(value).strip()
            if text_value.endswith("s"):
                return max(0.0, float(text_value[:-1]))
            return max(0.0, float(text_value))
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        pass
    return None


def execute_google_request(request, description: str):
    """Execute a Gmail API request with adaptive pacing and retry handling.

    Quota responses increase the delay between all subsequent API calls. A
    server-provided retry delay is honoured when present; otherwise bounded
    exponential backoff with jitter is used. Non-quota 403s are not retried.
    """
    for attempt in range(MAX_API_RETRIES + 1):
        pace_google_request()
        try:
            result = request.execute()
            note_google_request_success()
            return result
        except HttpError as exc:
            status = getattr(getattr(exc, "resp", None), "status", None)
            reason = ""
            try:
                payload = json.loads(exc.content.decode("utf-8"))
                errors = payload.get("error", {}).get("errors", [])
                reason = errors[0].get("reason", "") if errors else ""
            except Exception:
                pass
            retryable = (
                status == 429
                or (status == 403 and reason in {
                    "rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded"
                })
                or (status is not None and 500 <= status < 600)
            )
            if not retryable:
                raise

            is_quota_rejection = (
                status == 429
                or (status == 403 and reason in {
                    "rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded"
                })
            )
            if is_quota_rejection:
                note_google_rate_limit()
            if attempt >= MAX_API_RETRIES:
                raise GmailRateLimitExhausted(
                    f"Gmail API kept rejecting {description} after "
                    f"{MAX_API_RETRIES + 1} attempts (HTTP {status}, "
                    f"reason {reason or 'unknown'}). The import has stopped; "
                    "run it again later to resume."
                ) from exc

            suggested_delay = server_retry_delay(exc)
            if suggested_delay is not None:
                delay = suggested_delay
                delay_source = "Gmail Retry-After/retry-delay"
            else:
                delay = min(
                    MAX_BACKOFF_SECONDS,
                    INITIAL_BACKOFF_SECONDS * (2 ** attempt),
                )
                delay *= random.uniform(0.8, 1.2)
                delay_source = "local exponential backoff"

            logging.warning(
                "Gmail API rate limit/transient error for %s "
                "(HTTP %s, %s); retry %s/%s in %.1f seconds (%s). "
                "Adaptive request interval is now %.2f seconds.",
                description, status, reason or "transient error",
                attempt + 1, MAX_API_RETRIES, delay, delay_source,
                _api_request_interval,
            )
            interruptible_wait(delay)


def minimum_free_bytes() -> int:
    raw = os.environ.get("GMAIL_MIN_FREE_BYTES", str(DEFAULT_MIN_FREE_BYTES))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError("GMAIL_MIN_FREE_BYTES must be a non-negative integer.") from exc
    if value < 0:
        raise ValueError("GMAIL_MIN_FREE_BYTES must be a non-negative integer.")
    return value


def check_disk_space(path: Path) -> None:
    """Keep a configurable free-space reserve before writing more data."""
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    usage = shutil.disk_usage(probe)
    reserve = minimum_free_bytes()
    if usage.free < reserve:
        raise DiskSpaceExhausted(
            f"Insufficient disk space at {probe}: {usage.free:,} bytes free; "
            f"the configured safety reserve is {reserve:,} bytes. "
            "The import has stopped. Free space and run it again to resume. "
            "Set GMAIL_MIN_FREE_BYTES=0 to disable the reserve check."
        )


def is_disk_full_error(exc: BaseException) -> bool:
    if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
        return True
    if isinstance(exc, sqlite3.Error):
        return getattr(exc, "sqlite_errorcode", None) == getattr(sqlite3, "SQLITE_FULL", 13)
    return False


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=Path(sys.argv[0]).name,
        description=(
            "Import Gmail messages into an authoritative local repository, or "
            "authorise a personal Gmail account on a computer with a browser."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""\
AUTHENTICATION FLOWS

1. Google Workspace (existing flow; unchanged):
   GOOGLE_SERVICE_ACCOUNT_FILE and GMAIL_IMPERSONATE_USER identify the
   service-account key and Workspace mailbox. The service account must have
   domain-wide delegation enabled and the exact scope below authorised by the
   Workspace administrator.

2. Personal Gmail (one-time browser authorisation on a computer):
   Run --authorise on a Mac/PC/Linux computer with a browser. Supply the
   Desktop OAuth client JSON and the Gmail address. The command writes a
   refreshable token file. Transfer that token securely to the Raspberry Pi.
   The Pi then runs normal imports headlessly.

   {Path(sys.argv[0]).name} --authorise --account you@gmail.com \
       --oauth-client-file ~/Downloads/oauth-client.json \
       --output ~/gmail-token.json

   On the Pi, set GMAIL_OAUTH_TOKEN_FILE to the transferred token and
   GMAIL_ACCOUNT to the same Gmail address. No browser is needed on the Pi.

COMMON SETTINGS
  DB_LOCATION                  Parent directory for repositories
                               (optional; default: {DEFAULT_DB_LOCATION})
  GMAIL_MIN_FREE_BYTES         Minimum free-space reserve (optional;
                               default: {DEFAULT_MIN_FREE_BYTES}; set 0 to disable)
  GMAIL_AUTH_METHOD            workspace or oauth; optional when the
                               credentials clearly identify one method

WORKSPACE SETTINGS
  GOOGLE_SERVICE_ACCOUNT_FILE  Workspace service-account JSON key
  GMAIL_IMPERSONATE_USER       Workspace mailbox address

PERSONAL GMAIL SETTINGS
  GMAIL_ACCOUNT                Personal Gmail address
  GMAIL_OAUTH_TOKEN_FILE       OAuth token JSON (transferred from authorising
                               computer)

OAuth authorisation options:
  --authorise                  Perform personal Gmail OAuth authorisation
  --account                    Gmail address to authorise
  --oauth-client-file          Google OAuth Desktop client JSON
  --output                     Token output path (owner-only permissions)

OAuth scope (read-only):
  {GMAIL_READONLY_SCOPE}

The repository is created at:
  DB_LOCATION/<encoded-mailbox-address>/

It contains {DATABASE_FILENAME}, {REPOSITORY_MARKER}, and a messages/
directory. Each messages shard contains no more than
{MAX_FILES_PER_SHARD} RFC822 files.

Examples for Workspace imports (same configuration as previous versions):
  export GOOGLE_SERVICE_ACCOUNT_FILE="$HOME/.config/gmail-local-repository/service-account.json"
  export GMAIL_IMPERSONATE_USER="you@yourdomain.com"
  export DB_LOCATION="./data"
  {Path(sys.argv[0]).name}

Examples for personal Gmail imports on the Pi:
  export GMAIL_AUTH_METHOD="oauth"
  export GMAIL_ACCOUNT="you@gmail.com"
  export GMAIL_OAUTH_TOKEN_FILE="$HOME/.config/gmail-local-repository/personal-gmail-token.json"
  export DB_LOCATION="./data"
  {Path(sys.argv[0]).name}

Import behaviour:
  - A missing or empty user repository directory starts an initial import.
  - A recognised repository syncs from its saved Gmail History API
    checkpoint; a full All Mail scan is used only for initial import or
    expired-history recovery.
  - Existing local messages are checked using metadata; raw message content
    is downloaded only for newly imported messages.
  - Every active message has a relationship to the normal persisted
    ALL EMAIL folder.
  - Gmail deletions never delete local messages or remove their ALL EMAIL
    membership.
  - Gmail label changes synchronise memberships in mapped local folders.
  - Local-only folder memberships are not changed by Gmail label updates.
  - Local deletion removes the message and its folder memberships,
    including ALL EMAIL, and creates a tombstone to suppress re-import.
""",
    )
    parser.add_argument(
        "--authorise", action="store_true",
        help="authorise a personal Gmail account (run on a computer with a browser)",
    )
    parser.add_argument("--account", help="personal Gmail address to authorise")
    parser.add_argument(
        "--oauth-client-file", type=Path,
        help="Google OAuth Desktop client JSON downloaded from Google Cloud",
    )
    parser.add_argument(
        "--output", type=Path,
        help="where to save the personal Gmail OAuth token JSON",
    )
    return parser


def required_environment(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise ValueError(f"Required environment variable {name} is not set.")
    return value.strip()


def safe_repository_name(username: str) -> str:
    return quote(username, safe="") or "user"


def get_configuration() -> dict:
    auth_method = os.environ.get("GMAIL_AUTH_METHOD", "").strip().casefold()
    workspace_mailbox = os.environ.get("GMAIL_IMPERSONATE_USER", "").strip()
    personal_mailbox = os.environ.get("GMAIL_ACCOUNT", "").strip()
    service_account_value = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
    oauth_token_value = os.environ.get("GMAIL_OAUTH_TOKEN_FILE", "").strip()

    if not auth_method:
        if service_account_value and workspace_mailbox:
            auth_method = "workspace"
        elif oauth_token_value and (personal_mailbox or workspace_mailbox):
            auth_method = "oauth"
        else:
            raise ValueError(
                "Cannot determine Gmail authentication method. Set "
                "GMAIL_AUTH_METHOD=workspace or oauth and configure the "
                "corresponding environment variables."
            )

    if auth_method not in ("workspace", "oauth"):
        raise ValueError("GMAIL_AUTH_METHOD must be 'workspace' or 'oauth'.")
    mailbox = workspace_mailbox if auth_method == "workspace" else (personal_mailbox or workspace_mailbox)
    if not mailbox:
        raise ValueError("Set GMAIL_IMPERSONATE_USER (Workspace) or GMAIL_ACCOUNT (OAuth).")

    db_location = Path(
        os.environ.get("DB_LOCATION", DEFAULT_DB_LOCATION)
    ).expanduser()
    config = {
        "auth_method": auth_method,
        "mailbox": mailbox,
        "db_location": db_location,
        "repository_path": db_location / safe_repository_name(mailbox),
    }
    config["database_path"] = config["repository_path"] / DATABASE_FILENAME
    if auth_method == "workspace":
        if not service_account_value:
            raise ValueError("GOOGLE_SERVICE_ACCOUNT_FILE is required for Workspace authentication.")
        config["service_account_file"] = Path(service_account_value).expanduser()
    else:
        if not oauth_token_value:
            raise ValueError("GMAIL_OAUTH_TOKEN_FILE is required for personal Gmail OAuth.")
        config["oauth_token_file"] = Path(oauth_token_value).expanduser()
    return config


def detect_repository_mode(repository_path: Path) -> str:
    """Return initial/sync mode, rejecting incomplete or unknown directories."""
    if not repository_path.exists():
        return "initial"
    if not repository_path.is_dir():
        raise RuntimeError(
            f"Repository target exists but is not a directory: {repository_path}"
        )

    if not list(repository_path.iterdir()):
        return "initial"

    marker = repository_path / REPOSITORY_MARKER
    database = repository_path / DATABASE_FILENAME
    messages = repository_path / "messages"
    if not marker.is_file() or not database.is_file() or not messages.is_dir():
        raise RuntimeError(
            "The target directory is non-empty but is not a complete recognised "
            f"repository: {repository_path}. Nothing has been changed."
        )

    try:
        with marker.open("r", encoding="utf-8") as handle:
            metadata = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read repository marker {marker}: {exc}") from exc

    if metadata.get("application") != "gmail-local-repository":
        raise RuntimeError(f"Unrecognised repository marker: {marker}")
    version = metadata.get("schema_version")
    if version not in (1, 2, SCHEMA_VERSION):
        raise RuntimeError(
            f"Unsupported repository schema version {version!r}; "
            f"this script supports versions 1 and {SCHEMA_VERSION}."
        )
    return "sync"


def write_repository_marker(repository_path: Path, mailbox: str) -> None:
    marker_path = repository_path / REPOSITORY_MARKER
    created_at = datetime.now(timezone.utc).isoformat()
    if marker_path.is_file():
        try:
            with marker_path.open("r", encoding="utf-8") as existing:
                old_marker = json.load(existing)
            created_at = old_marker.get("created_at", created_at)
        except (OSError, json.JSONDecodeError):
            pass
    marker = {
        "application": "gmail-local-repository",
        "schema_version": SCHEMA_VERSION,
        "mailbox": mailbox,
        "created_at": created_at,
    }
    temp_path = repository_path / (REPOSITORY_MARKER + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(marker, handle, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, repository_path / REPOSITORY_MARKER)


def connect_database(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


@contextmanager
def transaction(connection: sqlite3.Connection):
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def create_schema(connection: sqlite3.Connection, mailbox: str) -> None:
    connection.executescript(
        """
        CREATE TABLE repository_info (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE folders (
            folder_id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            uidvalidity TEXT,
            last_seen_at TEXT
        );

        CREATE TABLE message_shards (
            shard_id INTEGER PRIMARY KEY AUTOINCREMENT,
            directory_name TEXT NOT NULL UNIQUE,
            file_count INTEGER NOT NULL DEFAULT 0
                CHECK(file_count >= 0 AND file_count <= 500),
            created_at TEXT NOT NULL
        );

        CREATE TABLE messages (
            message_id INTEGER PRIMARY KEY,
            gm_id TEXT NOT NULL UNIQUE,
            gmail_thread_id TEXT,
            shard_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            message_size INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            internal_date TEXT,
            imported_at TEXT NOT NULL,
            is_read INTEGER NOT NULL DEFAULT 0,
            is_starred INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(shard_id) REFERENCES message_shards(shard_id)
        );

        CREATE TABLE folder_messages (
            folder_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            gmail_uid TEXT,
            first_seen_at TEXT NOT NULL,
            PRIMARY KEY(folder_id, message_id),
            FOREIGN KEY(folder_id) REFERENCES folders(folder_id),
            FOREIGN KEY(message_id) REFERENCES messages(message_id)
        );

        CREATE TABLE deleted_messages (
            gm_id TEXT PRIMARY KEY,
            deleted_at TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT 'local deletion'
        );

        CREATE TABLE sync_state (
            account_id TEXT PRIMARY KEY,
            history_id TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        -- Gmail's labels are a source-side snapshot, not local folders.
        CREATE TABLE gmail_labels (
            label_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            label_type TEXT,
            local_folder_id INTEGER,
            last_seen_at TEXT NOT NULL,
            FOREIGN KEY(local_folder_id) REFERENCES folders(folder_id)
        );

        CREATE TABLE gmail_message_labels (
            message_id INTEGER NOT NULL,
            label_id TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            PRIMARY KEY(message_id, label_id),
            FOREIGN KEY(message_id) REFERENCES messages(message_id),
            FOREIGN KEY(label_id) REFERENCES gmail_labels(label_id)
        );

        CREATE INDEX idx_folder_messages_message
            ON folder_messages(message_id);
        CREATE INDEX idx_messages_thread
            ON messages(gmail_thread_id);
        CREATE INDEX idx_gmail_message_labels_label
            ON gmail_message_labels(label_id);
        """
    )
    connection.execute(
        "INSERT INTO repository_info(key, value) VALUES (?, ?)",
        ("username", mailbox),
    )
    connection.execute(
        "INSERT INTO repository_info(key, value) VALUES (?, ?)",
        ("schema_version", str(SCHEMA_VERSION)),
    )
    connection.commit()


def ensure_indexes(connection: sqlite3.Connection) -> None:
    """Ensure required performance and foreign-key indexes exist.

    Safe to run repeatedly against both new and existing repositories.
    Does not alter tables or stored data.
    """
    statements = (
        """
        CREATE INDEX IF NOT EXISTS idx_messages_shard
        ON messages(shard_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_gmail_labels_local_folder
        ON gmail_labels(local_folder_id)
        """,
    )

    with transaction(connection):
        for statement in statements:
            connection.execute(statement)


def ensure_all_email_folder(connection: sqlite3.Connection) -> int:
    """Create the ordinary ALL EMAIL folder and ensure it contains every message.

    This is deliberately persisted through the same folders/folder_messages
    tables used by every other mailbox. The IMAP server therefore needs no
    special knowledge of the canonical messages table.
    """
    now = datetime.now(timezone.utc).isoformat()
    with transaction(connection):
        connection.execute(
            """
            INSERT INTO folders(name, uidvalidity, last_seen_at)
            VALUES (?, NULL, ?)
            ON CONFLICT(name) DO UPDATE SET last_seen_at = excluded.last_seen_at
            """,
            (ALL_EMAIL_FOLDER_NAME, now),
        )
        row = connection.execute(
            "SELECT folder_id FROM folders WHERE name = ?",
            (ALL_EMAIL_FOLDER_NAME,),
        ).fetchone()
        folder_id = int(row["folder_id"])

        # Backfill existing repositories and repair any missing ALL EMAIL
        # relationships without disturbing other folder memberships.
        connection.execute(
            """
            INSERT OR IGNORE INTO folder_messages(
                folder_id, message_id, gmail_uid, first_seen_at
            )
            SELECT ?, message_id, NULL, imported_at
            FROM messages
            """,
            (folder_id,),
        )
    return folder_id


def migrate_schema(connection: sqlite3.Connection, repository_path: Path,
                   mailbox: str) -> None:
    """Migrate earlier repository schemas without changing stored messages."""
    row = connection.execute(
        "SELECT value FROM repository_info WHERE key = 'schema_version'"
    ).fetchone()
    if row is None:
        raise RuntimeError("Repository database has no schema-version metadata.")
    version = int(row["value"])
    if version == SCHEMA_VERSION:
        return
    if version not in (1, 2):
        raise RuntimeError(f"No migration path from repository schema {version}.")

    with transaction(connection):
        if version == 1:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS gmail_labels (
                    label_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    label_type TEXT,
                    local_folder_id INTEGER,
                    last_seen_at TEXT NOT NULL,
                    FOREIGN KEY(local_folder_id) REFERENCES folders(folder_id)
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS gmail_message_labels (
                    message_id INTEGER NOT NULL,
                    label_id TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    PRIMARY KEY(message_id, label_id),
                    FOREIGN KEY(message_id) REFERENCES messages(message_id),
                    FOREIGN KEY(label_id) REFERENCES gmail_labels(label_id)
                )"""
            )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS idx_gmail_message_labels_label
                   ON gmail_message_labels(label_id)"""
            )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS sync_state (
                account_id TEXT PRIMARY KEY,
                history_id TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        connection.execute(
            "UPDATE repository_info SET value = ? WHERE key = 'schema_version'",
            (str(SCHEMA_VERSION),),
        )
    logging.info(
        "Migrated repository database from schema version %s to %s; "
        "existing message files were not changed.", version, SCHEMA_VERSION,
    )


def ensure_repository(connection: sqlite3.Connection, repository_path: Path,
                      mailbox: str, mode: str) -> None:

    if mode == "initial":
        repository_path.mkdir(parents=True, exist_ok=True)
        (repository_path / "messages").mkdir(exist_ok=True)
        create_schema(connection, mailbox)
        ensure_indexes(connection)
        ensure_all_email_folder(connection)
        write_repository_marker(repository_path, mailbox)
        logging.info("Created new repository at %s", repository_path)
        return

    row = connection.execute(
        "SELECT value FROM repository_info WHERE key = 'username'"
    ).fetchone()
    if row is None or row["value"].casefold() != mailbox.casefold():
        raise RuntimeError(
            "The repository database does not match the configured Gmail account."
        )

    required_tables = {
        "repository_info", "folders", "messages", "message_shards",
        "folder_messages", "deleted_messages",
    }
    found = {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    missing = required_tables - found
    if missing:
        raise RuntimeError(
            "Repository database is incomplete; missing tables: "
            + ", ".join(sorted(missing))
        )

    existing_version = connection.execute(
        "SELECT value FROM repository_info WHERE key = 'schema_version'"
    ).fetchone()
    old_version = int(existing_version["value"]) if existing_version else 0
    migrate_schema(connection, repository_path, mailbox)
    ensure_indexes(connection)
    ensure_all_email_folder(connection)
    if old_version != SCHEMA_VERSION:
        write_repository_marker(repository_path, mailbox)


def _warn_if_permissive_permissions(path: Path, description: str) -> None:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            logging.warning(
                "%s permissions are broader than owner-only. Consider: chmod 600 %s",
                description, path,
            )
    except OSError:
        logging.debug("Could not inspect %s permissions.", description)


def connect_gmail_api_workspace(service_account_file: Path, mailbox: str):
    if not service_account_file.is_file():
        raise RuntimeError(f"Service-account JSON key file not found: {service_account_file}")
    _warn_if_permissive_permissions(service_account_file, "Service-account key")
    credentials = service_account.Credentials.from_service_account_file(
        str(service_account_file), scopes=[GMAIL_READONLY_SCOPE], subject=mailbox,
    )
    credentials.refresh(Request(session=TimeoutSession()))
    return _build_and_verify_gmail_service(credentials, mailbox)


def connect_gmail_api_oauth(token_file: Path, mailbox: str):
    if not token_file.is_file():
        raise RuntimeError(
            f"OAuth token file not found: {token_file}. Run --authorise on a "
            "computer with a browser, then transfer the token to this machine."
        )
    _warn_if_permissive_permissions(token_file, "OAuth token")
    from google.oauth2.credentials import Credentials
    credentials = Credentials.from_authorized_user_file(
        str(token_file), scopes=[GMAIL_READONLY_SCOPE]
    )
    if not credentials.valid:
        if credentials.expired and credentials.refresh_token:
            credentials.refresh(Request(session=TimeoutSession()))
            # Persist a rotated/refreshed token where possible.
            atomic_write_private_text(token_file, credentials.to_json())
        else:
            raise RuntimeError(
                "The personal Gmail OAuth token is invalid or has no refresh "
                "token. Re-run --authorise on a computer with a browser and "
                "transfer the new token to the Pi."
            )
    return _build_and_verify_gmail_service(credentials, mailbox)


def _build_and_verify_gmail_service(credentials, mailbox: str):
    # The discovery client's default HTTP transport can have no socket timeout.
    # Bound network waits so a stalled connection cannot hang the importer
    # indefinitely; SIGINT remains able to interrupt the active request.
    import httplib2
    from google_auth_httplib2 import AuthorizedHttp

    http = AuthorizedHttp(
        credentials,
        http=httplib2.Http(timeout=DEFAULT_HTTP_TIMEOUT_SECONDS),
    )
    service = build("gmail", "v1", http=http, cache_discovery=False)
    profile = execute_google_request(service.users().getProfile(userId="me"), "Gmail profile lookup")
    returned_mailbox = profile.get("emailAddress", "")
    if returned_mailbox.casefold() != mailbox.casefold():
        raise RuntimeError(
            "Gmail API authenticated a different mailbox than requested. "
            f"Requested {mailbox!r}; Google returned {returned_mailbox!r}."
        )
    logging.info(
        "Authenticated to Gmail API as %s (%s messages reported).",
        returned_mailbox, f"{profile.get('messagesTotal', 0):,}",
    )
    return service


def atomic_write_private_text(path: Path, contents: str) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        temporary_path.unlink(missing_ok=True)
        raise


def authorise_personal_gmail(account: str, oauth_client_file: Path, output_file: Path) -> int:
    if not account or "@" not in account:
        raise ValueError("--account must be a full Gmail address.")
    oauth_client_file = oauth_client_file.expanduser()
    if not oauth_client_file.is_file():
        raise RuntimeError(f"OAuth Desktop client JSON not found: {oauth_client_file}")
    _warn_if_permissive_permissions(oauth_client_file, "OAuth client configuration")
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise RuntimeError(
            "Personal Gmail authorisation requires google-auth-oauthlib. Install "
            "it with: python -m pip install --upgrade google-auth-oauthlib"
        ) from exc

    flow = InstalledAppFlow.from_client_secrets_file(
        str(oauth_client_file), scopes=[GMAIL_READONLY_SCOPE],
        autogenerate_code_verifier=True,
    )
    credentials = flow.run_local_server(
        host="127.0.0.1", port=0, open_browser=True,
        authorization_prompt_message="Open this URL to authorise Gmail: {url}",
        success_message="Gmail authorisation completed. You may close this browser tab.",
        access_type="offline", prompt="consent",
    )
    if not credentials.refresh_token:
        raise RuntimeError(
            "Google did not return a refresh token. Revoke this app's existing "
            "grant in your Google Account and run --authorise again."
        )
    credentials.refresh(Request(session=TimeoutSession()))
    _build_and_verify_gmail_service(credentials, account)
    atomic_write_private_text(output_file, credentials.to_json())
    print(f"Authorisation successful for {account}.")
    print(f"Token saved to: {output_file.expanduser()}")
    print("Transfer this token securely to the Raspberry Pi; do not email or publish it.")
    return 0


def connect_gmail_api(config: dict):
    if config["auth_method"] == "workspace":
        return connect_gmail_api_workspace(config["service_account_file"], config["mailbox"])
    return connect_gmail_api_oauth(config["oauth_token_file"], config["mailbox"])


def list_gmail_labels(service) -> list[dict]:
    response = execute_google_request(service.users().labels().list(userId="me"), "Gmail label listing")
    labels = response.get("labels", [])
    return sorted(labels, key=lambda item: item.get("name", "").casefold())


def get_or_create_folder(connection: sqlite3.Connection, name: str) -> int:
    now = datetime.now(timezone.utc).isoformat()
    with transaction(connection):
        connection.execute(
            """
            INSERT INTO folders(name, uidvalidity, last_seen_at)
            VALUES (?, NULL, ?)
            ON CONFLICT(name) DO UPDATE SET last_seen_at = excluded.last_seen_at
            """,
            (name, now),
        )
        row = connection.execute(
            "SELECT folder_id FROM folders WHERE name = ?", (name,)
        ).fetchone()
    return int(row["folder_id"])


def register_gmail_labels(connection: sqlite3.Connection, labels: list[dict],
                          mode: str) -> dict[str, Optional[int]]:
    """Record current Gmail labels and return their local-folder mappings.

    During the initial import, each Gmail label gets a corresponding local
    folder. During later syncs, existing mappings are retained; newly created
    Gmail labels are recorded as source metadata but do not reorganise the
    local folder tree.
    """
    mapping: dict[str, Optional[int]] = {}
    now = datetime.now(timezone.utc).isoformat()

    for label in labels:
        label_id = label["id"]
        name = label.get("name", label_id)
        label_type = label.get("type")

        row = connection.execute(
            "SELECT local_folder_id FROM gmail_labels WHERE label_id = ?",
            (label_id,),
        ).fetchone()
        local_folder_id = int(row["local_folder_id"]) if (
            row and row["local_folder_id"] is not None
        ) else None

        if local_folder_id is None and mode == "initial":
            local_folder_id = get_or_create_folder(connection, name)
        elif local_folder_id is None and mode == "sync":
            # Preserve existing v1 local folders by matching their old names.
            legacy_folder = connection.execute(
                "SELECT folder_id FROM folders WHERE name = ?", (name,)
            ).fetchone()
            if legacy_folder:
                local_folder_id = int(legacy_folder["folder_id"])

        with transaction(connection):
            connection.execute(
                """
                INSERT INTO gmail_labels(
                    label_id, name, label_type, local_folder_id, last_seen_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(label_id) DO UPDATE SET
                    name = excluded.name,
                    label_type = excluded.label_type,
                    local_folder_id = COALESCE(
                        gmail_labels.local_folder_id, excluded.local_folder_id
                    ),
                    last_seen_at = excluded.last_seen_at
                """,
                (label_id, name, label_type, local_folder_id, now),
            )
        mapping[label_id] = local_folder_id

    logging.info("Found %s Gmail labels.", len(labels))
    return mapping


def get_gmail_history_id(service) -> str:
    """Return Gmail's current opaque historyId as text."""
    response = execute_google_request(
        service.users().getProfile(userId="me"), "Gmail profile/history checkpoint"
    )
    history_id = response.get("historyId")
    if history_id is None:
        raise RuntimeError("Gmail profile response did not contain a historyId.")
    return str(history_id)


def list_gmail_message_ids(service):
    """Yield every message in Gmail's All Mail collection, excluding Spam/Trash."""
    page_token = None
    while True:
        kwargs = {
            "userId": "me",
            "maxResults": API_PAGE_SIZE,
            "includeSpamTrash": False,
        }
        if page_token:
            kwargs["pageToken"] = page_token
        response = execute_google_request(
            service.users().messages().list(**kwargs),
            "Gmail All Mail listing page",
        )
        for item in response.get("messages", []):
            message_id = item.get("id")
            if message_id:
                yield str(message_id)
        page_token = response.get("nextPageToken")
        if not page_token:
            break


def list_gmail_history(service, start_history_id: str):
    """Yield chronological Gmail history records and return the final checkpoint.

    The returned checkpoint is Gmail's current historyId from the final page
    response. It is safe to persist only after every yielded change is handled.
    """
    page_token = None
    latest_history_id = None
    while True:
        kwargs = {
            "userId": "me",
            "startHistoryId": str(start_history_id),
            "maxResults": API_PAGE_SIZE,
        }
        if page_token:
            kwargs["pageToken"] = page_token
        response = execute_google_request(
            service.users().history().list(**kwargs),
            f"Gmail history page from {start_history_id}",
        )
        if response.get("historyId") is not None:
            latest_history_id = str(response["historyId"])
        for record in response.get("history", []):
            yield record
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    if latest_history_id is None:
        # Never substitute a later profile historyId here: a change could occur
        # after the history listing and be skipped by such a checkpoint.
        raise RuntimeError(
            "Gmail history.list returned no historyId; refusing to advance "
            "the sync checkpoint. Rerun the importer."
        )
    return latest_history_id


def fetch_gmail_message(service, gm_id: str, include_raw: bool) -> dict:
    """Fetch message metadata, optionally including raw RFC 2822 content.

    Existing local messages need only metadata during a repository sync,
    avoiding repeated downloads of their potentially large message bodies.

    Incremental sync uses this only for messages affected by Gmail history;
    raw content is requested only when a message is not already local.
    """
    response_format = "raw" if include_raw else "metadata"
    message = execute_google_request(
        service.users().messages().get(
            userId="me", id=gm_id, format=response_format
        ),
        f"Gmail message {gm_id} ({response_format})",
    )

    raw_message = None
    if include_raw:
        encoded = message.get("raw")
        if not encoded:
            raise RuntimeError(f"Gmail returned no raw content for message {gm_id}.")
        try:
            padded = encoded + ("=" * (-len(encoded) % 4))
            raw_message = base64.urlsafe_b64decode(padded.encode("ascii"))
        except Exception as exc:
            raise RuntimeError(
                f"Could not base64url-decode raw message {gm_id}: {exc}"
            ) from exc
        if not raw_message:
            raise RuntimeError(f"Gmail returned an empty raw message for {gm_id}.")

    internal_date = message.get("internalDate")
    if internal_date is not None:
        try:
            internal_date = datetime.fromtimestamp(
                int(internal_date) / 1000, tz=timezone.utc
            ).isoformat()
        except (ValueError, OverflowError, OSError):
            internal_date = str(internal_date)

    return {
        "gm_id": message.get("id", gm_id),
        "thread_id": message.get("threadId"),
        "label_ids": message.get("labelIds", []),
        "internal_date": internal_date,
        "raw": raw_message,
    }


def get_or_create_shard(connection: sqlite3.Connection,
                         repository_path: Path) -> tuple[int, str]:
    row = connection.execute(
        """
        SELECT shard_id, directory_name
        FROM message_shards
        WHERE file_count < ?
        ORDER BY shard_id DESC
        LIMIT 1
        """,
        (MAX_FILES_PER_SHARD,),
    ).fetchone()

    if row is None:
        with transaction(connection):
            cursor = connection.execute(
                """
                INSERT INTO message_shards(directory_name, file_count, created_at)
                VALUES (?, 0, ?)
                """,
                ("", datetime.now(timezone.utc).isoformat()),
            )
            shard_id = int(cursor.lastrowid)
            directory_name = f"{shard_id:06d}"
            connection.execute(
                "UPDATE message_shards SET directory_name = ? WHERE shard_id = ?",
                (directory_name, shard_id),
            )
        row = {"shard_id": shard_id, "directory_name": directory_name}

    shard_id = int(row["shard_id"])
    directory_name = str(row["directory_name"])
    (repository_path / "messages" / directory_name).mkdir(
        parents=True, exist_ok=True
    )
    return shard_id, directory_name


def refresh_source_labels(connection: sqlite3.Connection, message_id: int,
                          label_ids: list[str], now: str) -> None:
    """Replace the stored source-label snapshot for one message."""
    previous = {
        row["label_id"]: row["first_seen_at"]
        for row in connection.execute(
            "SELECT label_id, first_seen_at FROM gmail_message_labels "
            "WHERE message_id = ?",
            (message_id,),
        )
    }
    connection.execute(
        "DELETE FROM gmail_message_labels WHERE message_id = ?", (message_id,)
    )
    for label_id in label_ids:
        exists = connection.execute(
            "SELECT 1 FROM gmail_labels WHERE label_id = ?", (label_id,)
        ).fetchone()
        if exists:
            connection.execute(
                """
                INSERT INTO gmail_message_labels(
                    message_id, label_id, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?)
                """,
                (message_id, label_id, previous.get(label_id, now), now),
            )


def sync_gmail_label_folder_memberships(
    connection: sqlite3.Connection,
    message_id: int,
    label_ids: list[str],
    label_mapping: dict[str, Optional[int]],
    all_email_folder_id: int,
    now: str,
) -> None:
    """Synchronise mapped local folder memberships with current Gmail labels.

    Only folders mapped to Gmail labels are managed here. Local-only folders
    are left untouched. ALL EMAIL is explicitly protected and always retained.
    """
    desired_folder_ids = {
        label_mapping[label_id]
        for label_id in label_ids
        if label_mapping.get(label_id) is not None
    }

    # ALL EMAIL is permanent ownership, not a removable Gmail-label membership.
    desired_folder_ids.discard(all_email_folder_id)

    # These are the folders whose memberships are managed by Gmail labels.
    managed_folder_ids = {
        int(row["local_folder_id"])
        for row in connection.execute(
            """
            SELECT DISTINCT local_folder_id
            FROM gmail_labels
            WHERE local_folder_id IS NOT NULL
            """
        )
    }

    managed_folder_ids.discard(all_email_folder_id)

    for folder_id in managed_folder_ids:
        if folder_id in desired_folder_ids:
            connection.execute(
                """
                INSERT OR IGNORE INTO folder_messages(
                    folder_id, message_id, gmail_uid, first_seen_at
                ) VALUES (?, ?, NULL, ?)
                """,
                (folder_id, message_id, now),
            )
        else:
            connection.execute(
                """
                DELETE FROM folder_messages
                WHERE folder_id = ? AND message_id = ?
                """,
                (folder_id, message_id),
            )

    # Repair missing ALL EMAIL membership without disturbing other folders.
    connection.execute(
        """
        INSERT OR IGNORE INTO folder_messages(
            folder_id, message_id, gmail_uid, first_seen_at
        ) VALUES (?, ?, NULL, ?)
        """,
        (all_email_folder_id, message_id, now),
    )


def import_one_message(connection: sqlite3.Connection, repository_path: Path,
                        message: dict, label_mapping: dict[str, Optional[int]],
                        mode: str, all_email_folder_id: int) -> bool:
    gm_id = str(message["gm_id"])
    tombstone = connection.execute(
        "SELECT 1 FROM deleted_messages WHERE gm_id = ?", (gm_id,)
    ).fetchone()
    if tombstone:
        return False

    existing = connection.execute(
        "SELECT message_id FROM messages WHERE gm_id = ?", (gm_id,)
    ).fetchone()
    now = datetime.now(timezone.utc).isoformat()
    label_ids = list(dict.fromkeys(message.get("label_ids", [])))

    if existing:
        # Update only the source-side Gmail label snapshot. Do not change local
        # folder memberships or local read/starred state on incremental sync.
        with transaction(connection):
            refresh_source_labels(
                connection, int(existing["message_id"]), label_ids, now
            )
        return False

    raw_message = message["raw"]
    digest = hashlib.sha256(raw_message).hexdigest()
    shard_id, directory_name = get_or_create_shard(connection, repository_path)
    filename = f"{quote(gm_id, safe='')}.eml"
    shard_path = repository_path / "messages" / directory_name
    final_path = shard_path / filename

    if final_path.exists():
        raise RuntimeError(
            f"Refusing to overwrite unexpected existing message file: {final_path}"
        )

    check_disk_space(repository_path)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{gm_id}.", suffix=".tmp", dir=shard_path
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw_message)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, final_path)

        try:
            with transaction(connection):
                cursor = connection.execute(
                    """
                    INSERT INTO messages(
                        gm_id, gmail_thread_id, shard_id, filename,
                        message_size, sha256, internal_date, imported_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        gm_id, message.get("thread_id"), shard_id, filename,
                        len(raw_message), digest, message.get("internal_date"), now,
                    ),
                )
                message_id = int(cursor.lastrowid)

                refresh_source_labels(
                    connection, message_id, label_ids, now
                )

                # ALL EMAIL is an ordinary persisted folder. Every new
                # canonical message is linked to it in the same transaction.
                connection.execute(
                    """
                    INSERT OR IGNORE INTO folder_messages(
                        folder_id, message_id, gmail_uid, first_seen_at
                    ) VALUES (?, ?, NULL, ?)
                    """,
                    (all_email_folder_id, message_id, now),
                )

                # For both initial and incremental imports, a newly imported
                # message is placed in the local folders mapped to its labels.
                for label_id in label_ids:
                    folder_id = label_mapping.get(label_id)
                    if folder_id is not None:
                        connection.execute(
                            """
                            INSERT OR IGNORE INTO folder_messages(
                                folder_id, message_id, gmail_uid, first_seen_at
                            ) VALUES (?, ?, NULL, ?)
                            """,
                            (folder_id, message_id, now),
                        )

                connection.execute(
                    """
                    UPDATE message_shards
                    SET file_count = file_count + 1
                    WHERE shard_id = ?
                    """,
                    (shard_id,),
                )
        except BaseException:
            final_path.unlink(missing_ok=True)
            raise
    except BaseException as exc:
        temporary_path.unlink(missing_ok=True)
        if is_disk_full_error(exc):
            raise DiskSpaceExhausted(
                "The filesystem ran out of space while saving a message or "
                "updating SQLite. The current message was not committed; "
                "successfully completed earlier messages remain in the repository. "
                "Free space and run the importer again to resume."
            ) from exc
        raise

    return True


def is_gmail_not_found(exc: HttpError) -> bool:
    return getattr(getattr(exc, "resp", None), "status", None) == 404


def save_history_checkpoint(connection: sqlite3.Connection, mailbox: str,
                            history_id: str) -> None:
    """Atomically persist the opaque Gmail historyId after a completed sync."""
    now = datetime.now(timezone.utc).isoformat()
    with transaction(connection):
        connection.execute(
            """INSERT INTO sync_state(account_id, history_id, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(account_id) DO UPDATE SET
                   history_id = excluded.history_id,
                   updated_at = excluded.updated_at""",
            (mailbox.casefold(), str(history_id), now),
        )


def get_saved_history_checkpoint(connection: sqlite3.Connection,
                                 mailbox: str) -> Optional[str]:
    row = connection.execute(
        "SELECT history_id FROM sync_state WHERE account_id = ?",
        (mailbox.casefold(),),
    ).fetchone()
    return str(row["history_id"]) if row else None


def process_changed_message(connection: sqlite3.Connection, service,
                             repository_path: Path, gm_id: str,
                             label_mapping: dict[str, Optional[int]], mode: str,
                             all_email_folder_id: int) -> bool:
    """Refresh a changed message and synchronise its Gmail-mapped folders."""
    if connection.execute(
        "SELECT 1 FROM deleted_messages WHERE gm_id = ?", (gm_id,)
    ).fetchone():
        return False

    existing = connection.execute(
        "SELECT message_id FROM messages WHERE gm_id = ?", (gm_id,)
    ).fetchone()
    try:
        metadata = fetch_gmail_message(service, gm_id, include_raw=False)
    except HttpError as exc:
        # Gmail may have deleted the message after the history event was written.
        # A remote deletion never removes an already-local copy.
        if is_gmail_not_found(exc):
            return False
        raise

    labels = set(metadata.get("label_ids", []))

    if existing:
        # Keep the canonical local message regardless of remote Spam/Trash
        # placement. Refresh its source labels and synchronise its Gmail-mapped
        # local folder memberships.
        now = datetime.now(timezone.utc).isoformat()
        message_id = int(existing["message_id"])
        label_ids = list(dict.fromkeys(metadata.get("label_ids", [])))

        with transaction(connection):
            refresh_source_labels(
                connection,
                message_id,
                label_ids,
                now,
            )
            sync_gmail_label_folder_memberships(
                connection,
                message_id,
                label_ids,
                label_mapping,
                all_email_folder_id,
                now,
            )
        return False

    if "SPAM" in labels or "TRASH" in labels:
        # Match the initial All Mail scan for messages not already local.
        return False

    # A message added during the initial scan, or moved out of Spam/Trash,
    # may not yet have a local record. Fetch its raw content exactly once.
    try:
        message = fetch_gmail_message(service, gm_id, include_raw=True)
    except HttpError as exc:
        if is_gmail_not_found(exc):
            return False
        raise

    return import_one_message(
        connection,
        repository_path,
        message,
        label_mapping,
        mode,
        all_email_folder_id,
    )


def process_history(connection: sqlite3.Connection, service,
                     repository_path: Path, start_history_id: str,
                     label_mapping: dict[str, Optional[int]], mode: str,
                     all_email_folder_id: int) -> tuple[str, int]:
    """Apply all history changes since start_history_id, then return checkpoint."""
    changed_ids: dict[str, None] = {}
    ignored_deletions = 0
    records = 0
    # Gmail returns history records in chronological order. Coalescing changed
    # IDs lets us refresh each message from its final current state only once.
    # messagesDeleted are deliberately not acted upon: local data is authoritative.
    generator = list_gmail_history(service, start_history_id)
    while True:
        try:
            record = next(generator)
        except StopIteration as stop:
            latest_history_id = stop.value
            break
        records += 1
        for item in record.get("messagesAdded", []):
            message = item.get("message", {})
            if message.get("id"):
                changed_ids[str(message["id"])] = None
        for item in record.get("labelsAdded", []):
            message = item.get("message", {})
            if message.get("id"):
                changed_ids[str(message["id"])] = None
        for item in record.get("labelsRemoved", []):
            message = item.get("message", {})
            if message.get("id"):
                changed_ids[str(message["id"])] = None
        ignored_deletions += len(record.get("messagesDeleted", []))

    # Refresh the label catalogue after collecting history so label IDs first
    # seen during this sync are available when message snapshots are written.
    current_labels = list_gmail_labels(service)
    refreshed_mapping = register_gmail_labels(connection, current_labels, "sync")
    label_mapping.clear()
    label_mapping.update(refreshed_mapping)

    imported = 0
    for index, gm_id in enumerate(changed_ids, start=1):
        check_disk_space(repository_path)
        if process_changed_message(
            connection, service, repository_path, gm_id, label_mapping,
            mode, all_email_folder_id,
        ):
            imported += 1
        if index % 10 == 0:
            sys.stdout.write(
                f"\rHistory: processed {index:,} changed message(s) | "
                f"newly imported {imported:,}"
            )
            sys.stdout.flush()
    if changed_ids and len(changed_ids) % 10:
        sys.stdout.write(
            f"\rHistory: processed {len(changed_ids):,} changed message(s) | "
            f"newly imported {imported:,}"
        )
        sys.stdout.flush()
    if changed_ids:
        sys.stdout.write("\n")
        sys.stdout.flush()

    logging.info(
        "Processed Gmail history: %s record(s), %s distinct changed message(s), "
        "%s remote deletion event(s) ignored; checkpoint %s.",
        f"{records:,}", f"{len(changed_ids):,}",
        f"{ignored_deletions:,}", latest_history_id,
    )
    return latest_history_id, imported


def process_scanned_message(connection: sqlite3.Connection, service,
                            repository_path: Path, gm_id: str,
                            label_mapping: dict[str, Optional[int]], mode: str,
                            all_email_folder_id: int) -> bool:
    """Import/update one message returned by the All Mail list without a duplicate fetch."""
    if connection.execute(
        "SELECT 1 FROM deleted_messages WHERE gm_id = ?", (gm_id,)
    ).fetchone():
        return False
    existing = connection.execute(
        "SELECT 1 FROM messages WHERE gm_id = ?", (gm_id,)
    ).fetchone()
    try:
        message = fetch_gmail_message(service, gm_id, include_raw=(existing is None))
    except HttpError as exc:
        # A message can disappear between messages.list and messages.get.
        # This is not an import failure and must not prevent checkpointing.
        if is_gmail_not_found(exc):
            return False
        raise
    return import_one_message(
        connection, repository_path, message, label_mapping, mode,
        all_email_folder_id,
    )


def full_all_mail_scan(connection: sqlite3.Connection, service,
                       repository_path: Path, label_mapping: dict,
                       mode: str, all_email_folder_id: int) -> tuple[int, int]:
    """Full paginated All Mail scan used only for first sync or recovery."""
    total_imported = 0
    total_failed = 0
    total_seen = 0
    progress_line = ""
    try:
        check_disk_space(repository_path)
        for gm_id in list_gmail_message_ids(service):
            check_disk_space(repository_path)
            total_seen += 1
            try:
                if process_scanned_message(
                    connection, service, repository_path, gm_id,
                    label_mapping, mode, all_email_folder_id,
                ):
                    total_imported += 1
            except (DiskSpaceExhausted, GmailRateLimitExhausted):
                raise
            except Exception as exc:
                if is_disk_full_error(exc):
                    raise DiskSpaceExhausted(
                        "The filesystem ran out of space during import. "
                        "Completed messages remain saved; free space and rerun."
                    ) from exc
                total_failed += 1
                logging.exception("Failed importing Gmail message %s", gm_id)
            if total_seen % 10 == 0:
                progress_line = (
                    f"Progress: checked {total_seen:,} | "
                    f"imported {total_imported:,} | failed {total_failed:,}"
                )
                sys.stdout.write("\r" + progress_line.ljust(100))
                sys.stdout.flush()
    finally:
        if total_seen and total_seen % 10:
            progress_line = (
                f"Progress: checked {total_seen:,} | "
                f"imported {total_imported:,} | failed {total_failed:,}"
            )
            sys.stdout.write("\r" + progress_line.ljust(100))
            sys.stdout.flush()
        if progress_line:
            sys.stdout.write("\n")
            sys.stdout.flush()
    logging.info(
        "Completed full All Mail scan: checked %s; newly imported %s; failures %s",
        f"{total_seen:,}", f"{total_imported:,}", f"{total_failed:,}",
    )
    return total_imported, total_failed


def sync_gmail(connection: sqlite3.Connection, service,
               repository_path: Path, mode: str, labels: list[dict],
               mailbox: str) -> tuple[int, int]:
    label_mapping = register_gmail_labels(connection, labels, mode)
    all_email_row = connection.execute(
        "SELECT folder_id FROM folders WHERE name = ?",
        (ALL_EMAIL_FOLDER_NAME,),
    ).fetchone()
    if all_email_row is None:
        raise RuntimeError(
            "The ALL EMAIL folder is missing. Repository initialisation failed."
        )
    all_email_folder_id = int(all_email_row["folder_id"])

    saved_history_id = get_saved_history_checkpoint(connection, mailbox)
    full_scan_required = saved_history_id is None
    recovery_reason = "initial import" if full_scan_required else None
    total_imported = 0
    total_failed = 0

    # Initial sync and expired-history recovery both establish a fresh starting
    # point BEFORE the full scan, then replay everything that changed meanwhile.
    while True:
        if full_scan_required:
            start_history_id = get_gmail_history_id(service)
            logging.info(
                "Starting full All Mail scan (%s); baseline historyId=%s.",
                recovery_reason or "history recovery", start_history_id,
            )
            imported, failed = full_all_mail_scan(
                connection, service, repository_path, label_mapping,
                mode, all_email_folder_id,
            )
            total_imported += imported
            total_failed += failed
            if failed:
                logging.error(
                    "Not saving a Gmail history checkpoint because the full scan "
                    "had %s failed message(s). Rerun to retry.", failed,
                )
                return total_imported, total_failed
        else:
            start_history_id = str(saved_history_id)

        try:
            latest_history_id, history_imported = process_history(
                connection, service, repository_path, start_history_id,
                label_mapping, mode, all_email_folder_id,
            )
            total_imported += history_imported
        except HttpError as exc:
            if is_gmail_not_found(exc) and not full_scan_required:
                logging.warning(
                    "Gmail historyId %s has expired; preserving the local "
                    "repository and rebuilding from a fresh All Mail scan.",
                    start_history_id,
                )
                full_scan_required = True
                recovery_reason = "expired history recovery"
                saved_history_id = None
                continue
            if is_gmail_not_found(exc) and full_scan_required:
                # An unusually long full scan can outlive the history window.
                # Retry once from a newer baseline rather than committing a gap.
                if recovery_reason == "history recovery retry":
                    raise RuntimeError(
                        "Gmail history expired repeatedly during full-scan recovery. "
                        "No checkpoint was saved; rerun the importer."
                    ) from exc
                logging.warning(
                    "History expired during the full scan; restarting with a "
                    "new baseline to avoid missing changes."
                )
                recovery_reason = "history recovery retry"
                continue
            raise

        save_history_checkpoint(connection, mailbox, latest_history_id)
        logging.info("Saved Gmail history checkpoint %s.", latest_history_id)
        break

    return total_imported, total_failed


def delete_local_message(connection: sqlite3.Connection,
                         repository_path: Path, gm_id: str,
                         reason: str = "local deletion") -> bool:
    """Permanently remove one active message and create a Gmail-ID tombstone.

    Offline backup copies are unaffected. The rename makes a failed database
    transaction recoverable by restoring the original filename.
    """
    row = connection.execute(
        """
        SELECT m.message_id, m.shard_id, m.filename, s.directory_name
        FROM messages m
        JOIN message_shards s ON s.shard_id = m.shard_id
        WHERE m.gm_id = ?
        """,
        (gm_id,),
    ).fetchone()
    if row is None:
        with transaction(connection):
            connection.execute(
                """
                INSERT INTO deleted_messages(gm_id, deleted_at, reason)
                VALUES (?, ?, ?)
                ON CONFLICT(gm_id) DO UPDATE SET
                    deleted_at = excluded.deleted_at,
                    reason = excluded.reason
                """,
                (gm_id, datetime.now(timezone.utc).isoformat(), reason),
            )
        return False

    original_path = (
        repository_path / "messages" / row["directory_name"] / row["filename"]
    )
    if not original_path.is_file():
        raise RuntimeError(
            f"Cannot delete {gm_id}: its repository file is missing: {original_path}"
        )

    quarantine_path = original_path.with_name(original_path.name + ".deleting")
    if quarantine_path.exists():
        raise RuntimeError(f"Deletion recovery file already exists: {quarantine_path}")
    os.replace(original_path, quarantine_path)
    try:
        with transaction(connection):
            connection.execute(
                """
                INSERT INTO deleted_messages(gm_id, deleted_at, reason)
                VALUES (?, ?, ?)
                ON CONFLICT(gm_id) DO UPDATE SET
                    deleted_at = excluded.deleted_at,
                    reason = excluded.reason
                """,
                (gm_id, datetime.now(timezone.utc).isoformat(), reason),
            )
            connection.execute(
                "DELETE FROM folder_messages WHERE message_id = ?",
                (int(row["message_id"]),),
            )
            connection.execute(
                "DELETE FROM gmail_message_labels WHERE message_id = ?",
                (int(row["message_id"]),),
            )
            connection.execute(
                "DELETE FROM messages WHERE message_id = ?",
                (int(row["message_id"]),),
            )
            connection.execute(
                "UPDATE message_shards SET file_count = file_count - 1 "
                "WHERE shard_id = ? AND file_count > 0",
                (int(row["shard_id"]),),
            )
    except Exception:
        os.replace(quarantine_path, original_path)
        raise

    quarantine_path.unlink(missing_ok=True)
    return True


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_repository_files(connection: sqlite3.Connection,
                            repository_path: Path) -> None:
    """Check DB/file consistency; never silently delete or rewrite files."""
    all_email_row = connection.execute(
        "SELECT folder_id FROM folders WHERE name = ?",
        (ALL_EMAIL_FOLDER_NAME,),
    ).fetchone()
    if all_email_row is None:
        raise RuntimeError("Repository integrity check failed: ALL EMAIL folder is missing.")

    missing_all_email = connection.execute(
        """
        SELECT COUNT(*) AS missing_count
        FROM messages m
        LEFT JOIN folder_messages fm
          ON fm.message_id = m.message_id AND fm.folder_id = ?
        WHERE fm.message_id IS NULL
        """,
        (int(all_email_row["folder_id"]),),
    ).fetchone()["missing_count"]
    if int(missing_all_email):
        raise RuntimeError(
            "Repository integrity check failed: "
            f"{int(missing_all_email):,} message(s) are missing ALL EMAIL membership."
        )

    missing = []
    rows = connection.execute(
        """
        SELECT m.gm_id, s.directory_name, m.filename, m.message_size, m.sha256
        FROM messages m
        JOIN message_shards s ON s.shard_id = m.shard_id
        """
    )
    known_paths = set()
    for row in rows:
        path = (
            repository_path / "messages" /
            row["directory_name"] / row["filename"]
        )
        known_paths.add((row["directory_name"], row["filename"]))
        if not path.is_file():
            missing.append((row["gm_id"], str(path)))
            continue
        if path.stat().st_size != int(row["message_size"]):
            raise RuntimeError(
                f"Repository integrity check found a size mismatch for "
                f"Gmail ID {row['gm_id']}: {path}"
            )
        digest = sha256_file(path)
        if digest != row["sha256"]:
            raise RuntimeError(
                f"Repository integrity check found a SHA-256 mismatch for "
                f"Gmail ID {row['gm_id']}: {path}"
            )

    if missing:
        examples = "\n".join(
            f"  Gmail ID {gm_id}: {path}" for gm_id, path in missing[:10]
        )
        raise RuntimeError(
            f"Repository integrity check found {len(missing)} missing message "
            f"file(s). No sync was started. Examples:\n{examples}"
        )

    orphaned = []
    messages_root = repository_path / "messages"
    for shard_dir in messages_root.iterdir():
        if not shard_dir.is_dir():
            orphaned.append(str(shard_dir))
            continue
        for item in shard_dir.iterdir():
            if item.name.endswith(".eml") and (
                shard_dir.name, item.name
            ) not in known_paths:
                orphaned.append(str(item))
            elif item.name.endswith(".deleting") or item.name.endswith(".tmp"):
                orphaned.append(str(item))

    if orphaned:
        raise RuntimeError(
            "Repository integrity check found untracked or interrupted message "
            "files. No sync was started. Inspect these paths before proceeding:\n  "
            + "\n  ".join(orphaned[:20])
        )

    with transaction(connection):
        counts = connection.execute(
            """
            SELECT s.shard_id, COUNT(m.message_id) AS actual_count
            FROM message_shards s
            LEFT JOIN messages m ON m.shard_id = s.shard_id
            GROUP BY s.shard_id
            """
        ).fetchall()
        for row in counts:
            actual_count = int(row["actual_count"])
            if actual_count > MAX_FILES_PER_SHARD:
                raise RuntimeError(
                    f"Shard {row['shard_id']} contains {actual_count} records; "
                    f"the limit is {MAX_FILES_PER_SHARD}."
                )
            connection.execute(
                "UPDATE message_shards SET file_count = ? WHERE shard_id = ?",
                (actual_count, int(row["shard_id"])),
            )


def main() -> int:
    parser = build_argument_parser()
    args = parser.parse_args()  # --help works without requiring environment variables.

    connection = None
    try:
        if args.authorise:
            if not args.account or not args.oauth_client_file or not args.output:
                parser.error("--authorise requires --account, --oauth-client-file and --output")
            return authorise_personal_gmail(args.account, args.oauth_client_file, args.output)
        if args.account or args.oauth_client_file or args.output:
            parser.error("--account, --oauth-client-file and --output are only used with --authorise")

        config = get_configuration()
        repository_path = config["repository_path"]
        mode = detect_repository_mode(repository_path)

        # Authenticate and retrieve the label catalogue before creating a new
        # repository. A failed Google setup therefore leaves no half-created
        # repository that could be mistaken for an established incremental sync.
        service = connect_gmail_api(config)
        labels = list_gmail_labels(service)

        repository_path.mkdir(parents=True, exist_ok=True)
        connection = connect_database(config["database_path"])
        ensure_repository(
            connection, repository_path, config["mailbox"], mode
        )
        if mode == "sync":
            verify_repository_files(connection, repository_path)

        logging.info(
            "Import mode: %s",
            "initial import" if mode == "initial" else "incremental sync",
        )
        logging.info("Repository: %s", repository_path)

        check_disk_space(repository_path)
        total_imported, total_failed = sync_gmail(
            connection, service, repository_path, mode, labels, config["mailbox"]
        )

        logging.info(
            "Finished: newly imported %s message(s); failed %s message(s).",
            f"{total_imported:,}", f"{total_failed:,}",
        )
        return 1 if total_failed else 0

    except KeyboardInterrupt:
        logging.warning(
            "Interrupted by user (Ctrl-C). The importer is stopping; "
            "completed messages remain saved and the next run can resume."
        )
        return 130
    except GmailRateLimitExhausted as exc:
        logging.error("%s", exc)
        return 3
    except DiskSpaceExhausted as exc:
        logging.error("DISK SPACE: %s", exc)
        return 4
    except HttpError as exc:
        logging.error("Google API error: %s", exc)
        return 2
    except (ValueError, RuntimeError, OSError, sqlite3.Error) as exc:
        if is_disk_full_error(exc):
            logging.error(
                "DISK SPACE: the filesystem could not complete a write. "
                "The importer has stopped. Free space and run it again to resume."
            )
            return 4
        logging.error("%s", exc)
        return 2
    except Exception as exc:
        logging.error("Authentication or importer failure (%s): %s", type(exc).__name__, exc)
        return 2
    finally:
        if connection is not None:
            connection.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    raise SystemExit(main())
