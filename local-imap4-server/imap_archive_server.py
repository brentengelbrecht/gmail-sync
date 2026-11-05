#!/usr/bin/env python3
"""
Read-only, multi-account IMAP4rev1 server for Gmail local repositories
(each: repository.json + repository.db + messages/<shard>/<file>.eml).

  - Accounts        = one [account:LOGIN] section per repository in the .ini; the login
                      name used by the mail client selects the repository, and a session
                      can never see any other account's mail
  - Passwords       = stored salted + hashed (scrypt) in a separate password file,
                      managed with --set-password
  - IMAP UID        = messages.message_id (stable, increasing, 32-bit safe)
  - UIDVALIDITY     = derived from repository.json "created_at" (or set in the .ini)
  - Folders         = the `folders` table (one per imported Gmail label)
  - Flags           = is_read -> \\Seen, is_starred -> \\Flagged
  - Message bytes   = served exactly as stored; size/SHA-256 checked against the database
  - Database access = opened with PRAGMA query_only=ON (safe alongside a running sync, WAL mode)

All settings live in imap_archive_server.ini next to this script (every option is
documented there). Usage:

    ./imap_archive_server.py                     # run the server
    ./imap_archive_server.py --set-password USER # create/change an account's password
    ./imap_archive_server.py --verify            # check every file's size/SHA-256, then exit
    ./imap_archive_server.py --verify --account USER   # ... for one account only
    ./imap_archive_server.py -v                  # run with debug logging
    ./imap_archive_server.py --config /path/to/other.ini
"""
import argparse
import asyncio
import base64
import configparser
import email
import fnmatch
import getpass
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import signal
import sqlite3
import ssl
import sys
from collections import OrderedDict
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.utils import collapse_rfc2231_value, getaddresses

log = logging.getLogger("imapd")
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


# ============================================================================
# BACKEND: Gmail local repository layout
# ============================================================================
@dataclass
class Msg:
    uid: int                 # messages.message_id
    path: str                # absolute path to the .eml file
    size: int                # messages.message_size
    sha256: str              # messages.sha256
    internaldate: datetime
    flags: frozenset = frozenset()


def _try_date(s):
    if not s:
        return None
    s = str(s).strip()
    try:
        if s.isdigit():
            v = int(s)
            return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, timezone.utc)
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def parse_date(*candidates):
    for c in candidates:
        d = _try_date(c)
        if d:
            return d
    return datetime.fromtimestamp(0, timezone.utc)


class ArchiveBackend:
    def __init__(self, repo_dir, uidvalidity="auto", hide=()):
        self.repo_dir = repo_dir
        self.hide = list(hide)
        meta_path = os.path.join(repo_dir, "repository.json")
        try:
            with open(meta_path, encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError) as e:
            raise SystemExit(f"cannot read {meta_path}: {e}")
        if meta.get("application") != "gmail-local-repository":
            raise SystemExit(f"{meta_path}: not a gmail-local-repository repository")
        if meta.get("schema_version") != 2:
            raise SystemExit(f"{meta_path}: schema_version {meta.get('schema_version')!r} "
                             "is not supported (this server reads schema 2)")
        self.mailbox = meta.get("mailbox")
        self.db_path = os.path.join(repo_dir, "repository.db")
        self.msg_root = os.path.join(repo_dir, "messages")
        for needed in (self.db_path, self.msg_root):
            if not os.path.exists(needed):
                raise SystemExit(f"repository incomplete, missing {needed}")
        if str(uidvalidity).lower() == "auto":
            created = _try_date(meta.get("created_at"))
            if not created:
                raise SystemExit("repository.json has no usable created_at; "
                                 "set uidvalidity explicitly in the .ini")
            self.uidvalidity = int(created.timestamp())
        else:
            self.uidvalidity = int(uidvalidity)

    def _hidden(self, name):
        return any(fnmatch.fnmatchcase(name, pat) for pat in self.hide)

    def _db(self):
        db = sqlite3.connect(self.db_path, timeout=10)
        db.execute("PRAGMA query_only=ON")
        return db

    def folders(self):
        with closing(self._db()) as db:
            names = [r[0] for r in db.execute("SELECT name FROM folders ORDER BY name")]
        return [n for n in names if not self._hidden(n)]

    def load(self, folder):
        """Return (uidvalidity, [Msg sorted by uid]). Raise KeyError if no such folder."""
        if self._hidden(folder):
            raise KeyError(folder)
        with closing(self._db()) as db:
            if not db.execute("SELECT 1 FROM folders WHERE name=?", (folder,)).fetchone():
                raise KeyError(folder)
            rows = db.execute(
                "SELECT m.message_id, s.directory_name, m.filename, m.message_size, m.sha256, "
                "       m.internal_date, m.imported_at, m.is_read, m.is_starred "
                "FROM folders f "
                "JOIN folder_messages fm ON fm.folder_id = f.folder_id "
                "JOIN messages m ON m.message_id = fm.message_id "
                "JOIN message_shards s ON s.shard_id = m.shard_id "
                "WHERE f.name = ? ORDER BY m.message_id", (folder,)).fetchall()
        msgs = []
        for mid, shard, fn, size, sha, idate, imported, read, star in rows:
            flags = set()
            if read:
                flags.add("\\Seen")
            if star:
                flags.add("\\Flagged")
            msgs.append(Msg(mid, os.path.join(self.msg_root, shard, fn), size, sha,
                            parse_date(idate, imported), frozenset(flags)))
        return self.uidvalidity, msgs

    def all_messages(self):
        """Every message in the repository (ignores hide_folders; used by --verify)."""
        with closing(self._db()) as db:
            yield from db.execute(
                "SELECT m.gm_id, s.directory_name || '/' || m.filename, m.message_size, m.sha256 "
                "FROM messages m JOIN message_shards s ON s.shard_id = m.shard_id "
                "ORDER BY m.message_id")


