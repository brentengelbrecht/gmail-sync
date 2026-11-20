# AGENTS.md

Technical context for anyone (human or coding agent) modifying
`imap_archive_server.py`. It records things that are **not obvious from the
code**: why it exists, the decisions made, the reasoning, the constraints, and
what has and has not been tested. User-facing documentation is in `README.md`.

## 1. Purpose and context

- The owner has an email archive that is the **authoritative backup of a
  cloud-hosted Gmail/Workspace mailbox**. The sync is complete; Gmail is now
  only an acquisition source.
- Goal: a tiny server on a Raspberry Pi that lets a mail client on the **local
  network** view that archive, to check, read and verify mail.
- The owner tried Dovecot and found it too complicated and overkill. **Keep this
  project small, readable and dependency-free.** Do not turn it into a general
  IMAP server.
- The data folder can hold **several mailbox repositories** (the importer makes
  one directory per mailbox). All accounts belong to the same owner, so the
  deciding criterion for the design was **isolation**, giving one login and
  password per account (see section 4, "Multi-account design").
- Modification support (flags, moving, deleting) was explicitly deferred
  ("might add that later"). Today the server is strictly read-only.
- Deployment target: Raspberry Pi OS with system Python 3.13 (the owner's
  tracebacks show `/usr/lib/python3.13`), script run from `/home/pi/`.
  Development/testing was done on Python 3.12.
- Only Thunderbird has been used by the owner (it "works well"). Apple Mail,
  iOS Mail and others are untested.

## 2. The data source (not part of this repo)

The archive is produced by a separate importer, `gmail_local_repository_api.py`
(README supplied by the owner, which supersedes an older `imap_sync.py`). Facts
about it that matter here:

- One physical raw `.eml` per Gmail message (Gmail API `format="raw"`,
  base64url-decoded, **not** re-encoded). Google does not guarantee these bytes
  equal the original SMTP message. The importer records `message_size` and
  `sha256` for each file.
- Layout: `<DB_LOCATION>/<url-encoded-mailbox>/{repository.json, repository.db,
  messages/<shard>/<gmail-id>.eml}`. Shards hold at most 500 files; shard
  allocation is recorded in SQLite, not derived from the Gmail ID.
- `repository.json` (schema version 2):
  `{"application": "gmail-local-repository", "schema_version": 2, "mailbox": ...,
  "created_at": "2026-11-04T12:33:04.310412+00:00"}`.
- Gmail labels become local folders **only during the initial import**. Later
  label changes update `gmail_labels` / `gmail_message_labels` but never change
  `folder_messages`. So the folder list reflects the label set at import time
  and may include system labels (Spam, Trash, Starred, categories...).
- Remote deletions never delete local data. Local deletion is an internal helper
  (`delete_local_message()`) with no CLI; it removes the message and writes a
  tombstone to `deleted_messages`.
- The importer uses WAL mode (`synchronous=FULL`) and may be running while this
  server is.

### Schema columns this server uses

```
folders(folder_id, name)
folder_messages(folder_id, message_id)
messages(message_id INTEGER PRIMARY KEY, gm_id, shard_id, filename,
         message_size, sha256, internal_date TEXT, imported_at TEXT,
         is_read, is_starred)
message_shards(shard_id, directory_name)
```

Deliberately **not used**: `folders.uidvalidity` (probably a remnant of Gmail's
own IMAP values, not meaningful for this server), `folder_messages.gmail_uid`,
`messages.gmail_thread_id`, `gmail_labels`, `gmail_message_labels`,
`deleted_messages`, `repository_info`.

The exact format written to `internal_date` has **not been confirmed** (the
owner has not shown sample rows). `parse_date()` accepts epoch milliseconds,
epoch seconds, and ISO 8601, and falls back to `imported_at`, then to the epoch.
If dates look wrong in a client, this is the first place to look.

## 3. Architecture

Single file, standard library only, asyncio. Sections in order:

1. **Backend** (`Msg`, `parse_date`, `ArchiveBackend`, `verify`): the only code
   that knows the repository layout. Interface used by the protocol layer:
   `folders()`, `load(name) -> (uidvalidity, [Msg])` (raises `KeyError`),
   `all_messages()`. To support another layout, replace this class.
2. **Helpers**: IMAP string quoting (`q`), ENVELOPE/BODYSTRUCTURE builders,
   section extraction (`section_data`), sequence-set parsing, the argument
   tokenizer, modified UTF-7 codec.
3. **Accounts and passwords** (`Account`, scrypt helpers, `read_passwd`,
   `set_passwd_entry`).
4. **Session**: one instance per connection; a state machine
   (`nonauth` -> `auth` -> `selected`). `self.backend` is `None` until `LOGIN`
   binds the session to one account's backend. Commands are methods named
   `cmd_<lowercase imap command>`, found with `getattr` in `run()`. A handler
   returns optional completion text, or raises `Err(status, text)`. Adding a
   command means adding one `async def cmd_x(self, a)`.
5. **Config**: `GLOBAL_SCHEMA`, `ACCOUNT_SCHEMA`, `Config`, `AccountCfg`,
   `load_config`, `build_accounts`, `set_password_cmd`, `peer_allowed`.
6. **serve / main**: listener, signal handling, `--set-password`, `--verify`.

No third-party dependencies. Keep it that way (the owner values a copy-and-run
single file on a Pi).

## 4. Key decisions and reasoning

### Language: Python, not Go
Considered and rejected for now. The workload is a few LAN clients over an
immutable archive, which Python handles easily; the hot spot is disk I/O and
MIME parsing. A working Python version already existed and is easy to change
while the IMAP-to-backend mapping was still being discovered. Go (with
`emersion/go-imap` v2, which was still beta when last checked, and
`emersion/go-message`) would give a single static ARM binary and a ready-made
protocol layer, and is the preferred path **if** the owner wants single-binary
deployment or hits performance limits. Not requested so far.

### Hand-written protocol layer
The needed command subset is small and read-only, so a hand-rolled parser keeps
the code self-contained. The hard parts are FETCH (`BODY[...]` section grammar,
`BODYSTRUCTURE`) and SEARCH (recursive criteria).

### UID = `messages.message_id`
- It is an integer, increases in import order, fits IMAP's 32-bit UID range,
  and needs no extra storage or sync logic.
- Gmail IDs (`gm_id`) are 64-bit hex strings and cannot be UIDs.
- Same message in two folders -> same UID in each (UIDs are per-folder
  namespaces, so this is legal).
- **Known weakness:** SQLite can reuse the highest rowid if that row is deleted.
  The importer's `delete_local_message()` is not exposed today, so this is
  latent. If it ever becomes a supported workflow, bump `uidvalidity` in the
  `.ini`.
- **Hard constraint for future work:** if write features are added (copy/move a
  message into a folder), UIDs within a folder would no longer be guaranteed
  ascending by assignment order, which IMAP requires. At that point replace
  this with a **sidecar table** (in a separate SQLite file, never in
  `repository.db`) mapping `(folder_id, message_id) -> uid` with a per-folder
  next-UID counter.

### UIDVALIDITY derived from `repository.json` `created_at`
Stable for the repository's life, unique per repository, and fits 32 bits (a 2026
Unix timestamp is ~1.79e9; the limit is 4.29e9, i.e. until 2106). Can be
overridden in the `.ini`. The server also refuses to start unless
`application == "gmail-local-repository"` and `schema_version == 2`, so a schema
change fails loudly instead of returning wrong data.

### Bytes served exactly as stored (no line-ending normalisation)
An early version converted LF to CRLF on read. That was **removed**: the database
stores `sha256` and `message_size`, and the owner's goal is verification, so
`BODY[]` must be byte-identical to the file and `RFC822.SIZE` must equal
`message_size`. Gmail raw output is normally CRLF already. If a file is LF-only,
the server logs a warning (some clients misparse it) but still serves it as is.

Consequence: `RFC822.SIZE` comes from the database with no file read, so listing
a folder does not touch every file for sizes.

