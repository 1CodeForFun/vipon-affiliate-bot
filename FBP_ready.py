#!/usr/bin/env python3
"""
Posts Facebook page content from the vipon Google Sheet.

US  (Sheet1): post text+link to FreshDeals FB for rows where col Q is not Yes.
              Mark col Q = Yes when done.
CA  (Sheet2): post text+link to Fresh Deals Canada FB for rows where col Q is not Yes.
              Mark col Q = Yes when done.

Requirements:
    pip install gspread oauth2client requests

Required local files (SECRETS_DIR or current folder):
    vipon_google_creds.json
    fb_page_token.json          <- FreshDeals US page token
    fb_page_token-canada.json   <- Fresh Deals Canada page token
"""

import json
import os
import time
from pathlib import Path

import gspread
import requests
from oauth2client.service_account import ServiceAccountCredentials

# ---------------- CONFIG ----------------
SECRETS_DIR = os.environ.get("SECRETS_DIR", ".")

def _p(filename: str) -> str:
    return os.path.join(SECRETS_DIR, filename)

GOOGLE_SHEET_NAME    = "vipon"
GOOGLE_CREDS_FILE    = _p("vipon_google_creds.json")
TOKEN_FILE           = Path(_p("fb_page_token.json"))
TOKEN_FILE_CANADA    = Path(_p("fb_page_token-canada.json"))
FB_CANADA_PAGE_ID    = "100649111740976"   # Fresh Deals Canada (fallback if no token file)
POST_DELAY_SECONDS   = 3
TIMEOUT              = 60

# Posts published per sheet PER RUN. This used to drain every unposted row in a
# single run, which dumped the whole day's backlog onto the page at once. With
# the workflow triggered hourly, 2 gives a steady 2 posts/hour per page and
# clears a full 48-product sheet in 24 hours.
POSTS_PER_RUN        = int(os.environ.get("POSTS_PER_RUN") or "2")

# Column numbers (1-based)
COL_A_LINK       = 1
COL_F_CODE       = 6    # discount code — appended to the post body
COL_O_POST       = 15   # VO/body copy (piece 1) — no link, no spelled code
COL_Q_POSTED_FLAG = 17

DONE_VALUE = "Yes"


# ---------------- HELPERS ----------------
def log(msg: str):
    print(msg, flush=True)


def load_fb_config(token_file: Path, fallback_page_id: str = ""):
    """Load FB page credentials from a token JSON file."""
    if not token_file.exists():
        if fallback_page_id:
            # No dedicated token — try the US FreshDeals token with the given page ID
            if not TOKEN_FILE.exists():
                raise RuntimeError(f"Missing {token_file} and no fallback token at {TOKEN_FILE}.")
            data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
            page_token = (data.get("page_access_token") or "").strip()
            graph_api_version = (data.get("graph_api_version") or "v25.0").strip()
            return fallback_page_id, page_token, graph_api_version
        raise RuntimeError(f"Missing {token_file}. Run fb_get_long_lived_page_token.py first.")

    data = json.loads(token_file.read_text(encoding="utf-8"))
    page_id = (data.get("page_id") or fallback_page_id or "").strip()
    page_token = (data.get("page_access_token") or "").strip()
    graph_api_version = (data.get("graph_api_version") or "v25.0").strip()

    if not page_id or not page_token:
        raise RuntimeError(f"{token_file} is missing page_id or page_access_token.")

    return page_id, page_token, graph_api_version


def open_sheet(tab_name: str = None):
    """Open a worksheet by tab name, or Sheet1 if tab_name is None."""
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDS_FILE, scope)
    client = gspread.authorize(creds)
    ss = client.open(GOOGLE_SHEET_NAME)
    if tab_name:
        return ss.worksheet(tab_name)
    return ss.sheet1


def normalize_text(value):
    return (value or "").strip()


def is_done(value):
    return normalize_text(value).lower() == "yes"


def ensure_row_length(row, min_len):
    if len(row) < min_len:
        row.extend([""] * (min_len - len(row)))
    return row


def publish_link_post_to_facebook(page_id: str, page_token: str, graph_api_version: str, message: str, link: str):
    url = f"https://graph.facebook.com/{graph_api_version}/{page_id}/feed"
    payload = {
        "message": message,
        "link": link,
        "access_token": page_token,
    }

    resp = requests.post(url, data=payload, timeout=TIMEOUT)

    try:
        data = resp.json()
    except Exception:
        data = {"raw_text": resp.text}

    if not resp.ok:
        raise RuntimeError(f"Facebook link post failed: {data}")

    return data.get("id", ""), data