def verify(backend):
    checked = bad = 0
    for gm_id, rel, size, sha in backend.all_messages():
        checked += 1
        path = os.path.join(backend.msg_root, rel)
        try:
            raw = load_raw(path)
        except OSError as e:
            print(f"MISSING   {gm_id}  {path}: {e.strerror}")
            bad += 1
            continue
        if len(raw) != size:
            print(f"SIZE      {gm_id}  {path}: file {len(raw)} != db {size}")
            bad += 1
        elif hashlib.sha256(raw).hexdigest() != sha.lower():
            print(f"SHA256    {gm_id}  {path}: digest mismatch")
            bad += 1
    print(f"checked {checked} messages, {bad} problem(s)")
    return 1 if bad else 0


# ============================================================================
# Helpers
# ============================================================================
class Err(Exception):
    def __init__(self, status, text):
        self.status, self.text = status, text


def crlf(b):
    return re.sub(rb"\r?\n", b"\r\n", b)


def load_raw(path):
    with open(path, "rb") as f:
        return f.read()   # served exactly as stored


def utf7_encode(s):
    """Modified UTF-7 (RFC 3501 5.1.3) for non-ASCII mailbox names."""
    out, buf = [], []

    def flush():
        if buf:
            b = "".join(buf).encode("utf-16-be")
            out.append("&" + base64.b64encode(b).decode().rstrip("=").replace("/", ",") + "-")
            buf.clear()
    for c in s:
        if 0x20 <= ord(c) <= 0x7e:
            flush()
            out.append("&-" if c == "&" else c)
        else:
            buf.append(c)
    flush()
    return "".join(out)


def utf7_decode(s):
    def rep(m):
        t = m.group(1)
        if not t:
            return "&"
        t = t.replace(",", "/")
        t += "=" * (-len(t) % 4)
        return base64.b64decode(t).decode("utf-16-be")
    return re.sub(r"&([^-]*)-", rep, s)


class Loaded:
    def __init__(self, raw):
        self.raw = raw
        self._parsed = None
        self._text = None

    @property
    def parsed(self):
        if self._parsed is None:
            self._parsed = email.message_from_bytes(self.raw)
        return self._parsed

    @property
    def text(self):
        if self._text is None:
            out = []
            for p in self.parsed.walk():
                if p.get_content_maintype() == "text":
                    try:
                        out.append((p.get_payload(decode=True) or b"").decode(
                            p.get_content_charset() or "utf-8", "replace"))
                    except Exception:
                        pass
            self._text = "\n".join(out).lower()
        return self._text


def tobytes(x):
    if not isinstance(x, str):
        x = str(x)
    try:
        return x.encode("utf-8", "surrogateescape")
    except UnicodeEncodeError:
        return x.encode("utf-8", "replace")


def q(x):
    """IMAP string: NIL, quoted, or literal."""
    if x is None:
        return b"NIL"
    b = tobytes(x)
    if re.fullmatch(rb"[\x20-\x7e]*", b) and b'"' not in b and b"\\" not in b:
        return b'"' + b + b'"'
    return b"{%d}\r\n%s" % (len(b), b)


def hdr(m, name):
    v = m.get(name)
    if v is None:
        return None
    return re.sub(r"\s*\r?\n\s*", " ", str(v)).strip()


def dec(s):
    try:
        return str(make_header(decode_header(s)))
    except Exception:
        return s


def addr_list(v):
    if not v:
        return b"NIL"
    out = []
    for name, a in getaddresses([v]):
        if not a and not name:
            continue
        mbox, host = a.rsplit("@", 1) if "@" in a else (a, None)
        out.append(b"(" + b" ".join([q(name or None), b"NIL", q(mbox), q(host)]) + b")")
    return b"(" + b"".join(out) + b")" if out else b"NIL"


def envelope(m):
    frm = hdr(m, "From")
    return b"(" + b" ".join([
        q(hdr(m, "Date")), q(hdr(m, "Subject")),
        addr_list(frm), addr_list(hdr(m, "Sender") or frm),
        addr_list(hdr(m, "Reply-To") or frm),
        addr_list(hdr(m, "To")), addr_list(hdr(m, "Cc")), addr_list(hdr(m, "Bcc")),
        q(hdr(m, "In-Reply-To")), q(hdr(m, "Message-ID")),
    ]) + b")"


def split_hdr(raw):
    i = raw.find(b"\r\n\r\n")
    return (raw[:i + 4], raw[i + 4:]) if i >= 0 else (raw, b"")


def part_raw(part):
    return crlf(part.as_bytes())


def part_body(part):
    if not part.is_multipart():
        p = part.get_payload(decode=False)
        if isinstance(p, str):
            try:
                p = p.encode("ascii", "surrogateescape")
            except UnicodeEncodeError:
                p = p.encode("utf-8", "replace")
        return crlf(bytes(p))
    return split_hdr(part_raw(part))[1]


def get_part(msg, path):
    for n in path:
        if msg.is_multipart():
            subs = msg.get_payload()
            if n < 1 or n > len(subs):
                return None
            msg = subs[n - 1]
        elif n != 1:
            return None
    return msg


def params(part, header):
    ps = (part.get_params(header=header) or [])[1:]
    if not ps:
        return b"NIL"
    items = []
    for k, v in ps:
        if isinstance(v, tuple):
            v = collapse_rfc2231_value(v)
        items += [q(k.upper()), q(v)]
    return b"(" + b" ".join(items) + b")"


def disposition(p):
    d = p.get_content_disposition()
    if not d:
        return b"NIL"
    return b"(" + q(d.upper()) + b" " + params(p, "content-disposition") + b")"


