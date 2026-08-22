"""
One-time setup: connect the SPLITFRAME STUDIO mailbox (alexhickey@splitframestudio.com)
to Composio, under its own entity — so CLARVIS can read that inbox directly and
put reply DRAFTS in it.

Why it matters: every revenue email goes out FROM the studio address. Until this
runs, CLARVIS can only see studio mail via its forward into personal Gmail, and
can only draft into personal/school — meaning any reply to a prospect had to be
retyped in the right account by hand.

Read + draft only. There is no send capability anywhere in CLARVIS (mail_drafts
has none, and an AST test fails the suite if a send slug ever appears). This
OAuth grant covers Composio's Gmail scopes; what CLARVIS can actually reach is
still the whitelist in second-brain-chat.

Run once, on the Mac:  python3 scripts/connect_studio_gmail.py
Then add to .env and Coolify:  STUDIO_GMAIL_ENTITY=alex-studio
Requires: COMPOSIO_API_KEY
"""

import os
import sys

# Load secrets from the project-root .env (gitignored). This file lives in scripts/.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
except ImportError:
    pass  # dotenv optional — fall back to the ambient environment

from composio import Composio

COMPOSIO_API_KEY = os.environ.get("COMPOSIO_API_KEY")
# Its OWN entity, deliberately: sharing 'alex' would merge the studio inbox into
# the personal one, and the whole point is that these are separate identities
# with separate mail. Matches how school uses 'alex-school'.
STUDIO_USER_ID = os.environ.get("STUDIO_GMAIL_ENTITY", "alex-studio")
STUDIO_ADDRESS = "alexhickey@splitframestudio.com"

if not COMPOSIO_API_KEY:
    sys.exit("Missing required environment variable: COMPOSIO_API_KEY")

composio = Composio(api_key=COMPOSIO_API_KEY)


def get_or_create_auth_config() -> str:
    existing = composio.auth_configs.list(toolkit_slug="gmail")
    for item in existing.items:
        if item.type == "default":          # Composio-managed auth config
            print(f"Reusing existing auth config: {item.id}")
            return item.id
    auth_config = composio.auth_configs.create(
        "gmail", {"type": "use_composio_managed_auth"})
    print(f"Created new auth config: {auth_config.id}")
    return auth_config.id


def main():
    existing = composio.connected_accounts.list(user_ids=[STUDIO_USER_ID])
    if getattr(existing, "items", None):
        for acct in existing.items:
            if str(getattr(acct, "status", "")).upper() == "ACTIVE":
                print(f"Already connected: entity {STUDIO_USER_ID} is ACTIVE. Nothing to do.")
                return

    auth_config_id = get_or_create_auth_config()
    connection_request = composio.connected_accounts.link(STUDIO_USER_ID, auth_config_id)
    print(f"\nOpen this link and sign in as {STUDIO_ADDRESS}")
    print("(NOT your personal or school account — the account picker defaults to "
          "whichever you used last):\n")
    print(connection_request.redirect_url)
    print("\nWaiting for you to complete authorization...")

    connected_account = connection_request.wait_for_connection(timeout=300)
    print(f"\nConnected! Status: {connected_account.status}")
    print(f"\nNow set STUDIO_GMAIL_ENTITY={STUDIO_USER_ID} in ~/second-brain/.env "
          "and in Coolify's environment, then restart the app.")
    print("Verify with:  ask CLARVIS \"list my studio emails\"")


if __name__ == "__main__":
    main()
