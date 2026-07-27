#!/usr/bin/env python3
"""
buffer_publish.py — Publish the hook reel to TikTok + Pinterest via the Buffer API.

This is an EXTENSION of the hook-reel flow (publish_reel_hook.py). It runs AFTER the
FB/IG/YT posting block and is fully fail-safe: any error here is caught by the caller
so it can never affect the main posts.

Buffer specifics (new GraphQL API, public beta):
  - Endpoint: POST https://api.buffer.com  (single GraphQL endpoint)
  - Auth:     Authorization: Bearer <API key>   (key from ~/buffer_API_Key.txt)
  - Channels: account.organizations -> channels(input:{organizationId}) {id,name,service}
  - Publish:  createPost(input: CreatePostInput!) with mode:shareNow (immediate),
              assets:[{video:{url}}] (media must be a public URL — our Cloudinary reel),
              metadata.tiktok / metadata.pinterest for per-network fields.
  - TikTok requires a Business account for hands-free auto-publish (this account is).
  - Pinterest pins need a target board (boardServiceId) + a clickable destination url.

Connected channels (Free plan): freshdealsusa (TikTok), FreshDeals (Pinterest Business).
"""

import os
import requests

BUFFER_ENDPOINT = "https://api.buffer.com"

# Per-channel Amazon Associates tracking IDs (so Amazon reports attribute sales per platform)
TIKTOK_TAG    = "tiktoktiktok-20"
PINTEREST_TAG = "pinpinterestfd-20"

PINTEREST_BOARD_NAME = "Daily Coupons and Discounts"

# Amazon disclosure required by both Amazon Associates and TikTok for affiliate content
DISCLOSURE = "#ad As an Amazon Associate I earn from qualifying purchases."


def log(m):
    print(m, flush=True)


def _load_key():
    """Buffer API key: ~/buffer_API_Key.txt (CI copies it there) -> SECRETS_DIR -> env."""
    candidates = [
        os.path.expanduser("~/buffer_API_Key.txt"),
        os.path.join(os.environ.get("SECRETS_DIR", "."), "buffer_API_Key.txt"),
    ]
    for p in candidates:
        if os.path.exists(p):
            k = open(p).read().strip()
            if k:
                return k
    return os.environ.get("BUFFER_API_KEY", "").strip()


def _gql(key, query, variables=None):
    """POST a GraphQL request; raise RuntimeError on HTTP or GraphQL errors."""
    r = requests.post(
        BUFFER_ENDPOINT,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"query": query, "variables": variables or {}},
        timeout=60,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Buffer HTTP {r.status_code}: {r.text[:300]}")
    j = r.json()
    if j.get("errors"):
        raise RuntimeError(f"Buffer GraphQL error: {j['errors']}")
    return j.get("data") or {}


def _get_org_id(key):
    data = _gql(key, "query { account { organizations { id name } } }")
    orgs = (data.get("account") or {}).get("organizations") or []
    if not orgs:
        raise RuntimeError("No Buffer organizations on this account")
    return orgs[0]["id"]


def _get_channels(key, org_id):
    # IDs are Buffer-issued (trusted); inline as string literals to match the docs and
    # avoid guessing custom-scalar variable type names.
    import json as _json
    q = (f"query {{ channels(input: {{ organizationId: {_json.dumps(org_id)} }}) "
         "{ id name service } }")
    data = _gql(key, q)
    return data.get("channels") or []


def _find_channel(channels, service_substr):
    for c in channels:
        if service_substr in (c.get("service") or "").lower():
            return c
    return None


def _get_pinterest_board_id(key, channel_id, board_name):
    """Return the serviceId of the named Pinterest board (or the first board as fallback)."""
    import json as _json
    q = (f"query {{ channel(input: {{ id: {_json.dumps(channel_id)} }}) "
         "{ metadata { ... on PinterestMetadata { boards { serviceId name } } } } }")
    data = _gql(key, q)
    boards = (((data.get("channel") or {}).get("metadata") or {}).get("boards")) or []
    if not boards:
        return None
    want = (board_name or "").strip().lower()
    for b in boards:
        if (b.get("name") or "").strip().lower() == want:
            return b.get("serviceId")
    for b in boards:
        if want and want in (b.get("name") or "").strip().lower():
            return b.get("serviceId")
    log(f"  Buffer: board '{board_name}' not found — using first board '{boards[0].get('name')}'")
    return boards[0].get("serviceId")


_CREATE_POST = """
mutation($input: CreatePostInput!) {
  createPost(input: $input) {
    ... on PostActionSuccess { post { id } }
    ... on MutationError { message }
  }
}
"""


def _create_post_assets(key, channel_id, text, assets, metadata, first_comment=None):
    """Low-level createPost with an explicit assets array (shareNow / immediate)."""
    inp = {
        "channelId": channel_id,
        "schedulingType": "automatic",
        "mode": "shareNow",          # publish immediately when this run executes
        "text": text,
        "assets": assets,
        "metadata": metadata,
    }
    if first_comment:
        inp["firstComment"] = {"text": first_comment}
    res = _gql(key, _CREATE_POST, {"input": inp})
    cp = res.get("createPost") or {}
    if cp.get("message"):
        raise RuntimeError(f"createPost rejected: {cp['message']}")
    return (cp.get("post") or {}).get("id")


