#!/usr/bin/env python3
"""
Standalone seller form processor — runs Phase 3 (US) and Phase 4 (CA)
from vipon26.py independently of the full daily scrape.

Opens Sheet1 and Sheet2 for appending, then calls process_seller_forms()
and process_seller_forms_ca() from vipon26 (the authoritative implementations).

Does NOT scrape Vipon, does NOT reset or delete rows from Sheet1/Sheet2.
Safe to run any time to retry stuck seller form rows.

Form tabs and their destination sheets:
  Form Responses 2  ->  Sheet1  (US, amazon.com)
  Form Responses 3  ->  Sheet2  (CA, amazon.ca)

Status column (added automatically to each form tab):
  <blank>       — not yet processed (will be picked up)
  Done          — permanent skip (already added to sheet)
  Blocked       — permanent skip (blocked keyword / invalid)
  No ASIN       — transient; retried next run
  No Images     — transient; retried next run
  Video Failed  — transient; retried next run
"""

import gspread
from oauth2client.service_account import ServiceAccountCredentials

from vipon26 import (
    GOOGLE_SHEET_NAME,
    GOOGLE_CREDS_FILE,
    SHEET2_TAB,
    log,
    process_seller_forms,
    process_seller_forms_ca,
)


def _open_sheet_only():
    """Open Sheet1 (US) for appending — no reset, no row deletion."""
    log("▶ Opening Sheet1 (US)…")
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDS_FILE, scope)
    client = gspread.authorize(creds)
    return client.open(GOOGLE_SHEET_NAME).sheet1


def _open_sheet2_only():
    """Open Sheet2 (Canada) for appending — no reset, no row deletion."""
    log("▶ Opening Sheet2 (Canada)…")
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDS_FILE, scope)
    client = gspread.authorize(creds)
    ss = client.open(GOOGLE_SHEET_NAME)
    try:
        return ss.worksheet(SHEET2_TAB)
    except Exception:
        return ss.get_worksheet(1)


def main():
    ws  = _open_sheet_only()
    ws2 = _open_sheet2_only()

    log("\n=== Phase 3: US Seller Forms (Form Responses 2 → Sheet1) ===")
    process_seller_forms(ws)

    log("\n=== Phase 4: CA Seller Forms (Form Responses 3 → Sheet2) ===")
    process_seller_forms_ca(ws2)

    log("\n----- Seller intake complete -----")


if __name__ == "__main__":
    main()
