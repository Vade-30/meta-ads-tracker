"""
get_refresh_token.py — One-time local OAuth setup for Meta Ads Gmail Monitor.

Run this script LOCALLY (never in CI) to generate the Gmail OAuth refresh
token and save the output values as GitHub Secrets.

Prerequisites
-------------
1. Google Cloud project with Gmail API enabled.
2. OAuth 2.0 Desktop App credentials downloaded as credentials.json in the
   same directory as this script.
3. Your Google account added as a test user on the OAuth consent screen
   (since the app is in "Testing" mode).

Usage
-----
    python get_refresh_token.py

A browser window opens for Google sign-in.  After authorizing, this script
prints the three values to save as GitHub Secrets:

    GMAIL_CLIENT_ID
    GMAIL_CLIENT_SECRET
    GMAIL_REFRESH_TOKEN

SECURITY NOTES
--------------
- credentials.json is listed in .gitignore and must NEVER be committed.
- This script is safe to keep in the public repo — it contains no secrets.
- The printed tokens are for YOUR eyes only; save them immediately as
  GitHub Secrets and treat them like passwords.
"""

import json
import os
import sys

try:
    from google_auth_oauthlib.flow import InstalledAppFlow
except ImportError:
    print(
        "ERROR: google-auth-oauthlib is not installed.\n"
        "Run:  pip install google-auth-oauthlib\n"
    )
    sys.exit(1)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
CREDENTIALS_FILE = os.path.join(os.path.dirname(__file__), "credentials.json")


def main() -> None:
    if not os.path.exists(CREDENTIALS_FILE):
        print(
            f"ERROR: credentials.json not found at {CREDENTIALS_FILE}\n"
            "\n"
            "Steps to get it:\n"
            "  1. Go to https://console.cloud.google.com/apis/credentials\n"
            "  2. Create an OAuth 2.0 Client ID (type: Desktop app)\n"
            "  3. Download the JSON file and save it as credentials.json\n"
            "     in the root of this repo directory.\n"
        )
        sys.exit(1)

    print("Opening browser for Google OAuth authorization…\n")

    flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, scopes=SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")

    # Extract the three values needed as GitHub Secrets
    client_id     = creds.client_id
    client_secret = creds.client_secret
    refresh_token = creds.refresh_token

    if not refresh_token:
        print(
            "\nERROR: No refresh token was returned.\n"
            "This can happen if your account has already authorized this app.\n"
            "To force a new refresh token:\n"
            "  1. Go to https://myaccount.google.com/permissions\n"
            "  2. Revoke access for your app.\n"
            "  3. Re-run this script.\n"
        )
        sys.exit(1)

    # Also read client_id/secret directly from credentials.json as a fallback
    # (creds object always has them from the flow, but belt-and-suspenders)
    if not client_id or not client_secret:
        with open(CREDENTIALS_FILE) as f:
            creds_data = json.load(f)
        installed = creds_data.get("installed") or creds_data.get("web") or {}
        client_id     = client_id     or installed.get("client_id", "")
        client_secret = client_secret or installed.get("client_secret", "")

    print("\n" + "=" * 60)
    print("SUCCESS!  Save these three values as GitHub Secrets:")
    print("=" * 60)
    print(f"\nGMAIL_CLIENT_ID\n  {client_id}")
    print(f"\nGMAIL_CLIENT_SECRET\n  {client_secret}")
    print(f"\nGMAIL_REFRESH_TOKEN\n  {refresh_token}")
    print("\n" + "=" * 60)
    print(
        "\nWhere to add them:\n"
        "  Your GitHub repo → Settings → Secrets and variables\n"
        "  → Actions → New repository secret\n"
        "\nNEVER share these values or commit them to the repo.\n"
    )


if __name__ == "__main__":
    main()
