#!/usr/bin/env python3
"""vipon_publisher.py — Posts REELS_PER_RUN reels per run from the vipon Google Sheet.

Per run:
  US  (Sheet1): find next REELS_PER_RUN unposted reels -> post each to ALL platforms:
                FreshDeals FB + Ultafind FB + freshdealsus IG + ultafind IG
                + FreshDeals YT + Ultafind YT
  CA  (Sheet2): find next REELS_PER_RUN unposted reels -> post each to Fresh Deals Canada FB only

Col P = "Yes" when the row has been processed (prevents re-picking).
Col Q = "Yes" only when at least one YouTube upload succeeded (blank = Make.com retries YT).

Environment variable:
  SECRETS_DIR — folder containing credential files (default: current dir)
"""

import json
import os
import re
import shutil
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

# Reuse the PROVEN v4 concept builder (callable; importing runs no side effects)
from reel_concept_test import (
    build_concept_video, cloud_upload, _which_ffmpeg, _find_font, _read_gemini_keys,
)

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
REELS_PER_RUN            = 1      # one video per run (6 cron slots/day)
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
COL_H_EXPIRY     = 8
COL_I_TITLE      = 9
COL_J_PRICE      = 10
COL_L_IMAGE      = 12   # cover image
COL_N_REEL_URL   = 14
COL_O_VO         = 15   # VO script (piece 1) — now feeds the video voiceover
COL_P_POSTED     = 16   # "Yes" = reel posted (prevents re-pick)
COL_Q_FB_TEXT    = 17   # "Yes" = FB text post done (set by FBP_ready.py)
COL_R_YT_POSTED  = 18   # "Yes" = YouTube posted
COL_S_SOCIAL     = 19   # Social Score (set by scrape) — used to rank selection

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


def _asin_from_link(link: str) -> str:
    m = re.search(r"asin=([A-Za-z0-9]{10})", link, re.I) or re.search(r"\b(B0[A-Z0-9]{8})\b", link, re.I)
    return m.group(1).upper() if m else ""


def select_candidates(ws):
    """Return unposted rows ranked by Social Score (Col S), highest first.
    Each item: (sheet_row_num, row, product_dict). Only rows with a link, title
    and a valid ASIN qualify."""
    rows = ws.get_all_values()
    cands = []
    for data_idx, row in enumerate(rows[1:], start=2):
        while len(row) < COL_S_SOCIAL:
            row.append("")
        link   = row[COL_A_AFF_LINK - 1].strip()
        title  = row[COL_I_TITLE   - 1].strip()
        posted = row[COL_P_POSTED  - 1].strip().lower()
        if not link or not title or posted == "yes":
            continue
        asin = _asin_from_link(link)
        if not asin:
            continue
        try:    score = float(row[COL_S_SOCIAL - 1] or 0)
        except Exception: score = 0.0
        product = {
            "title":   title,
            "asin":    asin,
            "price":   row[COL_J_PRICE  - 1].strip(),
            "code":    row[COL_F_CODE   - 1].strip(),
            "disc":    row[COL_G_DISC   - 1].strip(),
            "expiry":  row[COL_H_EXPIRY - 1].strip(),
            "aff_link": link,                                       # A → freshdeal00cc-20 (FB text)
            "reel_link": row[COL_B_REEL_LINK - 1].strip() or link,   # B → manus00-20 (FB/IG reels)
            "ig_link":   row[COL_C_IG_LINK   - 1].strip() or link,   # C → insinstagram-20 (IG)
            "yt_link":   row[COL_D_YT_LINK   - 1].strip() or link,   # D → youtubefdusa-20 (YouTube)
            "cover":   row[COL_L_IMAGE  - 1].strip(),
            "vo_text": row[COL_O_VO     - 1].strip(),   # piece 1 (VO script)
        }
        cands.append((score, data_idx, row, product))
    cands.sort(key=lambda c: c[0], reverse=True)
    return [(idx, row, p) for _, idx, row, p in cands]


