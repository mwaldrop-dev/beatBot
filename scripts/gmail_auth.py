#!/usr/bin/env python3
"""
One-time script to generate a Gmail OAuth2 refresh token.

Run this LOCALLY (not on Railway) once:
    pip install google-auth-oauthlib
    python scripts/gmail_auth.py

It will open a browser for you to authorize the app, then print the
refresh token to copy into your Railway environment variables.
"""

import json
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Paste your OAuth2 client credentials from Google Cloud Console here,
# or point to your downloaded credentials JSON file.
CLIENT_CONFIG = {
    "installed": {
        "client_id": input("Enter your Gmail OAuth2 client_id: ").strip(),
        "client_secret": input("Enter your Gmail OAuth2 client_secret: ").strip(),
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["urn:ietf:wg:oauth:2.0:oob", "http://localhost"],
    }
}

flow = InstalledAppFlow.from_client_config(CLIENT_CONFIG, SCOPES)
creds = flow.run_local_server(port=0)

print("\n" + "=" * 60)
print("SUCCESS! Copy these values into your Railway environment:")
print("=" * 60)
print(f"GMAIL_CLIENT_ID={CLIENT_CONFIG['installed']['client_id']}")
print(f"GMAIL_CLIENT_SECRET={CLIENT_CONFIG['installed']['client_secret']}")
print(f"GMAIL_REFRESH_TOKEN={creds.refresh_token}")
print("=" * 60)