def bodystructure(p, ext):
    maintype, subtype = p.get_content_maintype(), p.get_content_subtype()
    if p.is_multipart() and maintype == "multipart":
        out = b"".join(bodystructure(c, ext) for c in p.get_payload())
        out += b" " + q(subtype.upper())
        if ext:
            out += b" " + params(p, "content-type") + b" " + disposition(p) + b" NIL NIL"
        return b"(" + out + b")"
    body = part_body(p)
    f = [q(maintype.upper()), q(subtype.upper()), params(p, "content-type"),
         q(hdr(p, "Content-ID")), q(hdr(p, "Content-Description")),
         q((hdr(p, "Content-Transfer-Encoding") or "7BIT").upper()),
         b"%d" % len(body)]
    if maintype == "message" and subtype == "rfc822" and p.is_multipart():
        inner = p.get_payload(0)
        f += [envelope(inner), bodystructure(inner, ext), b"%d" % body.count(b"\n")]
    elif maintype == "text":
        f.append(b"%d" % body.count(b"\n"))
    if ext:
        f += [b"NIL", disposition(p), b"NIL", b"NIL"]
    return b"(" + b" ".join(f) + b")"


SPEC = re.compile(
    r"^(?:(\d+(?:\.\d+)*)\.?)?(HEADER\.FIELDS\.NOT|HEADER\.FIELDS|HEADER|TEXT|MIME)?"
    r"(?:\s*\((.*)\))?$", re.I | re.S)
BODYRE = re.compile(r"BODY(?:\.PEEK)?\[(.*)\](?:<(\d+)(?:\.(\d+))?>)?$", re.I | re.S)


def filter_headers(head, names, negate):
    fields = re.split(rb"\r\n(?=[^ \t])", head.rstrip(b"\r\n"))
    kept = [f for f in fields
            if f and ((f.split(b":", 1)[0].strip().decode("latin-1").upper() in names) != negate)]
    return (b"\r\n".join(kept) + b"\r\n\r\n") if kept else b"\r\n"


def section_data(L, spec):
    if spec == "":
        return L.raw
    m = SPEC.match(spec.strip())
    if not m:
        raise Err("BAD", "bad section specifier")
    path, kind, flist = m.groups()
    kind = kind.upper() if kind else None
    if path is None:
        head, body = split_hdr(L.raw)
    else:
        part = get_part(L.parsed, [int(x) for x in path.split(".")])
        if part is None:
            return b""
        head, body = split_hdr(part_raw(part))[0], part_body(part)
        if kind is None:
            return body
    if kind in ("HEADER", "MIME"):
        return head
    if kind == "TEXT":
        return body
    if kind and kind.startswith("HEADER.FIELDS"):
        names = {x.strip('"').upper() for x in (flist or "").split()}
        return filter_headers(head, names, kind.endswith("NOT"))
    raise Err("BAD", "bad section specifier")


def fmt_date(d):
    return f"{d.day:02d}-{MONTHS[d.month - 1]}-{d.year} {d:%H:%M:%S} +0000"


def parse_set(s, maxval):
    out = []
    for part in s.split(","):
        a, _, b = part.partition(":")
        n = lambda x: maxval if x == "*" else int(x)
        lo = n(a)
        hi = n(b) if b else lo
        out.append((min(lo, hi), max(lo, hi)))
    return out


def in_set(v, ranges):
    return any(lo <= v <= hi for lo, hi in ranges)


def parse_imap_date(s):
    d, mon, y = s.split("-")
    return datetime(int(y), MONTHS.index(mon.title()) + 1, int(d)).date()


def tokenize(s):
    stack, pos, n = [[]], 0, len(s)
    while pos < n:
        c = s[pos]
        if c in " \t":
            pos += 1
        elif c == "(":
            stack.append([])
            pos += 1
        elif c == ")":
            if len(stack) < 2:
                raise Err("BAD", "unbalanced parentheses")
            l = stack.pop()
            stack[-1].append(l)
            pos += 1
        elif c == '"':
            pos += 1
            buf = []
            while pos < n and s[pos] != '"':
                if s[pos] == "\\" and pos + 1 < n:
                    pos += 1
                buf.append(s[pos])
                pos += 1
            pos += 1
            stack[-1].append("".join(buf))
        else:
            start, depth = pos, 0
            while pos < n:
                ch = s[pos]
                if ch == "[":
                    depth += 1
                elif ch == "]":
                    depth -= 1
                elif depth <= 0 and ch in " ()":
                    break
                pos += 1
            stack[-1].append(s[start:pos])
    if len(stack) != 1:
        raise Err("BAD", "unbalanced parentheses")
    return stack[0]


# ============================================================================
# Accounts and passwords
# ============================================================================
# Password file: one "login:hash" per line; blank lines and lines starting with # are
# ignored. Hash format (self-describing, so parameters can change later):
#     scrypt$N$r$p$salt$digest      (salt/digest: URL-safe base64, no padding)
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 15, 8, 1     # ~32 MiB and a fraction of a second


@dataclass
class Account:
    name: str
    backend: object
    pw_hash: str = ""


def _b64(b):
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(t):
    return base64.urlsafe_b64decode(t + "=" * (-len(t) % 4))


def _maxmem(n, r, p):
    return 128 * n * r * 2 + 2 ** 20


def hash_password(pw, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P):
    salt = os.urandom(16)
    h = hashlib.scrypt(pw.encode("utf-8"), salt=salt, n=n, r=r, p=p,
                       maxmem=_maxmem(n, r, p), dklen=32)
    return f"scrypt${n}${r}${p}${_b64(salt)}${_b64(h)}"


def parse_hash(stored):
    """Validate a stored hash; return (n, r, p, salt, digest). Raises ValueError."""
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        n, r, p = int(n), int(r), int(p)
        salt, digest = _unb64(salt), _unb64(digest)
    except (ValueError, TypeError) as e:
        raise ValueError("malformed password hash") from e
    if scheme != "scrypt":
        raise ValueError(f"unsupported hash scheme {scheme!r}")
    if not (n >= 2 and n & (n - 1) == 0 and n <= 2 ** 20 and 1 <= r <= 32 and 1 <= p <= 16):
        raise ValueError("password hash has unreasonable scrypt parameters")
    if not salt or not digest:
        raise ValueError("malformed password hash")
    return n, r, p, salt, digest