def _create_post(key, channel_id, text, video_url, metadata,
                 thumbnail_url=None, first_comment=None):
    # thumbnailUrl in the video asset is rejected by Buffer for all social networks
    # ("social networks do not accept custom video thumbnail images").
    # Pinterest/TikTok thumbnail offset must use metadata.thumbnailOffset (ms) instead.
    video_asset = {"url": video_url}
    return _create_post_assets(key, channel_id, text, [{"video": video_asset}],
                               metadata, first_comment=first_comment)



def post_to_buffer(video_url, deal, script, thumbnail_url=None, image_url=None):
    """Publish the (already-hosted) reel to TikTok + Pinterest via Buffer.
    Each channel is attempted independently; a failure on one never blocks the other,
    and the caller wraps this whole call so Buffer can never break FB/IG/YT."""
    key = _load_key()
    if not key:
        log("  Buffer: no API key (buffer_API_Key.txt) — skipping TikTok/Pinterest")
        return

    org_id   = _get_org_id(key)
    channels = _get_channels(key, org_id)
    log(f"  Buffer: {len(channels)} channel(s) — " +
        ", ".join(f"{c.get('name')}({c.get('service')})" for c in channels))

    title = (deal.get("title_text") or deal.get("title") or "Today's Amazon deal").strip()
    asin  = deal["asin"]
    pct   = deal.get("pct", 0)

    # ── TikTok ──
    # Buffer's GraphQL API has no createComment mutation and rejects firstComment
    # in CreatePostInput for TikTok. Link + code must live in the caption text.
    tk = _find_channel(channels, "tiktok")
    if tk:
        tk_link = f"https://www.amazon.com/dp/{asin}?tag={TIKTOK_TAG}"
        code     = (deal.get("code") or "").strip()

        lines = [
            f"{title[:150]}",
            "",
            f"🔥 {pct}% OFF — limited time!",
            "",
            f"🛒 {tk_link}",
            "",
            DISCLOSURE,
        ]
        if code:
            lines += ["", f"Use code 🏷️ {code} at checkout"]
        tk_text = "\n".join(lines)

        try:
            pid = _create_post(key, tk["id"], tk_text, video_url,
                               metadata={"tiktok": {"isAiGenerated": True}})
            log(f"  ✓ Buffer TikTok: posted (id={pid})")
        except Exception as e:
            log(f"  ✗ Buffer TikTok failed: {e}")
    else:
        log("  Buffer: no TikTok channel connected — skipping")

    # ── Pinterest (destination URL is natively clickable; pin needs a board) ──
    pin = _find_channel(channels, "pinterest")
    if pin:
        pin_link = f"https://www.amazon.com/dp/{asin}?tag={PINTEREST_TAG}"
        pin_text = f"{title[:200]} — {pct}% off today. 🛒 {pin_link}"
        try:
            board_id = _get_pinterest_board_id(key, pin["id"], PINTEREST_BOARD_NAME)
            if not board_id:
                raise RuntimeError("no Pinterest boards available on channel")
            meta = {"pinterest": {"title": title[:95], "url": pin_link,
                                  "boardServiceId": board_id}}
            # Buffer's Pinterest requires an image (a bare video asset is rejected).
            # Prefer a video pin WITH a cover image; if Pinterest is image-only, fall
            # back to a static image pin so it still publishes. image_url is a public
            # product image from the page capture.
            # thumbnailUrl in the video asset is rejected by Buffer for all networks.
            asset_options = [[{"video": {"url": video_url}}]]
            if image_url:
                asset_options.append([{"image": {"url": image_url}}])
            pid, last_err = None, None
            for assets in asset_options:
                try:
                    pid = _create_post_assets(key, pin["id"], pin_text, assets, meta)
                    break
                except Exception as e:
                    last_err = e
                    if "image" in str(e).lower():
                        log(f"  Buffer Pinterest: {e} — falling back to image pin")
                        continue
                    raise
            if pid:
                log(f"  ✓ Buffer Pinterest: posted (id={pid})")
            else:
                log(f"  ✗ Buffer Pinterest failed: {last_err}")
        except Exception as e:
            log(f"  ✗ Buffer Pinterest failed: {e}")
    else:
        log("  Buffer: no Pinterest channel connected — skipping")


if __name__ == "__main__":
    # Manual smoke test: lists channels so you can confirm auth + channel resolution.
    k = _load_key()
    if not k:
        raise SystemExit("No Buffer key found (~/buffer_API_Key.txt or BUFFER_API_KEY)")
    oid = _get_org_id(k)
    log(f"Org: {oid}")
    for c in _get_channels(k, oid):
        log(f"  channel: {c.get('name')} | service={c.get('service')} | id={c.get('id')}")
