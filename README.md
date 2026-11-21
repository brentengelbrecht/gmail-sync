# Gmail Local Repository Importer

A one-way Gmail-to-local repository importer. Gmail is the acquisition
source; the local repository is intended to remain usable independently
of Gmail.

This version supports two ways to authenticate to the Gmail API:

-   **Google Workspace:** service account with domain-wide delegation.
    This is the existing Raspberry Pi/headless flow and remains
    supported.
-   **Personal Gmail (`@gmail.com`):** OAuth 2.0 Desktop authorisation
    performed once on a computer with a browser. The resulting token is
    transferred to the Raspberry Pi; normal imports then run headlessly.

Both authentication methods use the same Gmail API extraction and
repository code. No App Password or IMAP authentication is used.

## 1. What the importer does

-   Creates a local repository on the first run and incrementally
    imports previously unseen Gmail messages on later runs.
-   Lists messages with `includeSpamTrash=False`, matching Gmail's All
    Mail scan and excluding Spam and Trash.
-   Retrieves new messages in Gmail's `raw` format and stores the
    decoded RFC 2822/MIME bytes as message files. Attachments remain
    part of the MIME message.
-   Stores each Gmail message once, even when it has multiple Gmail
    labels.
-   Records Gmail labels separately from local folder memberships.
    Initial label memberships are copied locally; later Gmail label
    changes do not reorganise existing local folders.
-   Does not delete local messages or local folder memberships when
    Gmail messages or labels disappear remotely.
-   Supports explicit local deletion: the message file, canonical
    database record and folder memberships are removed, and a tombstone
    prevents a later Gmail scan from re-importing that message.
-   Uses Gmail message IDs as the canonical remote identity, rather than
    relying on RFC 822 `Message-ID` headers.
-   Stores metadata in SQLite and message files in shards containing at
    most 500 messages each.
-   Maintains `ALL EMAIL` as a normal persisted local folder, with a
    `folder_messages` relationship for every active message. Existing
    repositories are backfilled automatically; new imports receive
    membership as part of the import transaction.
-   Stores file size and SHA-256 information and checks repository file
    integrity before an incremental sync.
-   Uses SQLite WAL mode and full synchronous writes.
-   Creates supporting indexes for message shard lookup and
    Gmail-label-to-local-folder lookup.
-   Displays a single-line progress indicator during message
    scanning/import.

The importer does not write to Gmail. The OAuth scope is read-only:
`https://www.googleapis.com/auth/gmail.readonly`.

The message scan excludes Spam and Trash (`includeSpamTrash=False`).
Gmail drafts are a separate Gmail API resource and are not explicitly
extracted by this version. Gmail account configuration (such as filters,
forwarding rules and signatures) is also outside the current
message-repository import.

## 2. Requirements

-   Python 3.9 or later is recommended.
-   A Google Cloud project with the Gmail API enabled.
-   Network access during authentication and imports.
-   A local filesystem with enough free space for the messages and
    SQLite database.

Install the Python dependencies in the same Python environment that will
run the script:

``` bash
python -m pip install --upgrade \
  google-api-python-client \
  google-auth \
  google-auth-oauthlib
```

`google-auth-oauthlib` is needed for the one-time personal Gmail
authorisation command. The normal OAuth import on the Pi uses the saved
token and does not open a browser.

## 3. Choose the authentication flow

  -----------------------------------------------------------------------
  Account type      Authentication    Where interactive Normal import
                                      setup happens     
  ----------------- ----------------- ----------------- -----------------
  Google Workspace  Service account + Google Cloud and  Headless on Pi
                    domain-wide       Workspace Admin   
                    delegation        configuration     

  Personal          OAuth 2.0 Desktop Mac/PC/Linux      Headless on Pi
  `gmail.com`       app               computer with a   
                                      browser           
  -----------------------------------------------------------------------

Use one authentication method per invocation. The same script supports
both.

------------------------------------------------------------------------

## 4. Google Workspace setup (existing flow)

This is the existing Workspace configuration. The personal Gmail OAuth
addition does not replace it.

### 4.1 Google Cloud configuration

1.  Open the Google Cloud project used for the importer.
2.  Enable the **Gmail API**.
3.  Create or retain the service account used by the importer.
4.  Enable **domain-wide delegation** for that service account.
5.  Record its numeric OAuth client ID.
6.  Create a JSON key for the service account and transfer it securely
    to the Raspberry Pi.

### 4.2 Workspace Admin configuration

1.  Open the Google Workspace Admin console.

2.  Go to **Security → Access and data control → API controls → Manage
    Domain Wide Delegation** (labels may vary slightly).