def check_password(pw, stored):
    try:
        n, r, p, salt, digest = parse_hash(stored)
        got = hashlib.scrypt(pw.encode("utf-8"), salt=salt, n=n, r=r, p=p,
                             maxmem=_maxmem(n, r, p), dklen=len(digest))
    except (ValueError, UnicodeEncodeError):
        return False
    return hmac.compare_digest(got, digest)


def read_passwd(path):
    """Return {login(lowercase): hash}. Missing file -> {}."""
    users = {}
    if not os.path.exists(path):
        return users
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            user, sep, h = line.partition(":")
            user, h = user.strip().lower(), h.strip()
            if not sep or not user:
                raise SystemExit(f"{path}:{i}: expected 'login:hash'")
            try:
                parse_hash(h)
            except ValueError as e:
                raise SystemExit(f"{path}:{i}: {e}")
            if user in users:
                raise SystemExit(f"{path}:{i}: duplicate entry for {user!r}")
            users[user] = h
    return users


def set_passwd_entry(path, user, hashed):
    """Add or replace one user's line, keeping other lines/comments. Atomic, mode 600."""
    lines = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    out, found = [], False
    for line in lines:
        t = line.strip()
        if t and not t.startswith("#") and t.partition(":")[0].strip().lower() == user:
            if not found:
                out.append(f"{user}:{hashed}")
                found = True
            continue
        out.append(line)
    if not found:
        if not out:
            out.append("# imap_archive_server passwords (salted scrypt hashes). "
                       "Manage with: imap_archive_server.py --set-password LOGIN")
        out.append(f"{user}:{hashed}")
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    os.replace(tmp, path)


# ============================================================================
# Protocol session
# ============================================================================
ALL_FLAGS = "\\Seen \\Answered \\Flagged \\Deleted \\Draft"
FLAG_NAMES = {"SEEN": "\\Seen", "ANSWERED": "\\Answered", "FLAGGED": "\\Flagged",
              "DELETED": "\\Deleted", "DRAFT": "\\Draft"}
MACROS = {"ALL": ["FLAGS", "INTERNALDATE", "RFC822.SIZE", "ENVELOPE"],
          "FAST": ["FLAGS", "INTERNALDATE", "RFC822.SIZE"],
          "FULL": ["FLAGS", "INTERNALDATE", "RFC822.SIZE", "ENVELOPE", "BODY"]}


