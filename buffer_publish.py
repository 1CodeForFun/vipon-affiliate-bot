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

# Pinterest gets a VIDEO pin, scheduled a short lead ahead rather than published
# instantly — see PINTEREST_LEAD_SECONDS. Set False to fall back to a static
# image pin built from the hook thumbnail.
PINTEREST_VIDEO_PINS = True

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


def _warm_url(url, timeout=90):
    """Pull the first bytes of the asset so Cloudinary's edge has it cached.

    Buffer fetches the media synchronously while validating createPost. On a
    cold Cloudinary object that fetch can time out, and Buffer reports
    "Invalid post: Video could not be read from its URL" even though the file is
    perfectly healthy — verified: HTTP 200, video/mp4, 1.4 MB, H.264 High
    720x1280 CFR 30fps. Warming the edge first makes Buffer's fetch fast.
    """
    if not url:
        return
    try:
        r = requests.get(url, stream=True, timeout=timeout)
        r.raise_for_status()
        got = 0
        for chunk in r.iter_content(65536):
            got += len(chunk)
            if got >= 262144:      # 256 KB is plenty to make the edge hot
                break
        r.close()
        log(f"  Buffer: warmed asset ({got:,} bytes read)")
    except Exception as e:
        log(f"  Buffer: could not warm asset ({e}) — posting anyway")


# createPost failures that are about Buffer FETCHING the asset, not about the
# post being malformed. These are worth retrying; a rejected board id is not.
_TRANSIENT_MARKERS = (
    "could not be read",
    "could not fetch",
    "unable to fetch",
    "timed out",
    "timeout",
    "internal server error",
)


def _is_transient(err) -> bool:
    s = str(err).lower()
    return any(m in s for m in _TRANSIENT_MARKERS)


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


def _create_post_assets(key, channel_id, text, assets, metadata,
                        first_comment=None, lead_seconds=0):
    """Low-level createPost with an explicit assets array.

    lead_seconds > 0 schedules the post that far ahead (customScheduled + dueAt)
    instead of publishing immediately. Buffer has to fetch and transcode the
    video before it can derive a cover frame from metadata.thumbnailOffset, and
    with mode:shareNow it pushes to the network before that finishes — Pinterest
    then rejects the coverless pin with "Sorry we could not fetch the image".
    Buffer's own video example uses addToQueue rather than shareNow for the same
    reason. The lead gives the asset time to process.
    """
    inp = {
        "channelId": channel_id,
        "schedulingType": "automatic",
        "text": text,
        "assets": assets,
        "metadata": metadata,
    }
    if lead_seconds > 0:
        import datetime as _dt
        due = (_dt.datetime.now(_dt.timezone.utc)
               + _dt.timedelta(seconds=lead_seconds))
        inp["mode"]  = "customScheduled"
        inp["dueAt"] = due.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    else:
        inp["mode"] = "shareNow"     # publish immediately when this run executes
    if first_comment:
        inp["firstComment"] = {"text": first_comment}

    # Retry only fetch-side failures — Buffer reads the media during validation,
    # and that read is flaky on a cold asset. A malformed post is not retried.
    import time as _t
    last = None
    for attempt, backoff in enumerate((0, 20, 45), start=1):
        if backoff:
            log(f"  Buffer: asset fetch failed — retrying in {backoff}s "
                f"(attempt {attempt}/3)")
            _t.sleep(backoff)
        res = _gql(key, _CREATE_POST, {"input": inp})
        cp  = res.get("createPost") or {}
        if not cp.get("message"):
            return (cp.get("post") or {}).get("id")
        last = RuntimeError(f"createPost rejected: {cp['message']}")
        if not _is_transient(last):
            raise last
    raise last


# Which frame Buffer uses as the video cover, in ms — one second into the AI hook
# clip, so the cover is the pain-point frame. Applies to TikTok and Pinterest.
THUMB_OFFSET_MS = 1000

