#!/usr/bin/env python3
"""
update_yt_bio.py — refresh the YouTube channel bio with today's deal links.

The reel voiceover now ends with "today's links are in the bio", so the bio has
to actually carry them. This reads the current sheet and rewrites the channel
description via the YouTube Data API.

WHY THE BIO AND NOT THE VIDEO DESCRIPTION: each Short already carries its own
affiliate link in its description (see _build_yt_description), but viewers were
missing it. The bio is one place, easy to say aloud, and the same word works on
YouTube, TikTok and Instagram.

WHAT IT CANNOT DO: the bio is shared and always current, while videos are
permanent. A video watched next week will point at a bio listing different
products. That is why the spoken line says TODAY'S links rather than promising
that specific product — the wording has to stay honest as the list rotates.

The header above the marker is preserved exactly as you have written it in
YouTube Studio; only the block below the marker is regenerated, so editing the
header by hand keeps working.

    python update_yt_bio.py --dry-run     # print the new bio, change nothing
    python update_yt_bio.py               # write it

Needs token_youtube.json with the youtube.force-ssl scope. token_youtube.json
is pinned deliberately: one of the other token files on this machine has only
upload+readonly and would fail the write.
"""

import argparse
import io
import json
import os
import re
import sys
import urllib.parse as up
import urllib.request as u

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

SECRETS_DIR = os.environ.get("SECRETS_DIR", ".")
TOKEN_FILE = os.path.join(SECRETS_DIR, "token_youtube.json")

# Everything below this line is regenerated each run; everything above is kept.
MARKER = "— TODAY'S DEALS —"
BIO_LIMIT = 1000          # YouTube's channel description limit
MAX_ITEMS = int(os.getenv("YT_BIO_ITEMS") or "8")
TITLE_CHARS = 42


def log(m):
    print(m, flush=True)


def _access_token():
    tok = json.load(open(TOKEN_FILE, encoding="utf-8"))
    scopes = tok.get("scopes") or []
    if not any("force-ssl" in s or s.endswith("/youtube") for s in scopes):
        raise RuntimeError(
            f"{TOKEN_FILE} lacks a write scope (has {scopes}); "
            "channels.update needs youtube.force-ssl")
    data = up.urlencode({
        "client_id": tok["client_id"], "client_secret": tok["client_secret"],
        "refresh_token": tok["refresh_token"], "grant_type": "refresh_token",
    }).encode()
    r = json.load(u.urlopen(u.Request("https://oauth2.googleapis.com/token", data=data),
                            timeout=45))
    return r["access_token"]


def _api(path, token, method="GET", body=None):
    req = u.Request(f"https://www.googleapis.com/youtube/v3/{path}",
                    method=method,
                    data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Authorization", f"Bearer {token}")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    return json.load(u.urlopen(req, timeout=60))


def _todays_products(limit):
    """Top products from the US sheet, best first, with their YouTube links."""
    import gspread
    from oauth2client.service_account import ServiceAccountCredentials
    import vipon26 as v

    scope = ["https://spreadsheets.google.com/feeds",
             "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(
        os.path.join(SECRETS_DIR, "vipon_google_creds.json"), scope)
    ws = gspread.authorize(creds).open(v.GOOGLE_SHEET_NAME).worksheet("Sheet1")
    rows = ws.get_all_values()[1:]

    out = []
    for r in rows:
        if len(r) < 19:
            continue
        yt_link, title, disc = r[3].strip(), r[8].strip(), r[6].strip()
        if not yt_link or not title:
            continue
        try:
            score = float(r[18] or 0)
        except ValueError:
            score = 0.0
        out.append({"title": title, "link": yt_link, "disc": disc, "score": score})

    out.sort(key=lambda d: -d["score"])          # strongest products first
    return out[:limit]


def _short_title(t):
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) <= TITLE_CHARS:
        return t
    cut = t[:TITLE_CHARS].rsplit(" ", 1)[0]
    return (cut or t[:TITLE_CHARS]).rstrip(" ,-") + "…"


def build_bio(header, products):
    """Header (kept verbatim) + as many of today's deals as fit in 1000 chars."""
    header = header.rstrip()
    body, used = [], len(header) + len(MARKER) + 4
    for p in products:
        disc = f"{p['disc']} off " if p["disc"] else ""
        entry = f"\n{disc}{_short_title(p['title'])}\n{p['link']}"
        if used + len(entry) > BIO_LIMIT:
            break
        body.append(entry)
        used += len(entry)
    if not body:
        return header
    return f"{header}\n\n{MARKER}{''.join(body)}"


def strip_generated(desc):
    """Keep only the hand-written header, dropping any block we added before."""
    return desc.split(MARKER)[0].rstrip() if MARKER in desc else desc.rstrip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="print the new bio without writing it")
    ap.add_argument("--items", type=int, default=MAX_ITEMS)
    args = ap.parse_args()

    token = _access_token()
    me = _api("channels?part=snippet,brandingSettings&mine=true", token)
    if not me.get("items"):
        log("✗ no channel returned for this token")
        return 1
    ch = me["items"][0]
    cur = (ch.get("brandingSettings", {}).get("channel", {}).get("description")
           or ch["snippet"].get("description") or "")
    log(f"  channel: {ch['snippet']['title']} ({ch['id']})")
    log(f"  current bio: {len(cur)} chars")

    header = strip_generated(cur)
    products = _todays_products(args.items)
    log(f"  products available: {len(products)}")
    new_bio = build_bio(header, products)

    listed = new_bio.count("http")
    log(f"\n  ── new bio ({len(new_bio)}/{BIO_LIMIT} chars, {listed} links) ──")
    for line in new_bio.splitlines():
        log(f"   {line[:92]}")
    log("  ──")

    if args.dry_run:
        log("\n  dry run — nothing written")
        return 0
    if new_bio.strip() == cur.strip():
        log("\n  bio already up to date — no write needed")
        return 0

    branding = ch.get("brandingSettings", {})
    branding.setdefault("channel", {})["description"] = new_bio
    _api("channels?part=brandingSettings", token, method="PUT",
         body={"id": ch["id"], "brandingSettings": branding})
    log(f"\n✅ bio updated — {listed} links live")
    return 0


if __name__ == "__main__":
    sys.exit(main())
