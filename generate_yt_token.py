#!/usr/bin/env python3
"""
Generate / re-authorize a YouTube OAuth token for a specific channel.

Usage:
  python generate_yt_token.py token_youtube.json            # FreshDeals channel
  python generate_yt_token.py token_youtube_ultafind.json   # Ultafind channel
  python generate_yt_token.py                                # defaults to token_youtube.json

It opens a browser, asks you to sign in / SWITCH to the correct YouTube
channel, verifies which channel the token belongs to, and saves the token file.

IMPORTANT — before running, set the OAuth app's Publishing status to
"In production" (Google Auth Platform → Audience), or the new refresh token
will be revoked again after 7 days.

Requirements:
  pip install google-auth-oauthlib google-api-python-client
  client_secret.json in this folder (your OAuth 2.0 "Desktop" client JSON)
"""

import json
import os
import sys
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# Match the scopes the publisher uses (incl. force-ssl, as in the original token)
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]

CLIENT_SECRET_FILE = "client_secret.json"   # download from Google Cloud Console

def main():
    # Output filename comes from the command line (so one script does both channels)
    output_token_file = sys.argv[1] if len(sys.argv) > 1 else "token_youtube.json"

    if not os.path.exists(CLIENT_SECRET_FILE):
        print(f"ERROR: {CLIENT_SECRET_FILE} not found.")
        print("Download it from Google Cloud Console:")
        print("  APIs & Services → Credentials → your OAuth 2.0 Client ID → Download JSON")
        return

    print(f"\n{'='*60}")
    print(f"Generating: {output_token_file}")
    print("When the browser opens, sign in as the account that OWNS the")
    print("target YouTube channel. If you have multiple channels, click the")
    print("YouTube icon (top right) and SWITCH to the right channel BEFORE")
    print("authorizing. If you see 'Google hasn't verified this app', click")
    print("Advanced → Go to [app] (unsafe) — it's your own app.")
    print(f"{'='*60}\n")
    input("Press Enter to open the browser...")

    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_FILE, SCOPES)
    # access_type=offline + prompt=consent forces Google to return a refresh_token
    # (without prompt=consent, a re-auth often comes back with no refresh token).
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")

    # Verify which channel this token belongs to
    youtube = build("youtube", "v3", credentials=creds)
    resp = youtube.channels().list(part="snippet", mine=True).execute()
    items = resp.get("items", [])
    if items:
        ch = items[0]["snippet"]
        ch_id = items[0]["id"]
        print(f"\n✓ Authenticated as channel: {ch['title']} (ID: {ch_id})")
    else:
        print("\n⚠️  Could not verify channel — no channels found for this token")

    # Save in same format as existing token_youtube.json
    token_data = {
        "token":         creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri":     creds.token_uri,
        "client_id":     creds.client_id,
        "client_secret": creds.client_secret,
        "scopes":        list(creds.scopes),
        "expiry":        creds.expiry.strftime("%Y-%m-%dT%H:%M:%SZ") if creds.expiry else None,
    }

    if not creds.refresh_token:
        print("\n⚠️  WARNING: no refresh_token returned — the token will die fast.")
        print("    Re-run; the prompt=consent should force one. If it persists,")
        print("    revoke the app's access at myaccount.google.com/permissions and retry.")

    with open(output_token_file, "w") as f:
        json.dump(token_data, f, indent=2)

    print(f"✓ Token saved to {output_token_file}")
    print(f"\nUpload this file to your vipon-secrets repo as: {output_token_file}")

if __name__ == "__main__":
    main()