Caveat: for **sub-parts** (`BODY[2]`, `BODYSTRUCTURE` sizes, `BODY[2.MIME]`) the
code uses Python's `email` package, applying `crlf()` and regenerating part
headers with `as_bytes()`. For CRLF files this is consistent, but sub-part
headers may differ in folding from the original bytes. Top-level header and body
come from the raw bytes. Don't "fix" the top level to use the parsed form.

### Integrity checking
- `--verify`: standalone check of every message (existence, size, SHA-256),
  including folders hidden from clients; exit code 1 on any problem.
- `verify_on_read` (default true): the same check the first time a client opens
  a message. On mismatch it **logs an ERROR but still serves the message**, so
  damaged mail can be inspected instead of disappearing.
- This verifies local files against what the importer recorded. It cannot show
  that Gmail's raw form equals the original SMTP message.

### Database access: `query_only`, not `mode=ro`
The importer's database is WAL mode. A `file:...?mode=ro` connection can fail on
a WAL database when `-shm`/`-wal` are absent and cannot be created. The server
therefore opens a normal connection and sets `PRAGMA query_only=ON`. It never
writes. Readers do not block the WAL writer, so the server may run during a
sync. Because of WAL, the process needs write permission on the repository
**directory** even though it writes no data. A fresh connection is made for each
`folders()`/`load()` call (cheap, and keeps results current).

### Folder snapshot semantics
`SELECT` loads the folder once into `self.msgs` for the session. There is no
refresh on `NOOP`/`CHECK` and no `* n EXISTS` push, so mail imported mid-session
shows only after re-select. Acceptable because the sync is complete and the
archive is effectively static. Improvement path: reload in `NOOP`/`CHECK`, diff
UIDs, send `EXISTS`.

### Folder names
Passed through from `folders.name`; delimiter is `/`. Gmail labels may be
non-ASCII, so modified UTF-7 (RFC 3501 5.1.3) is implemented
(`utf7_encode`/`utf7_decode`); `LIST` patterns are matched against the
**encoded** names and `SELECT`/`STATUS` decode the client's name. Parent folders
of `A/B` are not synthesised (no `\Noselect` entries) and no special-use
attributes are sent. Hiding is by `fnmatch` patterns (`hide_folders`) applied in
the backend (so hidden folders are also unselectable), but not in `--verify`.

### Multi-account design
- **One `[account:LOGIN]` section per repository; the login name selects the
  repository.** Chosen over a single login exposing every account as top-level
  folders (`alice@x/INBOX`). That alternative needs only one password but gives
  no isolation, produces awkward folder names, and was rejected. If all accounts
  are the owner's, either would do, so isolation decided it.
- Isolation is **structural**: `LOGIN` sets `self.backend`; every other handler
  uses `self.backend`, and no code path holds two backends in one session. Do not
  add features that look across accounts (for example a combined search).
- Per-account settings are `path`, `uidvalidity`, `hide_folders`. Shared settings
  (server, TLS, logging, password file) stay global. Each account's
  `UIDVALIDITY` auto-derives from its own `repository.json`, so accounts get
  different values.
- All accounts are opened and validated at startup (`build_accounts`), so a bad
  path fails immediately with an error prefixed `[account:NAME]`, not at first
  login. Account names are lowercased; client logins are matched
  case-insensitively; names may not contain whitespace or `:` (the password-file
  delimiter).
- The earlier single-account format (`[repository]`, `[auth] user/password`,
  `IMAP_PASSWORD`) was **removed, not kept as an alias**, because plaintext
  passwords cannot coexist with the hashed-only policy. `load_config` detects
  the old format and prints a migration pointer. Keep that check as long as
  anyone might still hold an old config.
- Passwords come only from the password file; they are never in the `.ini` and
  there is no environment-variable path.

### Password storage
- Requirement from the owner: passwords must be **salted and hashed**, kept in a
  file separate from the `.ini`.
