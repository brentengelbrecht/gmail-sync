#!/usr/bin/env python3
"""Read-only inspection of a local mail repository (repository.json + repository.db + messages/).

Answers two questions without changing anything in the repository:

  1. How are Gmail IDs stored?  Format of messages.gm_id; whether the repository stores Gmail
     thread IDs and labels; whether the raw messages carry usable threading headers.
  2. How much disk would the server's optional state files need (metadata cache, full-text
     index, change log)?  Measured on a random sample of YOUR messages and extrapolated.

Usage:
    python3 tools/inspect_repository.py /path/to/repository [--sample 300] [--state-dir DIR]

The repository data is never modified: the database is opened like the server does it (normal
connection, PRAGMA query_only=ON), which on a WAL database may create the small -wal/-shm helper
files, so run it as the user that runs the server. The only other thing written is a temporary
SQLite file (system temp directory, deleted at exit) used to measure full-text index size.
Standard library only (Python 3.9+).
"""
import argparse
import email
import email.policy
import json
import os
import random
import re
import shutil
import sqlite3
import sys
import tempfile
import zlib


def human(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1024


def heading(t):
    print(f"\n=== {t} " + "=" * max(0, 70 - len(t)))


def classify_ids(values):
    """Describe a sample of gm_id values."""
    vals = [str(v) for v in values if v is not None]
    if not vals:
        return "no values (column empty or NULL)"
    lengths = sorted({len(v) for v in vals})
    all_digits = all(v.isdigit() for v in vals)
    all_hex = all(re.fullmatch(r"[0-9a-fA-F]+", v) for v in vals)
    has_letters = any(re.search(r"[a-fA-F]", v) for v in vals)
    prefix = any(v.lower().startswith("0x") for v in vals)
    if prefix:
        kind = "hexadecimal with 0x prefix"
    elif all_digits and not has_letters:
        kind = "decimal digits only (this is how IMAP's X-GM-MSGID is written)"
        if all(len(v) in (18, 19, 20) for v in vals):
            kind += "; 18-20 digits is the usual X-GM-MSGID size"
        else:
            kind += "; but the lengths are not 18-20, so it may be hex that happens to have no a-f"
    elif all_hex:
        kind = "hexadecimal (this is how the Gmail API writes message ids)"
    else:
        kind = "mixed or non-numeric; not a plain 64-bit number"
    return f"{kind}; lengths seen: {lengths}; sample: {vals[:3]}"


def open_ro(path):
    """Open the repository database the same way the server does (see AGENTS.md, 'Database
    access'): a normal connection with PRAGMA query_only=ON. A mode=ro connection can fail on the
    importer's WAL-mode database. Nothing is ever written to the data, but SQLite may create the
    small -wal/-shm helper files, so the folder must be writable by the user running the script."""
    if not os.path.isfile(path):          # a plain connect would silently create an empty file
        sys.exit(f"No database at {path}")
    try:
        db = sqlite3.connect(path, timeout=10)
        db.execute("PRAGMA query_only=ON")
        db.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
    except sqlite3.Error as e:
        folder = os.path.dirname(path)
        sys.exit(f"Cannot open {path}: {e}\n"
                 f"  readable: {os.access(path, os.R_OK)}; folder writable (needed for WAL): "
                 f"{os.access(folder, os.W_OK)}\n"
                 "  Run the script as the same user that runs the server, and not while the "
                 "importer holds an exclusive lock.")
    db.row_factory = sqlite3.Row
    return db


def message_files(db, repo, limit=None):
    """(message_id, path, size) for messages, using the same join the server uses."""
    q = ("SELECT m.message_id, s.directory_name, m.filename, m.message_size FROM messages m "
         "JOIN message_shards s ON s.shard_id = m.shard_id")
    for r in db.execute(q):
        yield r["message_id"], os.path.join(repo, "messages", r["directory_name"], r["filename"]), \
            r["message_size"]


def text_of(msg):
    out = []
    for part in msg.walk():
        if part.get_content_maintype() == "text":
            try:
                out.append(part.get_content())
            except Exception:                       # unknown charset etc.
                payload = part.get_payload(decode=True) or b""
                out.append(payload.decode("utf-8", "replace"))
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repository", help="folder containing repository.json and repository.db")
    ap.add_argument("--sample", type=int, default=300, help="messages to sample (default 300)")
    ap.add_argument("--state-dir", help="folder planned for the server's state files "
                    "(only used to report free space)")
    args = ap.parse_args()
    repo = os.path.abspath(os.path.expanduser(args.repository))

    heading("Repository")
    try:
        with open(os.path.join(repo, "repository.json"), encoding="utf-8") as f:
            meta = json.load(f)
    except OSError as e:
        sys.exit(f"Cannot read repository.json in {repo}: {e}")
    for k, v in meta.items():
        print(f"  {k}: {v}")
    db = open_ro(os.path.join(repo, "repository.db"))

    heading("Tables and columns")
    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                       "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    columns = {}
    for t in tables:
        columns[t] = [r["name"] for r in db.execute(f'PRAGMA table_info("{t}")')]
        n = db.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        print(f"  {t} ({n:,} rows): {', '.join(columns[t])}")

    # ------------------------------------------------------------------ gm_id
    heading("Question 3a: how is gm_id stored?")
    if "gm_id" not in columns.get("messages", []):
        print("  messages.gm_id does not exist in this repository (expected for a generic IMAP\n"
              "  repository; the importer identifies messages by sha256 there).")
    else:
        types = db.execute("SELECT typeof(gm_id) t, COUNT(*) c FROM messages GROUP BY t").fetchall()
        print("  SQLite storage types:", {r["t"]: r["c"] for r in types})
        sample = [r[0] for r in db.execute("SELECT gm_id FROM messages WHERE gm_id IS NOT NULL "
                                           "ORDER BY RANDOM() LIMIT 2000")]
        print("  Format:", classify_ids(sample))
        total, distinct = db.execute("SELECT COUNT(gm_id), COUNT(DISTINCT gm_id) FROM messages").fetchone()
        print(f"  Distinct values: {distinct:,} of {total:,}"
              + ("" if total == distinct else "   <-- DUPLICATES: gm_id is not unique"))
        fits = True
        for v in sample:
            s = str(v)
            try:
                n = int(s, 16) if re.fullmatch(r"[0-9a-fA-F]*[a-fA-F][0-9a-fA-F]*", s) else int(s)
            except ValueError:
                fits = False
                break
            fits = fits and n < 2 ** 64
        print("  Fits an unsigned 64-bit number:", "yes" if fits else "NO / not numeric")
        print("  (If the format is hexadecimal, X-GM-MSGID would be that number written in decimal;\n"
              "   if it is already decimal, it can be returned as is.)")

    # ------------------------------------------------------------------ threads / labels
    heading("Question 3b: Gmail thread IDs and labels")
    found = False
    for t, cols in columns.items():
        for c in cols:
            if re.search(r"thread|thrid|conversation", c, re.I):
                found = True
                row = db.execute(f'SELECT COUNT(*), COUNT("{c}"), COUNT(DISTINCT "{c}") FROM "{t}"').fetchone()
                ex = [r[0] for r in db.execute(f'SELECT "{c}" FROM "{t}" WHERE "{c}" IS NOT NULL '
                                               "ORDER BY RANDOM() LIMIT 3")]
                print(f"  {t}.{c}: {row[1]:,} of {row[0]:,} rows populated, {row[2]:,} distinct values; "
                      f"examples: {ex}")
                if row[1] == 0:
                    print("     -> the column exists but is EMPTY: the importer does not fill it.")
                elif classify_ids(ex).startswith("hexadecimal") or classify_ids(ex).startswith("decimal"):
                    print("     -> populated with numeric-looking ids: usable for X-GM-THRID after conversion.")
    if not found:
        print("  No column with 'thread', 'thrid' or 'conversation' in its name.")
    for t in tables:
        if re.search(r"label", t, re.I):
            n = db.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            ex = [dict(r) for r in db.execute(f'SELECT * FROM "{t}" LIMIT 3')]
            print(f"  Table {t}: {n:,} rows; examples: {ex}")
    n_folders = db.execute("SELECT COUNT(*) FROM folders").fetchone()[0]
    print(f"  Folders (what IMAP clients see as mailboxes/labels): {n_folders:,}")

    # ------------------------------------------------------------------ folders
    heading("Folders (names, delimiter, message counts)")
    fcols = columns.get("folders", [])
    if "delimiter" in fcols:
        delims = db.execute("SELECT COALESCE(delimiter, '(none)') d, COUNT(*) c FROM folders GROUP BY d").fetchall()
        print("  Hierarchy delimiters stored by the importer:", {r["d"]: r["c"] for r in delims})
        if len({r["d"] for r in delims}) > 1:
            print("  -> MIXED delimiters: the server will need a single one (spec item C2).")
        elif delims and delims[0]["d"] not in ("/", "(none)"):
            print(f"  -> The delimiter is {delims[0]['d']!r}, not '/'. Today the server still reports '/' "
                  "(spec item C2).")
    else:
        print("  (no delimiter column; labels in a Gmail repository use '/' inside their names)")
    rows = db.execute("SELECT f.name n, COUNT(fm.message_id) c FROM folders f "
                      "LEFT JOIN folder_messages fm ON fm.folder_id = f.folder_id "
                      "GROUP BY f.folder_id ORDER BY f.name").fetchall()
    for r in rows[:60]:
        print(f"  {r['c']:>7,}  {r['n']}")
    if len(rows) > 60:
        print(f"  ... and {len(rows) - 60} more")
    if "server_flags" in columns.get("imap_sources", []):
        flags = {}
        for (f,) in db.execute("SELECT server_flags FROM imap_sources WHERE server_flags IS NOT NULL"):
            for tok in str(f).replace("(", " ").replace(")", " ").replace(",", " ").split():
                flags[tok] = flags.get(tok, 0) + 1
        print("  Server flags recorded at import (imap_sources.server_flags):",
              dict(sorted(flags.items(), key=lambda kv: -kv[1])[:10]) or "none")

    # ------------------------------------------------------------------ sampling
    ids = list(message_files(db, repo))
    total_msgs = len(ids)
    heading("Messages")
    total_bytes = sum(s or 0 for _, _, s in ids)
    print(f"  {total_msgs:,} messages, {human(total_bytes)} in total, "
          f"average {human(total_bytes / max(1, total_msgs))}")
    if not total_msgs:
        return
    random.seed(12345)
    sample = random.sample(ids, min(args.sample, total_msgs))
    have = {"msgid": 0, "inreply": 0, "refs": 0, "xgmthrid": 0, "xgmlabels": 0, "xgmmsgid": 0}
    hdr_z, text_bytes, raw_bytes, fts_texts, missing = [], 0, 0, [], 0
    for _, path, size in sample:
        try:
            with open(path, "rb") as f:
                raw = f.read()
        except OSError:
            missing += 1
            continue
        head = raw.split(b"\r\n\r\n", 1)[0] if b"\r\n\r\n" in raw else raw.split(b"\n\n", 1)[0]
        hl = head.lower()
        have["msgid"] += b"\nmessage-id:" in b"\n" + hl
        have["inreply"] += b"\nin-reply-to:" in b"\n" + hl
        have["refs"] += b"\nreferences:" in b"\n" + hl
        have["xgmthrid"] += b"x-gm-thrid" in hl
        have["xgmlabels"] += b"x-gm-labels" in hl or b"x-gmail-labels" in hl
        have["xgmmsgid"] += b"x-gm-msgid" in hl
        hdr_z.append(len(zlib.compress(head, 6)))
        try:
            msg = email.message_from_bytes(raw, policy=email.policy.default)
            txt = text_of(msg)
        except Exception:
            txt = ""
        text_bytes += len(txt.encode("utf-8"))
        raw_bytes += len(raw)
        fts_texts.append(txt)
    n = len(hdr_z)
    print(f"  Sampled {n:,} messages ({missing} unreadable files skipped).")
    heading("Question 3c: threading headers inside the messages (sample)")
    for label, key in (("Message-ID", "msgid"), ("In-Reply-To", "inreply"), ("References", "refs"),
                       ("X-GM-THRID header", "xgmthrid"), ("X-GM-LABELS / X-Gmail-Labels header", "xgmlabels"),
                       ("X-GM-MSGID header", "xgmmsgid")):
        print(f"  {label:<38} {have[key]:>5} of {n} ({100 * have[key] / max(1, n):.0f}%)")
    print("  THREAD=REFERENCES (RFC 5256) needs only Message-ID/In-Reply-To/References, so threading\n"
          "  can work without Gmail thread ids if most messages have them.")
    if "gmail_thread_id" in columns.get("messages", []):
        multi = {r[0] for r in db.execute(
            "SELECT message_id FROM messages WHERE gmail_thread_id IN (SELECT gmail_thread_id "
            "FROM messages WHERE gmail_thread_id IS NOT NULL GROUP BY gmail_thread_id "
            "HAVING COUNT(*) > 1)")}
        print(f"\n  {len(multi):,} of {total_msgs:,} messages belong to a Gmail thread with 2 or more "
              f"messages.")
        pool = [x for x in ids if x[0] in multi]
        if pool:
            random.seed(54321)
            pick = random.sample(pool, min(args.sample, len(pool)))
            irt = refs = readable = 0
            for _, path, _size in pick:
                try:
                    with open(path, "rb") as f:
                        raw = f.read(65536)
                except OSError:
                    continue
                readable += 1
                head = (raw.split(b"\r\n\r\n", 1)[0] if b"\r\n\r\n" in raw else raw.split(b"\n\n", 1)[0]).lower()
                irt += b"\nin-reply-to:" in b"\n" + head
                refs += b"\nreferences:" in b"\n" + head
            print(f"  Of {readable} of those sampled: In-Reply-To in {irt}, References in {refs}.")
            # in a thread of 2+ messages, at least half the messages are normally replies
            if readable and max(irt, refs) < 0.3 * readable:
                print("  -> Most messages that Gmail groups into threads carry no In-Reply-To/References\n"
                      "     header in the stored files. Header-based threading (RFC 5256 THREAD=REFERENCES)\n"
                      "     would then find few relationships; gmail_thread_id (X-GM-THRID) is the reliable\n"
                      "     source of conversation grouping.")
            elif readable:
                print("  -> The stored files keep the threading headers, so header-based threading can work.")
    if not n:
        return

    # ------------------------------------------------------------------ disk budget
    heading("Question 4: estimated disk budget for the server's state files")
    avg_hdr_z = sum(hdr_z) / n
    text_ratio = text_bytes / max(1, raw_bytes)
    total_text = total_bytes * text_ratio
    print(f"  Measured on the sample: compressed header block {avg_hdr_z:,.0f} B per message; "
          f"decoded text is {100 * text_ratio:.0f}% of raw message size.")
    # metadata cache (P1): measured header part + assumed per-row overhead (ASSUMPTION, see below)
    lo = total_msgs * (avg_hdr_z + 1000)
    hi = total_msgs * (avg_hdr_z + 2500)
    print(f"\n  Metadata cache (P1):  {human(lo)} to {human(hi)}")
    print("     = messages x (measured compressed header + 1.0-2.5 KB for the envelope, body structure,\n"
          "       search columns and SQLite overhead). The 1.0-2.5 KB is an ASSUMPTION until P1 exists\n"
          "       and can be measured.")
    # change log (P3)
    print(f"  Change log (P3):      about {human(total_msgs * 40)} for the remembered flag state, plus"
          f" ~40 bytes per later change")
    # FTS (P2): measure a real trigram index on the sample
    fts = None
    tmpd = tempfile.mkdtemp(prefix="fts-measure-")
    try:
        tdb = sqlite3.connect(os.path.join(tmpd, "t.sqlite"))
        try:
            tdb.execute("CREATE VIRTUAL TABLE f USING fts5(body, tokenize='trigram')")
            for t in fts_texts:
                tdb.execute("INSERT INTO f(body) VALUES (?)", (t.casefold(),))
            tdb.commit()
            tdb.execute("INSERT INTO f(f) VALUES('optimize')")
            tdb.commit()
            tdb.close()
            fts = os.path.getsize(os.path.join(tmpd, "t.sqlite"))
        except sqlite3.OperationalError as e:
            print(f"  Full-text index (P2): cannot be measured; this SQLite lacks FTS5 trigram ({e}).\n"
                  f"     SQLite version here: {sqlite3.sqlite_version}; the server needs 3.34 or newer.")
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)
    if fts is not None:
        sample_text = max(1, sum(len(t.encode()) for t in fts_texts))
        ratio = fts / sample_text
        est = total_text * ratio
        print(f"  Full-text index (P2): about {human(est)}")
        print(f"     = {human(total_text)} of text x {ratio:.1f} (trigram index size / text size, measured on\n"
              f"       your sample). Very small samples can mislead; run with --sample 1000 for a firmer number.")
    print("\n  Rule of thumb for planning: pick the largest line you intend to enable, add the others,\n"
          "  then keep 50% free space as headroom (SQLite needs room to rebuild).")
    print("  Minimum: 0 (all state features are off by default; everything can be rebuilt from the\n"
          "  repository, so the state folder can be deleted at any time).")
    target = args.state_dir or repo
    try:
        free = shutil.disk_usage(target).free
        print(f"  Free space where the state folder would live ({target}): {human(free)}")
    except OSError as e:
        print(f"  Could not read free space for {target}: {e}")
    print(f"  Size of the repository itself, for comparison: {human(total_bytes)}")


if __name__ == "__main__":
    main()
