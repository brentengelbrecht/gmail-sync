# imap_archive_server

A small, read-only IMAP server for browsing local mail repositories with an
ordinary mail client such as Thunderbird. It serves repositories produced by
either importer, side by side:

- `gmail_local_repository_api.py` (`gmail-local-repository`, schema 2)
- `imap_local_repository.py` (`imap-local-repository`, schema 1), for any
  generic IMAP4 server

Each repository is one account; mix them freely in one `.ini`.

It is intended to run on a Raspberry Pi (or any Linux box) on a trusted local
network. It exists so you can **check, read and verify** the archived mail
without running a full mail server such as Dovecot.

- Single Python file, standard library only (no `pip install` needed).
- Strictly read-only: it never writes to the repository, and refuses every
  IMAP command that would modify mail.
- Serves **several accounts**, each with its own login and its own repository.
  A session can only ever see the account it signed in to.
- Passwords are stored **salted and hashed** (scrypt) in a separate file, never
  in the `.ini`.
- Configured with one documented `.ini` file.
- Can verify every message file against the size and SHA-256 recorded by the
  importer.

## Contents

- [What it does](#what-it-does)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Accounts and passwords](#accounts-and-passwords)
- [Configuration](#configuration)
- [Migrating from the single-account config](#migrating-from-the-single-account-config)
- [Command-line options](#command-line-options)
- [Verifying the archive](#verifying-the-archive)
- [Connecting a mail client](#connecting-a-mail-client)
- [Enabling TLS](#enabling-tls)
- [How the repository maps to IMAP](#how-the-repository-maps-to-imap)
- [Supported IMAP features](#supported-imap-features)
- [Security notes](#security-notes)
- [Troubleshooting](#troubleshooting)
- [Known limitations](#known-limitations)
- [Files](#files)

## What it does

The importer stores every Gmail message as one raw `.eml` file in a sharded
folder tree and records metadata in `repository.db` (SQLite). This server reads
that layout directly and presents it over IMAP:

- Each account (one repository per mailbox) has its own login. After signing
  in, the client sees only that account's mail.
- Each local folder (created from a Gmail label during the initial import)
  appears as an IMAP folder.
- A message that carries several labels is stored once but appears in each of
  its folders.
- Read and starred state appear as the standard `\Seen` and `\Flagged` flags.
- Message bytes are served **exactly as stored**, so a message downloaded
  through IMAP hashes to the SHA-256 recorded in the database.
- Messages are checked against their recorded size and hash the first time a
  client opens them; a mismatch is logged as an error.
- It can run alongside a Gmail sync: the database is opened read-only (SQLite
  WAL mode lets readers and the importer's writer coexist).

## Requirements

- Python 3.10 or later (developed on 3.12, run on 3.13). No third-party
  packages.
- Python built with OpenSSL (standard on Raspberry Pi OS), which provides
  `hashlib.scrypt` for password hashing.
- One or more repositories produced by `gmail_local_repository_api.py`
  (`repository.json` schema version 2) or `imap_local_repository.py` (schema
  version 1), each a directory containing `repository.json`, `repository.db`
  and `messages/`.
- Read access to that directory. SQLite may need to create or touch the
  `repository.db-shm` / `-wal` helper files, so the user running the server
  should normally have write access to the repository *directory* (the server
  itself never writes data).

## Quick start

1. Copy `imap_archive_server.py` and `imap_archive_server.ini` into the same
   folder, for example `/home/pi/`.
2. Edit the `.ini` and define one `[account:LOGIN]` section per mailbox. For a
   single mailbox:

   ```ini
   [account:me]
   path = /home/pi/mail-data/you%40yourdomain.com
   ```

   `LOGIN` is the username your mail client will use. It does not have to be
   the real email address.
3. Set that account's password (you are prompted twice; nothing is echoed):

   ```bash
   python3 imap_archive_server.py --set-password me
   ```

   This creates `imap_archive_server.passwd` beside the script, holding only a
   salted hash. The password is for *this server*; it has nothing to do with
   your Gmail or Workspace account.
4. Check the archive:

   ```bash
   python3 imap_archive_server.py --verify
   ```
5. Start the server:

   ```bash
   python3 imap_archive_server.py
   ```

   You should see lines such as:

   ```
   INFO listening on 0.0.0.0:1143 (plaintext), 1 account(s)
   INFO account me: mailbox you@yourdomain.com, UIDVALIDITY 1793795584, 0 hidden-folder pattern(s)
   ```
6. Point a mail client at it (see [Connecting a mail client](#connecting-a-mail-client)).

Stop it with Ctrl-C or SIGTERM; it closes open connections and exits cleanly.

## Accounts and passwords

**One login per account.** Each `[account:LOGIN]` section in the `.ini` is one
mailbox repository. The username a client signs in with selects the repository,
and that session is bound to it for its whole life: there is no code path by
which it can reach another account's folders or messages. Logins are
case-insensitive and may not contain spaces or colons.

**Passwords are salted and hashed.** They are never in the `.ini`. Each account
has one line in the password file (default `imap_archive_server.passwd`):

```
# comments and blank lines are ignored
alice:scrypt$32768$8$1$mLSK4xg0V_D-KlVdu3zstQ$HUGbr86lRbzuvmCX3Gq9vlqc...
work:scrypt$32768$8$1$p__And7a0o5lJl1iVFlzmw$NzWKy8ymkDhvt-eHAdJC_2ec...
```

The format is `login:scrypt$N$r$p$salt$hash`:

- `scrypt` is a deliberately slow, memory-hungry password hash, from Python's
  standard library.
- `N`, `r`, `p` are the cost parameters (new hashes use 32768, 8, 1, roughly
  32 MiB of memory and a fraction of a second per check). They are stored in
  each line, so the cost can be raised later without invalidating old hashes.
- `salt` is 16 random bytes generated for every password, and `hash` is the
  32-byte result, both URL-safe base64 without padding.

You don't write these lines by hand. Use:

```bash
python3 imap_archive_server.py --set-password LOGIN
```

It prompts twice, then adds or replaces that login's line, keeping all other
lines and comments. The file is written atomically with mode `600`. `LOGIN`
must already have an `[account:LOGIN]` section. If standard input is not a
terminal, the two passwords are read from standard input (one per line), which
allows scripting:

```bash
printf 'the-password\nthe-password\n' | python3 imap_archive_server.py --set-password work
```

(Typing a password literally like this stores it in your shell history, so
prefer the interactive prompt unless you are scripting.)

Notes:

- **Restart the server** after adding an account or changing a password. The
  password file is read at startup.
- The server **refuses to start** if any configured account has no password,
  and warns if the password file is readable by other users (`chmod 600` it) or
  has entries for accounts that no longer exist.
- **Forgotten password:** just run `--set-password` again. There is nothing to
  recover, because only the hash is stored.
- **Removing an account:** delete its `[account:...]` section and, optionally,
  its line in the password file.
- Hashes are checked off the main event loop, so a login never stalls other
  clients, and at most two checks run at once so a flood of login attempts
  cannot exhaust the Pi's memory.

## Configuration

All settings live in `imap_archive_server.ini`, which must sit **next to the
script** unless you pass `--config`. The shipped file documents every option
inline. This table is a summary.

**Shared settings**

| Section | Option | Default | Meaning |
|---|---|---|---|
| `[server]` | `host` | `0.0.0.0` | Listen address. `127.0.0.1` = this machine only; `::` = IPv6. |
| | `port` | `1143` | TCP port (1-65535). Ports below 1024 need extra privileges. |
| | `allow_networks` | empty | Comma-separated CIDR ranges allowed to connect, e.g. `192.168.0.0/16, 127.0.0.1/32`. Empty = no restriction. |
| | `cache_messages` | `16` | Parsed messages cached per connection (1-10000). |
| | `verify_on_read` | `true` | Check size and SHA-256 when a message is first opened. Mismatches are logged, not blocked. |
| `[tls]` | `cert` | empty | PEM certificate. Setting it enables implicit TLS. Empty = plain IMAP. |
| | `key` | empty | PEM private key; may be empty if it is in the certificate file. |
| | `port` | empty | Port for TLS (for example `1993`). Empty = TLS is served on `[server] port`. Requires `cert`. |
| | `also_plain` | `false` | With `[tls] port` set, also open the unencrypted `[server] port`. |
| `[auth]` | `password_file` | `imap_archive_server.passwd` | Hashed password file (see below). Relative paths are relative to the `.ini`. |
| `[logging]` | `level` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR`. |
| | `file` | empty | Log to this file instead of the terminal. |

**Per-account settings**: one `[account:LOGIN]` section for each mailbox.

| Option | Default | Meaning |
|---|---|---|
| `path` | *required* | The repository directory (contains `repository.json`, `repository.db`, `messages/`). |
| `uidvalidity` | `auto` | `auto` derives it from `created_at` in `repository.json`. Otherwise a fixed number from 1 to 4294967295. |
| `hide_folders` | empty | Folder-name patterns to hide (`*`, `?`, `[seq]`; case-sensitive; comma- or newline-separated). Example: `CATEGORY_*, CHAT`. |

`hide_folders` uses shell-style patterns, so `[` starts a character class. To
hide folders such as `[Gmail]/All Mail` or `[Gmail]/Spam`, write the bracket as
`[[]`: `hide_folders = [[]Gmail]/All Mail, [[]Gmail]/Spam`.

Example with two accounts (one Gmail API repository, one generic IMAP):

```ini
[account:alice]
path = /home/pi/mail-data/alice%40example.com
hide_folders = CATEGORY_*

[account:work]
path = /home/pi/mail-data/work%40example.org
```

For a repository created by `imap_local_repository.py`, `path` is the
repository directory under its `DB_LOCATION` (the URL-encoded username, or the
`IMAP_REPOSITORY_NAME` you set). Nothing else changes.

### Rules for the `.ini` file

- Relative paths are resolved against the `.ini` file's folder; `~` is expanded.
- Comments must be on their **own line** (starting with `#` or `;`). A comment
  after a value becomes part of the value.
- Lines that set an option must start in **column 1**. An indented line is
  treated as a continuation of the previous option's value.
- Unknown sections or option names stop the server at startup, so typos are
  caught.
- Invalid values produce an error naming the option and the value found.

### Ports

The standard IMAP ports are 143 (plain) and 993 (implicit TLS), but binding
ports below 1024 requires root or special capabilities. Using a high port such
as 1143 (default) or 1993 avoids that. If you really want 993, a firewall
redirect from 993 to your chosen port is safer than giving Python
`cap_net_bind_service`, which applies to every Python script.

## Migrating from the single-account config

Earlier versions had one `[repository]` section, `[auth] user`, and an optional
plaintext password (`password`, `password_file` or the `IMAP_PASSWORD`
environment variable). That format is no longer accepted; the server prints an
error pointing here. To migrate:

1. Replace the `[repository]` section and `[auth] user`/`password` with one
   account section, using your old username as the login:

   ```ini
   # before
   [repository]
   path = /home/pi/mail-data/you%40yourdomain.com
   hide_folders = CATEGORY_*
   uidvalidity = auto
   [auth]
   user = me
   password_file = /home/pi/.imap_password

   # after
   [account:me]
   path = /home/pi/mail-data/you%40yourdomain.com
   hide_folders = CATEGORY_*
   uidvalidity = auto
   [auth]
   password_file = imap_archive_server.passwd
   ```
2. Run `python3 imap_archive_server.py --set-password me` and enter the
   password. You can reuse the old one.
3. Delete the old plaintext password file and stop using `IMAP_PASSWORD`;
   neither is read any more.

`UIDVALIDITY`, UIDs and folders are unchanged, so existing client caches stay
valid.

## Command-line options

```
imap_archive_server.py [--config PATH] [--set-password LOGIN]
                       [--verify [--account LOGIN]] [-v]
```

| Option | Meaning |
|---|---|
| `--config PATH` | Use a different `.ini` file (default: `imap_archive_server.ini` beside the script). |
| `--set-password LOGIN` | Create or change an account's password, then exit. |
| `--verify` | Check every message file of every account against the database, print problems, and exit. |
| `--account LOGIN` | With `--verify`: check only this account. |
| `-v`, `--verbose` | Debug logging (overrides `level` in the `.ini`). |

Exit codes: `--verify` returns 0 if all files check out and 1 if any problem is
found. Configuration and repository errors print a message and exit non-zero.

## Verifying the archive

```bash
python3 imap_archive_server.py --verify
```

For every message in every account's repository (including folders hidden
from clients) this checks that the file exists, that its size matches `message_size`, and
that its SHA-256 matches `sha256`. Example output:

```
== alice (alice@example.com) ==
SIZE      18c0a1  /home/pi/mail-data/.../messages/000001/18c0a1.eml: file 208 != db 206
MISSING   18c0a3  /home/pi/mail-data/.../messages/000001/18c0a3.eml: No such file or directory
checked 3 messages, 2 problem(s)
== work (work@example.org) ==
checked 1204 messages, 0 problem(s)
```

Use `--account LOGIN` to check a single account.

This is a file-level integrity check against what the importer recorded. It
cannot tell you whether Gmail's raw representation matches the original SMTP
message (Google does not guarantee that), only that your stored files have not
changed since import.

While the server runs, `verify_on_read` performs the same check on each message
a client opens and logs `INTEGRITY MISMATCH` if it fails.

## Connecting a mail client

Thunderbird cannot auto-discover a private server, so choose **Configure
manually** and enter:

| Field | Value |
|---|---|
| Protocol | IMAP |
| Server | Pi's IP address or hostname (e.g. `192.168.1.50`) |
| Port | `1143` (or your configured port) |
| Connection security | None (or SSL/TLS if you configured `[tls]`) |
| Authentication | Normal password |
| Username / password | the account's `LOGIN` and the password you set with `--set-password` |

Use a separate Thunderbird account for each login; each one shows only its own
mailbox. The email address you give Thunderbird can be anything; it only labels
the account. This server
cannot send mail. If the wizard insists on an outgoing (SMTP) server, enter a
placeholder such as `localhost`, port 25; you can delete it afterwards. With
connection security "None", Thunderbird shows an "unencrypted" warning that you
must acknowledge.

Quick connectivity test from another machine:

```bash
nc -v 192.168.1.50 1143
# expect: * OK [CAPABILITY IMAP4rev1] archive server ready
# type:   a1 LOGOUT
```

Thunderbird is the client this has been used with. Other clients (Apple Mail,
iOS Mail, Outlook) have not been tested; some are stricter about unencrypted
logins, so TLS is recommended for them.

## Enabling TLS

On a LAN a self-signed certificate is fine:

```bash
openssl req -x509 -newkey rsa:3072 -nodes -days 3650 \
  -keyout imap.key -out imap.crt -subj "/CN=pi.local" \
  -addext "subjectAltName=DNS:pi.local,IP:192.168.1.50"
chmod 600 imap.key
```

Then in the `.ini`:

```ini
[tls]
cert = /home/pi/imap.crt
key = /home/pi/imap.key
```

To carry TLS on its own port (the usual unprivileged substitute for 993), add:

```ini
[tls]
cert = /home/pi/imap.crt
key = /home/pi/imap.key
port = 1993
```

The server then listens for TLS on 1993 and does **not** open the unencrypted
`[server] port`. Set `also_plain = true` if you also want the plaintext port
(for example 1143) open at the same time. Without `[tls] port`, the `[server]
port` itself carries TLS when a certificate is set, as in earlier versions.

Setting a certificate enables **implicit TLS** (like port 993). Set the client's connection
security to "SSL/TLS", not "STARTTLS". The server does not implement STARTTLS.
Thunderbird will ask you to accept the self-signed certificate on first
connect.

## How the repository maps to IMAP

| IMAP concept | Source |
|---|---|
| Account | `[account:LOGIN]` section; the login name selects the repository |
| Folder list | `folders.name` (hidden by `hide_folders` if matched) |
| Folder contents | `folder_messages` joined to `messages` |
| Message UID | `messages.message_id` |
| UIDVALIDITY | `repository.json` `created_at` as a Unix timestamp (or the configured value) |
| `\Seen` | `messages.is_read` |
| `\Flagged` | `messages.is_starred` |
| Internal date | `messages.internal_date` (epoch ms/s or ISO 8601), falling back to `imported_at` |
| `RFC822.SIZE` | `messages.message_size` |
| Message content | `messages/<shard directory>/<filename>` |

Notes:

- For IMAP repositories, folders are the IMAP folders the importer copied
  (`INBOX`, `Sent`, `Spam`, ...), shown by their decoded names. `--verify`
  labels each message by its SHA-256 instead of a Gmail ID.
- Folders include whatever the importer created from Gmail labels, which may
  include system labels such as Spam, Trash, Starred or category labels. Use
  `hide_folders` to hide ones you don't want to see.
- Folder names are shown with `/` as the hierarchy delimiter. Non-ASCII names
  are encoded as IMAP modified UTF-7, as clients expect.
- A folder's contents are read from the database each time a client selects
  it. Mail imported while a client is connected appears after the client
  re-selects the folder or reconnects.

## Supported IMAP features

**Commands:** `CAPABILITY`, `NOOP`, `LOGOUT`, `LOGIN`, `LIST`, `LSUB`,
`SUBSCRIBE`/`UNSUBSCRIBE` (accepted, no effect), `STATUS`, `SELECT`, `EXAMINE`
(both open read-only), `CLOSE`, `UNSELECT`, `CHECK`, `FETCH`, `SEARCH`,
`UID FETCH`, `UID SEARCH`.

**FETCH items:** `UID`, `FLAGS`, `INTERNALDATE`, `RFC822.SIZE`, `ENVELOPE`,
`BODYSTRUCTURE`, `BODY`, `RFC822`, `RFC822.HEADER`, `RFC822.TEXT`, and
`BODY[...]` / `BODY.PEEK[...]` with `HEADER`, `HEADER.FIELDS`,
`HEADER.FIELDS.NOT`, `TEXT`, `MIME`, numbered parts such as `1.2`, and partial
ranges (`<0.2048>`). Macros `ALL`, `FAST`, `FULL`.

**SEARCH keys:** `ALL`, `SEEN`/`UNSEEN`, `ANSWERED`/`UNANSWERED`,
`FLAGGED`/`UNFLAGGED`, `DELETED`/`UNDELETED`, `DRAFT`/`UNDRAFT`, `FROM`, `TO`,
`CC`, `BCC`, `SUBJECT`, `HEADER`, `BODY`, `TEXT`, `SINCE`, `BEFORE`, `ON`,
`LARGER`, `SMALLER`, `UID`, message-number sets, `NOT`, `OR`, and parenthesised
groups. Header searches match decoded (MIME-decoded) text; `BODY`/`TEXT`
search the decoded text parts. Date searches use the internal date.

**Refused with `NO [READ-ONLY]`:** `STORE`, `COPY`, `EXPUNGE`, `APPEND`,
`CREATE`, `DELETE`, `RENAME`, and their `UID` forms.

**Not implemented:** `STARTTLS`, `AUTHENTICATE`, `IDLE`, `NAMESPACE`,
`SORT`/`THREAD`, `ENABLE`, `UIDPLUS`, `SENTSINCE`-style date searches, mailbox
special-use attributes. The server advertises only `IMAP4rev1`, and clients
fall back gracefully.

## Security notes

- The server is **read-only**. It opens each database with
  `PRAGMA query_only=ON` and has no code path that writes to a repository.
- **Account isolation:** a session is bound to one account at login and only
  holds that account's backend.
- **Passwords** are stored as salted scrypt hashes and compared in constant
  time. Logins for unknown users do the same amount of work as real ones, so
  timing does not reveal which logins exist. A failed login is delayed by a
  second and a connection is closed after three failures; there is no
  per-address lockout, so use `allow_networks` or a firewall as well.
- By default traffic is **unencrypted**, including the password the client
  sends. That is acceptable on a trusted home LAN but enable TLS if you are
  unsure.
- Use `allow_networks` and/or a firewall to keep the port off untrusted
  networks. Do not expose the port to the internet.
- The archives contain personal mail. Keep the password file and TLS key
  `chmod 600`, and keep the repositories themselves protected.

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `[server] port must be an integer, got '1143   # ...'` | A comment follows the value. Move the comment to its own line. |
| `... got '1143\n\nallow_networks = ...'` | The next option line starts with spaces. Remove the leading spaces so it starts in column 1. |
| `unknown option 'xxx' in [server]` | Misspelled option name. Compare against the shipped `.ini`. |
| `cannot read .../repository.json` | The account's `path` is wrong. It must be the mailbox directory itself, e.g. `.../mail-data/you%40yourdomain.com`. |
| `unrecognised repository application` / `schema_version ... is not supported` | The directory is not a Gmail (schema 2) or IMAP (schema 1) repository, or was made by a newer importer. |
| Thunderbird: "failed to find the settings" | Expected. Use **Configure manually**. |
| Connection refused / times out | Server not running, `host` set to `127.0.0.1`, wrong port, or firewall. |
| Connects, then drops immediately | Log shows `rejected connection ... (not in allow_networks)`. Add your client's network. |
| Login rejected | Check the username (an `[account:...]` login, not your email address unless you named it that) and password. Look for `failed login for ...` in the log. |
| `no password set for: ...` at startup | Run `--set-password LOGIN` for each account listed. |
| `this is the old single-account format` | See [Migrating](#migrating-from-the-single-account-config). |
| `password file ... malformed password hash` / `expected 'login:hash'` | The password file contains something other than hashes (for example an old plaintext password). Replace it using `--set-password`. |
| Password change has no effect | The password file is read at startup; restart the server. |
| `too many failed login attempts` | The client sent three bad logins on one connection. Fix the password in the client. |
| Binding port fails (permission) | Ports below 1024 need privileges; use 1143 or a redirect. |
| Log: `INTEGRITY MISMATCH` | A message file differs from the importer's recorded size/SHA-256. Run `--verify` and compare with a backup. |
| Log: `LF-only line endings` | A stored message lacks CRLF line endings; some clients may misparse it. |
| All message dates look like the import date | `internal_date` is in an unrecognised format and the server fell back to `imported_at`. |
| Database errors about `-shm`/`-wal` | The user running the server needs write access to the repository directory so SQLite can open the WAL database. |

Run with `-v` to log connections and debugging detail.

## Known limitations

- New messages imported during a client session appear only after the client
  re-selects the folder (no push updates, no `IDLE`).
- The first open of a very large folder can be slow, because clients request
  `ENVELOPE` and `BODYSTRUCTURE` for every message and each is parsed from its
  file. Parsed results are not persisted between runs.
- `message/rfc822` attachments are returned whole; their inner parts cannot be
  addressed separately.
- Headers of nested MIME parts (`BODY[2.MIME]`) are regenerated by Python's
  `email` library and may differ in line folding from the original bytes.
  Top-level headers and bodies are the stored bytes.
- Gmail labels with `/` in the name show as slash-delimited folders, but parent
  folders are not synthesised, so some clients display the tree oddly.
- Each account has exactly one login and one password. The password file is
  read at startup only (restart to change it).
- Only Thunderbird has been used against it so far.

## Files

| File | Purpose |
|---|---|
| `imap_archive_server.py` | The server (single file, standard library only). |
| `imap_archive_server.ini` | Configuration, with every option documented. |
| `imap_archive_server.passwd` | Created by `--set-password`: salted password hashes, one line per account. Keep it `chmod 600`. |
| `README.md` | This document. |
| `AGENTS.md` | Technical context and design decisions for developers and coding agents. |
