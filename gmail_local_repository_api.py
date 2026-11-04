#!/usr/bin/env python3
"""
Gmail-to-local email repository importer.

The local repository is authoritative. Gmail is only an import source:
- messages are stored once, as the raw RFC 2822 bytes returned by Gmail's API;
- Gmail labels are recorded separately as source metadata;
- initial Gmail labels are copied into local folder memberships;
- later Gmail label changes do not reorganise existing local messages;
- later Gmail deletions never delete local data;
- locally deleted Gmail messages are suppressed by a tombstone.

Authentication uses a Google Workspace service account with domain-wide
delegation. It is designed to run headlessly on a Linux host.

Configuration is supplied through environment variables. Run with --help for
the required variables and usage information.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
import sqlite3
import stat
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from google.auth.transport.requests import Request
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


APP_NAME = "Gmail Local Repository Importer"
SCHEMA_VERSION = 2
MAX_FILES_PER_SHARD = 500
DEFAULT_DB_LOCATION = "./data"
REPOSITORY_MARKER = "repository.json"
DATABASE_FILENAME = "repository.db"
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
API_PAGE_SIZE = 500


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=Path(sys.argv[0]).name,
        description=(
            "Import Gmail messages into an authoritative local repository. "
            "The first run creates a repository; later runs import previously "
            "unseen messages without overwriting local organisation."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""\
Environment variables:
  GOOGLE_SERVICE_ACCOUNT_FILE  Path to the Workspace service-account JSON key
                               (required)
  GMAIL_IMPERSONATE_USER       Workspace mailbox address to import (required)
  DB_LOCATION                  Parent directory for repositories
                               (optional; default: {DEFAULT_DB_LOCATION})

The service account must have Workspace domain-wide delegation enabled and
must be authorised for this exact scope:
  {GMAIL_READONLY_SCOPE}

The repository is created at:
  DB_LOCATION/<encoded-mailbox-address>/

It contains {DATABASE_FILENAME}, {REPOSITORY_MARKER}, and a messages/
directory. Each messages shard contains no more than
{MAX_FILES_PER_SHARD} RFC822 files.

Examples:
  export GOOGLE_SERVICE_ACCOUNT_FILE="$HOME/.config/gmail-local-repository/service-account.json"
  export GMAIL_IMPERSONATE_USER="you@yourdomain.com"
  export DB_LOCATION="./data"
  {Path(sys.argv[0]).name}

Import behaviour:
  - A missing or empty user repository directory starts an initial import.
  - A recognised repository starts an incremental sync.
  - An unrecognised or incomplete non-empty directory is rejected.
  - Gmail deletions and label removals do not delete local messages.
  - Gmail label changes are recorded separately from local folder membership.
  - Local deletion tombstones suppress future re-imports.
""",
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
    mailbox = required_environment("GMAIL_IMPERSONATE_USER")
    key_file = Path(
        required_environment("GOOGLE_SERVICE_ACCOUNT_FILE")
    ).expanduser()
    db_location = Path(
        os.environ.get("DB_LOCATION", DEFAULT_DB_LOCATION)
    ).expanduser()
    repository_path = db_location / safe_repository_name(mailbox)
    return {
        "mailbox": mailbox,
        "service_account_file": key_file,
        "db_location": db_location,
        "repository_path": repository_path,
        "database_path": repository_path / DATABASE_FILENAME,
    }


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
    if version not in (1, SCHEMA_VERSION):
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
    except Exception:
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


