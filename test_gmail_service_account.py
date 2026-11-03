#!/usr/bin/env python3
"""Headless Gmail API test using Workspace domain-wide delegation.

Environment:
  GOOGLE_SERVICE_ACCOUNT_FILE=/path/to/service-account.json
  GMAIL_IMPERSONATE_USER=mailbox@your-domain.example

Install:
  python -m pip install --upgrade google-api-python-client google-auth
"""

import os
import sys
from pathlib import Path

from google.oauth2 import service_account
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def main():
    key_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
    mailbox = os.environ.get("GMAIL_IMPERSONATE_USER", "").strip()

    if not key_path:
        print("ERROR: GOOGLE_SERVICE_ACCOUNT_FILE is not set.")
        return 2
    if not mailbox or "@" not in mailbox:
        print("ERROR: GMAIL_IMPERSONATE_USER must be a full Workspace email address.")
        return 2

    key_file = Path(key_path).expanduser()
    if not key_file.is_file():
        print(f"ERROR: Service account key file not found: {key_file}")
        return 2

    try:
        credentials = service_account.Credentials.from_service_account_file(
            str(key_file), scopes=SCOPES, subject=mailbox
        )
        credentials.refresh(Request())

        gmail = build("gmail", "v1", credentials=credentials, cache_discovery=False)
        profile = gmail.users().getProfile(userId="me").execute()

        returned = profile.get("emailAddress", "")
        if returned.lower() != mailbox.lower():
            print("ERROR: Gmail returned a different account than requested.")
            print(f"Requested: {mailbox}")
            print(f"Returned:  {returned or '(not supplied)'}")
            return 1

        print("SUCCESS: Headless service-account authentication is working.")
        print(f"Authenticated Workspace mailbox: {returned}")
        print(f"Gmail-reported message count: {profile.get('messagesTotal', 'unknown')}")
        print("Scope: gmail.readonly")
        print("No messages were retrieved, changed, sent, or deleted.")
        return 0

    except HttpError as exc:
        print(f"GMAIL API ERROR: {exc}")
        print(
            "Check Gmail API enablement, domain-wide delegation, the authorised "
            "numeric Client ID, the exact gmail.readonly scope, and the mailbox "
            "address. Newly authorised delegation can take time to propagate."
        )
        return 1
    except Exception as exc:
        print(f"AUTHENTICATION ERROR: {type(exc).__name__}: {exc}")
        print("Check the JSON key path, file permissions, and Python environment.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