- File: `login:scrypt$N$r$p$salt$hash` per line (`#` comments, blank lines
  allowed), default `imap_archive_server.passwd` beside the `.ini`. Salt = 16
  random bytes per password; digest = 32 bytes; base64 is URL-safe without
  padding so the line contains no `=` or `+/`. The format is self-describing,
  so cost can be raised later and old lines keep verifying.
- **scrypt via `hashlib`** (standard library, no dependency) rather than
  PBKDF2/bcrypt/argon2: stronger than PBKDF2 against GPUs, and avoids adding
  `bcrypt`/`argon2-cffi`. Parameters N=2^15, r=8, p=1 (~32 MiB, about 0.1 s on a
  desktop-class CPU; slower on a Pi, **not yet measured on the owner's Pi**).
  `maxmem` is set explicitly because OpenSSL's default 32 MiB cap would reject
  these parameters. `parse_hash` bounds N, r, p so a corrupt file cannot request
  absurd memory.
- Hash checks run in the default thread executor (`hashlib.scrypt` releases the
  GIL), so one login never blocks the event loop; an `asyncio.Semaphore(2)` caps
  concurrent hashes to bound CPU/memory under a login flood.
- **Unknown users** are checked against a dummy hash created at startup, so
  response time does not reveal whether a login exists. A failed login sleeps
  1 s, and the connection is closed (`* BYE`) after 3 failures. There is no
  per-address lockout (`allow_networks`/firewall cover that).
- `--set-password LOGIN` (via `getpass`, twice) rewrites one line atomically
  (temp file + `os.replace`, mode 600) and preserves all other lines and
  comments. It requires a matching `[account:LOGIN]`. When stdin is not a TTY,
  `getpass` reads stdin, which the tests use for scripting.
- The server refuses to start if an account lacks a password; it only *warns*
  for password-file entries without an account and for group/other-readable
  files. The file is read once at startup (no reload; restart to apply changes).
  A `SIGHUP` reload is a possible improvement.
- `LOGIN` is refused (`BAD`) on an already-authenticated session so a session
  can never switch accounts.

### TLS and the network
- Single-factor `LOGIN` over **plaintext by default**; the server does **not**
  advertise `LOGINDISABLED` or `STARTTLS`, so plaintext login always works.
- TLS is **implicit only** (an `ssl` context passed to `asyncio.start_server`,
  like port 993). STARTTLS is not implemented; adding it means upgrading the
  transport mid-connection and advertising `STARTTLS` / `LOGINDISABLED`.
- Plaintext by default is a conscious choice for a first LAN test; Thunderbird
  accepts it after a warning. Stricter clients may refuse it (untested).
- `allow_networks` is an app-level CIDR allow list (handles IPv4-mapped IPv6
  addresses). It supplements, not replaces, a firewall.

### Configuration design
The owner asked for configurable items in an `.ini` beside the script, each
documented with a sensible default.

- `GLOBAL_SCHEMA` / `ACCOUNT_SCHEMA` list every valid `section -> option ->
  default` (`None` = required). **Unknown sections/options are rejected** to
  catch typos. When adding an option: add it to the schema, `Config` or
  `AccountCfg`, `load_config`, and **document it in `imap_archive_server.ini` and
  `README.md`**.
- `ConfigParser(interpolation=None)`: no `%` interpolation (`%40` in repository
  paths would otherwise break).
- **No inline comments**, deliberately: values such as paths or future options
  may contain `#` or `;`. Comments must be on their own line.
- **Indentation pitfall (the owner hit this):** an indented option line is a
  continuation of the previous value, so a stray indented `allow_networks = ...`
  after `port = 1143` produced "port must be an integer". `load_config` now
  reports the offending value and has a `hint()` that detects a newline in the
  value (indented continuation) or a trailing comment. Keep these messages.
- Default port is 1143 so no privileges are needed. Ports below 1024 need root or
  `cap_net_bind_service` (which applies to all Python scripts, hence the
  documented preference for a firewall redirect).
- Relative paths resolve against the `.ini`'s folder, not the current directory.

### Shutdown
`serve()` installs `SIGINT`/`SIGTERM` handlers that set an `asyncio.Event`,
close the listener and every tracked client writer, then wait up to 5 s for
`wait_closed()`. Explicit writer closing is required: on Python 3.12+
`Server.wait_closed()` waits for all connections, so an open Thunderbird
session would otherwise hang shutdown. This was added after the owner saw a
`KeyboardInterrupt` traceback on Ctrl-C, and it also makes `systemctl stop`
(SIGTERM) behave.

### Read-only invariant
Every mutating command (`STORE`, `COPY`, `EXPUNGE`, `APPEND`, `CREATE`,
`DELETE`, `RENAME`, and the `UID` forms) returns `NO [READ-ONLY]`. `SELECT`
reports `[READ-ONLY]` and empty `PERMANENTFLAGS`. Do not weaken this without
revisiting the UID scheme (see above), flag storage (local `is_read` /
`is_starred` live in the importer's database, which this server must not
write), and the importer's concurrency model (it has no concurrent-run
protection).