3.  Add the service account's numeric Client ID.

4.  Authorise exactly this OAuth scope:

    ``` text
    https://www.googleapis.com/auth/gmail.readonly
    ```

5.  Allow time for the delegation change to propagate.

### 4.3 Store the service-account key on the Pi

For example:

``` bash
mkdir -p "$HOME/.config/gmail-local-repository"
chmod 700 "$HOME/.config/gmail-local-repository"
cp /secure/location/service-account.json \
  "$HOME/.config/gmail-local-repository/service-account.json"
chmod 600 "$HOME/.config/gmail-local-repository/service-account.json"
```

Keep the key outside the repository and source-code directory. Do not
commit it to version control or share it.

### 4.4 Configure and run a Workspace import

``` bash
export GMAIL_AUTH_METHOD="workspace"
export GOOGLE_SERVICE_ACCOUNT_FILE="$HOME/.config/gmail-local-repository/service-account.json"
export GMAIL_IMPERSONATE_USER="you@yourdomain.com"
export DB_LOCATION="$HOME/gmail-data"

python gmail_local_repository_api.py
```

For compatibility, the importer also infers Workspace mode when
`GOOGLE_SERVICE_ACCOUNT_FILE` and `GMAIL_IMPERSONATE_USER` are set and
`GMAIL_AUTH_METHOD` is omitted. Existing Workspace environment
configuration therefore continues to work without needing the new
variable.

### 4.5 Existing Workspace authentication test

The separate `test_gmail_service_account.py` diagnostic can still be
used to test the Workspace service-account configuration. It does not
import or modify messages.

------------------------------------------------------------------------

## 5. Personal Gmail setup (one-time off-device authorisation)

The Raspberry Pi does **not** need a browser. Run the `--authorise`
command on a Mac, Windows PC or Linux computer that has a browser. The
command uses a loopback listener on that same computer (`127.0.0.1`); it
does not connect to the Pi, expose a public callback URL, or require
port forwarding or tunnelling.

### 5.1 Create/configure the Google Cloud project

You can use a separate Google Cloud project for personal Gmail OAuth, or
a suitable existing project.

1.  Open Google Cloud Console and select the project.
2.  Enable the **Gmail API**.
3.  Configure the OAuth consent screen / Google Auth Platform branding
    and audience.
4.  For personal `gmail.com` accounts, configure the audience as
    **External**.
5.  Add the Google account you will authorise as a test user while
    testing.
6.  Create an OAuth Client ID with application type **Desktop app**.
7.  Download the client JSON file. Keep it available on the computer
    where you will run `--authorise`.

The OAuth client is a Desktop/installed-app client, not a Web
application client. The script uses the installed-app loopback redirect
and PKCE support provided by Google's OAuth client library.

### 5.2 Understand Google's publishing/testing status

The Gmail `gmail.readonly` scope is a restricted scope. For an app used
only by you (or a small number of personally known users), Google's
personal-use exception may allow use without completing the full
public-app verification process. Google can still show an unverified-app
warning and apply a user cap.

**Do not leave the OAuth consent configuration in Testing for unattended
long-term use.** Google documents that authorisations for external test
users expire after seven days, including offline refresh tokens for
scopes such as Gmail. For ongoing use, review the app's publishing
status and Google's current personal-use exception/verification
requirements. Publishing to Production avoids the Testing-mode seven-day
expiry, but it does not itself mean Google has verified the app or
removed all warnings/limits.

If this importer is later distributed broadly to unrelated users,
revisit Google's restricted-scope verification requirements before doing
so.

### 5.3 Run authorisation on the computer with a browser

Install the dependencies on that computer, then run the same importer
script:

``` bash
python -m pip install --upgrade \
  google-api-python-client google-auth google-auth-oauthlib

python gmail_local_repository_api.py \
  --authorise \
  --account "you@gmail.com" \
  --oauth-client-file "$HOME/Downloads/client_secret_xxxxx.json" \
  --output "$HOME/personal-gmail-token.json"
```

What happens:

1.  The script reads the Desktop OAuth client JSON.
2.  It requests only `gmail.readonly` and requests offline access.
3.  It opens your default browser to Google's sign-in/consent page.
4.  You select the intended personal Gmail account and approve access.
5.  Google redirects the browser to a temporary loopback listener on the
    authorising computer.
6.  The script exchanges the authorisation result, checks that Gmail
    reports the requested account address, and saves a refreshable token
    JSON file.
7.  The token file is written with owner-only file permissions (`600`).

If the browser does not open automatically, the script prints the
authorisation URL. Open that URL in a browser on the same computer where
the command is running.