# Seconds ahead to schedule the Pinterest video pin, giving Buffer time to fetch
# and ingest the video before the publish fires. Set to 0 to publish immediately.
#
# 45s was measured to be too short: a pin scheduled 45s out did not go until 87s
# PAST its due time, and needed a manual retry. Pins whose due time was ~4 min or
# more after creation published within 4-7s of due, every time. 240s sits above
# that observed threshold without being the 12 min originally guessed.
PINTEREST_LEAD_SECONDS = int(os.environ.get("PINTEREST_LEAD_SECONDS") or "240")


def _video_asset(video_url, thumbnail_offset_ms=THUMB_OFFSET_MS, title=None):
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
    meta  = {}
    if thumbnail_offset_ms is not None:
        meta["thumbnailOffset"] = int(thumbnail_offset_ms)
    if title:
        # Buffer's composer shows "Untitled Video" for API-created posts, so this
        # was never being set. VideoMetadataInput.title is the only other field
        # the schema exposes; cheap to populate and it labels the asset properly.
        meta["title"] = title[:100]
    if meta:
        asset["metadata"] = meta
    return {"video": asset}


def _create_post(key, channel_id, text, video_url, metadata,
                 thumbnail_url=None, first_comment=None, title=None):
    return _create_post_assets(key, channel_id, text,
                               [_video_asset(video_url, title=title)],
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

    # Warm the media on Cloudinary's edge before Buffer tries to read it.
    _warm_url(video_url)
    if image_url and image_url != video_url:
        _warm_url(image_url, timeout=45)

    # ── TikTok ──
    # The link and code live in the CAPTION. The original plan was link in comment
    # 1 and code in comment 2, but Buffer's GraphQL API has no createComment
    # mutation and firstComment on CreatePostInput returns HTTP 400 for TikTok, so
    # there is no API path to a comment at all — the caption was going out with no
    # link whatsoever. Each is on its own line so either can be selected and copied
    # without dragging the other along.
    tk = _find_channel(channels, "tiktok")
    if tk:
        tk_link = f"https://www.amazon.com/dp/{asin}?tag={TIKTOK_TAG}"
        code     = (deal.get("code") or "").strip()

        tk_text = (f"{title[:150]}\n\n"
                   f"🔥 {pct}% OFF — limited time!\n\n"
                   f"🛒 {tk_link}")
        if code:
            tk_text += f"\n\n🏷️ Code: {code}"

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
            # Pinterest video pin, SCHEDULED a few minutes out rather than shared
            # instantly. Buffer's documented video example uses addToQueue, not
            # shareNow, and that difference matters: Buffer must fetch and
            # transcode the video before it can cut a cover frame at
            # metadata.thumbnailOffset. shareNow pushes to Pinterest before that
            # finishes, Pinterest gets a pin with no image, and it fails with
            # "Sorry we could not fetch the image". Retry re-sends the same
            # unprocessed asset, which is why only editing and re-saving in
            # Buffer's composer (which forces processing) ever worked.
            #
            # The static image pin stays as a fallback for when the video asset is
            # rejected outright; it publishes, but Pinterest shows it as a still.
            attempts = []
            if PINTEREST_VIDEO_PINS:
                attempts.append(("video pin", [_video_asset(video_url, title=title)],
                                 PINTEREST_LEAD_SECONDS))
            if image_url:
                attempts.append(("image pin", [{"image": {"url": image_url}}], 0))
            if not attempts:
                attempts.append(("video pin", [_video_asset(video_url, title=title)],
                                 PINTEREST_LEAD_SECONDS))

            posted, last_err = False, None
            for label, assets, lead in attempts:
                try:
                    pid = _create_post_assets(key, pin["id"], pin_text, assets, meta,
                                              lead_seconds=lead)
                except Exception as e:
                    last_err = e
                    log(f"  Buffer Pinterest: {label} rejected: {e}")
                    continue

                if lead:
                    # Scheduled: it cannot have published yet, and polling for
                    # 'sent' would just time out. Buffer publishes it once the
                    # asset is processed.
                    log(f"  ✓ Buffer Pinterest: {label} scheduled ~{lead}s out "
                        f"(id={pid}) — Buffer processes the video, then publishes")
                    posted = True
                    break

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