# ---------------- MAIN ----------------
def post_sheet(ws, label: str, page_id: str, page_token: str, graph_api_version: str):
    """Post text+link for up to POSTS_PER_RUN unposted rows in this worksheet.

    Rows that are skipped (no link, no copy, already posted) do NOT count
    against the cap — only rows actually published to Facebook do.
    """
    rows = ws.get_all_values()
    if not rows:
        log(f"{label}: sheet is empty.")
        return 0, 0, 0

    log(f"{label}: loaded {len(rows)} rows (including header); "
        f"posting up to {POSTS_PER_RUN} this run.")
    posted_count = skipped_count = failed_count = 0

    for sheet_row_num in range(2, len(rows) + 1):
        if posted_count >= POSTS_PER_RUN:
            log(f"{label}: reached the {POSTS_PER_RUN}-post cap for this run — "
                f"remaining rows stay queued for the next one.")
            break
        row = rows[sheet_row_num - 1]
        row = ensure_row_length(row, COL_Q_POSTED_FLAG)

        link       = normalize_text(row[COL_A_LINK - 1])
        post_text  = normalize_text(row[COL_O_POST - 1])
        code       = normalize_text(row[COL_F_CODE - 1])
        posted_flag = normalize_text(row[COL_Q_POSTED_FLAG - 1])

        if not link:
            log(f"{label} Row {sheet_row_num}: skipped (column A is empty)")
            skipped_count += 1
            continue

        if is_done(posted_flag):
            log(f"{label} Row {sheet_row_num}: skipped (Q already Yes)")
            skipped_count += 1
            continue

        if not post_text:
            log(f"{label} Row {sheet_row_num}: skipped (column O is empty)")
            skipped_count += 1
            continue

        # Col O is the VO/body copy (no code, no link). Append the discount code
        # here so the FB text post shows it; the link goes in the link field.
        message = post_text
        if code:
            message = f"{post_text}\n\n🏷️ Use code {code} at checkout"

        try:
            log(f"{label} Row {sheet_row_num}: posting to Facebook...")
            post_id, response_data = publish_link_post_to_facebook(
                page_id=page_id,
                page_token=page_token,
                graph_api_version=graph_api_version,
                message=message,
                link=link,
            )
            ws.update_acell(f"Q{sheet_row_num}", DONE_VALUE)
            posted_count += 1
            log(f"{label} Row {sheet_row_num}: posted successfully (post id: {post_id})")
            log(f"{label} Row {sheet_row_num}: response -> {response_data}")
            time.sleep(POST_DELAY_SECONDS)

        except Exception as e:
            failed_count += 1
            log(f"{label} Row {sheet_row_num}: FAILED -> {e}")

    return posted_count, skipped_count, failed_count


def main():
    # ── US: Sheet1 ───────────────────────────────────────────────────────────
    log("=== US (Sheet1) ===")
    try:
        page_id, page_token, graph_api_version = load_fb_config(TOKEN_FILE)
        ws_us = open_sheet()
        p, s, f = post_sheet(ws_us, "US", page_id, page_token, graph_api_version)
        log(f"US — Posted: {p}  Skipped: {s}  Failed: {f}")
    except Exception as e:
        log(f"US section error: {e}")
        p = s = f = 0

    # ── Canada: Sheet2 ───────────────────────────────────────────────────────
    log("\n=== Canada (Sheet2) ===")
    try:
        ca_pid, ca_tok, ca_ver = load_fb_config(TOKEN_FILE_CANADA, FB_CANADA_PAGE_ID)
        log(f"CA token: page_id={ca_pid}, api_version={ca_ver}, token_len={len(ca_tok)}")
        ws_ca = open_sheet("Sheet2")
        p2, s2, f2 = post_sheet(ws_ca, "CA", ca_pid, ca_tok, ca_ver)
        log(f"CA — Posted: {p2}  Skipped: {s2}  Failed: {f2}")
    except Exception as e:
        log(f"CA section error: {e}")
        p2 = s2 = f2 = 0

    log("\n----- DONE -----")
    log(f"Posted:  {p + p2}")
    log(f"Skipped: {s + s2}")
    log(f"Failed:  {f + f2}")


if __name__ == "__main__":
    main()