## 5. Protocol implementation notes

- Argument parsing: `tokenize()` handles atoms, quoted strings, nested
  parentheses, and treats `[...]` as part of an atom (so `BODY.PEEK[HEADER.FIELDS
  (From To)]<0.100>` is one token). Quoted strings and atoms are
  indistinguishable after tokenizing (fine for this command subset).
- Client literals (`{n}` / `{n+}`) are read in `read_command()` and folded into
  quoted strings (max 10 MB). `LITERAL+` is **not** advertised though `{n+}` is
  tolerated.
- `section_data()` regex handles `HEADER`, `HEADER.FIELDS[.NOT] (...)`, `TEXT`,
  `MIME`, numeric paths, and the empty section. Numeric paths use `get_part()`,
  which treats a non-multipart message as having a single part `1`. A
  `message/rfc822` part is returned whole and cannot be descended into.
- `BODYSTRUCTURE` is built recursively; `message/rfc822` parts include the inner
  envelope and structure. Extension data (disposition, language) is included for
  `BODYSTRUCTURE`, omitted for `BODY`.
- `ENVELOPE` header values are unfolded but left MIME-encoded (clients decode
  `=?utf-8?...?=` themselves). Non-ASCII/special strings are sent as literals.
- `SEARCH` evaluates predicates over the in-memory message list; header searches
  decode RFC 2047; `BODY`/`TEXT` use decoded text parts (`Loaded.text`).
  Body searches read files, so they are slow over big folders. `CHARSET` is
  accepted and ignored. Unsupported keys return `BAD`.
- Unknown commands return `BAD`; a missing file during FETCH returns
  `NO <strerror>` and logs, rather than crashing the connection.
- `RECENT` is always 0; `UNSEEN` is not sent in the `SELECT` response.
- Per-connection cache: an `OrderedDict` of parsed messages, size
  `cache_messages`. `msg_size()` returns the DB value and never reads the file.

## 6. Testing

There is no test suite in the repository. Testing so far was done in a sandbox
with a **synthetic repository** built from the owner's exact `CREATE TABLE`
statements plus `repository.json`, and Python's `imaplib` as the client:

- messages in two folders; one with a multipart/alternative + attachment; a
  non-ASCII folder name (`Réunions/2023`); a non-ASCII subject and sender;
  an `internal_date` as epoch ms, as ISO 8601, and as junk (fallback);
  files stored with CRLF.
- Multi-account tests used two synthetic repositories with different mailboxes,
  folder sets and `created_at` values. Verified: each login sees only its own
  folders and UIDs; selecting the other account's folder returns `NO`; fetching
  the other account's UID returns nothing; wrong/cross/unknown logins are
  rejected after about 1 s; 3 failures close the connection; `LOGIN` after
  authentication is `BAD`; commands before login are refused (checked over a raw
  socket, because `imaplib` blocks those client-side); a password sent as an IMAP
  literal works; six concurrent logins across both accounts overlap; case-
  insensitive login; `--set-password` (new, replace, mismatch, unknown account),
  preservation of other lines, mode 600; startup refusal with a missing password,
  a malformed hash line, and the old single-account config; `--verify` for all
  accounts and with `--account`.