class Session:
    def __init__(self, reader, writer, accounts, cfg, auth_gate, dummy_hash):
        self.reader, self.writer = reader, writer
        self.accounts, self.cfg = accounts, cfg
        self.auth_gate, self.dummy_hash = auth_gate, dummy_hash
        self.peer = (writer.get_extra_info("peername") or ("?",))[0]
        self.account = self.backend = None   # bound at LOGIN: a session sees one account only
        self.failed_logins = 0
        self.state = "nonauth"          # nonauth -> auth -> selected
        self.msgs, self.folder = [], None
        self.cache = OrderedDict()
        self.closing = False

    # ---- I/O ---------------------------------------------------------------
    async def send(self, *parts):
        self.writer.write(b"".join(p if isinstance(p, bytes) else p.encode() for p in parts))
        await self.writer.drain()

    async def read_command(self):
        line = await self.reader.readline()
        if not line:
            return None
        line = line.decode("utf-8", "replace").rstrip("\r\n")
        while True:  # fold client literals into quoted strings
            m = re.search(r"\{(\d+)(\+?)\}$", line)
            if not m:
                return line
            n = int(m.group(1))
            if n > 10_000_000:
                raise Err("BAD", "literal too large")
            if not m.group(2):
                await self.send(b"+ ready\r\n")
            data = (await self.reader.readexactly(n)).decode("utf-8", "replace")
            rest = (await self.reader.readline()).decode("utf-8", "replace").rstrip("\r\n")
            line = line[:m.start()] + '"' + data.replace("\\", "\\\\").replace('"', '\\"') + '"' + rest

    async def run(self):
        await self.send(b"* OK [CAPABILITY IMAP4rev1] archive server ready\r\n")
        try:
            while not self.closing:
                try:
                    line = await self.read_command()
                except Err as e:
                    await self.send(f"* {e.status} {e.text}\r\n")
                    continue
                if line is None:
                    break
                m = re.match(r"(\S+)(?:\s+(\S+))?(?:\s+(.*))?$", line, re.S)
                if not m or not m.group(2):
                    await self.send(f"{m.group(1) if m else '*'} BAD missing command\r\n")
                    continue
                tag, cmd = m.group(1), m.group(2).lower()
                try:
                    h = getattr(self, "cmd_" + cmd, None)
                    if h is None:
                        raise Err("BAD", "unknown command")
                    text = await h(tokenize(m.group(3) or ""))
                    await self.send(f"{tag} OK {text or cmd.upper() + ' completed'}\r\n")
                except Err as e:
                    await self.send(f"{tag} {e.status} {e.text}\r\n")
                except (ValueError, IndexError):
                    await self.send(f"{tag} BAD invalid arguments\r\n")
                except OSError as e:
                    log.warning("file error: %s", e)
                    await self.send(f"{tag} NO {e.strerror or 'file error'}\r\n")
                except Exception:
                    log.exception("command failed: %s", line[:80])
                    await self.send(f"{tag} NO internal error\r\n")
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            self.writer.close()

    # ---- state guards ------------------------------------------------------
    def need_auth(self):
        if self.state == "nonauth":
            raise Err("NO", "login first")

    def need_sel(self):
        if self.state != "selected":
            raise Err("NO", "select a mailbox first")

    # ---- any state ---------------------------------------------------------
    async def cmd_capability(self, a):
        await self.send(b"* CAPABILITY IMAP4rev1\r\n")

    async def cmd_noop(self, a):
        pass

    async def cmd_logout(self, a):
        await self.send(b"* BYE logging out\r\n")
        self.closing = True

    async def cmd_login(self, a):
        if self.state != "nonauth":
            raise Err("BAD", "already authenticated")
        if len(a) != 2 or not all(isinstance(x, str) for x in a):
            raise Err("BAD", "LOGIN user password")
        acct = self.accounts.get(a[0].strip().lower())
        # Unknown users are checked against a dummy hash so timing doesn't reveal them.
        stored = acct.pw_hash if acct else self.dummy_hash
        async with self.auth_gate:   # bound concurrent scrypt (CPU/memory)
            ok = await asyncio.get_running_loop().run_in_executor(
                None, check_password, a[1], stored)
        if not (ok and acct):
            self.failed_logins += 1
            log.warning("failed login for %r from %s", a[0], self.peer)
            await asyncio.sleep(1)
            if self.failed_logins >= 3:
                await self.send(b"* BYE too many failed login attempts\r\n")
                self.closing = True
            raise Err("NO", "[AUTHENTICATIONFAILED] invalid credentials")
        self.account, self.backend = acct, acct.backend
        self.state = "auth"
        log.info("login: %s from %s", acct.name, self.peer)
        return "LOGIN completed"

    # ---- authenticated -----------------------------------------------------
    async def _list(self, a, kw):
        self.need_auth()
        if len(a) < 2:
            raise Err("BAD", f"{kw} reference mailbox")
        ref, pat = a[0], a[1]
        if pat == "":
            await self.send(f'* {kw} (\\Noselect) "/" ""\r\n')
            return
        rx = re.compile("^" + "".join(
            ".*" if c == "*" else "[^/]*" if c == "%" else re.escape(c)
            for c in ref + pat) + "$")
        for f in self.backend.folders():
            enc = utf7_encode(f)
            if rx.match(enc):
                await self.send(f"* {kw} () \"/\" ".encode() + q(enc) + b"\r\n")

    async def cmd_list(self, a):
        await self._list(a, "LIST")

    async def cmd_lsub(self, a):
        await self._list(a, "LSUB")

    async def cmd_subscribe(self, a):
        self.need_auth()

    cmd_unsubscribe = cmd_subscribe

    async def cmd_status(self, a):
        self.need_auth()
        name, items = a[0], a[1]
        try:
            uv, msgs = self.backend.load(utf7_decode(name))
        except KeyError:
            raise Err("NO", "no such mailbox")
        out = []
        for it in items:
            u = it.upper()
            val = {"MESSAGES": len(msgs),
                   "UIDNEXT": (msgs[-1].uid + 1) if msgs else 1,
                   "UIDVALIDITY": uv,
                   "UNSEEN": sum("\\Seen" not in m.flags for m in msgs),
                   "RECENT": 0}.get(u)
            if val is None:
                raise Err("BAD", f"unknown status item {it}")
            out.append(f"{u} {val}")
        await self.send(f"* STATUS ".encode() + q(name) + f" ({' '.join(out)})\r\n".encode())

    async def _select(self, a, cmd):
        self.need_auth()
        if not a or not isinstance(a[0], str):
            raise Err("BAD", f"{cmd} mailbox")
        self.state, self.msgs, self.folder = "auth", [], None
        self.cache.clear()
        try:
            uv, msgs = self.backend.load(utf7_decode(a[0]))
        except KeyError:
            raise Err("NO", "no such mailbox")
        self.state, self.msgs, self.folder = "selected", msgs, a[0]
        await self.send(
            f"* FLAGS ({ALL_FLAGS})\r\n* {len(msgs)} EXISTS\r\n* 0 RECENT\r\n"
            f"* OK [UIDVALIDITY {uv}] UIDs valid\r\n"
            f"* OK [UIDNEXT {(msgs[-1].uid + 1) if msgs else 1}] predicted next UID\r\n"
            f"* OK [PERMANENTFLAGS ()] read-only\r\n")
        return f"[READ-ONLY] {cmd} completed"

    async def cmd_select(self, a):
        return await self._select(a, "SELECT")

    async def cmd_examine(self, a):
        return await self._select(a, "EXAMINE")

    async def cmd_close(self, a):
        self.need_sel()
        self.state, self.msgs, self.folder = "auth", [], None

    cmd_unselect = cmd_close

    async def cmd_check(self, a):
        self.need_sel()

    # ---- read-only refusals ------------------------------------------------
    async def _ro(self, a):
        raise Err("NO", "[READ-ONLY] this archive is read-only")

    cmd_store = cmd_copy = cmd_expunge = cmd_append = _ro
    cmd_create = cmd_delete = cmd_rename = _ro

    # ---- selected: FETCH / SEARCH -----------------------------------------
    def loaded(self, m):
        L = self.cache.get(m.uid)
        if L is None:
            raw = load_raw(m.path)
            if self.cfg.verify_on_read and (len(raw) != m.size or
                    hashlib.sha256(raw).hexdigest() != m.sha256.lower()):
                log.error("INTEGRITY MISMATCH uid=%s %s (size/sha256 differ from database)",
                          m.uid, m.path)
            if b"\n" in raw and b"\r\n" not in raw:
                log.warning("uid=%s has LF-only line endings; IMAP clients may misbehave", m.uid)
            L = self.cache[m.uid] = Loaded(raw)
            if len(self.cache) > self.cfg.cache_messages:
                self.cache.popitem(last=False)
        return L

    def msg_size(self, m):
        return m.size

    def fetch_item(self, m, it):
        u = it.upper()
        if u == "UID":
            return b"UID %d" % m.uid
        if u == "FLAGS":
            return b"FLAGS (" + " ".join(sorted(m.flags)).encode() + b")"
        if u == "INTERNALDATE":
            return b'INTERNALDATE "' + fmt_date(m.internaldate).encode() + b'"'
        if u == "RFC822.SIZE":
            return b"RFC822.SIZE %d" % self.msg_size(m)
        L = self.loaded(m)
        if u == "ENVELOPE":
            return b"ENVELOPE " + envelope(L.parsed)
        if u in ("BODYSTRUCTURE", "BODY"):
            return u.encode() + b" " + bodystructure(L.parsed, u == "BODYSTRUCTURE")
        legacy = {"RFC822": "", "RFC822.HEADER": "HEADER", "RFC822.TEXT": "TEXT"}
        if u in legacy:
            d = section_data(L, legacy[u])
            return u.encode() + b" {%d}\r\n" % len(d) + d
        mm = BODYRE.match(it)
        if not mm:
            raise Err("BAD", f"unsupported fetch item {it}")
        spec, origin, size = mm.groups()
        d = section_data(L, spec)
        label = f"BODY[{spec}]"
        if origin is not None:
            o = int(origin)
            d = d[o:o + int(size)] if size else d[o:]
            label += f"<{o}>"
        return label.encode() + b" {%d}\r\n" % len(d) + d

    async def _fetch(self, a, uid_mode):
        self.need_sel()
        if len(a) < 2:
            raise Err("BAD", "FETCH set items")
        raw_items = a[1] if isinstance(a[1], list) else [a[1]]
        items = []
        for it in raw_items:
            if not isinstance(it, str):
                raise Err("BAD", "bad fetch item")
            items += MACROS.get(it.upper(), [it])
        if uid_mode and not any(i.upper() == "UID" for i in items):
            items.insert(0, "UID")
        n = len(self.msgs)
        ranges = parse_set(a[0], (self.msgs[-1].uid if uid_mode and n else n))
        for i, m in enumerate(self.msgs, 1):
            if in_set(m.uid if uid_mode else i, ranges):
                parts = [self.fetch_item(m, it) for it in items]
                await self.send(b"* %d FETCH (" % i + b" ".join(parts) + b")\r\n")

    async def cmd_fetch(self, a):
        await self._fetch(a, False)

    def parse_search(self, toks):
        pos = [0]
        n = len(self.msgs)
        maxuid = self.msgs[-1].uid if n else 0

        def nxt():
            if pos[0] >= len(toks):
                raise Err("BAD", "search: missing argument")
            pos[0] += 1
            return toks[pos[0] - 1]

        def hsub(name, needle):
            needle = needle.lower()
            return lambda i, m: needle in dec(hdr(self.loaded(m).parsed, name) or "").lower()

        def one():
            t = nxt()
            if isinstance(t, list):
                return self.parse_search(t)
            u = t.upper()
            if u == "ALL":
                return lambda i, m: True
            if u in FLAG_NAMES:
                f = FLAG_NAMES[u]
                return lambda i, m: f in m.flags
            if u.startswith("UN") and u[2:] in FLAG_NAMES:
                f = FLAG_NAMES[u[2:]]
                return lambda i, m: f not in m.flags
            if u in ("RECENT", "NEW"):
                return lambda i, m: False
            if u == "OLD":
                return lambda i, m: True
            if u == "NOT":
                p = one()
                return lambda i, m: not p(i, m)
            if u == "OR":
                p1, p2 = one(), one()
                return lambda i, m: p1(i, m) or p2(i, m)
            if u == "UID":
                r = parse_set(nxt(), maxuid)
                return lambda i, m: in_set(m.uid, r)
            if re.fullmatch(r"[\d*:,]+", t):
                r = parse_set(t, n)
                return lambda i, m: in_set(i, r)
            if u in ("FROM", "TO", "CC", "BCC", "SUBJECT"):
                return hsub(u.title(), nxt())
            if u == "HEADER":
                name, val = nxt(), nxt()
                return hsub(name, val)
            if u in ("BODY", "TEXT"):
                needle = nxt().lower()
                if u == "BODY":
                    return lambda i, m: needle in self.loaded(m).text
                return lambda i, m: needle in self.loaded(m).text or \
                    needle in self.loaded(m).raw.lower().decode("latin-1")
            if u in ("SINCE", "BEFORE", "ON"):
                d = parse_imap_date(nxt())
                if u == "SINCE":
                    return lambda i, m: m.internaldate.date() >= d
                if u == "BEFORE":
                    return lambda i, m: m.internaldate.date() < d
                return lambda i, m: m.internaldate.date() == d
            if u in ("LARGER", "SMALLER"):
                k = int(nxt())
                return (lambda i, m: self.msg_size(m) > k) if u == "LARGER" \
                    else (lambda i, m: self.msg_size(m) < k)
            raise Err("BAD", f"unsupported search key {t}")

        preds = []
        while pos[0] < len(toks):
            preds.append(one())
        return lambda i, m: all(p(i, m) for p in preds)

    async def _search(self, a, uid_mode):
        self.need_sel()
        if len(a) >= 2 and isinstance(a[0], str) and a[0].upper() == "CHARSET":
            a = a[2:]
        pred = self.parse_search(a)
        hits = [(m.uid if uid_mode else i) for i, m in enumerate(self.msgs, 1) if pred(i, m)]
        await self.send(("* SEARCH" + "".join(f" {h}" for h in hits) + "\r\n"))

    async def cmd_search(self, a):
        await self._search(a, False)

    async def cmd_uid(self, a):
        self.need_sel()
        sub = a[0].upper() if a and isinstance(a[0], str) else ""
        if sub == "FETCH":
            await self._fetch(a[1:], True)
        elif sub == "SEARCH":
            await self._search(a[1:], True)
        elif sub in ("STORE", "COPY", "EXPUNGE"):
            raise Err("NO", "[READ-ONLY] this archive is read-only")
        else:
            raise Err("BAD", "unknown UID command")
        return f"UID {sub} completed"