The script checks the returned Gmail profile before saving the token, so
authorising the wrong Google account will not silently create a token
for the requested address.

### 5.4 Transfer the token to the Raspberry Pi

The token file is a credential. Transfer it over SSH/SCP or another
secure method. Do not email it, put it in a public cloud folder, commit
it to source control, or include it in an ordinary repository backup
without protecting that backup.

For example, first create the destination directory on the Pi:

``` bash
ssh pi@raspberrypi 'mkdir -p ~/.config/gmail-local-repository && chmod 700 ~/.config/gmail-local-repository'
```

Then transfer the token (replace the host and paths as appropriate):

``` bash
scp "$HOME/personal-gmail-token.json" \
  pi@raspberrypi:~/.config/gmail-local-repository/personal-gmail-token.json
```

On the Pi, restrict the token file permissions:

``` bash
chmod 600 "$HOME/.config/gmail-local-repository/personal-gmail-token.json"
```

The token JSON contains the refresh token and OAuth client information
needed by the Google credential library. The Desktop client JSON does
not normally need to be separately installed on the Pi for this
token-based import flow.

### 5.5 Configure and run personal Gmail imports on the Pi

``` bash
export GMAIL_AUTH_METHOD="oauth"
export GMAIL_ACCOUNT="you@gmail.com"
export GMAIL_OAUTH_TOKEN_FILE="$HOME/.config/gmail-local-repository/personal-gmail-token.json"
export DB_LOCATION="$HOME/gmail-data"

python gmail_local_repository_api.py
```

The Pi reads the token, refreshes an expired access token automatically,
and uses the same Gmail API import code as Workspace. When a refreshed
token is written back, the script uses an atomic replacement and
owner-only file permissions.

No browser, OAuth client download, App Password, IMAP connection,
tunnel, or manual token refresh is needed for normal imports.

### 5.6 Reauthorisation

If Google revokes the grant, the refresh token expires, the token file
is lost, or the OAuth client/scopes change:

1.  Run the `--authorise` command again on the computer with a browser.
2.  Use the same OAuth Desktop client and the intended Gmail account.
3.  Transfer the newly created token to the Pi, replacing the old token.
4.  Restore `chmod 600` on the Pi token file.
5.  Run the importer again.

The script deliberately stops with an explanatory error if the token
cannot be refreshed; it does not fall back to Workspace credentials or
silently switch accounts.

------------------------------------------------------------------------

## 6. Configuration reference

### Common


|Variable            |            Required| Description   |
|--------------------|--------------------|---------------|
|`GMAIL_AUTH_METHOD` | No, but recommended|`workspace` or `oauth`. If omitted, the script infers the method from the configured credential variables.|
|`DB_LOCATION`       |                  No|Parent directory for account repositories. Defaults to `./data`.|


### Workspace


|Variable            |            Required| Description   |
|--------------------|--------------------|---------------|
|`GOOGLE_SERVICE_ACCOUNT_FILE`|  Yes | Path to service-account JSON key.|
|`GMAIL_IMPERSONATE_USER`     |  Yes | Workspace mailbox to impersonate.|


### Personal Gmail OAuth

|Variable            |   Required for OAuth| Description   |
|--------------------|--------------------|---------------|
|`GMAIL_ACCOUNT`     |     Yes | Personal Gmail address represented by the token.|
|`GMAIL_OAUTH_TOKEN_FILE`| Yes | Path to the token JSON created by `--authorise` and transferred to the Pi.|


### Authorisation command options

|Option            |   Required with `--authorise`| Description|
|------------------|------------------|---------------|
|`--authorise`     |              Yes | Starts personal Gmail OAuth authorisation.|
|`--account`       |              Yes | Gmail address to authorise and verify.|
|`--oauth-client-file` |          Yes | Downloaded Google OAuth Desktop client JSON.|
|`--output`            |          Yes | Output token path. The token is written owner-only.|


`--help` displays command usage without requiring any account
environment variables.

## 7. Repository layout and import behaviour

For each account, the repository is created under:

``` text
DB_LOCATION/<URL-encoded-email-address>/
    repository.json
    repository.db
    messages/
        <shard directories>/
            <message files>.eml
```

-   A missing or empty account directory starts an initial import.
-   A recognised repository starts a sync that scans Gmail's message
    list and imports only messages not already present locally.
-   This version still performs a full Gmail message-list scan on each
    run; Gmail History API checkpoint-based incremental scanning has not
    been implemented.
-   A non-empty directory that is not a recognised complete repository
    is rejected.
-   A message is stored once, regardless of the number of Gmail labels
    attached to it.