def migrate_schema(connection: sqlite3.Connection, repository_path: Path,
                   mailbox: str) -> None:
    """Migrate the previous IMAP schema (v1) without changing stored messages."""
    row = connection.execute(
        "SELECT value FROM repository_info WHERE key = 'schema_version'"
    ).fetchone()
    if row is None:
        raise RuntimeError("Repository database has no schema-version metadata.")
    version = int(row["value"])
    if version == SCHEMA_VERSION:
        return
    if version != 1:
        raise RuntimeError(f"No migration path from repository schema {version}.")

    with transaction(connection):
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS gmail_labels (
                label_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                label_type TEXT,
                local_folder_id INTEGER,
                last_seen_at TEXT NOT NULL,
                FOREIGN KEY(local_folder_id) REFERENCES folders(folder_id)
            );
            CREATE TABLE IF NOT EXISTS gmail_message_labels (
                message_id INTEGER NOT NULL,
                label_id TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                PRIMARY KEY(message_id, label_id),
                FOREIGN KEY(message_id) REFERENCES messages(message_id),
                FOREIGN KEY(label_id) REFERENCES gmail_labels(label_id)
            );
            CREATE INDEX IF NOT EXISTS idx_gmail_message_labels_label
                ON gmail_message_labels(label_id);
            """
        )
        connection.execute(
            "UPDATE repository_info SET value = ? WHERE key = 'schema_version'",
            (str(SCHEMA_VERSION),),
        )
    logging.info(
        "Migrated repository database from schema version 1 to %s; "
        "existing message files were not changed.",
        SCHEMA_VERSION,
    )


def ensure_repository(connection: sqlite3.Connection, repository_path: Path,
                      mailbox: str, mode: str) -> None:
    if mode == "initial":
        repository_path.mkdir(parents=True, exist_ok=True)
        (repository_path / "messages").mkdir(exist_ok=True)
        create_schema(connection, mailbox)
        write_repository_marker(repository_path, mailbox)
        logging.info("Created new repository at %s", repository_path)
        return

    row = connection.execute(
        "SELECT value FROM repository_info WHERE key = 'username'"
    ).fetchone()
    if row is None or row["value"].casefold() != mailbox.casefold():
        raise RuntimeError(
            "The repository database does not match GMAIL_IMPERSONATE_USER."
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
    if old_version != SCHEMA_VERSION:
        write_repository_marker(repository_path, mailbox)


def connect_gmail_api(service_account_file: Path, mailbox: str):
    if not service_account_file.is_file():
        raise RuntimeError(
            f"Service-account JSON key file not found: {service_account_file}"
        )

    try:
        mode = stat.S_IMODE(service_account_file.stat().st_mode)
        if mode & 0o077:
            logging.warning(
                "Service-account key permissions are broader than owner-only. "
                "Consider: chmod 600 %s",
                service_account_file,
            )
    except OSError:
        logging.debug("Could not inspect service-account key permissions.")

    credentials = service_account.Credentials.from_service_account_file(
        str(service_account_file),
        scopes=[GMAIL_READONLY_SCOPE],
        subject=mailbox,
    )
    credentials.refresh(Request())

    service = build(
        "gmail",
        "v1",
        credentials=credentials,
        cache_discovery=False,
    )
    profile = service.users().getProfile(userId="me").execute()
    returned_mailbox = profile.get("emailAddress", "")
    if returned_mailbox.casefold() != mailbox.casefold():
        raise RuntimeError(
            "Gmail API authenticated a different mailbox than requested. "
            f"Requested {mailbox!r}; Google returned {returned_mailbox!r}."
        )
    logging.info(
        "Authenticated to Gmail API as %s (%s messages reported).",
        returned_mailbox,
        f"{profile.get('messagesTotal', 0):,}",
    )
    return service


def list_gmail_labels(service) -> list[dict]:
    response = service.users().labels().list(userId="me").execute()
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


def list_gmail_message_ids(service):
    """Yield every Gmail message ID, following all API pagination tokens."""
    page_token = None
    while True:
        kwargs = {
            "userId": "me",
            "maxResults": API_PAGE_SIZE,
            "includeSpamTrash": True,
        }
        if page_token:
            kwargs["pageToken"] = page_token
        response = service.users().messages().list(**kwargs).execute()
        for item in response.get("messages", []):
            message_id = item.get("id")
            if message_id:
                yield message_id
        page_token = response.get("nextPageToken")
        if not page_token:
            break


def fetch_gmail_message(service, gm_id: str, include_raw: bool) -> dict:
    """Fetch message metadata, optionally including its raw RFC 2822 content.

    Existing local messages need only metadata on incremental sync, avoiding
    repeated downloads of their potentially large message bodies.
    """
    response_format = "raw" if include_raw else "metadata"
    message = service.users().messages().get(
        userId="me", id=gm_id, format=response_format
    ).execute()

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


def import_one_message(connection: sqlite3.Connection, repository_path: Path,
                        message: dict, label_mapping: dict[str, Optional[int]],
                        mode: str) -> bool:
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
        except Exception:
            final_path.unlink(missing_ok=True)
            raise
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise

    return True


def sync_gmail(connection: sqlite3.Connection, service,
               repository_path: Path, mode: str,
               labels: list[dict]) -> tuple[int, int]:
    label_mapping = register_gmail_labels(connection, labels, mode)

    total_imported = 0
    total_failed = 0
    total_seen = 0

    # Update one terminal line rather than emitting a log entry for every
    # batch. Gmail's API does not provide a reliable total while paginating,
    # so show the live checked/imported/failed counts without a percentage.
    progress_line = ""
    try:
        for gm_id in list_gmail_message_ids(service):
            total_seen += 1
            try:
                tombstone = connection.execute(
                    "SELECT 1 FROM deleted_messages WHERE gm_id = ?", (gm_id,)
                ).fetchone()
                if not tombstone:
                    existing = connection.execute(
                        "SELECT 1 FROM messages WHERE gm_id = ?", (gm_id,)
                    ).fetchone()
                    message = fetch_gmail_message(
                        service, gm_id, include_raw=(existing is None)
                    )
                    if import_one_message(
                        connection, repository_path, message, label_mapping, mode
                    ):
                        total_imported += 1
            except HttpError as exc:
                total_failed += 1
                logging.error(
                    "Gmail API failed retrieving message %s: %s", gm_id, exc
                )
            except Exception:
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
        "Completed Gmail scan: checked %s; newly imported %s; failures %s",
        f"{total_seen:,}", f"{total_imported:,}", f"{total_failed:,}",
    )
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
    parser.parse_args()  # --help works without requiring environment variables.

    connection = None
    try:
        config = get_configuration()
        repository_path = config["repository_path"]
        mode = detect_repository_mode(repository_path)

        # Authenticate and retrieve the label catalogue before creating a new
        # repository. A failed Google setup therefore leaves no half-created
        # repository that could be mistaken for an established incremental sync.
        service = connect_gmail_api(
            config["service_account_file"], config["mailbox"]
        )
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

        total_imported, total_failed = sync_gmail(
            connection, service, repository_path, mode, labels
        )

        logging.info(
            "Finished: newly imported %s message(s); failed %s message(s).",
            f"{total_imported:,}", f"{total_failed:,}",
        )
        return 1 if total_failed else 0

    except HttpError as exc:
        logging.error("Google API error: %s", exc)
        return 2
    except (ValueError, RuntimeError, OSError, sqlite3.Error) as exc:
        logging.error("%s", exc)
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
