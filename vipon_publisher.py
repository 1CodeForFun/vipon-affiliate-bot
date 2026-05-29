#!/usr/bin/env python3
"""vipon_publisher.py — Posts ONE reel per run from the vipon Google Sheet.

Per run:
  US  (Sheet1): find first unposted reel -> post to ALL platforms simultaneously:
                FreshDeals FB + Ultafind FB + freshdealsus IG + ultafind IG
                + FreshDeals YT + Ultafind YT
  CA  (Sheet2): find first unposted reel -> post to Fresh Deals Canada FB only

Col P = "Yes" when the row has been processed (prevents re-picking).
Col Q = "Yes" only when at least one YouTube upload succeeded (blank = Make.com retries YT).

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


GOOGLE_SHEET_NAME        = "vipon"
GOOGLE_CREDS_FILE        = _p("vipon_google_creds.json")

# US FB pages
FB_FRESHDEALS_TOKEN      = _p("fb_page_token.json")
FB_ULTAFIND_TOKEN        = _p("fb_page_token-ultafind.json")

# Canada FB page
FB_CANADA_TOKEN          = _p("fb_page_token-canada.json")
FB_CANADA_PAGE_ID        = "100649111740976"   # Fresh Deals Canada

# YouTube channels
YT_TOKEN_FILE            = _p("token_youtube.json")           # FreshDeals YT
YT_ULTAFIND_TOKEN_FILE   = _p("token_youtube_ultafind.json")  # Ultafind YT

# Instagram accounts
IG_FRESHDEALS_USER_ID    = "17841462518097134"   # freshdealsus
IG_ULTAFIND_USER_ID      = "17841465105802629"   # ultafind

GRAPH_API_VERSION        = "v25.0"
TIMEOUT                  = 120
UPLOAD_TIMEOUT           = 600
IG_PROCESS_WAIT          = 60
IG_RETRY_WAIT            = 30
IG_MAX_RETRIES           = 3

# Sheet columns (1-based)
COL_A_AFF_LINK   = 1
COL_B_REEL_LINK  = 2
COL_C_IG_LINK    = 3
COL_D_YT_LINK    = 4
COL_F_CODE       = 6   # discount code
COL_G_DISC       = 7   # discount %
COL_I_TITLE      = 9
COL_N_REEL_URL   = 14
COL_O_POST_TEXT  = 15
COL_P_POSTED     = 16   # "Yes" = FB + IG reels posted (prevents re-pick)
COL_Q_FB_TEXT    = 17   # "Yes" = FB text post done (set by FBP_ready.py)
COL_R_YT_POSTED  = 18   # "Yes" = YouTube posted (set by publisher OR Make.com)

# ─── LOGGING ─────────────────────────────────────────────────────────────────
def log(msg: str) -> None:
    print(msg, flush=True)


# ─── GOOGLE SHEET ────────────────────────────────────────────────────────────
def _open_worksheet(tab=None):
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds  = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDS_FILE, scope)
    client = gspread.authorize(creds)
    ss     = client.open(GOOGLE_SHEET_NAME)
    if tab is None:
        return ss.sheet1
    try:
        return ss.worksheet(tab)
    except Exception:
        return ss.get_worksheet(1)   # fallback: 2nd tab by index


def open_sheet():
    return _open_worksheet()          # Sheet1 — US


def open_sheet2():
    return _open_worksheet("Sheet2")  # Sheet2 — Canada


def get_next_reel(ws):
    """Return (sheet_row_num, row, data_row_index) for first unposted reel.
    data_row_index is 1-based (1 = first data row below the header).
    Returns (None, None, None) when nothing to post.
    """
    rows = ws.get_all_values()
    for data_idx, row in enumerate(rows[1:], start=1):
        while len(row) < COL_P_POSTED:
            row.append("")
        reel_url = row[COL_N_REEL_URL - 1].strip()
        posted   = row[COL_P_POSTED - 1].strip().lower()
        if reel_url and posted != "yes":
            return data_idx + 1, row, data_idx   # +1 for header row
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


# ─── FACEBOOK REELS ──────────────────────────────────────────────────────────
def post_fb_reel(page_id: str, page_token: str, video_url: str,
                 title: str, description: str) -> str:
    base = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

    log("FB: initialising reel upload session...")
    r = requests.post(
        f"{base}/{page_id}/video_reels",
        data={"upload_phase": "start", "access_token": page_token},
        timeout=TIMEOUT,
    )
    _check(r, "FB Reel init")
    d          = r.json()
    video_id   = d["video_id"]
    upload_url = d["upload_url"]
    log(f"FB: video_id={video_id}")

    video_bytes = _download_video(video_url)
    log(f"FB: uploading {len(video_bytes):,} bytes...")
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

    log("FB: publishing reel...")
    r = requests.post(
        f"{base}/{page_id}/video_reels",
        data={
            "upload_phase": "finish",
            "video_id":     video_id,
            "video_state":  "PUBLISHED",
            "title":        title[:250],
            "description":  description[:2000],
            "access_token": page_token,
        },
        timeout=TIMEOUT,
    )
    _check(r, "FB Reel publish")
    log(f"FB: reel published ->video_id={video_id}")
    return video_id


# ─── INSTAGRAM REELS ─────────────────────────────────────────────────────────
def post_ig_reel(ig_user_id: str, page_token: str, video_url: str,
                 caption: str) -> str:
    base = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

    log("IG: creating reel container...")
    r = requests.post(
        f"{base}/{ig_user_id}/media",
        data={
            "media_type":    "REELS",
            "video_url":     video_url,
            "caption":       caption[:2200],
            "share_to_feed": "true",
            "access_token":  page_token,
        },
        timeout=TIMEOUT,
    )
    _check(r, "IG container create")
    container_id = r.json()["id"]
    log(f"IG: container_id={container_id}, waiting for processing...")

    log(f"IG: waiting {IG_PROCESS_WAIT}s for Instagram to process video...")
    time.sleep(IG_PROCESS_WAIT)

    for attempt in range(1, IG_MAX_RETRIES + 1):
        log(f"IG: publishing reel (attempt {attempt}/{IG_MAX_RETRIES})...")
        r = requests.post(
            f"{base}/{ig_user_id}/media_publish",
            data={"creation_id": container_id, "access_token": page_token},
            timeout=TIMEOUT,
        )
        try:
            resp_data = r.json()
        except Exception:
            resp_data = {"raw": r.text[:500]}

        if (not r.ok
                and isinstance(resp_data, dict)
                and resp_data.get("error", {}).get("error_subcode") == 2207027
                and attempt < IG_MAX_RETRIES):
            log(f"IG: video not ready — waiting {IG_RETRY_WAIT}s more...")
            time.sleep(IG_RETRY_WAIT)
            continue

        _check(r, "IG publish")
        media_id = resp_data["id"]
        log(f"IG: reel published ->media_id={media_id}")
        return media_id


# ─── YOUTUBE SHORTS ──────────────────────────────────────────────────────────
def _build_yt_client(token_file=None):
    token_file = token_file or YT_TOKEN_FILE
    with open(token_file, encoding="utf-8") as f:
        td = json.load(f)

    expiry_str = td.get("expiry")
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
        td["token"]  = creds.token
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
    video_bytes = _download_video(video_url)

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        tmp.write(video_bytes)
        tmp_path = tmp.name

    try:
        youtube = _build_yt_client(yt_token_file)

        body = {
            "snippet": {
                "title":       title[:100],
                "description": description[:5000],
                "categoryId":  "26",
            },
            "status": {
                "privacyStatus":          "public",
                "selfDeclaredMadeForKids": False,
            },
            "paidProductPlacementDetails": {
                "hasPaidProductPlacement": True,
            },
        }

        def _make_media():
            return googleapiclient.http.MediaFileUpload(
                tmp_path, chunksize=-1, resumable=True, mimetype="video/mp4"
            )

        log("YT: uploading...")
        req      = youtube.videos().insert(
            part="snippet,status,paidProductPlacementDetails",
            body=body,
            media_body=_make_media(),
        )
        response = _yt_upload(req)
        video_id = response.get("id", "")
        log(f"YT: Short uploaded ->video_id={video_id}")
        return video_id
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


# ─── YOUTUBE DESCRIPTION ─────────────────────────────────────────────────────
def _build_yt_description(aff_link: str, code: str, disc: str) -> str:
    """Short, copy-friendly YouTube description with clickable affiliate link."""
    parts = []
    if code:
        discount_str = f" ({disc} off)" if disc else ""
        parts.append(f"🏷️ Discount code: {code}{discount_str}")
    if aff_link:
        parts.append(f"\n🛒 Get the deal:\n{aff_link}")
    return "\n".join(parts) if parts else ""


# ─── MAIN ────────────────────────────────────────────────────────────────────
def main() -> None:
    log("=== vipon_publisher starting ===")

    # ── US: Sheet1 — post to ALL platforms ───────────────────────────────────
    ws        = open_sheet()
    sheet_row, row, data_idx = get_next_reel(ws)

    if sheet_row is None:
        log("US: No unposted reels — skipping.")
    else:
        log(f"US: Row {sheet_row} -> FreshDeals + Ultafind (FB, IG, YT)")

        reel_url   = row[COL_N_REEL_URL  - 1].strip()
        post_text  = row[COL_O_POST_TEXT - 1].strip()
        title      = row[COL_I_TITLE     - 1].strip() or "Deal Alert!"
        reel_link  = row[COL_B_REEL_LINK - 1].strip()
        yt_link    = row[COL_D_YT_LINK   - 1].strip()
        aff_link   = row[COL_A_AFF_LINK  - 1].strip()
        code       = row[COL_F_CODE      - 1].strip()
        disc       = row[COL_G_DISC      - 1].strip()
        yt_desc    = _build_yt_description(aff_link, code, disc)

        errors     = []
        yt_success = False

        # ── Facebook: FreshDeals + Ultafind ──────────────────────────────────
        for fb_file, fb_label in [
            (FB_FRESHDEALS_TOKEN, "FreshDeals"),
            (FB_ULTAFIND_TOKEN,   "Ultafind"),
        ]:
            log(f"--- Facebook ({fb_label}) ---")
            try:
                pid, tok, _ = load_fb_token(fb_file)
                post_fb_reel(pid, tok, reel_url, title, aff_link)
            except Exception as e:
                log(f"ERROR (FB {fb_label}): {e}")
                errors.append(f"FB-{fb_label}: {e}")

        # ── Instagram: freshdealsus + ultafind ───────────────────────────────
        for ig_uid, ig_label, fb_file in [
            (IG_FRESHDEALS_USER_ID, "freshdealsus", FB_FRESHDEALS_TOKEN),
            (IG_ULTAFIND_USER_ID,   "ultafind",     FB_ULTAFIND_TOKEN),
        ]:
            log(f"--- Instagram ({ig_label}) ---")
            try:
                _, tok, _ = load_fb_token(fb_file)
                post_ig_reel(ig_uid, tok, reel_url, aff_link)
            except Exception as e:
                log(f"ERROR (IG {ig_label}): {e}")
                errors.append(f"IG-{ig_label}: {e}")

        # ── YouTube: FreshDeals YT + Ultafind YT ─────────────────────────────
        for yt_file, yt_label in [
            (YT_TOKEN_FILE,         "FreshDeals YT"),
            (YT_ULTAFIND_TOKEN_FILE,"Ultafind YT"),
        ]:
            log(f"--- YouTube ({yt_label}) ---")
            if not os.path.exists(yt_file):
                log(f"  warning: {yt_file} not found — skipping")
                continue
            try:
                post_youtube_short(reel_url, title, yt_desc, yt_token_file=yt_file)
                yt_success = True
            except Exception as e:
                log(f"ERROR (YT {yt_label}): {e}")
                errors.append(f"YT-{yt_label}: {e}")

        # ── Mark sheet ────────────────────────────────────────────────────────
        ws.update_acell(f"P{sheet_row}", "Yes")
        log(f"US: row {sheet_row} col P -> Yes")
        if yt_success:
            ws.update_acell(f"R{sheet_row}", "Yes")
            log(f"US: row {sheet_row} col R -> Yes (YouTube posted)")
        else:
            log(f"US: row {sheet_row} col R left blank (YouTube failed — Make.com retries)")

        if errors:
            log(f"US done with {len(errors)} error(s): {'; '.join(str(e) for e in errors)}")
        else:
            log("US: All platforms posted successfully.")

    # ── Canada: Sheet2 — FB only ─────────────────────────────────────────────
    ws2        = open_sheet2()
    sheet_row2, row2, _ = get_next_reel(ws2)

    if sheet_row2 is None:
        log("CA: No unposted reels — skipping.")
    else:
        log(f"CA: Row {sheet_row2} -> Fresh Deals Canada (FB only)")

        reel_url2  = row2[COL_N_REEL_URL  - 1].strip()
        title2     = row2[COL_I_TITLE     - 1].strip() or "Deal Alert!"
        aff_link2  = row2[COL_A_AFF_LINK  - 1].strip()
        errors2    = []

        log("--- Facebook (Fresh Deals Canada) ---")
        try:
            if os.path.exists(FB_CANADA_TOKEN):
                pid2, tok2, ver2 = load_fb_token(FB_CANADA_TOKEN)
                log(f"  CA token file found: page_id={pid2}, api_version={ver2}, token_len={len(tok2)}")
            else:
                # Token file not yet created — use Canada Page ID with FreshDeals user token
                log("  warning: fb_page_token-canada.json not found — using FreshDeals token with CA page ID")
                _, tok2, ver2 = load_fb_token(FB_FRESHDEALS_TOKEN)
                pid2 = FB_CANADA_PAGE_ID
            post_fb_reel(pid2, tok2, reel_url2, title2, aff_link2)
        except Exception as e:
            log(f"ERROR (FB Canada): {e}")
            errors2.append(f"FB-CA: {e}")

        if errors2:
            log(f"CA done with {len(errors2)} error(s): {'; '.join(str(e) for e in errors2)}")
            log(f"CA: row {sheet_row2} col P NOT marked (FB reel failed — will retry next run)")
        else:
            ws2.update_acell(f"P{sheet_row2}", "Yes")
            log(f"CA: row {sheet_row2} col P -> Yes")
            log("CA: Facebook reel posted successfully.")

    log("=== vipon_publisher done ===")


if __name__ == "__main__":
    main()
