#!/usr/bin/env python3
"""vipon_publisher.py — Posts ONE reel per run from the vipon Google Sheet.

Per run:
  1. Read Sheet1, find first row where col N (Reel URL) is set and col P != "Yes"
  2. Determine odd/even row position → select FB page + YT channel
  3. Post FB Reel  (3-step: init → upload bytes → publish)
  4. Post IG Reel  (2-step: create container → publish)
  5. Post YouTube Short (download + resumable upload)
  6. Mark col P = "Yes" in the sheet

Odd rows  (1, 3, 5 …) → Fresh Deals FB page + FreshDeals YT channel
Even rows (2, 4, 6 …) → Ultafind FB page    + Ultafind YT channel
IG always posts to freshdealsus (single IG account).

Environment variable:
  SECRETS_DIR — folder containing credential files (default: current dir)
"""

import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import gspread
import requests
from oauth2client.service_account import ServiceAccountCredentials
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
import googleapiclient.discovery
import googleapiclient.http

# ─── CONFIG ──────────────────────────────────────────────────────────────────
SECRETS_DIR = os.environ.get("SECRETS_DIR", ".")


def _p(filename: str) -> str:
    return os.path.join(SECRETS_DIR, filename)


GOOGLE_SHEET_NAME   = "vipon"
GOOGLE_CREDS_FILE   = _p("vipon_google_creds.json")
FB_FRESHDEALS_TOKEN = _p("fb_page_token.json")
FB_ULTAFIND_TOKEN   = _p("fb_page_token-ultafind.json")
YT_TOKEN_FILE           = _p("token_youtube.json")           # FreshDeals channel
YT_ULTAFIND_TOKEN_FILE  = _p("token_youtube_ultafind.json")  # Ultafind channel

IG_FRESHDEALS_USER_ID = "17841462518097134"   # freshdealsus Instagram business account
IG_ULTAFIND_USER_ID   = "17841465105802629"   # ultafind Instagram business account
YT_FRESHDEALS_CH    = "UCoD24sN6sKc7kxvVsCxgUCg"
YT_ULTAFIND_CH      = "UCX-OndLQAxZJMDE21Vj_hBA"
GRAPH_API_VERSION   = "v25.0"

TIMEOUT             = 120   # seconds for normal API calls
UPLOAD_TIMEOUT      = 600   # seconds for video upload/download
IG_POLL_INTERVAL    = 15    # seconds between Instagram status polls
IG_MAX_POLLS        = 40    # 40 × 15 s = 10 min max wait

# Sheet columns (1-based; subtract 1 for Python list index)
COL_B_REEL_LINK = 2
COL_C_IG_LINK   = 3
COL_D_YT_LINK   = 4
COL_I_TITLE     = 9
COL_N_REEL_URL  = 14
COL_O_POST_TEXT = 15
COL_P_POSTED    = 16

# ─── LOGGING ─────────────────────────────────────────────────────────────────
def log(msg: str) -> None:
    print(msg, flush=True)


# ─── GOOGLE SHEET ────────────────────────────────────────────────────────────
def open_sheet():
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDS_FILE, scope)
    return gspread.authorize(creds).open(GOOGLE_SHEET_NAME).sheet1


def get_next_reel(ws):
    """Return (sheet_row_num, row, data_row_index) for the first unposted reel.

    data_row_index is 1-based (1 = first row below the header).
    Returns (None, None, None) when there is nothing to post.
    """
    rows = ws.get_all_values()
    for data_idx, row in enumerate(rows[1:], start=1):
        while len(row) < COL_P_POSTED:
            row.append("")
        reel_url = row[COL_N_REEL_URL - 1].strip()
        posted   = row[COL_P_POSTED - 1].strip().lower()
        if reel_url and posted != "yes":
            sheet_row = data_idx + 1  # +1 accounts for header row
            return sheet_row, row, data_idx
    return None, None, None


# ─── FACEBOOK TOKEN ──────────────────────────────────────────────────────────
def load_fb_token(token_file: str):
    data = json.loads(Path(token_file).read_text(encoding="utf-8"))
    return (
        data["page_id"],
        data["page_access_token"],
        data.get("graph_api_version", GRAPH_API_VERSION),
    )


# ─── HELPERS ─────────────────────────────────────────────────────────────────
def _check(r: requests.Response, label: str) -> None:
    try:
        data = r.json()
    except Exception:
        data = {"raw": r.text[:500]}
    if not r.ok:
        raise RuntimeError(f"{label} HTTP {r.status_code}: {data}")
    if isinstance(data, dict) and "error" in data:
        raise RuntimeError(f"{label} API error: {data['error']}")


def _download_video(url: str) -> bytes:
    log(f"Downloading video ({url[:70]}...)...")
    r = requests.get(url, timeout=UPLOAD_TIMEOUT)
    r.raise_for_status()
    log(f"Downloaded {len(r.content):,} bytes")
    return r.content