# ============================================================================
# ============================================================================
# Configuration (imap_archive_server.ini, next to this script)
# ============================================================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(SCRIPT_DIR, "imap_archive_server.ini")

# section -> option -> default (None = required). Documentation lives in the .ini.
GLOBAL_SCHEMA = {
    "server": {"host": "0.0.0.0", "port": "1143", "allow_networks": "",
               "cache_messages": "16", "verify_on_read": "true"},
    "tls": {"cert": "", "key": ""},
    "auth": {"password_file": "imap_archive_server.passwd"},
    "logging": {"level": "INFO", "file": ""},
}
ACCOUNT_PREFIX = "account:"
ACCOUNT_SCHEMA = {"path": None, "uidvalidity": "auto", "hide_folders": ""}


@dataclass
class AccountCfg:
    name: str
    repo_path: str
    uidvalidity: str = "auto"
    hide_folders: list = field(default_factory=list)


@dataclass
class Config:
    path: str = ""
    accounts: dict = field(default_factory=dict)
    host: str = "0.0.0.0"
    port: int = 1143
    allow_networks: list = field(default_factory=list)
    cache_messages: int = 16
    verify_on_read: bool = True
    cert: str = ""
    key: str = ""
    password_file: str = ""
    log_level: str = "INFO"
    log_file: str = ""


