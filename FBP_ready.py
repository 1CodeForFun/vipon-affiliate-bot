#!/usr/bin/env python3
"""
Posts Facebook page content from the vipon Google Sheet.

Logic:
- skip header row
- if column A has a link
- and column O has post text
- and column Q is not Yes
then:
    1) post column O as the message
    2) send column A as the separate Facebook link field
    3) update column Q to Yes

Requirements:
    pip install gspread oauth2client requests

Required local files in the same folder:
    vipon_google_creds.json
    fb_page_token.json   <- created by fb_get_long_lived_page_token.py
"""

import json
import time
from pathlib import Path

import gspread
import requests
from oauth2client.service_account import ServiceAccountCredentials

# ---------------- CONFIG ----------------
GOOGLE_SHEET_NAME = "vipon"
WORKSHEET_NAME = None  # None = first sheet
GOOGLE_CREDS_FILE = "vipon_google_creds.json"
TOKEN_FILE = Path("fb_page_token.json")
POST_DELAY_SECONDS = 3
TIMEOUT = 60

# Column numbers (1-based)
COL_A_LINK = 1
COL_O_POST = 15
COL_Q_POSTED_FLAG = 17

DONE_VALUE = "Yes"


# ---------------- HELPERS ----------------
def log(msg: str):
    print(msg, flush=True)


def load_fb_config():
    if not TOKEN_FILE.exists():
        raise RuntimeError(
            f"Missing {TOKEN_FILE}. Run fb_get_long_lived_page_token.py first."
        )

    data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    page_id = (data.get("page_id") or "").strip()
    page_token = (data.get("page_access_token") or "").strip()
    graph_api_version = (data.get("graph_api_version") or "v25.0").strip()

    if not page_id or not page_token:
        raise RuntimeError(f"{TOKEN_FILE} is missing page_id or page_access_token.")

    return page_id, page_token, graph_api_version


def open_sheet():
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDS_FILE, scope)
    client = gspread.authorize(creds)

    ss = client.open(GOOGLE_SHEET_NAME)
    if WORKSHEET_NAME:
        return ss.worksheet(WORKSHEET_NAME)
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
def main():
    page_id, page_token, graph_api_version = load_fb_config()
    ws = open_sheet()
    rows = ws.get_all_values()

    if not rows:
        log("Sheet is empty.")
        return

    log(f"Loaded {len(rows)} total rows including header.")

    posted_count = 0
    skipped_count = 0
    failed_count = 0

    for sheet_row_num in range(2, len(rows) + 1):
        row = rows[sheet_row_num - 1]
        row = ensure_row_length(row, COL_Q_POSTED_FLAG)

        link = normalize_text(row[COL_A_LINK - 1])
        post_text = normalize_text(row[COL_O_POST - 1])
        posted_flag = normalize_text(row[COL_Q_POSTED_FLAG - 1])

        if not link:
            log(f"Row {sheet_row_num}: skipped (column A is empty)")
            skipped_count += 1
            continue

        if is_done(posted_flag):
            log(f"Row {sheet_row_num}: skipped (Q already Yes)")
            skipped_count += 1
            continue

        if not post_text:
            log(f"Row {sheet_row_num}: skipped (column O is empty)")
            skipped_count += 1
            continue

        try:
            log(f"Row {sheet_row_num}: posting to Facebook...")
            post_id, response_data = publish_link_post_to_facebook(
                page_id=page_id,
                page_token=page_token,
                graph_api_version=graph_api_version,
                message=post_text,
                link=link,
            )

            ws.update_acell(f"Q{sheet_row_num}", DONE_VALUE)

            posted_count += 1
            log(f"Row {sheet_row_num}: posted successfully (post id: {post_id})")
            log(f"Row {sheet_row_num}: response -> {response_data}")

            time.sleep(POST_DELAY_SECONDS)

        except Exception as e:
            failed_count += 1
            log(f"Row {sheet_row_num}: FAILED -> {e}")

    log("----- DONE -----")
    log(f"Posted:  {posted_count}")
    log(f"Skipped: {skipped_count}")
    log(f"Failed:  {failed_count}")


if __name__ == "__main__":
    main()