# ─── FACEBOOK REELS (3-step resumable upload) ────────────────────────────────
def post_fb_reel(page_id: str, page_token: str, video_url: str,
                 title: str, description: str) -> str:
    base = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

    # Step 1: initialise upload session
    log("FB: initialising reel upload session...")
    r = requests.post(
        f"{base}/{page_id}/video_reels",
        data={"upload_phase": "start", "access_token": page_token},
        timeout=TIMEOUT,
    )
    _check(r, "FB Reel init")
    d = r.json()
    video_id   = d["video_id"]
    upload_url = d["upload_url"]
    log(f"FB: video_id={video_id}")

    # Step 2: upload raw bytes
    video_bytes = _download_video(video_url)
    log(f"FB: uploading {len(video_bytes):,} bytes to upload URL...")
    r = requests.post(
        upload_url,
        headers={
            "Authorization": f"OAuth {page_token}",
            "offset": "0",
            "file_size": str(len(video_bytes)),
        },
        data=video_bytes,
        timeout=UPLOAD_TIMEOUT,
    )
    _check(r, "FB Reel upload")
    log("FB: bytes uploaded successfully")

    # Step 3: publish
    log("FB: publishing reel...")
    r = requests.post(
        f"{base}/{page_id}/video_reels",
        data={
            "upload_phase": "finish",
            "video_id": video_id,
            "video_state": "PUBLISHED",
            "title": title[:250],
            "description": description[:2000],
            "access_token": page_token,
        },
        timeout=TIMEOUT,
    )
    _check(r, "FB Reel publish")
    log(f"FB: reel published → video_id={video_id}")
    return video_id


# ─── INSTAGRAM REELS (2-step container + publish) ────────────────────────────
def post_ig_reel(ig_user_id: str, page_token: str, video_url: str,
                 caption: str) -> str:
    base = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

    # Step 1: create media container (Instagram downloads from Cloudinary URL)
    log("IG: creating reel container...")
    r = requests.post(
        f"{base}/{ig_user_id}/media",
        data={
            "media_type": "REELS",
            "video_url": video_url,
            "caption": caption[:2200],
            "share_to_feed": "true",
            "access_token": page_token,
        },
        timeout=TIMEOUT,
    )
    _check(r, "IG container create")
    container_id = r.json()["id"]
    log(f"IG: container_id={container_id}, waiting for processing...")

    # Poll until FINISHED or ERROR.
    # Tolerate up to 3 consecutive HTTP errors (subcode 33 "object not accessible"
    # is a known transient error while Instagram is still initialising the container).
    consecutive_http_errors = 0
    status_code = "UNKNOWN"
    for attempt in range(1, IG_MAX_POLLS + 1):
        time.sleep(IG_POLL_INTERVAL)
        r = requests.get(
            f"{base}/{container_id}",
            params={"fields": "status_code,status", "access_token": page_token},
            timeout=TIMEOUT,
        )
        if not r.ok:
            consecutive_http_errors += 1
            log(f"IG: poll {attempt}/{IG_MAX_POLLS} → HTTP {r.status_code} "
                f"(transient error {consecutive_http_errors}/3, retrying...)")
            if consecutive_http_errors >= 3:
                _check(r, "IG status poll")   # now raise permanently
            continue
        consecutive_http_errors = 0
        st = r.json()
        status_code = st.get("status_code", "UNKNOWN")
        log(f"IG: poll {attempt}/{IG_MAX_POLLS} → {status_code}")
        if status_code == "FINISHED":
            break
        if status_code == "ERROR":
            raise RuntimeError(f"IG container processing failed: {st}")
    else:
        raise RuntimeError(f"IG container timed out after {IG_MAX_POLLS} polls, last status: {status_code}")

    # Step 2: publish the container
    log("IG: publishing reel...")
    r = requests.post(
        f"{base}/{ig_user_id}/media_publish",
        data={"creation_id": container_id, "access_token": page_token},
        timeout=TIMEOUT,
    )
    _check(r, "IG publish")
    media_id = r.json()["id"]
    log(f"IG: reel published → media_id={media_id}")
    return media_id


# ─── YOUTUBE SHORTS (download + resumable upload) ────────────────────────────
def _build_yt_client(token_file=None):
    token_file = token_file or YT_TOKEN_FILE
    with open(token_file, encoding="utf-8") as f:
        td = json.load(f)

    expiry_str = td.get("expiry")
    # google-auth's creds.expired compares expiry with datetime.utcnow() (naive),
    # so expiry must also be naive UTC — do NOT attach tzinfo here.
    expiry = (
        datetime.strptime(expiry_str, "%Y-%m-%dT%H:%M:%SZ")
        if expiry_str else None
    )

    creds = Credentials(
        token=td.get("token"),
        refresh_token=td.get("refresh_token"),
        token_uri=td.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=td.get("client_id"),
        client_secret=td.get("client_secret"),
        scopes=td.get("scopes"),
        expiry=expiry,
    )

    if creds.expired and creds.refresh_token:
        log("YT: access token expired — refreshing...")
        creds.refresh(Request())
        td["token"] = creds.token
        td["expiry"] = (
            creds.expiry.strftime("%Y-%m-%dT%H:%M:%SZ") if creds.expiry else None
        )
        with open(token_file, "w", encoding="utf-8") as f:
            json.dump(td, f, indent=2)
        log("YT: token refreshed and saved")

    return googleapiclient.discovery.build(
        "youtube", "v3", credentials=creds, cache_discovery=False
    )