def load_config(path):
    if not os.path.isfile(path):
        raise SystemExit(f"config file not found: {path}")
    cp = configparser.ConfigParser(interpolation=None)  # no %-interpolation: paths contain %40
    try:
        cp.read(path, encoding="utf-8")
    except configparser.Error as e:
        raise SystemExit(f"{path}: {e}")
    if cp.has_section("repository") or cp.has_option("auth", "user") \
            or cp.has_option("auth", "password"):
        raise SystemExit(
            f"{path}: this is the old single-account format. Accounts are now [account:LOGIN] "
            "sections and passwords are stored hashed (see README: 'Migrating from the "
            "single-account config').")
    for sec in cp.sections():
        if sec in GLOBAL_SCHEMA:
            allowed = GLOBAL_SCHEMA[sec]
        elif sec.startswith(ACCOUNT_PREFIX):
            allowed = ACCOUNT_SCHEMA
        else:
            raise SystemExit(f"{path}: unknown section [{sec}] (valid: server, tls, auth, "
                             "logging, and [account:LOGIN])")
        for key in cp[sec]:
            if key not in allowed:
                raise SystemExit(f"{path}: unknown option '{key}' in [{sec}]")
    base = os.path.dirname(os.path.abspath(path))

    def get(sec, key):
        schema = GLOBAL_SCHEMA.get(sec, ACCOUNT_SCHEMA)
        v = cp.get(sec, key, fallback=schema[key])
        if v is None:
            raise SystemExit(f"{path}: [{sec}] {key} is required")
        return v.strip()

    def rpath(v):
        if not v:
            return ""
        v = os.path.expanduser(v)
        return v if os.path.isabs(v) else os.path.join(base, v)

    def hint(v):
        if "\n" in v:
            return (" (the value continues onto the next line: a line that starts with "
                    "spaces is treated as part of the previous option; remove the leading spaces)")
        if re.search(r"\s[#;]", v):
            return " (looks like a comment on the same line; comments must be on their own line)"
        return ""

    def as_int(sec, key, lo, hi):
        v = get(sec, key)
        try:
            n = int(v)
        except ValueError:
            raise SystemExit(f"{path}: [{sec}] {key} must be an integer, got {v!r}{hint(v)}")
        if not lo <= n <= hi:
            raise SystemExit(f"{path}: [{sec}] {key} must be between {lo} and {hi}")
        return n

    def as_list(v):
        return [x.strip() for x in re.split(r"[,\n]", v) if x.strip()]

    verify_raw = get("server", "verify_on_read").lower()
    if verify_raw not in ("true", "false", "yes", "no", "on", "off", "1", "0"):
        raise SystemExit(f"{path}: [server] verify_on_read must be true or false, "
                         f"got {verify_raw!r}{hint(verify_raw)}")
    nets = []
    for n in as_list(get("server", "allow_networks")):
        try:
            nets.append(ipaddress.ip_network(n, strict=False))
        except ValueError:
            raise SystemExit(f"{path}: [server] allow_networks: bad network {n!r}")
    level = get("logging", "level").upper()
    if level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        raise SystemExit(f"{path}: [logging] level must be DEBUG, INFO, WARNING or ERROR, "
                         f"got {level!r}{hint(level)}")
    cert, key = rpath(get("tls", "cert")), rpath(get("tls", "key"))
    if key and not cert:
        raise SystemExit(f"{path}: [tls] key is set but cert is not")

    accounts = {}
    for sec in cp.sections():
        if not sec.startswith(ACCOUNT_PREFIX):
            continue
        name = sec[len(ACCOUNT_PREFIX):].strip().lower()
        if not name or ":" in name or re.search(r"\s", name):
            raise SystemExit(f"{path}: [{sec}]: the login name must be non-empty and contain "
                             "no spaces or colons")
        if name in accounts:
            raise SystemExit(f"{path}: duplicate account {name!r} (logins are case-insensitive)")
        uidv = get(sec, "uidvalidity")
        if uidv.lower() != "auto" and not (uidv.isdigit() and 1 <= int(uidv) <= 0xFFFFFFFF):
            raise SystemExit(f"{path}: [{sec}] uidvalidity must be 'auto' or 1..4294967295, "
                             f"got {uidv!r}{hint(uidv)}")
        accounts[name] = AccountCfg(name, rpath(get(sec, "path")), uidv,
                                    as_list(get(sec, "hide_folders")))
    if not accounts:
        raise SystemExit(f"{path}: no accounts defined; add at least one [account:LOGIN] section")

    return Config(
        path=path, accounts=accounts,
        host=get("server", "host"),
        port=as_int("server", "port", 1, 65535),
        allow_networks=nets,
        cache_messages=as_int("server", "cache_messages", 1, 10000),
        verify_on_read=verify_raw in ("true", "yes", "on", "1"),
        cert=cert, key=key,
        password_file=rpath(get("auth", "password_file")),
        log_level=level,
        log_file=rpath(get("logging", "file")),
    )


