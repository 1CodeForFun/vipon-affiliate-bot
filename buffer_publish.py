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

# Buffer's API cannot attach a cover frame to a video post, and Pinterest refuses
# a coverless video pin. Leave False so Pinterest receives an image pin, which
# publishes unattended. See the long note in post_to_buffer.
PINTEREST_VIDEO_PINS = False

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


# Which frame Buffer uses as the video cover, in ms — one second into the AI hook
# clip, so the cover is the pain-point frame. This only affects TIKTOK now, since
# Pinterest receives an image pin. It was briefly disabled to test whether it was
# breaking Pinterest; it was not — Buffer simply never attaches a cover to
# API-created video posts at all.
THUMB_OFFSET_MS = 1000


def _video_asset(video_url, thumbnail_offset_ms=THUMB_OFFSET_MS):
    """Video asset carrying an explicit cover frame.

    Buffer's schema documents VideoAssetInput.thumbnailUrl as "Do not use:
    social networks do not accept custom video thumbnail images". The supported
    control is VideoMetadataInput.thumbnailOffset (ms), which picks a frame out
    of the video itself. It belongs on the VIDEO ASSET's metadata — not on
    metadata.tiktok, which has no such field and returns INTERNAL_SERVER_ERROR.

    Without it Buffer stores the post with no cover at all, and Pinterest
    refuses to publish a coverless video pin ("An unknown error has occurred").
    Retrying does not help because retry republishes the same coverless asset;
    only re-saving through Buffer's own composer generates a frame.
    """
    asset = {"url": video_url}
    if thumbnail_offset_ms is not None:
        asset["metadata"] = {"thumbnailOffset": int(thumbnail_offset_ms)}
    return {"video": asset}


def _create_post(key, channel_id, text, video_url, metadata,
                 thumbnail_url=None, first_comment=None):
    return _create_post_assets(key, channel_id, text, [_video_asset(video_url)],
                               metadata, first_comment=first_comment)


_POST_STATUS = """
query($id: PostId!) {
  post(input: { id: $id }) {
    id status sentAt externalLink
    error { message rawError }
  }
}
"""


def _verify_published(key, post_id, tries=6, delay=5):
    """Poll a post until it leaves the sending state.

    createPost only means Buffer ACCEPTED the post; publishing happens
    asynchronously and can still fail. Without this the log reported success
    for pins that never went out. Returns (status, detail).
    """
    import time
    status, detail = "unknown", ""
    for i in range(tries):
        try:
            p = (_gql(key, _POST_STATUS, {"id": post_id}) or {}).get("post") or {}
        except Exception as e:
            return "unknown", f"status check failed: {e}"
        status = p.get("status") or "unknown"
        err    = p.get("error") or {}
        detail = err.get("message") or err.get("rawError") or p.get("externalLink") or ""
        if status in ("sent", "error"):
            return status, detail
        if i < tries - 1:
            time.sleep(delay)
    return status, detail   # still sending/scheduled — report as-is



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
    # NOTE: Buffer's GraphQL API has no createComment mutation, and firstComment in
    # CreatePostInput returned HTTP 400 for TikTok. Pending a confirmed approach for
    # posting comments, the caption contains only the hook text — no link or code.
    tk = _find_channel(channels, "tiktok")
    if tk:
        tk_link = f"https://www.amazon.com/dp/{asin}?tag={TIKTOK_TAG}"
        code     = (deal.get("code") or "").strip()

        tk_text = (f"{title[:150]}\n\n"
                   f"🔥 {pct}% OFF — limited time!")

        try:
            pid = _create_post(key, tk["id"], tk_text, video_url,
                               metadata={"tiktok": {"isAiGenerated": True}})
            status, detail = _verify_published(key, pid)
            if status == "sent":
                log(f"  ✓ Buffer TikTok: published — {detail or pid}")
            elif status == "error":
                log(f"  ✗ Buffer TikTok: Buffer accepted but publishing FAILED — {detail}")
            else:
                log(f"  … Buffer TikTok: still '{status}' (id={pid}) — check Buffer")
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
            # PINTEREST GETS AN IMAGE PIN, NOT A VIDEO.
            #
            # Buffer's API never attaches a cover frame to a video post — only its
            # own composer generates one. Pinterest requires a cover for a video
            # pin, so every API-created video pin dies with:
            #   "Failed to send pin: Sorry we could not fetch the image."
            # Buffer's Retry re-sends the same coverless asset and fails again; the
            # only thing that worked was opening the post, hitting Edit Thumbnail,
            # saving and publishing by hand — every single day.
            #
            # Nothing is lost by dropping the video: Pinterest was only ever
            # rendering these as a static first frame anyway. The image pin uses
            # col L, which is the AI hook frame with the POV text already burned
            # in — the exact frame Pinterest was showing, at full quality — and it
            # publishes automatically. The clickable destination link is carried by
            # metadata.pinterest.url either way.
            #
            # Set PINTEREST_VIDEO_PINS = True to try video again if Buffer ever
            # starts attaching covers via the API.
            attempts = []
            if image_url:
                attempts.append(("image pin", [{"image": {"url": image_url}}]))
            if PINTEREST_VIDEO_PINS or not image_url:
                attempts.append(("video pin", [_video_asset(video_url)]))

            posted, last_err = False, None
            for label, assets in attempts:
                try:
                    pid = _create_post_assets(key, pin["id"], pin_text, assets, meta)
                except Exception as e:
                    last_err = e
                    log(f"  Buffer Pinterest: {label} rejected: {e}")
                    continue

                status, detail = _verify_published(key, pid)
                if status == "sent":
                    log(f"  ✓ Buffer Pinterest: {label} published — {detail or pid}")
                    posted = True
                    break
                if status == "error":
                    last_err = detail
                    log(f"  ✗ Buffer Pinterest: {label} accepted but publishing "
                        f"FAILED — {detail}")
                    continue          # try the image pin
                log(f"  … Buffer Pinterest: {label} still '{status}' (id={pid})")
                posted = True         # queued, not a failure — don't double-post
                break

            if not posted:
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
