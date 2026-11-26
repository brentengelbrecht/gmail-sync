# imap_archive_server

A small, read-only IMAP server that lets you browse local mail archives using a
normal mail client, such as Thunderbird. It is designed for a Raspberry Pi or
another Linux computer on a trusted local network.

## What the server does

- **Makes archived mail available over IMAP.** Connect with a mail client and
  read messages and browse folders without running a full mail server such as
  Dovecot.
- **Supports multiple mailboxes.** Configure one account per repository. Each
  login is isolated and can see only its own mailbox.
- **Reads two repository types.** It supports Gmail API archives (schema
  versions 2 and 3) and archives created by the generic IMAP importer (schema
  version 1). You can use both types at the same time.
- **Does not change your mail.** The server opens the archive database in
  read-only query mode and refuses IMAP commands that would modify messages or
  folders.
- **Keeps message files intact.** It serves the stored message bytes rather
  than rewriting the original message. It uses the recorded size for
  `RFC822.SIZE`.
- **Checks archive integrity.** It can check every message file against the
  size and SHA-256 recorded in the repository. It can also check a message when
  its raw bytes are first loaded by a client.
- **Protects logins with password hashes.** Passwords are salted and hashed
  with scrypt in a separate file; they are not stored in the `.ini` file.
- **Can use encrypted connections.** Configure implicit TLS with a certificate
  and private key. STARTTLS is not supported.
- **Uses only Python's standard library.** There are no third-party packages
  to install.

Each repository is one account. You can mix repository types freely in one
configuration file.

## Contents

- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Accounts and passwords](#accounts-and-passwords)
- [Configuration](#configuration)
- [Migrating from the older single-account configuration](#migrating-from-the-older-single-account-configuration)
- [Command-line options](#command-line-options)
- [Checking archive integrity](#checking-archive-integrity)
- [Connecting a mail client](#connecting-a-mail-client)
- [Enabling TLS](#enabling-tls)
- [How folders and messages appear in IMAP](#how-folders-and-messages-appear-in-imap)
- [Supported IMAP features](#supported-imap-features)
- [Security recommendations](#security-recommendations)
- [Troubleshooting](#troubleshooting)
- [Known limitations](#known-limitations)
- [Tests](#tests)
- [Files](#files)

## Requirements

- Python 3.11 or later. The server has been developed with Python 3.12 and run
  with Python 3.13.
- Python built with OpenSSL support, which provides `hashlib.scrypt` for
  password hashing (standard on Raspberry Pi OS).
- One or more repository directories containing `repository.json`,
  `repository.db` and `messages/`:
  - `gmail_local_repository_api.py` — application
    `gmail-local-repository`, schema versions **2 and 3**.
  - `imap_local_repository.py` — application `imap-local-repository`, schema
    version **1**.
- Read access to the repository files. Because the database uses SQLite WAL
  mode, SQLite may need to create or update `repository.db-shm` and
  `repository.db-wal` helper files. The server does not write mail or database
  records, but the user running it will normally need write permission on the
  repository directory for SQLite to manage those helper files.

## Quick start

1. Copy `imap_archive_server.py` and `imap_archive_server.ini` into the same
   directory, for example `/home/pi/`.
2. Edit the `.ini` file and add one `[account:LOGIN]` section for each mailbox.
   For one mailbox:

   ```ini
   [account:me]
   path = /home/pi/mail-data/you%40yourdomain.com
   ```

   `LOGIN` is the username you will enter in your mail client. It does not
   have to be the mailbox's email address.
3. Set a password for that account. You will be prompted twice; the password is
   not echoed:

   ```bash
   python3 imap_archive_server.py --set-password me
   ```

   This creates `imap_archive_server.passwd` beside the `.ini` file (unless
   configured otherwise). It contains a salted hash, not the password. This is
   a password for this server; it is unrelated to your Gmail or Workspace
   password.
4. Check the archive:

   ```bash
   python3 imap_archive_server.py --verify
   ```
5. Start the server:

   ```bash
   python3 imap_archive_server.py
   ```

   Typical startup messages look like this:

   ```text
   INFO listening on 0.0.0.0:1143 (plaintext), 1 account(s)
   INFO account me: mailbox you@yourdomain.com, UIDVALIDITY 1793795584, 0 hidden-folder pattern(s)
   ```
6. Configure your mail client as described in [Connecting a mail client](#connecting-a-mail-client).

Stop the server with Ctrl-C or SIGTERM. It closes open connections and exits
cleanly.

## Accounts and passwords

### Accounts

Each `[account:LOGIN]` section represents one mailbox repository. The login
selects the repository, and the session stays bound to that account for its
lifetime. A session cannot browse another account's folders or messages.

- Logins are case-insensitive.
- Logins may not contain spaces or colons.
- Each account has one login and one password.
- The server reads all configured accounts and validates their repositories at
  startup. A bad account path or unsupported repository schema prevents startup
  and identifies the account in the error.
- The server refuses to start if a configured account has no password.

### Password storage

Passwords are stored in a separate file, never in the `.ini`. The default file
is `imap_archive_server.passwd`, with one line per account. For example:

```text
# comments and blank lines are ignored
alice:scrypt$32768$8$1$mLSK4xg0V_D-KlVdu3zstQ$HUGbr86lRbzuvmCX3Gq9vlqc...
work:scrypt$32768$8$1$p__And7a0o5lJl1iV1Flzmw$NzWKy8ymkDhvt-eHAdJC_2ec...
```

The format is `login:scrypt$N$r$p$salt$hash`:

- `scrypt` is a deliberately slow, memory-intensive password hash provided by
  Python's standard library.
- New hashes use `N=32768`, `r=8`, `p=1` (roughly 32 MiB of memory per check).
  The parameters are stored with each hash, so they can be changed for new
  passwords without invalidating existing hashes.
- Each password gets a fresh 16-byte random salt and a 32-byte hash. Both are
  URL-safe base64 without padding.

Use the command below to create or replace a password; do not edit hash lines
by hand:

```bash
python3 imap_archive_server.py --set-password LOGIN
```

The login must already have a matching `[account:LOGIN]` section. The command
prompts twice, then adds or replaces that login's line while keeping other
lines and comments. It writes through a temporary file; ensure the resulting
password file is protected with mode `600`:

```bash
chmod 600 imap_archive_server.passwd
```

If standard input is not a terminal, the two passwords are read from standard
input, one per line. This allows scripting:

```bash
printf 'the-password\nthe-password\n' | python3 imap_archive_server.py --set-password work
```

Typing a password literally in a shell command can leave it in shell history,
so use the interactive prompt unless you are scripting in a suitably protected
environment.

Other password notes:

- Restart the server after adding an account or changing a password. The
  password file is read at startup.
- The server warns if the password file is readable by other users, or if it
  contains entries for accounts that are no longer configured.
- To change a forgotten password, run `--set-password` again. The original
  password cannot be recovered because only its hash is stored.
- To remove an account, delete its `[account:...]` section and, optionally,
  its line in the password file.
- Password checks run away from the main event loop, with at most two checks
  running at once. This prevents slow password hashing from stalling other
  clients and limits memory use during bursts of login attempts.
- Unknown usernames go through the same password-hashing work as known users,
  reducing the information exposed by login timing.

## Configuration

The default configuration file is `imap_archive_server.ini` beside the script.
Use `--config PATH` to select another file. The shipped `.ini` documents the
settings inline; the tables below summarise them.

### Shared settings

| Section | Option | Default | Meaning |
|---|---|---:|---|
| `[server]` | `host` | `0.0.0.0` | Listen address. Use `127.0.0.1` for this computer only; `::` for IPv6. |
| | `port` | `1143` | TCP port, from 1 to 65535. Ports below 1024 need extra privileges. |
| | `allow_networks` | empty | Comma-separated CIDR ranges allowed to connect, e.g. `192.168.0.0/16, 127.0.0.1/32`. Empty means no network restriction. |
| | `cache_messages` | `16` | Parsed messages cached per connection (1–10000). |
| | `verify_on_read` | `true` | Check file size and SHA-256 when raw message bytes are first loaded. Mismatches are logged but the message is still served. |
| | `login_timeout` | `120` | Seconds a new connection has to log in, counted from when it connects (0 to 86400; 0 = no limit). |
| | `idle_timeout` | `1800` | Seconds a logged-in client may send nothing before it is disconnected with `* BYE Autologout` (0 to 86400; 0 = no limit). |
| | `max_connections` | `100` | Most simultaneous connections (0 to 100000; 0 = no limit). Extra connections receive `* BYE Too many connections`. |
| `[tls]` | `cert` | empty | PEM certificate. Setting it enables implicit TLS. Empty means no TLS. |
| | `key` | empty | PEM private key; may be empty if the key is included in the certificate file. |
| | `port` | empty | Dedicated TLS port, e.g. `1993`. Requires `cert`. If empty, TLS (when enabled) uses `[server] port`. |
| | `also_plain` | `false` | With `[tls] port` set, also open the unencrypted `[server] port`. |
| `[auth]` | `password_file` | `imap_archive_server.passwd` | Password-hash file. Relative paths are resolved against the `.ini` file. |
| `[logging]` | `level` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR`. |
| | `file` | empty | Write logs to this file instead of the terminal. |

### Per-account settings

Add one `[account:LOGIN]` section per repository.

| Option | Default | Meaning |
|---|---|---|
| `path` | required | Repository directory containing `repository.json`, `repository.db` and `messages/`. |
| `uidvalidity` | `auto` | Derive from `created_at` in `repository.json`, or set a fixed integer from 1 to 4294967295. |
| `hide_folders` | empty | Case-sensitive shell-style folder patterns (`*`, `?`, `[seq]`), separated by commas or newlines. Example: `CATEGORY_*, CHAT`. |

Example with one Gmail API repository and one generic IMAP repository:

```ini
[account:alice]
path = /home/pi/mail-data/alice%40example.com
hide_folders = CATEGORY_*

[account:work]
path = /home/pi/mail-data/work%40example.org
```

For a repository created by `imap_local_repository.py`, `path` is the repository
directory under its `DB_LOCATION` (the URL-encoded username, or the
`IMAP_REPOSITORY_NAME` you configured).

`hide_folders` uses shell-style patterns, where `[` starts a character class.
To hide Gmail folders such as `[Gmail]/All Mail` or `[Gmail]/Spam`, escape the
opening bracket with `[[]`:

```ini
hide_folders = [[]Gmail]/All Mail, [[]Gmail]/Spam
```

### `.ini` file rules

- Relative paths are resolved against the `.ini` file's directory; `~` is
  expanded.
- Comments must be on their own line, starting with `#` or `;`. A comment after
  a setting becomes part of that setting's value.
- Setting lines must start in column 1. An indented line is treated as a
  continuation of the previous setting's value.
- Unknown sections or option names stop startup, helping catch typos.
- Invalid values produce an error naming the option and the value found.

### Port choices

The standard IMAP ports are 143 (plain) and 993 (implicit TLS), but binding a
port below 1024 requires root or special capabilities. The default port 1143
and an example TLS port 1993 avoid that requirement. If you want to use 993, a
firewall redirect to a higher port is safer than giving Python
`cap_net_bind_service`, which would apply to every Python script.

## Migrating from the older single-account configuration

Earlier versions used one `[repository]` section, `[auth] user`, and an
optional plaintext password (`password`, `password_file` or the
`IMAP_PASSWORD` environment variable). That configuration is no longer
accepted. The server reports that the old format was found and points to this
migration.

Replace the old settings with an account section and a hashed password file.
For example:

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

Then:

1. Run `python3 imap_archive_server.py --set-password me` and enter the
   password. You may reuse the old password.
2. Delete the old plaintext password file and stop setting `IMAP_PASSWORD`;
   neither is read by the current server.
3. Restart the server.

The account's UIDs and folders are unchanged. `UIDVALIDITY` is unchanged if
you retain the same repository and setting, so existing client caches should
remain valid.

## Command-line options

```text
imap_archive_server.py [--config PATH] [--set-password LOGIN]
                       [--verify [--account LOGIN]] [-v]
```

| Option | Meaning |
|---|---|
| `--config PATH` | Use a different `.ini` file. The default is `imap_archive_server.ini` beside the script. |
| `--set-password LOGIN` | Create or change an account password, then exit. |
| `--verify` | Check every message file in every configured account, print any problems, then exit. |
| `--account LOGIN` | With `--verify`, check only this account. |
| `-v`, `--verbose` | Enable debug logging, overriding `level` in the `.ini`. |

For `--verify`, exit status is 0 if all checked files pass and 1 if any
problem is found. Configuration and repository errors also exit non-zero.

## Checking archive integrity

Run:

```bash
python3 imap_archive_server.py --verify
```

The check covers every message in every configured repository, including
messages in folders hidden from mail clients. For each message it checks that:

- The message file exists.
- Its size matches `message_size` in the database.
- Its SHA-256 matches `sha256` in the database.

Example output:

```text
== alice (alice@example.com) ==
SIZE      18c0a1  /home/pi/mail-data/.../messages/000001/18c0a1.eml: file 208 != db 206
MISSING   18c0a3  /home/pi/mail-data/.../messages/000001/18c0a3.eml: No such file or directory
checked 3 messages, 2 problem(s)
== work (work@example.org) ==
checked 1204 messages, 0 problem(s)
```

Use `--account LOGIN` to check one account only. In the report, Gmail
repositories identify messages by Gmail ID; generic IMAP repositories identify
them by SHA-256.

This is a file-level check against the values recorded by the importer. It
cannot establish whether Gmail's stored raw representation is identical to the
original SMTP message; Google does not guarantee that. It tells you whether the
local files still match what the importer recorded.

### Checks while the server is running

With `verify_on_read = true` (the default), the server checks a message's size
and SHA-256 when it first loads that message's raw bytes for a connection. A
metadata-only request, such as fetching `UID`, `FLAGS`, `INTERNALDATE` or
`RFC822.SIZE`, does not by itself load the file and therefore may not trigger
the check. Once loaded, the parsed message is cached for that connection.

If a mismatch is found, the server logs `INTEGRITY MISMATCH` but still serves
the message. This lets you inspect a damaged message rather than having it
hidden from the mail client.

## Connecting a mail client

Thunderbird cannot automatically discover a private server, so choose
**Configure manually** and enter:

| Field | Value |
|---|---|
| Protocol | IMAP |
| Server | The Pi's IP address or hostname, e.g. `192.168.1.50` |
| Port | `1143`, or the port you configured |
| Connection security | None for plaintext, or SSL/TLS if you enabled TLS |
| Authentication | Normal password |
| Username / password | The account's `LOGIN` and the password set with `--set-password` |

Use a separate mail-client account for each server login; each one displays
only its own mailbox. The email address entered in the client is only a label
and does not have to match the login. This server cannot send mail. If the
setup wizard insists on an outgoing SMTP server, enter a placeholder such as
`localhost` on port 25; you can remove it afterwards.

With connection security set to None, Thunderbird displays an unencrypted
connection warning that you must acknowledge.

You can test a plaintext connection from another machine with:

```bash
nc -v 192.168.1.50 1143
# Expect: * OK [CAPABILITY IMAP4rev1] archive server ready
# Then type: a1 LOGOUT
```

Thunderbird is the client used with this server. Apple Mail, iOS Mail and
Outlook have not been tested. Some clients are stricter about unencrypted
logins, so TLS is recommended for them.

## Enabling TLS

A self-signed certificate is suitable for a trusted home LAN. For example:

```bash
openssl req -x509 -newkey rsa:3072 -nodes -days 3650 \
  -keyout imap.key -out imap.crt -subj "/CN=pi.local" \
  -addext "subjectAltName=DNS:pi.local,IP:192.168.1.50"
chmod 600 imap.key
```

Add the certificate and key to the `.ini`:

```ini
[tls]
cert = /home/pi/imap.crt
key = /home/pi/imap.key
```

By default, the `[server] port` now carries implicit TLS. To use a separate
TLS port (for example, 1993), configure:

```ini
[tls]
cert = /home/pi/imap.crt
key = /home/pi/imap.key
port = 1993
```

With this configuration, the server listens for TLS on port 1993 and does not
open the unencrypted `[server] port`. To run both listeners, add:

```ini
also_plain = true
```

When `also_plain = true`, the TLS listener uses `[tls] port` and the plaintext
listener uses `[server] port`. The two ports must be different.

This is **implicit TLS**, like standard IMAP port 993. In your mail client,
choose **SSL/TLS**, not STARTTLS. STARTTLS is not implemented. Thunderbird may
ask you to accept the self-signed certificate the first time it connects.

## How folders and messages appear in IMAP

The server reads repository metadata and message files and presents them using
these mappings:

| IMAP item | Repository source |
|---|---|
| Account | `[account:LOGIN]` section; the login selects the repository |
| Folder list | `folders.name`, except names matched by `hide_folders` |
| Folder contents | `folder_messages` joined to `messages` |
| Message UID | `messages.message_id` |
| UIDVALIDITY | `created_at` in `repository.json` as a Unix timestamp, or the configured `uidvalidity` value |
| `\Seen` | `messages.is_read` |
| `\Flagged` | `messages.is_starred` |
| Internal date | `messages.internal_date` (epoch milliseconds, epoch seconds or ISO 8601), falling back to `imported_at` |
| `RFC822.SIZE` | `messages.message_size` |
| Message bytes | `messages/<shard directory>/<filename>` |

Additional details:

- In Gmail repositories, folders reflect the folders created from Gmail labels.
  A message with several labels is stored once but appears in each matching
  folder. Depending on the archive, folders may include system labels such as
  Spam, Trash, Starred or category labels; use `hide_folders` to hide unwanted
  ones.
- In generic IMAP repositories, folders are the IMAP folders copied by the
  importer (for example `INBOX`, `Sent` or `Spam`), shown using their decoded
  names.
- Folder names use `/` as the hierarchy delimiter. Non-ASCII names are encoded
  as IMAP modified UTF-7 for clients.
- Parent folders are not automatically created for names containing `/`. For
  example, a label named `Projects/Travel` does not cause a synthetic `Projects`
  folder to be added. Some clients may therefore display such folder trees
  imperfectly.
- Folder contents are read from the database whenever a client selects the
  folder. Mail imported while a client is connected appears after it
  re-selects the folder or reconnects.
- `UIDVALIDITY` is automatically derived from the whole-second timestamp in
  `created_at`. Two repositories created in the same second can therefore have
  the same value; it is not guaranteed to be globally unique. You can set an
  explicit value per account if needed.
- Message IDs are used directly as IMAP UIDs. The server does not validate
  that repository IDs fit IMAP's 32-bit UID range; compatible repository data
  is expected to satisfy that requirement.

## Supported IMAP features

### Commands

The server supports:

`CAPABILITY`, `NOOP`, `LOGOUT`, `LOGIN`, `LIST`, `LSUB`, `SUBSCRIBE`,
`UNSUBSCRIBE`, `STATUS`, `SELECT`, `EXAMINE`, `CLOSE`, `UNSELECT`, `CHECK`,
`FETCH`, `SEARCH`, `UID FETCH` and `UID SEARCH`.

`SELECT` and `EXAMINE` both open folders read-only. `SUBSCRIBE` and
`UNSUBSCRIBE` are accepted but have no effect; there is no stored subscription
list. `LSUB` simply returns the same folder listing as `LIST`.

### FETCH data

Supported items include `UID`, `FLAGS`, `INTERNALDATE`, `RFC822.SIZE`,
`ENVELOPE`, `BODYSTRUCTURE`, `BODY`, `RFC822`, `RFC822.HEADER`, `RFC822.TEXT`
and `BODY[...]` / `BODY.PEEK[...]` sections, including:

- `HEADER`
- `HEADER.FIELDS` and `HEADER.FIELDS.NOT`
- `TEXT` and `MIME`
- Numbered parts such as `1.2`
- Partial ranges such as `<0.2048>`
- The `ALL`, `FAST` and `FULL` macros

### SEARCH keys

Supported search keys include:

`ALL`, `SEEN` / `UNSEEN`, `ANSWERED` / `UNANSWERED`, `FLAGGED` /
`UNFLAGGED`, `DELETED` / `UNDELETED`, `DRAFT` / `UNDRAFT`, `FROM`, `TO`,
`CC`, `BCC`, `SUBJECT`, `HEADER`, `BODY`, `TEXT`, `SINCE`, `BEFORE`, `ON`,
`LARGER`, `SMALLER`, `UID`, message-number sets, `NOT`, `OR` and parenthesised
groups.

Header searches match decoded (MIME-decoded) text. `BODY` and `TEXT` search
decoded text parts. Date searches use the internal date.

Searches that need to inspect message content process messages in short time
slices, allowing other client connections to run between slices. If a client
disconnects during a long search, the server stops work for that search.

### Commands refused or not implemented

The server refuses commands that would change mail or folders, returning
`NO [READ-ONLY]`. These include `STORE`, `COPY`, `EXPUNGE`, `APPEND`, `CREATE`,
`DELETE`, `RENAME` and their `UID` forms.

Not implemented: `STARTTLS`, `AUTHENTICATE`, `IDLE`, `NAMESPACE`,
`SORT` / `THREAD`, `ENABLE`, `UIDPLUS`, `SENTSINCE`-style date searches and
mailbox special-use attributes. The server advertises only `IMAP4rev1`.

## Security recommendations

- **Keep it on a trusted network.** By default, the server listens on all
  interfaces and uses plaintext IMAP. Do not expose it directly to the
  internet. Use `allow_networks`, a firewall, or both to restrict access.
- **Enable TLS when needed.** Without TLS, passwords and mail cross the network
  unencrypted. The server supports implicit TLS, not STARTTLS.
- **Protect the password file and private key.** Use file permissions such as
  `chmod 600 imap_archive_server.passwd` and `chmod 600 imap.key`.
- **Protect the archive directory.** The repository contains personal mail.
- **Remember the server is read-only, not a backup tool.** It does not modify
  repository content, but it also does not replace a separate backup.

The server opens each repository database with `PRAGMA query_only=ON`. It does
not issue database writes. Passwords are compared in constant time; unknown
logins perform equivalent hashing work. Failed logins are delayed by about one
second, and a connection is closed after three failed attempts. There is no
per-address lockout, so network restrictions and firewall rules are still
important.

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `[server] port must be an integer, got '1143   # ...'` | A comment follows the value. Move the comment to its own line. |
| `... got '1143\n\nallow_networks = ...'` | The next option line is indented. Remove leading spaces so it starts in column 1. |
| `unknown option 'xxx' in [server]` | The option name is misspelled. Compare it with the shipped `.ini`. |
| `cannot read .../repository.json` | The account's `path` is wrong. It must point to the mailbox repository directory itself. |
| `unrecognised repository application` / `schema_version ... is not supported` | The directory is not a supported Gmail schema 2 or 3 repository, or generic IMAP schema 1 repository, or it was created by a newer importer. |
| Thunderbird: “failed to find the settings” | Expected for a private server. Choose **Configure manually**. |
| Connection refused or times out | The server may not be running; `host` may be `127.0.0.1`; the port may be wrong; or a firewall may block it. |
| Connects, then drops immediately | Check the log for `rejected connection ... (not in allow_networks)`. Add the client's network to `allow_networks`. |
| Login rejected | Check the `[account:...]` login and password. The login is not necessarily the email address. Look for `failed login for ...` in the log. |
| `no password set for: ...` at startup | Run `--set-password LOGIN` for each listed account. |
| `this is the old single-account format` | Follow [the migration instructions](#migrating-from-the-older-single-account-configuration). |
| `password file ... malformed password hash` / `expected 'login:hash'` | The password file contains an invalid line or an old plaintext password. Replace it using `--set-password`. |
| Password change has no effect | The password file is read at startup. Restart the server. |
| `too many failed login attempts` | The client sent three incorrect logins on one connection. Correct the saved password in the client. |
| Binding a port fails with a permission error | Ports below 1024 need privileges. Use 1143 or a firewall redirect. |
| Log: `INTEGRITY MISMATCH` | A message file differs from the recorded size or SHA-256. Run `--verify` and compare with a backup. |
| Log: `LF-only line endings` | A stored message has LF-only line endings. Some clients may misparse it; the server still serves the original bytes. |
| Message dates look like the import date | `internal_date` may be in an unrecognised format, so the server fell back to `imported_at`. |
| SQLite errors mention `-shm` or `-wal` | The server user needs write permission on the repository directory so SQLite can manage WAL helper files. |

Run with `-v` to enable connection and debugging details.

### Limits and timeouts

The server protects itself against clients that stall, misbehave or flood it:

- **Login deadline.** A connection that has not logged in within `login_timeout`
  seconds receives `* BYE Autologout; login timed out` and is closed.
- **Idle timeout.** A logged-in client that sends no command for `idle_timeout`
  seconds receives `* BYE Autologout; idle for too long`. Only time spent waiting
  for the client counts; a long-running `SEARCH` or `FETCH` never trips it. This
  also applies while the server waits for the rest of a command, such as a
  literal that never arrives.
- **Slow readers.** A client that stops reading the server's replies for two
  minutes is disconnected, so a stuck client cannot hold a connection (and the
  data queued for it) forever.
- **Connection limit.** When `max_connections` is reached, new connections are
  refused with `* BYE Too many connections`; existing ones are unaffected.
- **Oversized input.** A command line longer than 64 KiB closes that connection
  (`* BYE command line too long`). A client literal larger than 10 MB is
  refused: a normal `{n}` literal gets a tagged `NO [TOOBIG]` and the connection
  carries on; a `{n+}` literal, which the client is already sending, closes it.
  Other connections are never affected.

### One busy client does not slow the others

Reading message files, parsing them and querying SQLite happen on a small pool
of worker threads, in slices of about 50 ms, and results are sent after each
slice. A client that fetches thousands of envelopes, or runs a body search over
a big folder, therefore does not stop other clients (or other accounts) from
being served. On a Raspberry Pi the total work is of course still limited by its
CPU, so a heavy request may make everything somewhat slower, but not unresponsive.

Log files do not contain command arguments. If a command fails unexpectedly,
only its tag and command name are logged, never a password.

## Known limitations

- **The inbox name is reported as stored.** `INBOX` is matched case-insensitively in `SELECT`, `EXAMINE`,
  `STATUS` and `LIST` (RFC 3501), so `SELECT INBOX` works on Yahoo and Outlook.com archives, which store
  the folder as `Inbox`. `LIST` still shows the stored name (`Inbox`). Every other folder name is matched
  exactly, case included.
- **The repository must not be replaced or rebuilt while the server is running.**
  The importer may keep adding or updating messages (new mail appears after a
  client re-selects the folder), but `repository.json`, `repository.db` and the
  `messages/` tree must stay the same repository for as long as the server runs.
  The server relies on message ids staying valid as UIDs and on the repository's
  creation time staying constant (it is the source of UIDVALIDITY). If you
  rebuild, restore from backup or swap in a different copy, stop the server
  first, then start it again.
- New messages imported while a client is connected appear only after the
  client re-selects the folder or reconnects. There are no push updates and no
  `IDLE` support.
- Python's global interpreter lock means the worker threads share one CPU core
  for parsing. Other clients stay responsive, but heavy parsing is not
  parallelised across cores.
- Opening a very large folder for the first time can be slow. Mail clients
  commonly request `ENVELOPE` and `BODYSTRUCTURE` for every message, and the
  server parses those from the message files. Parsed results are cached per
  connection, not persisted between server runs.
- `message/rfc822` attachments are returned as whole message parts. Numeric
  part paths can descend into an embedded message when Python's email parser
  exposes it as a multipart payload; behavior depends on how the message is
  represented by the parser.
- Headers of nested MIME parts (for example `BODY[2.MIME]`) are regenerated by
  Python's `email` library and may differ in line folding from the original
  bytes. Top-level headers and message bodies are served from the stored bytes.
- Gmail labels containing `/` appear as slash-delimited folder names, but
  parent folders are not synthesised. Some clients may display the hierarchy
  oddly.
- There is no persistent subscription list. `SUBSCRIBE` and `UNSUBSCRIBE` do
  nothing, and `LSUB` mirrors `LIST`.
- Each account has exactly one login and one password. Password changes take
  effect after a server restart.
- Only Thunderbird has been used with this server so far; other clients have
  not been tested.

## Tests

The `tests/` folder holds an automated test suite (Python standard library only,
plus the `openssl` command for the TLS tests, which are skipped if it is missing).
It builds small synthetic repositories in a temporary directory, starts the real
server as a subprocess and talks to it over real sockets. Nothing outside the
temporary directory is touched.

```bash
python3 -m unittest discover -s tests -v     # about 30-45 seconds
python3 -m unittest tests.test_server.ProtocolTests    # one group only
```

The suite covers the read-only protocol (LIST, STATUS, SELECT, FETCH sections,
SEARCH keys, refusals), authentication, account isolation, timeouts and limits,
oversized or garbage input, damaged archives (`--verify`, `INTEGRITY MISMATCH`),
implicit TLS, clean shutdown, and responsiveness of other clients while one
client runs a heavy FETCH or SEARCH.

To check that a change did not alter any reply, compare the new script with an
earlier copy. Both servers receive the same ~130 commands and every difference
is printed:

```bash
python3 tests/compare_servers.py /path/to/old/imap_archive_server.py
```

Set `ARCHIVE_SERVER=/path/to/imap_archive_server.py` to run the test suite against
a different copy of the script.

## Files

| File | Purpose |
|---|---|
| `imap_archive_server.py` | The server (one Python file, standard library only). |
| `imap_archive_server.ini` | Configuration file, with settings documented inline. |
| `imap_archive_server.passwd` | Created by `--set-password`; stores salted password hashes, one line per account. Protect it with `chmod 600`. |
| `tools/inspect_repository.py` | Read-only report on a repository: how Gmail IDs and thread IDs are stored, and an estimate of disk space for optional future state files. |
| `tests/` | Automated tests (`test_server.py`), the synthetic-repository builder, and `compare_servers.py`. |
| `README.md` | This user guide. |
| `AGENTS.md` | Developer notes, implementation details and design decisions. |
