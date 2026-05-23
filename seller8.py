#!/usr/bin/env python3
"""Seller intake pipeline (Form Responses 2 -> Seller)

- Uses vipon26.py for credentials, Cloudinary, OpenAI, and Amazon helpers
- Reads all form entries from 'Form Responses 2' newer than the latest
  'Response Date' stored in the Seller tab.
- For each new form row, it:
    * validates ASIN + discount code + percent
    * fetches Amazon images
    * builds a reel via make_and_upload_reel_from_images
    * builds smart deep links + FB/IG post text
  Only if the reel video URL is generated successfully do we append a new
  row to the Seller tab. No video -> no Seller row.

This script NEVER clears the Seller tab.
"""

from __future__ import annotations

import re
import time
import html
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import gspread
from oauth2client.service_account import ServiceAccountCredentials
import requests

# Import all the working pieces from vipon26
from vipon26 import (
    GOOGLE_SHEET_NAME,
    GOOGLE_CREDS_FILE,
    ASIN_RE,
    BLOCKED_TITLE_KEYWORDS,
    UA,
    is_plausible_code,
    _normalize_discount,
    fetch_amazon_images,
    make_and_upload_reel_from_images,
    get_affiliate_link,
    get_platform_links,
    generate_social_post,
    shorten_title,
    MAX_TITLE_LEN,
    log,
)


FORM_TAB_NAME         = "Form Responses 2"
SELLER_TAB_NAME       = "Seller"
RESPONSE_DATE_HEADER  = "Response Date"


# ---------------- Google Sheets helpers ----------------

def open_worksheet(tab_name: str):
    """Open an existing worksheet by name (do NOT clear)."""
    log(f"▶ Opening Google Sheet tab: {tab_name} …")
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDS_FILE, scope)
    client = gspread.authorize(creds)
    ss = client.open(GOOGLE_SHEET_NAME)
    try:
        ws = ss.worksheet(tab_name)
    except Exception as e:
        raise RuntimeError(
            f"Worksheet '{tab_name}' not found in Google Sheet '{GOOGLE_SHEET_NAME}'."
        ) from e
    return ws


def _norm_header(h: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (h or "").strip().lower())


# ---------------- Header index builders ----------------

def _build_form_header_index(header_row: List[str]) -> Dict[str, Optional[int]]:
    """Map Form Responses 2 headers to indexes safely."""
    base = {_norm_header(h): i for i, h in enumerate(header_row) if (h or "").strip()}

    def exact(*names: str) -> Optional[int]:
        for n in names:
            k = _norm_header(n)
            if k in base:
                return base[k]
        return None

    def contains_any(substrings) -> Optional[int]:
        subs_n = [_norm_header(s) for s in substrings if s]
        for key, i in base.items():
            for sub_n in subs_n:
                if sub_n and sub_n in key:
                    return i
        return None

    ts_i   = exact("Timestamp")
    asin_i = exact("ASIN", "Amazon Link", "Product Link")
    code_i = exact("Discount Code")

    disc_i = exact("Discount %", "Discount Percent", "Discount Percentage")
    if disc_i is None:
        disc_i = contains_any(["discountpercent", "discountpercentage", "percent", "example"])
    if disc_i is None:
        for key, i in base.items():
            if "discount" in key and "code" not in key and ("percent" in key or "example" in key):
                disc_i = i
                break

    price_i = exact("Final Price", "Final Price after discount", "Price")
    if price_i is None:
        price_i = contains_any(["finalprice", "priceafterdiscount"])

    expiry_i = exact("Expiry", "Expirey", "Expiration")
    if expiry_i is None:
        expiry_i = contains_any(["expiry", "expirey", "expiration"])

    return {
        "timestamp":    ts_i,
        "asin_or_link": asin_i,
        "discount_code": code_i,
        "discount_pct": disc_i,
        "expiry":       expiry_i,
        "price":        price_i,
    }


