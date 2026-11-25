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