def build_accounts(cfg, with_passwords):
    """Open every account's repository (validating it) and attach its password hash."""
    users = {}
    if with_passwords:
        users = read_passwd(cfg.password_file)
        if os.path.exists(cfg.password_file) and os.stat(cfg.password_file).st_mode & 0o077:
            log.warning("%s is readable by other users; run: chmod 600 %s",
                        cfg.password_file, cfg.password_file)
        missing = sorted(n for n in cfg.accounts if n not in users)
        if missing:
            raise SystemExit("no password set for: " + ", ".join(missing) +
                             "\nrun: imap_archive_server.py --set-password LOGIN")
        for extra in sorted(set(users) - set(cfg.accounts)):
            log.warning("password file has an entry for %r but no [account:%s] section", extra, extra)
    accounts = {}
    for name, ac in cfg.accounts.items():
        try:
            backend = ArchiveBackend(ac.repo_path, ac.uidvalidity, ac.hide_folders)
        except SystemExit as e:
            raise SystemExit(f"[account:{name}] {e}")
        accounts[name] = Account(name, backend, users.get(name, ""))
    return accounts


def set_password_cmd(cfg, user):
    user = user.strip().lower()
    if user not in cfg.accounts:
        raise SystemExit(f"no [account:{user}] section in {cfg.path}; configured accounts: "
                         + ", ".join(sorted(cfg.accounts)))
    pw = getpass.getpass(f"New password for {user}: ")
    if not pw:
        raise SystemExit("password must not be empty")
    if getpass.getpass("Repeat password: ") != pw:
        raise SystemExit("passwords do not match")
    if len(pw) < 8:
        print("warning: that password is short (under 8 characters)", file=sys.stderr)
    set_passwd_entry(cfg.password_file, user, hash_password(pw))
    print(f"saved hashed password for {user} in {cfg.password_file}")
    print("restart the server for the change to take effect")
    return 0


def peer_allowed(cfg, peer):
    if not cfg.allow_networks:
        return True
    try:
        ip = ipaddress.ip_address(peer)
    except ValueError:
        return False
    ip = getattr(ip, "ipv4_mapped", None) or ip
    return any(ip in n for n in cfg.allow_networks)


async def serve(cfg, accounts):
    auth_gate = asyncio.Semaphore(2)        # at most 2 scrypt computations at once
    dummy_hash = hash_password(os.urandom(16).hex())
    writers = set()   # open client connections, closed on shutdown

    async def handle(r, w):
        peer = w.get_extra_info("peername")
        if not peer_allowed(cfg, peer[0]):
            log.warning("rejected connection from %s (not in allow_networks)", peer[0])
            w.close()
            return
        log.info("connection from %s", peer)
        writers.add(w)
        try:
            await Session(r, w, accounts, cfg, auth_gate, dummy_hash).run()
        finally:
            writers.discard(w)

    ctx = None
    if cfg.cert:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cfg.cert, cfg.key or None)
    server = await asyncio.start_server(handle, cfg.host, cfg.port, ssl=ctx)
    log.info("listening on %s:%d (%s), %d account(s)", cfg.host, cfg.port,
             "TLS" if ctx else "plaintext", len(accounts))
    for a in accounts.values():
        log.info("account %s: mailbox %s, UIDVALIDITY %d, %d hidden-folder pattern(s)",
                 a.name, a.backend.mailbox, a.backend.uidvalidity, len(a.backend.hide))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):   # Ctrl-C and `systemctl stop`
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:               # e.g. Windows
            pass
    await stop.wait()
    log.info("shutting down (%d open connection(s))", len(writers))
    server.close()
    for w in list(writers):
        w.close()
    try:
        await asyncio.wait_for(server.wait_closed(), 5)
    except asyncio.TimeoutError:
        log.warning("some connections did not close within 5 seconds")


def main():
    ap = argparse.ArgumentParser(description="Read-only multi-account IMAP server for "
                                             "Gmail local repositories")
    ap.add_argument("--config", default=DEFAULT_CONFIG,
                    help="path to the .ini file (default: imap_archive_server.ini beside the script)")
    ap.add_argument("--set-password", metavar="LOGIN",
                    help="create or change the password for an account, then exit")
    ap.add_argument("--verify", action="store_true",
                    help="check every message file against its recorded size/SHA-256, then exit")
    ap.add_argument("--account", metavar="LOGIN", help="with --verify: check only this account")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = ap.parse_args()
    cfg = load_config(args.config)
    logging.basicConfig(level=logging.DEBUG if args.verbose else getattr(logging, cfg.log_level),
                        filename=cfg.log_file or None,
                        format="%(asctime)s %(levelname)s %(message)s")
    if args.set_password:
        sys.exit(set_password_cmd(cfg, args.set_password))
    if args.verify:
        accounts = build_accounts(cfg, with_passwords=False)
        names = [args.account.strip().lower()] if args.account else list(accounts)
        for n in names:
            if n not in accounts:
                raise SystemExit(f"no such account {n!r}; configured: {', '.join(accounts)}")
        rc = 0
        for n in names:
            print(f"== {n} ({accounts[n].backend.mailbox}) ==")
            rc |= verify(accounts[n].backend)
        sys.exit(rc)
    accounts = build_accounts(cfg, with_passwords=True)
    try:
        asyncio.run(serve(cfg, accounts))
    except KeyboardInterrupt:   # Ctrl-C before the signal handlers were installed
        pass


if __name__ == "__main__":
    main()