def _yt_upload(req) -> dict:
    response = None
    while response is None:
        status, response = req.next_chunk()
        if status:
            log(f"YT: upload {int(status.progress() * 100)}%")
    return response


def post_youtube_short(video_url: str, title: str, description: str,
                       yt_token_file: str = None) -> str:
    """Download the Cloudinary video and upload it to YouTube as a Short."""
    video_bytes = _download_video(video_url)

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        tmp.write(video_bytes)
        tmp_path = tmp.name

    try:
        youtube = _build_yt_client(yt_token_file)

        body = {
            "snippet": {
                "title": title[:100],
                "description": description[:5000],
                "categoryId": "26",  # Howto & Style
            },
            "status": {
                "privacyStatus": "public",
                "selfDeclaredMadeForKids": False,
            },
        }

        def _make_media():
            return googleapiclient.http.MediaFileUpload(
                tmp_path, chunksize=-1, resumable=True, mimetype="video/mp4"
            )

        # Upload directly — the token already authenticates the correct channel
        log("YT: uploading...")
        req = youtube.videos().insert(
            part="snippet,status",
            body=body,
            media_body=_make_media(),
        )
        response = _yt_upload(req)

        video_id = response.get("id", "")
        log(f"YT: Short uploaded → video_id={video_id}")
        return video_id
    finally:
        os.unlink(tmp_path)


# ─── MAIN ────────────────────────────────────────────────────────────────────
def main() -> None:
    log("=== vipon_publisher starting ===")

    ws = open_sheet()
    sheet_row, row, data_idx = get_next_reel(ws)

    if sheet_row is None:
        log("No unposted reels found — nothing to do.")
        return

    reel_url  = row[COL_N_REEL_URL - 1].strip()
    post_text = row[COL_O_POST_TEXT - 1].strip()
    title     = row[COL_I_TITLE - 1].strip() or "Deal Alert!"
    reel_link = row[COL_B_REEL_LINK - 1].strip()
    yt_link   = row[COL_D_YT_LINK - 1].strip()

    # Odd rows → FreshDeals, Even rows → Ultafind
    is_odd        = (data_idx % 2 == 1)
    platform      = "FreshDeals" if is_odd else "Ultafind"
    fb_token_file = FB_FRESHDEALS_TOKEN        if is_odd else FB_ULTAFIND_TOKEN
    yt_token_file = YT_TOKEN_FILE              if is_odd else YT_ULTAFIND_TOKEN_FILE

    log(f"Row {sheet_row} (position {data_idx}): posting to {platform}")
    log(f"  Reel URL : {reel_url[:80]}")
    log(f"  Title    : {title[:70]}")

    page_id, page_token, _ = load_fb_token(fb_token_file)
    # IG uses the same page token as FB — each IG account is linked to its own FB page
    ig_user_id = IG_FRESHDEALS_USER_ID if is_odd else IG_ULTAFIND_USER_ID

    errors = []

    # ── Facebook Reel ──────────────────────────────────────────────────────
    log("--- Facebook ---")
    try:
        post_fb_reel(page_id, page_token, reel_url, title, post_text)
    except Exception as e:
        log(f"ERROR (FB): {e}")
        errors.append(f"FB: {e}")

    # ── Instagram Reel (alternates: freshdealsus odd / ultafind even) ──────
    log(f"--- Instagram ({ig_user_id}) ---")
    ig_caption = f"{post_text}\n\n{reel_link}" if reel_link else post_text
    try:
        post_ig_reel(ig_user_id, page_token, reel_url, ig_caption)
    except Exception as e:
        log(f"ERROR (IG): {e}")
        errors.append(f"IG: {e}")

    # ── YouTube Short ──────────────────────────────────────────────────────
    yt_label = "FreshDeals YT" if is_odd else "Ultafind YT"
    log(f"--- YouTube ({yt_label}) ---")
    yt_desc = f"{post_text}\n\n{yt_link}" if yt_link else post_text
    # Check token file exists; if Ultafind token not yet created, fall back to FreshDeals
    if not os.path.exists(yt_token_file):
        log(f"  ⚠️ {yt_token_file} not found — falling back to FreshDeals YT token")
        yt_token_file = YT_TOKEN_FILE
    try:
        post_youtube_short(reel_url, title, yt_desc, yt_token_file=yt_token_file)
    except Exception as e:
        log(f"ERROR (YT): {e}")
        errors.append(f"YT: {e}")

    # ── Mark as posted regardless of individual platform errors ───────────
    ws.update_acell(f"P{sheet_row}", "Yes")
    log(f"Sheet row {sheet_row} col P → Yes")

    if errors:
        log(f"Done with {len(errors)} error(s): {'; '.join(errors)}")
    else:
        log("All platforms posted successfully.")

    log("=== vipon_publisher done ===")


if __name__ == "__main__":
    main()
