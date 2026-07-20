"""
One-time setup: connects Alex's Google Calendar to Composio for the chat brain.

Creates a Composio-managed OAuth auth config for the googlecalendar toolkit (no
Google Cloud project needed), prints a link for Alex to authorize in his browser,
then waits for the connection to go active.

Run once: python3 connect_google_calendar.py
Requires: COMPOSIO_API_KEY env var
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
COMPOSIO_USER_ID = "alex"

if not COMPOSIO_API_KEY:
    sys.exit("Missing required environment variable: COMPOSIO_API_KEY")

composio = Composio(api_key=COMPOSIO_API_KEY)


def get_or_create_auth_config() -> str:
    existing = composio.auth_configs.list(toolkit_slug="googlecalendar")
    for item in existing.items:
        if item.type == "default":  # Composio-managed auth config
            print(f"Reusing existing auth config: {item.id}")
            return item.id

    auth_config = composio.auth_configs.create(
        "googlecalendar",
        {"type": "use_composio_managed_auth"},
    )
    print(f"Created new auth config: {auth_config.id}")
    return auth_config.id


def main():
    auth_config_id = get_or_create_auth_config()

    connection_request = composio.connected_accounts.link(
        COMPOSIO_USER_ID, auth_config_id
    )
    print("\nOpen this link and authorize access to your Google Calendar:")
    print(connection_request.redirect_url)
    print("\nWaiting for you to complete authorization...")

    connected_account = connection_request.wait_for_connection(timeout=180)
    print(f"\nConnected! Status: {connected_account.status}")


if __name__ == "__main__":
    main()