-   `ALL EMAIL` is a normal row in `folders`, not a virtual mailbox or
    implicit property. Every active message has a corresponding row in
    `folder_messages` for this folder.
-   The importer creates `ALL EMAIL` when setting up a repository and
    backfills membership for all existing messages on the next run. This
    is idempotent and does not require downloading messages again.
-   Initial Gmail labels are mapped to local folders. Existing local
    folder membership is not rewritten from later Gmail label changes.
-   Gmail deletions and label removals do not delete local files or
    remove `ALL EMAIL` membership.
-   Only explicit local deletion removes a message from the repository;
    its tombstone prevents re-import.
-   The local repository is not synchronised back to Gmail.

### Persisted folders and `ALL EMAIL`

The local folder model is deliberately ordinary and explicit:

-   `folders` stores `ALL EMAIL` alongside `INBOX` and other local
    folders.
-   `folder_messages` stores the membership relationship between each
    folder and each canonical message.
-   Every active message must belong to `ALL EMAIL`. It is not a virtual
    IMAP mailbox, a special query, or an implicit property of the
    `messages` table.
-   `INBOX` and other folders are subsets/references into the same
    canonical messages and `.eml` files. Archiving or removing a message
    from `INBOX` must not remove it from `ALL EMAIL`.
-   Gmail label changes update Gmail-source label metadata; they do not
    reorganise existing local folder memberships.
-   Remote Gmail deletion does not remove local messages or their
    `ALL EMAIL` membership.
-   Explicit local deletion removes the message and all its folder
    memberships and records a tombstone keyed by the Gmail message ID.

When an existing repository is opened, the importer ensures that
`ALL EMAIL` exists and inserts any missing membership rows for messages
already in the database. This is safe to repeat and does not re-fetch or
re-download their message files.

### Sync behaviour and limitations

The importer currently performs a full Gmail `messages.list` scan on
every run. It is incremental in the sense that it imports only messages
not already stored locally; it does **not** yet use the Gmail History
API to retrieve only changes since a saved history checkpoint. A future
History API implementation is a separate enhancement.

The repository is designed to retain locally stored messages
independently of Gmail. Gmail is only the acquisition source; the
importer does not push local changes to Gmail.

### Integrity checks and indexes

Before a sync, the importer checks the repository's message-file
integrity. The `ALL EMAIL` invariant is also checked: the folder must
exist and every canonical message must have its membership row.
Supporting SQLite indexes include `messages(shard_id)` and
`gmail_labels(local_folder_id)`; index creation is idempotent.

## 8. Current implementation status

The current importer includes both Workspace service-account
authentication and personal Gmail OAuth, the persisted `ALL EMAIL`
folder and automatic backfill, the supporting SQLite indexes,
quota-aware retries, disk-space safeguards, and repository integrity
checks.

The `ALL EMAIL` backfill and membership handling have been exercised
with local SQLite tests, including repeat-run idempotency, membership on
new import, and membership removal on explicit local deletion. The
script has also passed Python syntax compilation.

## 9. Troubleshooting

### `ModuleNotFoundError: No module named 'google'`

Install the dependencies into the exact Python environment used to run
the importer:

``` bash
python -m pip install --upgrade google-api-python-client google-auth google-auth-oauthlib
```

If using a virtual environment, activate it first or use that
environment's full Python path for both `pip` and the script.

### Workspace: `accessNotConfigured` or Gmail API disabled

Check that the Gmail API is enabled in the Google Cloud project
associated with the service account.

### Workspace: delegation or permission error

Check that domain-wide delegation is enabled for the service account,
the numeric Client ID is authorised in Workspace Admin, and the scope is
exactly `https://www.googleapis.com/auth/gmail.readonly`.

### Personal Gmail: app not available / access denied

Check that the OAuth audience is External, the account is listed as a
test user if the app is in Testing, and the Gmail API is enabled. Review
Google's current OAuth publishing and verification requirements.

### Personal Gmail: token expires after seven days

The OAuth consent configuration is likely still in Testing. Google
documents a seven-day expiry for external test-user authorisations using
Gmail scopes. Review the Publishing status and personal-use exception
described above.

### Personal Gmail: token has expired or been revoked

Re-run `--authorise` on a computer with a browser and transfer the
replacement token to the Pi. Confirm that you authorised the intended
Gmail account.

### Wrong account authenticated

The script checks the Gmail profile email address against the configured
account and stops if they differ. Correct the account variable or create
a token for the intended account.

### Gmail API quota / rate-limit errors

Gmail API requests use adaptive pacing and bounded retries to reduce
repeated quota rejections:

-   Requests are initially spaced about 0.25 seconds apart
    (approximately four requests per second). This pacing applies across
    Gmail API operations.
-   HTTP 403 with `rateLimitExceeded`, `userRateLimitExceeded` or
    `quotaExceeded`, and HTTP 429 responses, cause the importer to slow
    its subsequent requests. The interval can grow to five seconds
    between requests.
-   After sustained successful requests, the interval gradually recovers
    toward its normal rate.
-   If the response includes an HTTP `Retry-After` header or Google's
    structured retry-delay information, that server-provided delay is
    used. Otherwise the importer uses exponential backoff with jitter,
    starting at about two seconds and capped at about 120 seconds.
-   HTTP 5xx responses are retried with backoff but do not independently
    change the quota pacing rate. Other HTTP 403 responses (for example,
    an authentication or permission problem) are not retried.
-   The importer makes up to 9 attempts per request (the initial attempt
    plus 8 retries). This applies to Gmail profile, label-list,
    message-list page and individual message retrieval requests.
-   Pressing Ctrl-C interrupts both normal request pacing and quota
    backoff waits. Gmail API HTTP operations and credential refreshes
    also have a 30-second network timeout, preventing a stalled
    connection from waiting indefinitely. The importer logs a clean
    interruption, closes SQLite, and exits with code `130`; messages
    committed before the interruption remain available for the next run.

If Google continues rejecting a request after all retries, the importer
stops rather than marking hundreds of subsequent messages as failed. It
exits with code `3`. Successfully imported messages remain in the
repository. Wait before trying again; the next run scans Gmail again and
skips messages already stored.

### Insufficient disk space / disk full

Before importing and before processing each message, the importer checks
the free space on the filesystem containing the repository. By default
it keeps a 64 MiB safety reserve. Change this with
`GMAIL_MIN_FREE_BYTES`:

``` bash
# Keep the default 64 MiB reserve (no setting required)

# Example: keep a 256 MiB reserve
export GMAIL_MIN_FREE_BYTES=268435456

# Disable the proactive reserve check (not recommended)
export GMAIL_MIN_FREE_BYTES=0
```

If free space falls below the reserve, the importer stops before
starting the next message. If a write nevertheless fails with filesystem
`ENOSPC` or SQLite `SQLITE_FULL`, it stops immediately. The current
message's file/database operation is rolled back or cleaned up where
possible; messages completed before the failure remain committed. SQLite
is closed normally on exit so its WAL state can be recovered by SQLite
on the next open. The importer exits with code `4`.

After freeing disk space, run the same command again. The importer
recognises the existing repository and resumes by skipping messages
already recorded. Do not manually delete `.tmp` files or database files
to force a restart; if an interrupted file is reported by the repository
integrity check, inspect it before proceeding.

### Exit codes

  ---------------------------------------------------------------------
                                     Code Meaning
  --------------------------------------- -----------------------------
                                      `0` Scan completed without
                                          message failures

                                      `1` Scan completed, but one or
                                          more individual messages
                                          failed

                                      `2` Configuration,
                                          authentication, repository or
                                          non-retryable API error

                                      `3` Gmail quota/transient API
                                          errors persisted after
                                          retries; rerun later

                                      `4` Insufficient disk space or
                                          filesystem full; free space
                                          and rerun

                                    `130` User interrupted the importer
                                          with Ctrl-C; rerun to resume
  ---------------------------------------------------------------------

### Repository integrity errors

Do not delete or rename files to silence an integrity error. The
importer stops if it finds missing, orphaned, temporary, or interrupted
message files. Inspect the named paths and restore from a known-good
backup if necessary.

## 10. Operational cautions

-   Do not run two importer processes concurrently against the same
    repository (i.e. email account).
-   Keep service-account keys and OAuth token files outside the
    repository and source tree.
-   Protect offline backups: they contain message contents and account
    metadata. Protect token files separately; the repository itself does
    not need the OAuth token to be readable.
-   Live end-to-end personal Gmail OAuth and full mailbox import have
    not yet been validated in this environment. The source has been
    syntax-checked and local retry/disk-safety helper tests have passed;
    live validation must still be performed against the user's Google
    Cloud OAuth client and account.

## 11. Google documentation

-   [OAuth 2.0 for Desktop
    Apps](https://developers.google.com/identity/protocols/oauth2/native-app)
-   [Using OAuth 2.0 for Google
    APIs](https://developers.google.com/identity/protocols/oauth2/)
-   [Gmail API
    authorization](https://developers.google.com/workspace/gmail/api/auth)
-   [Restricted-scope verification and personal-use
    exception](https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification)
