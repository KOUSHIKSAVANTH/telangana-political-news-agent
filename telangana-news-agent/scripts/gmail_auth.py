"""One-time helper: obtain a Gmail OAuth refresh token for the sending mailbox.

1. Google Cloud console -> APIs & Services -> enable "Gmail API".
2. OAuth consent screen: Internal (Workspace) or External + add the sender as a test user.
3. Credentials -> Create OAuth client ID -> "Desktop app" -> download JSON as client_secret.json.
4. pip install google-auth-oauthlib
   python scripts/gmail_auth.py client_secret.json
5. Sign in AS THE SENDING MAILBOX. Store the printed values in Secret Manager. Delete the JSON.

Scope is gmail.send only: the agent can send mail but cannot read the mailbox.
Note: refresh tokens for External apps in "Testing" status expire after 7 days; publish the
app (or use an Internal Workspace app) for a long-lived token.
"""
import json
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/gmail.send"]


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("usage: python scripts/gmail_auth.py <client_secret.json>")
    flow = InstalledAppFlow.from_client_secrets_file(sys.argv[1], SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
    info = json.load(open(sys.argv[1]))
    client = info.get("installed") or info.get("web")
    print("\nStore these as secrets (do not commit):")
    print(f"GMAIL_CLIENT_ID={client['client_id']}")
    print("GMAIL_CLIENT_SECRET=<from the JSON file>")
    print(f"GMAIL_REFRESH_TOKEN={creds.refresh_token}")


if __name__ == "__main__":
    main()
