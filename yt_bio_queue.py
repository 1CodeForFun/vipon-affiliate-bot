#!/usr/bin/env python3
"""
yt_bio_queue.py — keep the YouTube channel bio as a rolling list of deal links,
and post each Short's own link as a comment.

The reel voiceover now ends with "today's links are in the bio", so the bio has
to carry them. This is driven by the PUBLISHER, not the scrape: every time a
Short goes live its link is pushed onto the top of the bio list and the oldest
entry falls off the bottom. The bio therefore always reflects what was actually
published, in the order it was published.

State lives in the bio itself. Each run reads the current description, parses
the entries below the marker, prepends the new one and writes it back — so
there is no separate state file to drift out of sync, and editing the bio by
hand in Studio still works.

Format is deliberately bare: a short title and the link, nothing else. Someone
reading the bio has just watched the video and knows the product and the
discount; they are there for the link.

Only the block below the marker is regenerated. The header above it is yours
and is preserved exactly.

    python yt_bio_queue.py --show                      # print the current list
    python yt_bio_queue.py --push "Ninja Air Fryer" "https://..." --dry-run
    python yt_bio_queue.py --push "Ninja Air Fryer" "https://..."
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

SECRETS_DIR = os.environ.get("SECRETS_DIR", ".")
TOKEN_FILE = os.path.join(SECRETS_DIR, "token_youtube.json")

MARKER = "— LATEST DEALS —"
BIO_LIMIT = 1000            # YouTube's channel description limit
TITLE_CHARS = 34            # short label, not the full Amazon title
SAFETY = 40                 # leave room so a long header can never overflow


def log(m):
    print(m, flush=True)


# ─── auth ────────────────────────────────────────────────────────────────────
def _access_token(token_file=TOKEN_FILE):
    tok = json.load(open(token_file, encoding="utf-8"))
    scopes = tok.get("scopes") or []
    # One of the token files on this machine carries only upload+readonly, which
    # would fail channels.update with an unhelpful error. Check up front.
    if not any("force-ssl" in s or s.endswith("/youtube") for s in scopes):
        raise RuntimeError(f"{token_file} lacks a write scope (has {scopes})")
    data = up.urlencode({
        "client_id": tok["client_id"], "client_secret": tok["client_secret"],
        "refresh_token": tok["refresh_token"], "grant_type": "refresh_token",
    }).encode()
    return json.load(u.urlopen(
        u.Request("https://oauth2.googleapis.com/token", data=data), timeout=45)
    )["access_token"]


def _api(path, token, method="GET", body=None):
    req = u.Request(f"https://www.googleapis.com/youtube/v3/{path}", method=method,
                    data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Authorization", f"Bearer {token}")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    return json.load(u.urlopen(req, timeout=60))


# ─── bio text ────────────────────────────────────────────────────────────────
def short_title(t):
    """A recognisable label, not the full Amazon title."""
    t = re.sub(r"\s+", " ", (t or "")).strip()
    # Amazon titles front-load the useful words, so a clean truncation is enough.
    t = re.split(r"\s[-|,–]\s", t)[0].strip() or t
    if len(t) <= TITLE_CHARS:
        return t
    cut = t[:TITLE_CHARS].rsplit(" ", 1)[0]
    return (cut or t[:TITLE_CHARS]).rstrip(" ,-") + "…"


def parse_entries(desc):
    """(title, link) pairs already listed below the marker, newest first."""
    if MARKER not in desc:
        return []
    block = desc.split(MARKER, 1)[1]
    lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
    out, i = [], 0
    while i < len(lines) - 1:
        if not lines[i].startswith("http") and lines[i + 1].startswith("http"):
            out.append((lines[i], lines[i + 1]))
            i += 2
        else:
            i += 1
    return out


def header_of(desc):
    return desc.split(MARKER)[0].rstrip() if MARKER in desc else (desc or "").rstrip()


def render(header, entries):
    """Header + as many entries as fit, dropping the oldest that do not."""
    header = (header or "").rstrip()
    budget = BIO_LIMIT - SAFETY - len(header) - len(MARKER) - 4
    kept, used = [], 0
    for title, link in entries:                     # newest first
        block = f"\n{title}\n{link}"
        if used + len(block) > budget:
            break                                   # everything after is older
        kept.append((title, link))
        used += len(block)
    if not kept:
        return header, []
    body = "".join(f"\n{t}\n{l}" for t, l in kept)
    return f"{header}\n\n{MARKER}{body}", kept


def push(title, link, token=None, dry_run=False):
    """Put one product on top of the bio list; the oldest falls off."""
    token = token or _access_token()
    me = _api("channels?part=snippet,brandingSettings&mine=true", token)
    if not me.get("items"):
        raise RuntimeError("no channel for this token")
    ch = me["items"][0]
    branding = ch.get("brandingSettings", {})
    cur = (branding.get("channel", {}).get("description")
           or ch["snippet"].get("description") or "")

    entries = parse_entries(cur)
    label = short_title(title)
    # Re-publishing the same product should move it to the top, not duplicate it.
    entries = [(t, l) for t, l in entries if l != link]
    entries.insert(0, (label, link))

    new_bio, kept = render(header_of(cur), entries)
    log(f"  bio: {len(new_bio)}/{BIO_LIMIT} chars, {len(kept)} links "
        f"(pushed '{label}', dropped {len(entries) - len(kept)})")
    if dry_run:
        log("  dry run — not written")
        return new_bio
    if new_bio.strip() == cur.strip():
        log("  bio unchanged")
        return new_bio
    branding.setdefault("channel", {})["description"] = new_bio
    _api("channels?part=brandingSettings", token, method="PUT",
         body={"id": ch["id"], "brandingSettings": branding})
    log("  ✓ bio updated")
    return new_bio


# ─── comment on the Short ────────────────────────────────────────────────────
def comment_on_video(video_id, text, token=None):
    """Post the video's own affiliate link as a channel comment.

    NOTE: the API cannot PIN a comment — commentThreads exposes only insert and
    list, and no isPinned field exists anywhere in the v3 schema. The comment is
    posted as the channel and can be pinned by hand in Studio afterwards.
    """
    token = token or _access_token()
    body = {"snippet": {"videoId": video_id,
                        "topLevelComment": {"snippet": {"textOriginal": text[:9000]}}}}
    r = _api("commentThreads?part=snippet", token, method="POST", body=body)
    cid = r.get("id", "")
    log(f"  ✓ comment posted ({cid})")
    return cid


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--push", nargs=2, metavar=("TITLE", "LINK"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    token = _access_token()
    if args.show or not args.push:
        me = _api("channels?part=snippet,brandingSettings&mine=true", token)
        ch = me["items"][0]
        cur = (ch.get("brandingSettings", {}).get("channel", {}).get("description")
               or ch["snippet"].get("description") or "")
        entries = parse_entries(cur)
        log(f"  channel: {ch['snippet']['title']}  ({len(cur)}/{BIO_LIMIT} chars)")
        log(f"  header : {len(header_of(cur))} chars")
        log(f"  entries: {len(entries)}")
        for t, l in entries:
            log(f"     {t[:36]:38} {l[:62]}")
        return 0

    push(args.push[0], args.push[1], token=token, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
