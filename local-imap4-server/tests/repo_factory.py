"""Build small synthetic repositories (repository.json + repository.db + messages/) for tests.

The tables and columns are the ones imap_archive_server.py reads (see AGENTS.md, section 2).
Standard library only.
"""
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE folders(folder_id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL);
CREATE TABLE message_shards(shard_id INTEGER PRIMARY KEY, directory_name TEXT NOT NULL);
CREATE TABLE messages(
    message_id INTEGER PRIMARY KEY, gm_id TEXT, shard_id INTEGER NOT NULL,
    filename TEXT NOT NULL, message_size INTEGER NOT NULL, sha256 TEXT NOT NULL,
    internal_date TEXT, imported_at TEXT, is_read INTEGER DEFAULT 0, is_starred INTEGER DEFAULT 0);
CREATE TABLE folder_messages(folder_id INTEGER NOT NULL, message_id INTEGER NOT NULL);
"""


def simple_message(n, subject=None, sender="alice@example.com", body=None):
    """A small CRLF text message."""
    subject = subject or f"message {n}"
    body = body if body is not None else f"body of message {n}"
    return (f"From: {sender}\r\nTo: bob@example.com\r\nSubject: {subject}\r\n"
            f"Message-ID: <{n}@example.com>\r\nDate: Wed, 17 Jul 2024 02:44:25 -0700\r\n"
            f"MIME-Version: 1.0\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
            f"{body}\r\n").encode()


def multipart_message(n, parts=3, part_size=2000):
    """multipart/mixed with a text part and `parts - 1` base64 attachments."""
    out = (f"From: carol@example.com\r\nTo: bob@example.com\r\nSubject: multipart {n}\r\n"
           f"Message-ID: <mp{n}@example.com>\r\nDate: Wed, 17 Jul 2024 02:44:25 -0700\r\n"
           f"MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=\"BND{n}\"\r\n\r\n"
           f"--BND{n}\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nhello {n}\r\n").encode()
    for k in range(parts - 1):
        out += (f"--BND{n}\r\nContent-Type: application/octet-stream; name=\"f{k}.bin\"\r\n"
                f"Content-Disposition: attachment; filename=\"f{k}.bin\"\r\n"
                f"Content-Transfer-Encoding: base64\r\n\r\n").encode()
        line = b"QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVowMTIzNDU2Nzg5QUJDREVGR0hJSktMTU5PUFFSU1RV\r\n"
        out += line * max(1, part_size // len(line))
    out += f"--BND{n}--\r\n".encode()
    return out


def build_repo(path, messages, application="gmail-local-repository", schema_version=2,
               mailbox="test@example.com", created_at="2026-11-04T12:33:04.310412+00:00",
               shard_size=500):
    """messages: list of dicts {raw, folders, read=False, starred=False, date=None}.

    Returns the list of message_ids (1, 2, 3, ...) in the order given.
    """
    os.makedirs(os.path.join(path, "messages"), exist_ok=True)
    meta = {"application": application, "schema_version": schema_version,
            "created_at": created_at}
    meta["mailbox" if application.startswith("gmail") else "username"] = mailbox
    if not application.startswith("gmail"):
        meta["host"] = "imap.example.com"
    with open(os.path.join(path, "repository.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f)
    db = sqlite3.connect(os.path.join(path, "repository.db"))
    db.executescript(SCHEMA)
    folder_ids, ids, shards = {}, [], {}
    now = datetime.now(timezone.utc).isoformat()
    for i, spec in enumerate(messages, 1):
        raw = spec["raw"]
        shard_no = (i - 1) // shard_size + 1
        shard = f"{shard_no:06d}"
        if shard_no not in shards:
            db.execute("INSERT INTO message_shards(shard_id, directory_name) VALUES (?,?)",
                       (shard_no, shard))
            os.makedirs(os.path.join(path, "messages", shard), exist_ok=True)
            shards[shard_no] = shard
        fn = f"{i:08x}.eml"
        with open(os.path.join(path, "messages", shard, fn), "wb") as f:
            f.write(raw)
        db.execute(
            "INSERT INTO messages(message_id, gm_id, shard_id, filename, message_size, sha256,"
            " internal_date, imported_at, is_read, is_starred) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (i, f"{i:016x}", shard_no, fn, len(raw), hashlib.sha256(raw).hexdigest(),
             spec.get("date", "2024-07-17T09:44:25+00:00"), now,
             int(spec.get("read", False)), int(spec.get("starred", False))))
        for name in spec["folders"]:
            if name not in folder_ids:
                cur = db.execute("INSERT INTO folders(name) VALUES (?)", (name,))
                folder_ids[name] = cur.lastrowid
            db.execute("INSERT INTO folder_messages(folder_id, message_id) VALUES (?,?)",
                       (folder_ids[name], i))
        ids.append(i)
    db.commit()
    db.close()
    return ids


# The table layout written by the generic IMAP importer (imap_local_repository.py, application
# 'imap-local-repository', schema_version 1), copied from its create_schema(). Kept verbatim so
# the tests exercise the server against the real layout, including the columns it ignores.
IMAP_IMPORTER_SCHEMA = """
CREATE TABLE repository_info (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE folders (
    folder_id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, imap_name TEXT, delimiter TEXT,
    uidvalidity TEXT, last_seen_at TEXT, last_synced_at TEXT);
CREATE TABLE message_shards (
    shard_id INTEGER PRIMARY KEY AUTOINCREMENT, directory_name TEXT NOT NULL UNIQUE,
    file_count INTEGER NOT NULL DEFAULT 0 CHECK(file_count >= 0 AND file_count <= 500),
    created_at TEXT NOT NULL);
CREATE TABLE messages (
    message_id INTEGER PRIMARY KEY, sha256 TEXT NOT NULL UNIQUE, rfc822_message_id TEXT,
    shard_id INTEGER NOT NULL, filename TEXT NOT NULL, message_size INTEGER NOT NULL,
    internal_date TEXT, imported_at TEXT NOT NULL, is_read INTEGER NOT NULL DEFAULT 0,
    is_starred INTEGER NOT NULL DEFAULT 0, FOREIGN KEY(shard_id) REFERENCES message_shards(shard_id));
CREATE TABLE folder_messages (
    folder_id INTEGER NOT NULL, message_id INTEGER NOT NULL, first_seen_at TEXT NOT NULL,
    PRIMARY KEY(folder_id, message_id),
    FOREIGN KEY(folder_id) REFERENCES folders(folder_id),
    FOREIGN KEY(message_id) REFERENCES messages(message_id));
CREATE TABLE imap_sources (
    folder_id INTEGER NOT NULL, uidvalidity TEXT NOT NULL, uid INTEGER NOT NULL, message_id INTEGER,
    status TEXT NOT NULL CHECK(status IN ('imported','linked','suppressed')), server_flags TEXT,
    first_seen_at TEXT NOT NULL, PRIMARY KEY(folder_id, uidvalidity, uid));
CREATE TABLE deleted_messages (
    sha256 TEXT PRIMARY KEY, deleted_at TEXT NOT NULL, reason TEXT NOT NULL DEFAULT 'local deletion');
"""


def build_imap_importer_repo(path, messages, delimiter="/", username="user@example.org",
                             host="imap.example.org"):
    """Repository exactly as the generic IMAP importer lays it out.

    messages: list of dicts {raw, folders, read=False, starred=False, date=None, flags=''}.
    Folder names are given decoded and joined with `delimiter`, as the importer stores them.
    Files are named <sha256>.eml. Returns the message ids (1, 2, 3, ...).
    """
    os.makedirs(os.path.join(path, "messages", "000001"), exist_ok=True)
    created = "2026-11-21T19:59:31.271795+00:00"
    with open(os.path.join(path, "repository.json"), "w", encoding="utf-8") as f:
        json.dump({"application": "imap-local-repository", "schema_version": 1, "host": host,
                   "username": username, "created_at": created}, f)
    db = sqlite3.connect(os.path.join(path, "repository.db"))
    db.executescript(IMAP_IMPORTER_SCHEMA)
    db.execute("INSERT INTO repository_info VALUES ('username', ?), ('host', ?), ('schema_version', '1')",
               (username, host))
    db.execute("INSERT INTO message_shards(directory_name, file_count, created_at) VALUES ('000001', ?, ?)",
               (len(messages), created))
    folder_ids, ids = {}, []
    now = datetime.now(timezone.utc).isoformat()
    for i, spec in enumerate(messages, 1):
        raw = spec["raw"]
        digest = hashlib.sha256(raw).hexdigest()
        with open(os.path.join(path, "messages", "000001", digest + ".eml"), "wb") as f:
            f.write(raw)
        mid = None
        for line in raw.split(b"\r\n\r\n", 1)[0].splitlines():
            if line.lower().startswith(b"message-id:"):
                mid = line.split(b":", 1)[1].strip().decode()
        db.execute(
            "INSERT INTO messages(message_id, sha256, rfc822_message_id, shard_id, filename, message_size,"
            " internal_date, imported_at, is_read, is_starred) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (i, digest, mid, 1, digest + ".eml", len(raw),
             spec.get("date", "2024-07-17T09:44:25+00:00"), now,
             int(spec.get("read", False)), int(spec.get("starred", False))))
        for n, name in enumerate(spec["folders"], 1):
            if name not in folder_ids:
                cur = db.execute(
                    "INSERT INTO folders(name, imap_name, delimiter, uidvalidity, last_seen_at) "
                    "VALUES (?,?,?,?,?)", (name, name, delimiter, "1700000000", now))
                folder_ids[name] = cur.lastrowid
            db.execute("INSERT INTO folder_messages(folder_id, message_id, first_seen_at) VALUES (?,?,?)",
                       (folder_ids[name], i, now))
            db.execute("INSERT INTO imap_sources(folder_id, uidvalidity, uid, message_id, status,"
                       " server_flags, first_seen_at) VALUES (?,?,?,?,?,?,?)",
                       (folder_ids[name], "1700000000", i, i, "imported", spec.get("flags", ""), now))
        ids.append(i)
    db.commit()
    db.close()
    return ids