- Verified: `LIST`, `STATUS`, `SELECT`, `UID FETCH` of flags/size/date/envelope/
  structure/partial/sub-parts, `BODY[]` SHA-256 equals the database value,
  `SEARCH` variants, refusal of `STORE`, bad login, `hide_folders`,
  `allow_networks` rejection, password precedence, config validation errors,
  `--verify` on clean and corrupted/missing files, `INTEGRITY MISMATCH` logging,
  and clean SIGINT/SIGTERM shutdown with a connected client.

Gotchas for writing tests:

- `imaplib.IMAP4.select()` raises when the server returns `[READ-ONLY]`; call
  `select(name, readonly=True)`.
- Use a real Thunderbird session for anything client-specific; `imaplib` is
  much more forgiving.
- Not covered: very large repositories, concurrent clients, Apple/iOS/Outlook
  clients, TLS handshake with real clients, running against a live importer.

A reasonable next step is to commit the synthetic-repository generator and a
scripted `imaplib` suite as `tests/`.

## 7. Known gaps / backlog (rough priority)

1. **systemd unit** (agreed, deliberately deferred until bugs/changes settle):
   dedicated or `pi` user, `Restart=on-failure`, restrictive sandboxing, read
   access to every account's repository directory (plus write on those
   directories for SQLite WAL files) and to the password file. SIGTERM is
   already handled.
2. Persisted ENVELOPE/BODYSTRUCTURE cache (sidecar SQLite, never in
   `repository.db`) if first-open of large folders is slow on the Pi.
2b. Measure `hash_password` cost on the Pi and tune `SCRYPT_N` if logins are
   slow (Thunderbird opens several connections at once; two hashes run at a
   time).
2c. Optional `SIGHUP` reload of the password file and account list.
3. Refresh on `NOOP`/`CHECK` with `EXISTS` for live-sync visibility.
4. Confirm the `internal_date` format against real data and simplify
   `parse_date()`.
5. STARTTLS, `NAMESPACE`, `IDLE` (only if some client needs them).
6. Folder polish: parent `\Noselect` entries, special-use attributes
   (`\Sent`, `\Trash`, `\Junk`, `\All`), a rename map for Gmail system labels.
7. Descending into `message/rfc822` parts; `SENTSINCE`/`KEYWORD` search keys.
8. If write features are ever added: sidecar UID table (see section 4), and a
   design for where local flags/folder changes are stored without fighting the
   importer.

## 8. Conventions for changes

- Keep standard library only and a single file unless there is a strong reason.
- Never open `repository.db` writable and never modify repository files.
- Never log or echo passwords, never put them in the `.ini`, and never weaken
  account isolation (a session holds exactly one backend).
- Anything added to the config must be documented in the `.ini` and `README.md`,
  with a default, and be validated with a clear message that echoes the bad
  value.
- Keep user-facing error text specific (option, value found, how to fix); real
  mistakes in this project were configuration slips, not protocol bugs.
- Update `README.md` (features, config table, limitations) and this file when
  behaviour or decisions change.


## Supporting both importers (added)

The backend accepts two repository types, listed in `SUPPORTED_REPOSITORIES`
(`application` -> allowed `schema_version` values):

- `gmail-local-repository`, schema 2 (Gmail API importer; messages keyed by `gm_id`)
- `imap-local-repository`, schema 1 (generic IMAP importer; messages keyed by
  the SHA-256 of their bytes, no `gm_id`)

Both provide the columns the server reads: `folders.name`, `folder_messages`,
`messages(message_id, filename, message_size, sha256, internal_date,
imported_at, is_read, is_starred)` and `message_shards.directory_name`. The only
importer-specific column the server touched was `messages.gm_id` (used to label
`--verify` output); `all_messages()` now uses `gm_id` for Gmail repositories and
`sha256` for IMAP ones. `repository.json` records `mailbox` for Gmail and
`username` + `host` for IMAP; `ArchiveBackend.mailbox` is a display label only.

Adding another repository type means adding it to `SUPPORTED_REPOSITORIES` and
making sure it provides the columns above.