def _build_seller_header_index(header_row: List[str]) -> Dict[str, Optional[int]]:
    base = {_norm_header(h): i for i, h in enumerate(header_row) if (h or "").strip()}

    def pick(*names: str) -> Optional[int]:
        for n in names:
            k = _norm_header(n)
            if k in base:
                return base[k]
        return None

    return {
        "link":           pick("Link"),
        "reel_link":      pick("Reel"),
        "ig_link":        pick("IG"),
        "youtube_link":   pick("Youtube", "YouTube"),
        "tiktok_link":    pick("TikTok", "Tiktok"),
        "discount_code":  pick("Discount Code"),
        "disc":           pick("Disc", "Discount"),
        "expiry":         pick("Expiry", "Expirey"),
        "product":        pick("Product"),
        "price":          pick("Price"),
        "pid":            pick("PID", "Asin", "ASIN"),
        "image":          pick("Image"),
        "pin_image":      pick("Pin Image", "PinImage"),
        "video":          pick("Video", "Reel Video"),
        "fbig":           pick("FB/IG", "FBIG"),
        "posted_youtube": pick("Posted on Youtube", "Posted On Youtube", "Posted Youtube"),
        "posted_fb":      pick("Posted on FB", "Posted On FB", "Posted FB"),
        "response_date":  pick(RESPONSE_DATE_HEADER, "ResponseDate", "Form Date", "Form Timestamp"),
    }


# ---------------- Small parsers ----------------

def _parse_form_timestamp(ts: str) -> Optional[datetime]:
    """Parse Google Form timestamp like '1/7/2026 6:40:22'."""
    if not ts:
        return None
    s = str(ts).strip()
    for fmt in (
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y %H:%M",
        "%m/%d/%y %H:%M:%S",
        "%m/%d/%y %H:%M",
    ):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            continue
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _normalize_percent_to_disc(pct_cell: Any) -> str:
    """Normalize a percent cell into a clean string like '50%'."""
    if pct_cell is None:
        return ""
    s = str(pct_cell).strip()
    if not s:
        return ""
    m = re.search(r"(\d{1,3})", s)
    if not m:
        return s
    n = int(m.group(1))
    if n <= 0:
        return s
    if n > 95:
        n = 95
    return f"{n}%"


def _extract_asin_from_cell(cell_val: str) -> str:
    if not cell_val:
        return ""
    s = str(cell_val).strip()
    m = ASIN_RE.search(s)
    if m:
        return m.group(0).upper()
    m2 = re.search(r"\b([A-Z0-9]{10})\b", s, re.I)
    if m2 and m2.group(1).upper().startswith("B0"):
        return m2.group(1).upper()
    return ""


def fetch_amazon_title(asin: str, tld: str = "com", retries: int = 4) -> str:
    """Best-effort product title from Amazon HTML (no selenium)."""
    if not asin:
        return ""

    urls = [
        f"https://www.amazon.{tld}/dp/{asin}?th=1&psc=1",
        f"https://www.amazon.{tld}/gp/aw/d/{asin}",
        f"https://www.amazon.{tld}/dp/{asin}",
    ]

    def _is_robot_page(txt: str) -> bool:
        t = (txt or "").lower()
        return ("captchacharacters" in t) or ("robot check" in t) or ("type the characters you see" in t)

    session = requests.Session()

    for url in urls:
        delay = 1.2
        for attempt in range(1, retries + 1):
            headers = {
                "User-Agent": UA,
                "Accept-Language": "en-US,en;q=0.8",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Connection": "keep-alive",
            }
            try:
                r = session.get(url, headers=headers, timeout=25)

                if r.status_code in (429, 503):
                    time.sleep(min(delay, 10))
                    delay *= 1.8
                    continue

                if not r.ok:
                    time.sleep(min(delay, 10))
                    delay *= 1.6
                    continue

                html_src = r.text or ""
                if _is_robot_page(html_src):
                    time.sleep(min(delay, 10))
                    delay *= 1.9
                    continue

                m = re.search(r'id="productTitle"[^>]*>\s*([^<]+)\s*<', html_src, re.I)
                if m:
                    return html.unescape(m.group(1)).strip()

                m2 = re.search(r"<title>\s*(.*?)\s*</title>", html_src, re.I | re.S)
                if m2:
                    t = re.sub(r"\s+", " ", html.unescape(m2.group(1))).strip()
                    t = re.sub(r"\s*:\s*Amazon\.com.*$", "", t, flags=re.I).strip()
                    if t and t.lower() not in ("robot check", "sorry! something went wrong!"):
                        return t

                return ""
            except Exception:
                time.sleep(min(delay, 10))
                delay *= 1.6

    return ""


