#!/usr/bin/env python3
"""
post_best_to_buffer.py — Post the highest-ranked vipon reel to TikTok + Pinterest.

Each run picks the top unposted-to-Buffer row by Social Score (col S), posts via Buffer,
then marks col T = "Yes". When ALL eligible rows are already marked, col T is reset for
all of them so the cycle restarts — meaning you can trigger this as many times as you want.

Prerequisites: vipon_publisher.py must have already run (col N = video URL, col P = "Yes").
"""

import os
import re

import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ── CONFIG ────────────────────────────────────────────────────────────────────
SECRETS_DIR       = os.environ.get("SECRETS_DIR", ".")
GOOGLE_CREDS_FILE = os.path.join(SECRETS_DIR, "vipon_google_creds.json")
GOOGLE_SHEET_NAME = "vipon"

# Column indices (1-based) — must match vipon_publisher.py
COL_A_AFF_LINK = 1
COL_F_CODE     = 6
COL_G_DISC     = 7
COL_I_TITLE    = 9
COL_J_PRICE    = 10
COL_L_IMAGE    = 12
COL_N_REEL_URL = 14
COL_P_POSTED   = 16   # "Yes" = reel built + posted to FB/IG
COL_S_SOCIAL   = 19   # Social Score set by vipon26.py
COL_T_BUFFER   = 20   # "Yes" = posted to TikTok + Pinterest via Buffer


def log(m): print(m, flush=True)


def _open_sheet():
    scope  = ["https://spreadsheets.google.com/feeds",
               "https://www.googleapis.com/auth/drive"]
    creds  = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDS_FILE, scope)
    client = gspread.authorize(creds)
    return client.open(GOOGLE_SHEET_NAME).sheet1


def _asin_from_link(link):
    m = (re.search(r"asin=([A-Za-z0-9]{10})", link, re.I)
         or re.search(r"\b(B0[A-Z0-9]{8})\b", link, re.I))
    return m.group(1).upper() if m else ""


def _parse_pct(disc_str):
    m = re.search(r"(\d+)", disc_str or "")
    return int(m.group(1)) if m else 0


def main():
    log("=== post_best_to_buffer.py ===")

    ws   = _open_sheet()
    rows = ws.get_all_values()

    # Collect all rows that are built + have a video URL (regardless of T column)
    all_eligible = []
    cands        = []
    for idx, row in enumerate(rows[1:], start=2):
        while len(row) < COL_T_BUFFER:
            row.append("")

        posted    = row[COL_P_POSTED   - 1].strip().lower()
        video_url = row[COL_N_REEL_URL - 1].strip()
        buf_done  = row[COL_T_BUFFER   - 1].strip().lower()

        if posted != "yes" or not video_url:
            continue

        link = row[COL_A_AFF_LINK - 1].strip()
        asin = _asin_from_link(link)
        if not asin:
            continue

        try:    score = float(row[COL_S_SOCIAL - 1] or 0)
        except: score = 0.0

        all_eligible.append((score, idx, row))
        if buf_done != "yes":
            cands.append((score, idx, row))

    if not all_eligible:
        log("No eligible reels — no rows are built + posted with a video URL yet.")
        return

    # When every reel has already been posted, reset T for all of them and cycle
    if not cands:
        log(f"  All {len(all_eligible)} reel(s) already Buffer-posted — resetting cycle.")
        reset_cells = [gspread.utils.rowcol_to_a1(r_idx, COL_T_BUFFER)
                       for _, r_idx, _ in all_eligible]
        for cell in reset_cells:
            try:
                ws.update_acell(cell, "")
            except Exception as e:
                log(f"  ⚠️ Reset {cell} failed: {e}")
        cands = all_eligible

    cands.sort(key=lambda c: c[0], reverse=True)
    score, sheet_row, row = cands[0]

    link      = row[COL_A_AFF_LINK - 1].strip()
    asin      = _asin_from_link(link)
    title     = row[COL_I_TITLE    - 1].strip() or "Today's Amazon deal"
    disc      = row[COL_G_DISC     - 1].strip()
    pct       = _parse_pct(disc)
    code      = row[COL_F_CODE     - 1].strip()
    price     = row[COL_J_PRICE    - 1].strip()
    cover     = row[COL_L_IMAGE    - 1].strip() or None
    video_url = row[COL_N_REEL_URL - 1].strip()

    deal = {
        "title_text": title,
        "title":      title,
        "asin":       asin,
        "pct":        pct,
        "code":       code,
        "price_text": price,
    }

    log(f"  Row {sheet_row} | score={score:.0f} | {pct}% off | {title[:65]}")
    log(f"  Video: {video_url[:80]}")

    from buffer_publish import post_to_buffer
    post_to_buffer(video_url, deal, "", thumbnail_url=cover, image_url=cover)

    try:
        ws.update_acell(f"T{sheet_row}", "Yes")
        log(f"  ✓ Sheet T{sheet_row} = Yes")
    except Exception as e:
        log(f"  ⚠️ Sheet write-back failed (non-fatal): {e}")

    log("=== done ===")


if __name__ == "__main__":
    main()
