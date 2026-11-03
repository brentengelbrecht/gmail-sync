# imap_sync

The original script using Gmail's SMTP interface to extract emails to the local 
file system. 

# Gmail Local Repository Importer

Supercedes `imap_sync.py`.

A headless Python importer for copying a Google Workspace Gmail mailbox into a
local, SQLite-indexed repository. It is designed to run on Linux.

The guiding principle is that **Gmail is an acquisition source; the local
repository is authoritative after import**. The importer moves data in one
direction only: Gmail → local repository. It does not write messages, labels,
or other changes back to Gmail.

> **Status:** This is an early implementation. The authentication setup has
> been tested successfully against the Workspace account, and the repository
> database/file routines have had local tests. Keep Gmail and 
> separate backups until you have verified your imported repository.

## Contents

- [Features](#features)
- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Google Workspace and OAuth setup](#google-workspace-and-oauth-setup)
- [Install](#install)
- [Configuration](#configuration)
- [Run the importer](#run-the-importer)
- [Progress and logging](#progress-and-logging)
- [Repository layout](#repository-layout)
- [Data model](#data-model)
- [Initial import and incremental sync](#initial-import-and-incremental-sync)
- [Data integrity and preservation](#data-integrity-and-preservation)
- [Backups](#backups)
- [Security](#security)
- [Current limitations and planned work](#current-limitations-and-planned-work)
- [Troubleshooting](#troubleshooting)

## Features

- **Headless Google Workspace authentication** using a service account with
  domain-wide delegation. No browser sign-in is required bash cli.
- **Read-only Gmail access** using the
  `https://www.googleapis.com/auth/gmail.readonly` OAuth scope.
- **Initial import and incremental sync.** The first run creates a repository
  and imports messages; later runs scan Gmail for messages not already stored.
- **Raw message storage.** New messages are retrieved using Gmail API
  `format="raw"`, base64url-decoded, and written as binary `.eml` files. The
  importer does not parse and reconstruct MIME messages or rewrite headers.
- **One physical file per Gmail message.** A message carrying several Gmail
  labels is stored once, not copied into multiple directories.
- **Gmail labels tracked separately from local folders.** On the initial
  import, Gmail labels are mapped to local folders. Later Gmail label changes
  update the source-label snapshot but do not reorganise existing local folder
  memberships.
- **Local state is not overwritten by Gmail sync.** Existing local folder
  memberships and local read/starred fields are not reset when a message is
  encountered again.
- **Remote deletions do not delete local data.** A message disappearing from
  Gmail does not cause its local file or local folder memberships to be
  removed.
- **Tombstone support in the data model.** A locally deleted Gmail message ID
  can be recorded so a later sync will skip it rather than re-import it.
- **Spam and Trash included.** Gmail message listing uses
  `includeSpamTrash=True`.
- **SQLite metadata database** with foreign-key enforcement, write-ahead
  logging (WAL), and `synchronous=FULL`.
- **Sharded message storage.** The repository allocates shard directories in
  SQLite and limits each shard to 500 message files.
- **Integrity checks.** Before an incremental sync, the script checks that
  indexed message files exist, match their recorded size and SHA-256 digest,
  and that there are no unexpected `.eml`, `.tmp`, or `.deleting` files.
- **Schema migration.** A recognised schema-v1 repository is migrated to
  schema version 2 without rewriting existing message files.
- **Single-line progress display.** The terminal progress line is refreshed
  every ten messages, showing checked, newly imported, and failed counts.

## How it works

1. The script loads its service-account key and mailbox configuration from
   environment variables.
2. It creates delegated credentials for the configured Workspace mailbox and
   verifies the mailbox address returned by Gmail.
3. It retrieves the Gmail label catalogue.
4. It determines whether the target is a new repository or an existing,
   recognised repository.
5. For an existing repository, it checks the stored files before syncing.
6. It paginates through Gmail message IDs. For each non-tombstoned ID:
   - if the message is new locally, it fetches the raw message and stores it;
   - if the message already exists locally, it fetches metadata only and
     refreshes the stored Gmail-label snapshot.
7. It reports the final number of messages newly imported and any failures.

The Gmail API does not provide a reliable total count as the paginated scan is
underway, so the progress display deliberately does not show a percentage.

## Requirements

- Python **3.10 or later** is recommended.
- A Google Workspace account and administrative access to configure
  domain-wide delegation.
- A Google Cloud project with the Gmail API enabled.
- A service account with domain-wide delegation enabled and a downloaded JSON
  key file.
- Local disk space for the message files and SQLite database.
- Network access to Google APIs while authenticating and importing/syncing.
  The repository itself is local, but this importer needs an internet
  connection to contact Gmail.

Python packages:

- `google-auth`
- `google-api-python-client`

The Python standard library supplies SQLite, hashing, filesystem operations,
and the other utilities used by the script.

## Google Workspace and OAuth setup

This importer uses **service-account authentication with Google Workspace
domain-wide delegation**, rather than an interactive OAuth consent flow. This
is suitable for server Linux.

### 1. Create a Google Cloud project

Create or select a project in the Google Cloud Console and enable the
**Gmail API** for that project.

### 2. Create a service account

Create a service account for this importer. Enable **domain-wide delegation**
for the service account and note its numeric OAuth client ID.

### 3. Authorise domain-wide delegation in Workspace

In the Google Workspace Admin console, add the service account's numeric
client ID to the domain-wide delegation authorisation list.

Authorise this exact OAuth scope:

```text
https://www.googleapis.com/auth/gmail.readonly
```

The scope is read-only. Do not add broader Gmail scopes unless the application
is deliberately redesigned and the security implications reviewed.

### 4. Create and protect a JSON key

Create a JSON key for the service account and transfer it securely to the
Linux host. A recommended location is:

```text
~/.config/gmail-local-repository/service-account.json
```

Restrict access to the key:

```bash
mkdir -p "$HOME/.config/gmail-local-repository"
chmod 700 "$HOME/.config/gmail-local-repository"
chmod 600 "$HOME/.config/gmail-local-repository/service-account.json"
```

The last command assumes the key has already been copied to that location.

### 5. Confirm the setup

You can use `test_gmail_service_account.py` to test the service account 
credentials. Help is provided by running with the `-h`, `--help` flags.

The importer authenticates as the mailbox specified by
`GMAIL_IMPERSONATE_USER`. It checks the profile returned by Gmail and stops if
Google reports a different mailbox.

The service account's private key is sensitive: anyone who obtains it may be
able to use the delegated permissions authorised for that service account.
Protect it, do not commit it to source control, and rotate/revoke it if it is
exposed.

## Install

On the Linux host, create a virtual environment (recommended):

```bash
sudo apt update
sudo apt install -y python3 python3-venv

mkdir -p "$HOME/gmail-importer"
python3 -m venv "$HOME/gmail-importer/venv"

"$HOME/gmail-importer/venv/bin/python" -m pip install --upgrade pip
"$HOME/gmail-importer/venv/bin/python" -m pip install \
    google-auth google-api-python-client
```

Copy `gmail_local_repository_api.py` into the project directory, for example:

```text
/home/pi/gmail-importer/gmail_local_repository_api.py
```

Use the virtual environment's Python executable both to install dependencies
and to run the script. Installing packages with one Python interpreter and
running the script with another can cause errors such as
`ModuleNotFoundError: No module named 'google'`.

Check the command-line help:

```bash
"$HOME/gmail-importer/venv/bin/python" \
    "$HOME/gmail-importer/gmail_local_repository_api.py" --help
```

## Configuration

The script is configured using environment variables.

| Variable | Required | Purpose |
|---|---:|---|
| `GOOGLE_SERVICE_ACCOUNT_FILE` | Yes | Full path to the service-account JSON key |
| `GMAIL_IMPERSONATE_USER` | Yes | Workspace mailbox address to import |
| `DB_LOCATION` | No | Parent directory for repositories; defaults to `./data` |

Example:

```bash
export GOOGLE_SERVICE_ACCOUNT_FILE="$HOME/.config/gmail-local-repository/service-account.json"
export GMAIL_IMPERSONATE_USER="you@yourdomain.com"
export DB_LOCATION="$HOME/mail-data"
```

The repository for the mailbox is created below `DB_LOCATION`, in a directory
named using a URL-encoded version of the mailbox address. For example, the
mailbox `you@yourdomain.com` will normally be stored under:

```text
$HOME/mail-data/you%40yourdomain.com/
```

The script does not currently provide command-line options for choosing a
mailbox or repository path; use the environment variables above.

## Run the importer

Run it from a shell after setting the environment variables:

```bash
"$HOME/gmail-importer/venv/bin/python" \
    "$HOME/gmail-importer/gmail_local_repository_api.py"
```

Alternatively, export the variables in a small shell script that you keep
private and run that script. Do not put the service-account JSON key itself
inside the repository.

### First run

If the target repository directory does not exist or is empty, the script
starts an **initial import**.

A non-empty directory is not automatically treated as a repository. It must
contain a recognised `repository.json`, `repository.db`, and `messages/`
directory. If those are missing or the marker is not recognised, the script
stops rather than guessing what the directory contains.

### Subsequent runs

If the target is a recognised repository, the script starts an
**incremental sync**. It scans Gmail's message IDs and imports messages that
are not already present and are not tombstoned.

Existing message content is not downloaded again. Existing messages receive a
metadata fetch so the source-side Gmail-label snapshot can be refreshed.

### Exit status

- `0`: scan completed without per-message import failures.
- `1`: scan completed, but one or more messages failed to retrieve or import.
- `2`: setup, authentication, repository validation, or another top-level
  error prevented normal completion.

## Progress and logging

During a scan, the script refreshes one terminal line every ten messages.
For example:

```text
Progress: checked 1,250 | imported 1,247 | failed 3
```

- `checked` is the number of Gmail message IDs encountered in this scan.
- `imported` is the number of messages newly stored during this run.
- `failed` is the number of individual messages that could not be retrieved
  or imported.

The progress line is not a percentage because the total number of messages
is not known reliably at the start of the paginated scan. Normal log messages
include timestamps and are written to the terminal. A final summary is printed
when the scan completes.

## Repository layout

A typical repository looks like this:

```text
<DB_LOCATION>/
└── <encoded-mailbox-address>/
    ├── repository.json
    ├── repository.db
    ├── repository.db-shm
    ├── repository.db-wal
    └── messages/
        ├── 000001/
        │   ├── <gmail-message-id>.eml
        │   └── ...
        ├── 000002/
        │   └── ...
        └── ...
```

The SQLite `-wal` and `-shm` files may appear while the database is open.
They are SQLite working files, not additional message stores.

Each message shard is limited to 500 indexed message files. Shard allocation is
recorded in SQLite rather than derived from Gmail IDs, which are not
sequential.

`repository.json` identifies the repository and records its schema version,
mailbox, and creation timestamp.

## Data model

The database currently contains these principal tables:

| Table | Purpose |
|---|---|
| `repository_info` | Repository identity and schema metadata |
| `folders` | Local folder names |
| `message_shards` | Shard directory allocation and file counts |
| `messages` | Gmail ID, thread ID, file location, size, SHA-256, dates, and local read/starred fields |
| `folder_messages` | Local message-to-folder memberships |
| `deleted_messages` | Tombstones for locally deleted Gmail IDs |
| `gmail_labels` | Gmail label catalogue and optional local-folder mapping |
| `gmail_message_labels` | Current source-side label snapshot for each message |

The Gmail API message ID (`id`) is the canonical identity used by the
repository. The Gmail thread ID is stored as optional metadata. The RFC 822
`Message-ID` header is not used as the unique key because it may be absent or
duplicated.

### Gmail labels and local folders

Gmail labels and local folders are related during the initial import, but they
are not the same thing:

- During initial import, the script creates a local folder for each Gmail
  label and adds messages to the corresponding local folders.
- On later syncs, changes to Gmail labels update the source metadata tables.
  They do not remove or add local folder memberships for messages that already
  exist locally.
- A newly encountered message is assigned to local folders for labels that
  already have a local-folder mapping.
- A Gmail label first seen during a later sync is recorded as source metadata;
  it does not automatically create a new local folder.

This separation prevents changes made in Gmail from silently reorganising the
local repository.

## Initial import and incremental sync

| Behaviour | Initial import | Incremental sync |
|---|---|---|
| List Gmail messages | Yes | Yes |
| Include Spam and Trash | Yes | Yes |
| Download raw content for new local messages | Yes | Yes, for newly discovered messages |
| Re-download existing message bodies | Not applicable | No |
| Fetch metadata for existing messages | Not applicable | Yes |
| Record Gmail label state | Yes | Refreshes source snapshot |
| Create initial local folders from Gmail labels | Yes | No new folders for newly created Gmail labels |
| Delete local data when Gmail message is missing | No | No |
| Skip tombstoned Gmail IDs | Yes | Yes |

The current incremental implementation scans the complete Gmail message-ID
listing on every run. This ensures it can discover previously unseen messages
without relying on a saved cursor, but it is not yet optimised for very large
mailboxes: it also makes a metadata API request for each already-imported,
non-tombstoned message.

## Data integrity and preservation

### Message files

For each new message, the script:

1. Requests Gmail API `format="raw"`.
2. Base64url-decodes the API's raw field into bytes.
3. Writes those bytes to a temporary file in the destination shard.
4. Flushes and synchronises the file.
5. Renames it into its final `.eml` filename.
6. Records the file size and SHA-256 digest in SQLite.

The importer does not parse and reconstruct the message, decode and re-encode
MIME parts, normalise line endings, or rewrite headers.

**Important qualification:** these are the raw RFC 2822 message bytes returned
by Gmail's API after decoding its `raw` field. Google does not guarantee that
this representation is byte-for-byte identical to the original message as
first transmitted over SMTP.

### Existing repository checks

Before an incremental sync, the script checks:

- every database-indexed message file exists;
- each file's size matches the database;
- each file's SHA-256 digest matches the database;
- there are no untracked `.eml` files;
- there are no leftover `.tmp` or `.deleting` files;
- shard counts do not exceed the configured limit.

If a problem is found, the script stops and reports it rather than silently
deleting or rewriting files. Investigate and repair the repository before
running another sync.

### Deletions

A message deleted from Gmail is not deleted from the local repository.

The script contains an internal `delete_local_message()` helper that removes
an active local message and records its Gmail ID in `deleted_messages`. The
tombstone is checked on future syncs, preventing that ID from being imported
again. **This helper is not currently exposed through a command-line option or
a supported user-facing deletion workflow.** Do not treat local deletion as an
available CLI feature yet.

A tombstone only protects the active repository. It cannot erase copies held
on disconnected backup media.

## Backups

A backup should be a consistent copy of the **entire repository**, including:

- `repository.json`;
- `repository.db`;
- all message shard directories and `.eml` files.

Because SQLite uses WAL mode, do not assume that copying only
`repository.db` while the importer is running produces a complete snapshot.
For a simple filesystem backup, stop the importer first and ensure SQLite has
closed cleanly. Alternatively, use SQLite's online backup facilities or a
snapshot method that captures the database and message files consistently.

Keep backups on separate physical storage and verify them periodically.
A backup is a separate copy: deleting a message from the active repository
does not automatically remove it from old or disconnected backups. Define
your own retention and destruction process if deletion must apply to backups
as well.

## Security

- The OAuth scope is read-only: `gmail.readonly`.
- The service-account JSON key is a credential and must be protected.
- Keep the key outside the repository and outside source control.
- Restrict the key file to the account that runs the importer (`chmod 600`).
- Restrict the key's parent directory (`chmod 700`).
- Review and revoke domain-wide delegation authorisations and rotate the key
  if it is exposed or no longer needed.
- Protect the repository and backups: imported email may contain sensitive
  personal or business information.

The script does not currently encrypt message files or the SQLite database at
rest. Use operating-system or storage-level encryption if encryption at rest
is required.

## Current limitations and planned work

The following are **not implemented as complete user-facing features** in
this version:

- **No local IMAP or POP3 server.** The repository is not yet exposed to mail
  clients. The importer itself only imports and indexes messages.
- **No supported local deletion command.** The tombstone deletion helper is
  present internally, but there is no CLI or interactive deletion interface.
- **No backup automation.** Backups and retention are managed separately.
- **No encryption at rest built into the application.**
- **No incremental Gmail history cursor.** Every run scans all message IDs and
  fetches metadata for existing messages. This will need optimisation for
  large mailboxes.
- **No concurrent-run protection.** Do not run two copies against the same
  repository at the same time. Concurrent imports could race during shard
  allocation or file creation.
- **No explicit retry/backoff policy** for transient Google API failures.
  Individual message failures are counted and logged; the run exits with
  status 1 if any occur.
- **No automatic repair of orphaned files or database inconsistencies.** The
  integrity check stops and asks for investigation rather than guessing.
- **No Gmail-to-local propagation.** Local state is not written back to Gmail.

The database includes local read/starred fields, but this importer does not
currently provide a user interface or command to manage them.

## Troubleshooting

### `ModuleNotFoundError: No module named 'google'`

The Google client libraries are not installed in the Python environment used
to run the script. Install them using the same interpreter:

```bash
/path/to/venv/bin/python -m pip install --upgrade \
    google-auth google-api-python-client
```

Then run the script with that same `/path/to/venv/bin/python`.

### Service-account key file not found

Check that `GOOGLE_SERVICE_ACCOUNT_FILE` points to the JSON key file and that
the file exists and is readable by the account running the script:

```bash
ls -l "$GOOGLE_SERVICE_ACCOUNT_FILE"
```

### Permission warning for the key

Restrict permissions:

```bash
chmod 700 "$(dirname "$GOOGLE_SERVICE_ACCOUNT_FILE")"
chmod 600 "$GOOGLE_SERVICE_ACCOUNT_FILE"
```

### Gmail API `accessNotConfigured` / API not enabled

Enable the Gmail API in the Google Cloud project associated with the service
account. Confirm that the correct project is selected.

### `unauthorized_client` or insufficient permission

Check all of the following:

- domain-wide delegation is enabled on the service account;
- the service account's numeric client ID is authorised in Workspace Admin;
- the authorised scope exactly matches
  `https://www.googleapis.com/auth/gmail.readonly`;
- `GMAIL_IMPERSONATE_USER` is a mailbox in the Workspace domain;
- the key belongs to the service account whose client ID was authorised.

### Requested mailbox does not match Gmail profile

Check the spelling of `GMAIL_IMPERSONATE_USER` and confirm that the delegated
account is authorised to access that mailbox.

### Repository integrity check fails

Do not delete or rename files at random. Preserve a copy of the repository
before attempting repairs. The error should identify missing, altered, or
untracked files. Compare against a known-good backup if available.

### A run reports individual message failures

The importer logs the affected Gmail message ID and the exception. The run
continues with other messages, then exits with status `1` if any message
failed. Resolve the underlying API, permission, network, or local storage
problem and run the importer again; already imported messages are not
re-downloaded as raw content.

---

## Project principle

**Import once, preserve locally, and keep the repository independent of Gmail.**
Gmail is used to acquire messages; local storage and its metadata become the
working copy. Keep verified backups before relying on the repository as the
only copy.