def build_and_upload(product, keys, ffmpeg, font, tld="com"):
    """Build the v4 concept video (with AI hook prepended) and upload to Cloudinary.

    Returns (video_url, thumb_url, thumb_local_path):
      video_url       — Cloudinary URL of the final video (hook + carousel)
      thumb_url       — Cloudinary URL of the peak-moment hook image (thumbnail)
      thumb_local_path — local copy of the thumbnail image for YouTube upload;
                         caller must os.unlink() it after use.
    Returns ("", None, None) on build failure.
    """
    from cf_image_hook import generate_hook, prepend_hook_to_reel

    # Persistent temp file for thumbnail — lives beyond the build temp dir
    _tfd = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    thumb_local = _tfd.name
    _tfd.close()

    try:
        with tempfile.TemporaryDirectory(prefix="reel_build_") as td:
            # Step 1: Build carousel reel
            reel_path = build_concept_video(product, keys, ffmpeg, font, td,
                                            vo_text=product.get("vo_text") or None,
                                            tld=tld)
            if not reel_path:
                return "", None, None

            final_path = reel_path
            thumb_url  = None

            # Step 2: Generate AI hook images + prepend (non-fatal if it fails)
            try:
                hook_clip, thumb_img = generate_hook(product, keys, ffmpeg, td)
                if hook_clip and thumb_img:
                    hooked = os.path.join(td, "hooked.mp4")
                    prepend_hook_to_reel(hook_clip, reel_path, ffmpeg, hooked)
                    final_path = hooked
                    shutil.copy2(thumb_img, thumb_local)
                    log("  CF hook: prepended to reel, thumbnail saved")
            except Exception as e:
                log(f"  CF hook failed (non-fatal — posting reel without hook): {e}")

            # Step 3: Upload video
            pub       = f"vipon_reels/{product['asin']}_{int(time.time())}"
            video_url = cloud_upload(final_path, pub)
            if not video_url:
                return "", None, None

            # Step 4: Upload thumbnail image if we have one
            if os.path.getsize(thumb_local) > 0:
                try:
                    thumb_url = cloud_upload(thumb_local, pub + "_thumb", kind="image")
                    log(f"  Hook thumbnail → {thumb_url}")
                except Exception as e:
                    log(f"  Thumbnail upload failed (non-fatal): {e}")

        return video_url, thumb_url, thumb_local

    except Exception:
        try: os.unlink(thumb_local)
        except: pass
        raise


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
                       yt_token_file: str = None, thumbnail_path: str = None) -> str:
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

        # Set custom thumbnail (frame from Veo hook) if provided
        if thumbnail_path and os.path.exists(thumbnail_path) and video_id:
            try:
                log("YT: setting thumbnail...")
                youtube.thumbnails().set(
                    videoId=video_id,
                    media_body=googleapiclient.http.MediaFileUpload(
                        thumbnail_path, mimetype="image/jpeg", chunksize=-1, resumable=False)
                ).execute()
                log("YT: thumbnail set")
            except Exception as e:
                log(f"YT: thumbnail set failed (non-fatal): {e}")

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
    log("=== vipon_publisher starting (build-on-publish, 1 video/run) ===")
    keys   = _read_gemini_keys()
    ffmpeg = _which_ffmpeg()
    font   = _find_font()
    log(f"Gemini keys: {len(keys)} | ffmpeg: {ffmpeg} | font: {'yes' if font else 'no'}")

    # ── US: Sheet1 — build the top-ranked product, post to all platforms ─────
    ws    = open_sheet()
    cands = select_candidates(ws)
    log(f"\nUS: {len(cands)} candidate(s); selecting highest social score…")

    posted_us = False
    for sheet_row, row, product in cands[:4]:   # try up to 4 until one builds
        log(f"US: building reel for row {sheet_row} — {product['title'][:55]}")
        video_url, thumb_url, thumb_path = build_and_upload(
            product, keys, ffmpeg, font, tld="com"
        )
        if not video_url:
            log(f"US: build failed for row {sheet_row} — trying next candidate")
            if thumb_path:
                try: os.unlink(thumb_path)
                except: pass
            continue

        title     = product["title"] or "Deal Alert!"
        reel_link = product["reel_link"]
        ig_link   = product["ig_link"]
        yt_desc   = _build_yt_description(product["yt_link"], product["code"], product["disc"])
        errors, yt_success = [], False

        for fb_file, fb_label in [(FB_FRESHDEALS_TOKEN, "FreshDeals"), (FB_ULTAFIND_TOKEN, "Ultafind")]:
            log(f"--- Facebook ({fb_label}) ---")
            try:
                pid, tok, _ = load_fb_token(fb_file)
                post_fb_reel(pid, tok, video_url, title, reel_link)
            except Exception as e:
                log(f"ERROR (FB {fb_label}): {e}"); errors.append(f"FB-{fb_label}: {e}")

        for ig_uid, ig_label, fb_file in [
            (IG_FRESHDEALS_USER_ID, "freshdealsus", FB_FRESHDEALS_TOKEN),
            (IG_ULTAFIND_USER_ID,   "ultafind",     FB_ULTAFIND_TOKEN),
        ]:
            log(f"--- Instagram ({ig_label}) ---")
            try:
                _, tok, _ = load_fb_token(fb_file)
                post_ig_reel(ig_uid, tok, video_url, ig_link)
            except Exception as e:
                log(f"ERROR (IG {ig_label}): {e}"); errors.append(f"IG-{ig_label}: {e}")

        for yt_file, yt_label in [(YT_TOKEN_FILE, "FreshDeals YT"), (YT_ULTAFIND_TOKEN_FILE, "Ultafind YT")]:
            log(f"--- YouTube ({yt_label}) ---")
            if not os.path.exists(yt_file):
                log(f"  warning: {yt_file} not found — skipping"); continue
            try:
                post_youtube_short(video_url, title, yt_desc, yt_token_file=yt_file,
                                   thumbnail_path=thumb_path)
                yt_success = True
            except Exception as e:
                log(f"ERROR (YT {yt_label}): {e}"); errors.append(f"YT-{yt_label}: {e}")

        # Clean up local thumbnail after all YouTube uploads
        if thumb_path:
            try: os.unlink(thumb_path)
            except: pass

        # Write-back
        wb_cells = [
            (f"N{sheet_row}", video_url),
            (f"P{sheet_row}", "Yes"),
        ]
        if yt_success:
            wb_cells.append((f"R{sheet_row}", "Yes"))
        if thumb_url:
            wb_cells.append((f"L{sheet_row}", thumb_url))   # hook peak-frame → Pinterest cover
        for _cell, _val in wb_cells:
            try:
                ws.update_acell(_cell, _val)
            except Exception as _e:
                log(f"  ⚠️ sheet write {_cell}={_val!r} failed (row may no longer exist): {_e}")
        log(f"US row {sheet_row}: posted ({len(errors)} error(s))" +
            (f": {'; '.join(map(str, errors))}" if errors else " — all platforms OK"))
        posted_us = True
        break

    if not posted_us:
        log("US: nothing posted (no candidates or all builds failed).")

    # ── Canada: Sheet2 — build top product, FB only ──────────────────────────
    ws2    = open_sheet2()
    cands2 = select_candidates(ws2)
    log(f"\nCA: {len(cands2)} candidate(s); selecting highest social score…")

    for sheet_row2, row2, product2 in cands2[:4]:
        log(f"CA: building reel for row {sheet_row2} — {product2['title'][:55]}")
        video_url2, _thumb_url2, thumb_path2 = build_and_upload(
            product2, keys, ffmpeg, font, tld="ca"
        )
        if thumb_path2:
            try: os.unlink(thumb_path2)
            except: pass
        if not video_url2:
            log(f"CA: build failed for row {sheet_row2} — trying next candidate")
            continue
        try:
            if os.path.exists(FB_CANADA_TOKEN):
                pid2, tok2, _ = load_fb_token(FB_CANADA_TOKEN)
            else:
                _, tok2, _ = load_fb_token(FB_FRESHDEALS_TOKEN); pid2 = FB_CANADA_PAGE_ID
            post_fb_reel(pid2, tok2, video_url2, product2["title"] or "Deal Alert!", product2["reel_link"])
            log(f"CA row {sheet_row2}: Facebook reel posted.")
        except Exception as e:
            log(f"ERROR (FB Canada) row {sheet_row2}: {e} — skipping sheet write-back")
            break
        # Sheet write-back separate from the post so a stale row never re-triggers a post.
        for _cell, _val in [(f"N{sheet_row2}", video_url2), (f"P{sheet_row2}", "Yes")]:
            try:
                ws2.update_acell(_cell, _val)
            except Exception as _e:
                log(f"  ⚠️ CA sheet write {_cell} failed (row may no longer exist): {_e}")
        break

    log("=== vipon_publisher done ===")


if __name__ == "__main__":
    main()