# ---------------- Seller date tracking ----------------

def _ensure_response_date_header(
    seller_ws, header: List[str], sidx: Dict[str, Optional[int]]
) -> Tuple[List[str], Dict[str, Optional[int]]]:
    if sidx.get("response_date") is not None:
        return header, sidx
    new_col_idx = len(header)
    seller_ws.update_cell(1, new_col_idx + 1, RESPONSE_DATE_HEADER)
    header = list(header) + [RESPONSE_DATE_HEADER]
    sidx = _build_seller_header_index(header)
    return header, sidx


def _get_last_response_datetime(
    seller_values: List[List[str]], sidx: Dict[str, Optional[int]]
) -> Optional[datetime]:
    resp_col = sidx.get("response_date")
    if resp_col is None:
        return None
    if len(seller_values) <= 1:
        return None

    last_dt: Optional[datetime] = None
    for r in range(2, len(seller_values) + 1):
        row = seller_values[r - 1]
        if len(row) <= resp_col:
            continue
        cell = (row[resp_col] or "").strip()
        if not cell:
            continue
        dt = _parse_form_timestamp(cell)
        if not dt:
            continue
        if (last_dt is None) or (dt > last_dt):
            last_dt = dt
    return last_dt


# ---------------- Core pipeline ----------------

def process_new_form_rows() -> None:
    """Main logic: Form Responses 2 -> Seller (append only, video-first)."""
    form_ws   = open_worksheet(FORM_TAB_NAME)
    seller_ws = open_worksheet(SELLER_TAB_NAME)

    form_values = form_ws.get_all_values()
    if not form_values or len(form_values) < 2:
        log(f"⁉️  No rows found in '{FORM_TAB_NAME}'.")
        return

    f_header = form_values[0]
    fidx     = _build_form_header_index(f_header)
    if fidx["timestamp"] is None or fidx["asin_or_link"] is None:
        raise RuntimeError(f"'{FORM_TAB_NAME}' must have Timestamp and ASIN columns.")

    seller_values = seller_ws.get_all_values()
    if not seller_values:
        raise RuntimeError("Seller tab is empty — it must have a header row.")

    s_header = seller_values[0]
    sidx     = _build_seller_header_index(s_header)
    for req in ("link", "discount_code", "disc", "price", "expiry"):
        if sidx.get(req) is None:
            raise RuntimeError(f"Seller tab missing required column: {req}")

    s_header, sidx = _ensure_response_date_header(seller_ws, s_header, sidx)

    last_dt = _get_last_response_datetime(seller_values, sidx)
    if last_dt:
        log(f"ℹ️  Last Response Date in Seller: {last_dt}")
    else:
        log("ℹ️  No Response Date found in Seller — processing all form rows.")

    ts_col     = fidx["timestamp"]
    asin_col   = fidx["asin_or_link"]
    code_col   = fidx.get("discount_code")
    disc_col   = fidx.get("discount_pct")
    expiry_col = fidx.get("expiry")
    price_col  = fidx.get("price")

    to_append: List[List[str]] = []
    considered = 0
    appended   = 0

    for i in range(2, len(form_values) + 1):
        row = form_values[i - 1]
        if len(row) < len(f_header):
            row = row + [""] * (len(f_header) - len(row))

        ts_raw = row[ts_col]
        dt     = _parse_form_timestamp(ts_raw)
        if not dt:
            continue
        if last_dt and dt <= last_dt:
            continue

        considered += 1

        asin = _extract_asin_from_cell(row[asin_col])
        if not asin:
            log(f"  ✗ Row {i}: no ASIN found — skipping")
            continue

        code = (row[code_col] if code_col is not None and len(row) > code_col else "")
        code = (code or "").strip().upper()
        if not is_plausible_code(code, strict=False):
            log(f"  ✗ Row {i}: invalid discount code '{code}' — skipping ASIN {asin}")
            continue

        pct_raw  = (row[disc_col] if (disc_col is not None and len(row) > disc_col) else "")
        disc_txt = _normalize_percent_to_disc(pct_raw)
        expiry   = (row[expiry_col] if (expiry_col is not None and len(row) > expiry_col) else "").strip()
        price    = (row[price_col]  if (price_col  is not None and len(row) > price_col)  else "").strip()

        title = fetch_amazon_title(asin, "com")

        t_low   = (title or "").lower()
        bad_hit = None
        for bad in BLOCKED_TITLE_KEYWORDS:
            b = (bad or "").strip().lower()
            if not b:
                continue
            if (" " in b) or ("-" in b):
                if b in t_low:
                    bad_hit = bad
                    break
            else:
                if re.search(rf"\b{re.escape(b)}\b", t_low):
                    bad_hit = bad
                    break
        if bad_hit:
            log(f"  ✗ Row {i}: blocked by keyword '{bad_hit}' — skipping ASIN {asin}")
            continue

        images = fetch_amazon_images(asin, "com", max_imgs=10)
        if not images:
            log(f"  ✗ Row {i}: no Amazon images for ASIN {asin} — skipping (no video)")
            continue
        cover_image = images[0]

        discount_for_reel = _normalize_discount(disc_txt or pct_raw or "")
        aff_link          = get_affiliate_link(asin, "com")
        platform_links    = get_platform_links(asin, "com")

        video_url = make_and_upload_reel_from_images(
            asin,
            images,
            discount_for_reel,
            code,
            shorten_title(title, MAX_TITLE_LEN),
            price,
        )
        if not video_url:
            log(f"  ✗ Row {i}: video generation failed — NO row added for ASIN {asin}")
            continue

        post_text = generate_social_post(
            aff_link,
            code,
            discount_for_reel,
            expiry,
            shorten_title(title, MAX_TITLE_LEN),
            price,
        )

        out = [""] * len(s_header)
        _set(out, sidx, "link",          aff_link)
        _set(out, sidx, "reel_link",     platform_links.get("reel", ""))
        _set(out, sidx, "ig_link",       platform_links.get("ig", ""))
        _set(out, sidx, "youtube_link",  platform_links.get("youtube", ""))
        _set(out, sidx, "tiktok_link",   platform_links.get("tiktok", ""))
        _set(out, sidx, "discount_code", code)
        _set(out, sidx, "disc",          disc_txt)
        _set(out, sidx, "expiry",        expiry)
        _set(out, sidx, "price",         price)
        _set(out, sidx, "pid",           asin)
        _set(out, sidx, "product",       shorten_title(title, MAX_TITLE_LEN))
        _set(out, sidx, "image",         cover_image)
        _set(out, sidx, "pin_image",     cover_image)
        _set(out, sidx, "video",         video_url)
        _set(out, sidx, "fbig",          post_text)
        _set(out, sidx, "response_date", ts_raw)

        to_append.append(out)
        appended += 1
        log(f"✓ Prepared Seller row for ASIN {asin} (Form row {i})")

    if to_append:
        seller_ws.append_rows(to_append, value_input_option="USER_ENTERED")
        log(
            f"✅ Appended {appended} new row(s) to '{SELLER_TAB_NAME}' "
            f"(from {considered} considered form row(s))."
        )
    else:
        log(
            "ℹ️  No new rows appended to Seller "
            "(either none newer than last Response Date or all failed video generation)."
        )


def _set(out: list, sidx: dict, key: str, value: str) -> None:
    idx = sidx.get(key)
    if idx is not None:
        out[idx] = value


def main() -> None:
    process_new_form_rows()


if __name__ == "__main__":
    main()
