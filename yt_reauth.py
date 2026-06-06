#!/usr/bin/env python3
"""
yt_reauth.py — Re-authorize a YouTube channel and write its token file.

Run LOCALLY (it opens a browser). Do this once per channel, signing into the
correct Google/YouTube account each time.

Prereq:
    pip install google-auth-oauthlib google-api-python-client
    client_secret.json must be in this folder (your OAuth "Desktop" client)

Usage:
    python yt_reauth.py token_youtube.json            # FreshDeals channel
    python yt_reauth.py token_youtube_ultafind.json   # Ultafind channel

After it writes the file, upload it to the vipon-secrets repo (same filename).
IMPORTANT: set the OAuth app's Publishing status to "In production" FIRST
(Google Auth Platform → Audience), or the new token still dies in 7 days.
"""

import sys

from google_auth_oauthlib.flow import InstalledAppFlow

# Must match the scopes already used by the publisher
SCOPES = [
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]

CLIENT_SECRET_FILE = "client_secret.json"


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "token_youtube.json"
    print(f"Re-authorizing → will save to: {out}")
    print("A browser will open. Sign in with the channel's Google account and "
          "grant access.\n")

    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_FILE, SCOPES)
    # access_type=offline + prompt=consent forces Google to return a refresh_token
    creds = flow.run_local_server(
        port=0,
        access_type="offline",
        prompt="consent",
    )

    with open(out, "w", encoding="utf-8") as f:
        f.write(creds.to_json())

    print(f"\n✅ Saved {out}")
    print("   Now upload it to the vipon-secrets repo with the same filename.")


if __name__ == "__main__":
    main()
