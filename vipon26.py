#!/usr/bin/env python3
# vipon25.py — Optimized: 720p FFmpeg (fast), edge-tts (free VO), Gemini (free post text),
#              Chrome-first / video-second flow, Pinterest flag, all bugs fixed.
#
# ── ONE-TIME VM SETUP ───────────────────────────────────────────────────────
#  pip install edge-tts                   # free TTS, no API key needed
#  echo "YOUR_GEMINI_KEY" > ~/geminikey.txt   # free key → aistudio.google.com
#  (OpenAI key at ~/videokey.txt still works as fallback for both TTS & post text)
#
# ── TO RE-ENABLE PINTEREST ──────────────────────────────────────────────────#
#  Set  ENABLE_PINTEREST = True  below (all code is preserved, just gated)
# ────────────────────────────────────────────────────────────────────────────

import os, time, re, random, glob, tempfile, subprocess, hashlib, shutil, platform
import urllib.parse
import asyncio
from urllib.parse import quote
import json
import requests
import html

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import WebDriverException, TimeoutException, StaleElementReferenceException
from PIL import Image, ImageOps
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from selenium import webdriver
from selenium.common.exceptions import SessionNotCreatedException
from datetime import datetime, timedelta, timezone
from pathlib import Path
from gspread.exceptions import APIError

# ════════════════════════════════════════════════════════════════
#  CONFIG
# ════════════════════════════════════════════════════════════════

ENABLE_PINTEREST = False   # ← flip to True when you want Pinterest again

AFFILIATE_ID      = "freshdeal00cc-20"
TAG_REEL          = "manus00-20"
TAG_IG            = "insinstagram-20"
TAG_YOUTUBE       = "youtubefdusa-20"
TAG_TIKTOK        = "tiktoktiktok-20"
TAG_PINTEREST     = "pinpinterestfd-20"
GOOGLE_SHEET_NAME = "vipon"
GOOGLE_CREDS_FILE = "vipon_google_creds.json"

# ── Canada market ────────────────────────────────────────────────
AFFILIATE_ID_CA    = "fdcanada00-20"   # single tag for all CA platforms (amazon.ca)
AMAZON_TLD_CA      = "ca"
SHEET2_TAB         = "Sheet2"          # Canada products sheet
SELLER_FORM_TAB_CA = "Form Responses 3" # Canada seller form responses

# ── Vipon.com accounts — loaded from ~/vipon_accounts.json ──────
# Format: [{"username": "...", "password": "..."}, ...]
def _load_vipon_accounts():
    path = os.path.expanduser("~/vipon_accounts.json")
    if os.path.exists(path):
        with open(path) as f:
            accs = json.load(f)
        if accs:
            return accs
    return [{"username": "ayman1elmasry@yahoo.com", "password": "HighVoltage123*"}]

VIPON_ACCOUNTS  = _load_vipon_accounts()
_account_index  = datetime.now().timetuple().tm_yday % len(VIPON_ACCOUNTS)  # day-based start

def _current_account():
    return VIPON_ACCOUNTS[_account_index % len(VIPON_ACCOUNTS)]

def _rotate_account():
    global _account_index
    _account_index = (_account_index + 1) % len(VIPON_ACCOUNTS)
    acc = _current_account()
    print(f"[rotate] switching to account {_account_index + 1}/{len(VIPON_ACCOUNTS)}: {acc['username']}")
    return acc

_CONFIG_TAB = "_config"  # lightweight sheet that persists run state across GitHub Actions runs

def _read_account_state(ss) -> int:
    """Return the account index to start on this run.
    Reads last-saved index from the sheet and advances by one, so consecutive
    runs never start on the same account. Falls back to day-of-year if no state."""
    try:
        cfg = ss.worksheet(_CONFIG_TAB)
        val = cfg.acell("A1").value
        if val is not None and str(val).strip().lstrip("-").isdigit():
            saved = int(val)
            nxt = (saved + 1) % len(VIPON_ACCOUNTS)
            print(f"[account-state] last run ended on index {saved} — starting on {nxt} ({VIPON_ACCOUNTS[nxt]['username']})")
            return nxt
    except Exception:
        pass
    fallback = datetime.now().timetuple().tm_yday % len(VIPON_ACCOUNTS)
    print(f"[account-state] no saved state — day-based fallback index {fallback}")
    return fallback

def _write_account_state(ss) -> None:
    """Persist the current _account_index so the next run starts on the next account."""
    try:
        try:
            cfg = ss.worksheet(_CONFIG_TAB)
        except Exception:
            cfg = ss.add_worksheet(title=_CONFIG_TAB, rows=5, cols=2)
        cfg.update("A1", [[_account_index]])
        print(f"[account-state] saved index {_account_index} for next run")
    except Exception as _e:
        print(f"[account-state] WARNING: could not save state: {_e.__class__.__name__}")


def _read_pid_history(ss) -> tuple:
    """Return (us_historical_pids: set, ca_historical_pids: set) from last 2 days stored
    in _config A2 as JSON.  Today's PIDs are excluded — those are already in us_pids /
    ca_pids from _sheet_topup_state and will be deduplicated there."""
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        cfg = ss.worksheet(_CONFIG_TAB)
        val = cfg.acell("A2").value
        if not val:
            return set(), set()
        history = json.loads(val)
        us_pids, ca_pids = set(), set()
        for entry in history:
            if entry.get("date") != today:   # today is handled by sheet topup state
                us_pids.update(str(p) for p in entry.get("us", []))
                ca_pids.update(str(p) for p in entry.get("ca", []))
        print(f"[pid-history] loaded {len(us_pids)} US + {len(ca_pids)} CA historical PIDs "
              f"to exclude (last 2 days, excl. today)")
        return us_pids, ca_pids
    except Exception as _e:
        print(f"[pid-history] could not read history: {_e.__class__.__name__}")
        return set(), set()


def _write_pid_history(ss, today_us: list, today_ca: list) -> None:
    """Append today's scraped PIDs to the rolling 2-day history in _config A2.
    Re-runs today MERGE into today's entry (not replace) so early-run PIDs survive."""
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        cfg = ss.worksheet(_CONFIG_TAB)
        val = cfg.acell("A2").value
        history = json.loads(val) if val else []
        # Find or create today's entry and MERGE (handles re-runs)
        today_entry = next((e for e in history if e.get("date") == today), None)
        if today_entry:
            today_entry["us"] = list(set(today_entry.get("us", []) + [str(p) for p in today_us]))
            today_entry["ca"] = list(set(today_entry.get("ca", []) + [str(p) for p in today_ca]))
        else:
            history.insert(0, {"date": today,
                               "us": [str(p) for p in today_us],
                               "ca": [str(p) for p in today_ca]})
        # Keep only last 2 days
        seen, kept = set(), []
        for entry in history:
            d = entry.get("date", "")
            if d not in seen:
                seen.add(d); kept.append(entry)
            if len(kept) >= 2:
                break
        cfg.update("A2", [[json.dumps(kept)]])
        print(f"[pid-history] saved {len(today_us)} US + {len(today_ca)} CA PIDs for {today} "
              f"(history covers {len(kept)} day(s))")
    except Exception as _e:
        print(f"[pid-history] WARNING: could not save: {_e.__class__.__name__}")

# ── Video dimensions: 720×1280 is valid for all Reels/Shorts, ~56% fewer pixels → much faster ──
VIDEO_W = 720
VIDEO_H = 1280

LOGO_PATH             = os.path.expanduser("~/assets/logo.png")
IMG_SEG_DURATION_SEC  = 5
INFO_SEG_DURATION_SEC = 3
LOGO_SEG_DURATION_SEC = 2
MAX_AMAZON_IMAGES     = 6

PROMO_URL     = "https://www.myvipon.com"
PROMO_URL_CA  = "https://www.myvipon.com/promotion/index?type=instant"  # CA full deal listing (supports infinite scroll)
PRODUCT_LIMIT = int(os.getenv("PRODUCT_LIMIT") or "24")

# FIXED delay (seconds) between code reveals. Vipon rate-limits the GET-CODE
# endpoint per IP by requests-per-minute — the old VM never tripped it because it
# ran slowly; GitHub's speed does. A fixed, generous gap holds us under that rate.
# Bump REVEAL_PACE_SEC higher if a run still hits "limit reached" / no-code walls.
REVEAL_PACE_SEC = float(os.getenv("REVEAL_PACE_SEC") or "25")

# Early-stop guard: once we've collected at least EARLY_STOP_MIN_PRODUCTS and spent
# EARLY_STOP_AFTER_MIN minutes scraping, stop and write what we have to the sheet.
# Banking a solid batch beats burning more monthly code quota chasing the full
# PRODUCT_LIMIT and risking the whole run on a timeout.
EARLY_STOP_MIN_PRODUCTS = int(os.getenv("EARLY_STOP_MIN_PRODUCTS") or "12")
EARLY_STOP_AFTER_MIN    = float(os.getenv("EARLY_STOP_AFTER_MIN") or "75")

# Seller-intake reward: products that sellers send us directly get their real
# selection score PLUS this bonus, so the publisher produces their videos ahead of
# the regular scraped batch (we reward direct outreach instead of burying it under
# social scoring). Set to 0 to score sellers exactly like scraped products.
SELLER_SCORE_BONUS = float(os.getenv("SELLER_SCORE_BONUS") or "1000")

SCROLL_MIN         = int(os.getenv("SCROLL_MIN")    or "1")
SCROLL_MAX         = int(os.getenv("SCROLL_MAX")    or "50")
SCROLL_PAUSE_RANGE = (0.7, 1.7)
MAX_DISCOVERY      = int(os.getenv("MAX_DISCOVERY") or "150")

WAIT_SECS        = 10
PAGELOAD_TIMEOUT = 120
SCRIPT_TIMEOUT   = 10
IMPLICIT_WAIT    = 4

MUSIC_DIR = os.path.expanduser("~/assets/music")

# ── Cloudinary credentials — loaded from ~/cloudinary.json ──────
# Format: {"cloud_name": "...", "api_key": "...", "api_secret": "..."}
def _load_cloudinary_creds():
    path = os.path.expanduser("~/cloudinary.json")
    if os.path.exists(path):
        with open(path) as f:
            cfg = json.load(f)
        return cfg["cloud_name"], cfg["api_key"], cfg["api_secret"]
    return "diufrf8l7", "278766692116231", "ZmG521qjt-CNr0EgUWj3pJikScw"

CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET = _load_cloudinary_creds()
CLOUDINARY_VIDEO_FOLDER = "vipon_reels"

MAX_TILES_SNAPSHOT = 300
WORKER_BASE        = "https://amz.ifreshdeals.workers.dev"

MAX_TITLE_LEN = 100

# Fonts for FFmpeg drawtext
FONT_CANDIDATES = (
    [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    ] if platform.system().lower() != "windows" else [
        r"C:\Windows\Fonts\arialbd.ttf",
        r"C:\Windows\Fonts\arial.ttf",
        r"C:\Windows\Fonts\tahoma.ttf",
        r"C:\Windows\Fonts\verdana.ttf",
    ]
)

# ════════════════════════════════════════════════════════════════
#  HELPERS
# ════════════════════════════════════════════════════════════════

# Words that are NEVER a real coupon code — Vipon category names + page UI words.
# When the "Get Code" reveal fails (daily limit), the fallback scan can grab one of
# these (e.g. "ELECTRONICS" from the category nav). Blocking them makes the product
# skip instead of being saved with a wrong code — which also lets the account-
# rotation trigger and recover with a fresh account.
BAD_CODES = {
    # generic UI / promo words
    "CATEGORIES", "CATEGORY", "DISCOUNT", "DISCOUNTS", "PROMOTION", "PROMOTIONS",
    "VOUCHER", "VOUCHERS", "COUPON", "COUPONS", "COLLECTION", "COLLECTIONS",
    "FEATURED", "TRENDING", "CLEARANCE", "INSTANT", "GIVEAWAY", "NEWSLETTER",
    "SUBSCRIBE", "DELIVERY", "SHIPPING", "AMAZON", "VERIFIED", "DEALS", "BRANDS",
    "DAILY", "SELLER", "PRODUCTS", "PRODUCT", "REVEAL", "GETCODE", "USECODE",
    # Vipon / Amazon category names
    "ELECTRONICS", "ELECTRONIC", "BEAUTY", "PERSONAL", "CLOTHING", "APPAREL",
    "FASHION", "KITCHEN", "HOME", "GARDEN", "OUTDOORS", "OUTDOOR", "SPORTS",
    "TOOLS", "AUTOMOTIVE", "OFFICE", "HEALTH", "HOUSEHOLD", "TOYS", "GAMES",
    "BABY", "JEWELRY", "GROCERY", "FURNITURE", "ACCESSORIES", "SUPPLIES",
    "IMPROVEMENT", "INDUSTRIAL", "SCIENTIFIC", "MUSICAL", "INSTRUMENTS",
    "HANDMADE", "CAMERA", "PHOTO", "COMPUTERS", "SOFTWARE", "VIDEO", "MOVIES",
    "MUSIC", "BOOKS", "PETSUPPLIES", "WELLNESS",
    "CRAFTS", "ARTS", "SEWING", "LUGGAGE", "MENS", "WOMENS", "BOYS", "GIRLS",
    "KIDS", "ADULT", "UNISEX", "SMART", "VINYL", "CELL", "PHONES", "APPLIANCES",
    "LIGHTING", "BEDDING", "DECOR", "PATIO", "LAWN", "TRAVEL", "FITNESS",
}
CODE_RE   = re.compile(r"\b([A-Z0-9]{6,12})\b")
ASIN_RE   = re.compile(r"\bB0[A-Z0-9]{8}\b", re.I)

AMAZON_HOST_HINTS = (
    "m.media-amazon.com",
    "images-na.ssl-images-amazon.com",
    "images-na",
)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36")

def log(m): print(m, flush=True)

def is_plausible_code(code: str, *, strict: bool = False) -> bool:
    if not code: return False
    c = code.strip().upper()
    if c in BAD_CODES: return False
    if not (6 <= len(c) <= 12): return False
    if not c.isalnum(): return False
    if strict and not any(ch.isdigit() for ch in c): return False
    return True

# One-time (single-use) codes are multi-segment with dashes, e.g.
# "ZAF7-U8NJCD-RYYKAF". A normal shareable code is a single token (no dash).
# These can only be redeemed once, so we skip the product entirely.
_ONETIME_RE = re.compile(r"[A-Z0-9]{2,}-[A-Z0-9]{2,}")

def is_onetime_code(text: str) -> bool:
    return bool(_ONETIME_RE.search((text or "").upper()))

def expiry_to_date_text(expiry_txt: str) -> str:
    """Convert an expiry string to a short readable date like 'May 30'.

    Handles:
      - Relative: "9 days", "18 hours"
      - Absolute: "5/30/2026", "5/30/2026 11:59 PM PST", "2026-05-30"
    Returns "" if unparseable (so the VO branch skips the expiry phrase).
    """
    if not expiry_txt:
        return ""
    txt = expiry_txt.lower().strip()

    # ── Relative: "X days" / "X hours" ───────────────────────────
    m = re.search(r"(\d+)", txt)
    if m:
        n = int(m.group(1))
        if "day" in txt:
            end_date = datetime.now() + timedelta(days=n)
            return f"{end_date.strftime('%B')} {end_date.day}"
        if "hour" in txt:
            end_date = datetime.now() + timedelta(hours=n)
            return f"{end_date.strftime('%B')} {end_date.day}"

    # ── Absolute date: M/D/YYYY or YYYY-MM-DD ────────────────────
    dm = re.search(r"(\d{1,2})[/\-](\d{1,2})[/\-](\d{2,4})", expiry_txt)
    if dm:
        try:
            a, b, c = int(dm.group(1)), int(dm.group(2)), int(dm.group(3))
            # Distinguish M/D/YYYY from YYYY/M/D
            if a > 31:          # first number is a year
                year, month, day = a, b, c
            elif c < 100:       # two-digit year
                year, month, day = 2000 + c, a, b
            else:
                year, month, day = c, a, b
            end_date = datetime(year, month, day)
            return f"{end_date.strftime('%B')} {end_date.day}"
        except Exception:
            pass

    return ""   # unparseable → VO will use "Limited time" fallback

# ── Social proof (units sold + rating) → score for reel selection ──────────────
def _parse_units_sold(text: str) -> int:
    """'100+ bought in past month' → 100 ; '1K+' → 1000 ; '2.5K' → 2500."""
    if not text: return 0
    m = re.search(r"(\d+(?:\.\d+)?)\s*([KkMm]?)", text)
    if not m: return 0
    n = float(m.group(1))
    return int(n * {"k": 1_000, "m": 1_000_000}.get(m.group(2).lower(), 1))

def _parse_stars(text: str) -> float:
    if not text: return 0.0
    m = re.search(r"(\d(?:\.\d)?)", text)
    return float(m.group(1)) if m else 0.0

def _parse_rating_count(text: str) -> int:
    if not text: return 0
    m = re.search(r"([\d,]+)", text)
    return int(m.group(1).replace(",", "")) if m else 0

def _price_num(s) -> float:
    m = re.search(r"(\d+(?:\.\d+)?)", (s or "").replace(",", ""))
    return float(m.group(1)) if m else 0.0

def _disc_num(s) -> float:
    m = re.search(r"(\d{1,3})", s or "")
    return float(m.group(1)) if m else 0.0

def selection_score(units_sold: int, stars: float, rating_count: int,
                    price, disc_pct) -> float:
    """Rank products by COMMISSION potential, not raw unit velocity.

    Price (the $/sale driver) and discount (clickability) lead; social proof
    (units sold + ratings volume) is a SMOOTH, bounded multiplier so a cheap
    high-volume item (lunch bag) can't bury a higher-ticket one (chainsaw).
    """
    import math as _m
    pv = _price_num(price);  pv = pv if pv > 0 else 5.0
    dv = _disc_num(disc_pct)
    commercial = pv * (1 + dv / 100.0)               # higher price + bigger discount
    quality    = (stars / 5.0) if stars else 0.7      # 0..1 rating quality
    demand     = 1 + _m.log10(units_sold + 1) / 2.0 + _m.log10(rating_count + 1) / 4.0
    return round(commercial * quality * demand, 1)

def _social_score(units_sold: int, stars: float, rating_count: int) -> float:
    """Social-proof-only signal (kept for logging/reference)."""
    import math as _m
    quality = (stars / 5.0) if stars else 0.6
    volume  = 1 + _m.log10(rating_count + 1) / 4.0
    base    = units_sold if units_sold else 1
    return round(base * quality * volume, 1)

# Social proof needs a real browser — Amazon blocks plain requests from CI/datacenter
# IPs (that's why scores were all zero). A single cached headless Chrome handles all
# products; circuit-breaks after repeated blocks so it can't stall the scrape.
_SOCIAL_FAILS  = 0
_SOCIAL_OFF    = False
_SOCIAL_DRIVER = None

_SOCIAL_JS = r"""
function txt(el){ return el ? (el.innerText||el.textContent||'').trim() : ''; }
const out = {bought:'', rating:'', count:''};
{ const els = document.querySelectorAll('span,div,a');
  for (const e of els){ const t=(e.innerText||'').trim();
    if(/bought in past/i.test(t) && t.length<60){ out.bought=t; break; } } }
out.rating = txt(document.querySelector("[data-hook='rating-out-of-text']")
           || document.querySelector('#acrPopover')
           || document.querySelector('#averageCustomerReviews'));
out.count  = txt(document.querySelector('#acrCustomerReviewText'));
return out;
"""

def _get_social_driver():
    global _SOCIAL_DRIVER
    if _SOCIAL_DRIVER is not None:
        return _SOCIAL_DRIVER
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
        binary = next((b for b in ("/usr/bin/chromium-browser", "/usr/bin/chromium",
                                   "/usr/bin/google-chrome") if os.path.exists(b)), "")
        drv = next((d for d in ("/usr/bin/chromedriver",
                                "/usr/lib/chromium-browser/chromedriver",
                                "/usr/bin/chromium-chromedriver") if os.path.exists(d)), "")
        opts = Options()
        for a in ("--headless=new", "--no-sandbox", "--disable-dev-shm-usage",
                  "--disable-gpu", "--hide-scrollbars", "--lang=en-US", "--window-size=440,900"):
            opts.add_argument(a)
        opts.add_argument("--user-agent=Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                          "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")
        if binary: opts.binary_location = binary
        _SOCIAL_DRIVER = webdriver.Chrome(service=(Service(executable_path=drv) if drv else Service()), options=opts)
        return _SOCIAL_DRIVER
    except Exception as e:
        log(f"  ⚠️ social driver init failed: {e}")
        return None

def fetch_social_proof(asin: str, tld: str = "com") -> dict:
    """Social proof via a cached headless Chrome (units sold + stars + ratings) →
    compounded score. Best-effort; self-disables after repeated blocks/errors."""
    global _SOCIAL_FAILS, _SOCIAL_OFF
    out = {"units": 0, "stars": 0.0, "ratings": 0, "score": 0.0}
    if _SOCIAL_OFF or not asin:
        return out
    driver = _get_social_driver()
    if driver is None:
        _SOCIAL_OFF = True
        return out
    try:
        driver.get(f"https://www.amazon.{tld}/dp/{asin}?th=1&psc=1")
        time.sleep(2.0)
        page = (driver.page_source or "")[:5000].lower()
        if "captcha" in page or "robot check" in page:
            _SOCIAL_FAILS += 1
            if _SOCIAL_FAILS >= 3:
                _SOCIAL_OFF = True
                log("  ⚠️ social proof: Amazon blocking — disabled for this run")
            return out
        data = driver.execute_script(_SOCIAL_JS) or {}
        _SOCIAL_FAILS = 0
        out["units"]   = _parse_units_sold(data.get("bought", ""))
        out["stars"]   = _parse_stars(data.get("rating", ""))
        out["ratings"] = _parse_rating_count(data.get("count", ""))
        out["score"]   = _social_score(out["units"], out["stars"], out["ratings"])
    except Exception as e:
        _SOCIAL_FAILS += 1
        if _SOCIAL_FAILS >= 3:
            _SOCIAL_OFF = True
            log("  ⚠️ social proof: repeated errors — disabled for this run")
        else:
            log(f"  ⚠️ social proof failed for {asin}: {e}")
    return out

def _normalize_discount(raw: str) -> str:
    if not raw:
        return ""
    m = re.search(r"(\d{1,3})", raw)
    if not m:
        return (raw or "").strip()
    n = int(m.group(1))
    return f"{n}% OFF"

def utc_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def shorten_title(s: str, max_len: int = MAX_TITLE_LEN) -> str:
    s = (s or "").strip()
    if len(s) <= max_len:
        return s
    cut = s.rfind(" ", 0, max_len - 1)
    if cut < max_len // 2:
        cut = max_len - 1
    return s[:cut].rstrip() + "…"

# ════════════════════════════════════════════════════════════════
#  BLOCKED KEYWORDS
# ════════════════════════════════════════════════════════════════

BLOCKED_TITLE_KEYWORDS = [
    # Clothing / lingerie
    "lingerie",
    "sleepwear", "sleep ware", "sleep wear", "sleepware",
    "women's clothes", "womens clothes", "women clothes",
    "legging", "leggings", "pants", "sex", "neck",
    "panty", "panties", "underwear", "bra", "skirt",
    "sexy", "lace", "wig",
    "nightgown", "blouse", "dress", "dressy",
    "waist shaper", "waist trainer",
    "bikini", "swimsuit", "swimwear", "swim wear",
    "shorts",  # "short" removed (too generic — blocks "short cable" etc.)

    # Alcohol / tobacco / smoking paraphernalia
    "hooka", "hookah", "shisha",
    "smoking", "tobacco", "tobaco",
    "wine", "vodka", "whiskey", "whisky", "beer",

    # Religious (brand-safety on faith-targeted pages)
    "christian", "bible",

    # ── Adult / sexual entertainment ──────────────────────────────────────────
    "anal",          # covers "anal plug", "anal beads", etc. — \b...\b won't match "analysis"
    "dildo",
    "vibrator",
    "masturbator", "masturbation", "masturbate",
    "fleshlight",
    "butt plug",
    "cock ring",
    "sex toy", "adult toy",
    "bondage",
    "bdsm",
    "fetish",
    "erotic",
    "condom",
    "penis", "vagina", "vulva", "clitoris",
    "nipple clamp", "nipple cover",
    "g-string",

    # ── Weapons / injury-risk ─────────────────────────────────────────────────
    "stun gun",
    "taser",
    "brass knuckles",
    "switchblade",
    "butterfly knife",
    "nunchuck", "nunchaku",
]

def _blocked_keyword_hit(title: str) -> str:
    """Return the blocked keyword found in the title, or '' if the title is clean.

    Single words use WORD BOUNDARY + optional plural suffix so 'bra' blocks
    'bras' but not 'library', 'anal' blocks 'anals' but not 'analysis', etc.
    Multi-word phrases (space or hyphen) match as plain substrings.
    Comparison is case-insensitive.
    """
    t_low = (title or "").lower()
    for b in BLOCKED_TITLE_KEYWORDS:
        if not b:
            continue
        bl = b.lower()
        if " " in bl or "-" in bl:
            if bl in t_low:
                return b
        elif re.search(rf"\b{re.escape(bl)}(?:s|es)?\b", t_low):
            return b
    return ""

# ════════════════════════════════════════════════════════════════
#  GOOGLE SHEET — row update with retry
# ════════════════════════════════════════════════════════════════

def update_row(ws, row_idx: int, values: list, max_retries: int = 6):
    last_col = chr(ord('A') + len(values) - 1)
    rng = f"A{row_idx}:{last_col}{row_idx}"
    delay = 1.5
    for attempt in range(max_retries):
        try:
            ws.update(rng, [values], value_input_option="USER_ENTERED")
            return
        except APIError as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code in (429, 500, 502, 503, 504) and attempt < max_retries - 1:
                time.sleep(delay)
                delay *= 1.8
                continue
            raise

def append_rows(ws, rows):
    if rows:
        ws.append_rows(rows, value_input_option="USER_ENTERED")

# ════════════════════════════════════════════════════════════════
#  FFMPEG / FFPROBE UTILITIES
# ════════════════════════════════════════════════════════════════

def _which_ffmpeg()  -> str: return shutil.which("ffmpeg")  or ""
def _which_ffprobe() -> str: return shutil.which("ffprobe") or ""

def _probe_duration(path: str) -> float:
    ffprobe = _which_ffprobe()
    if not ffprobe or not os.path.exists(path):
        return 0.0
    try:
        out = subprocess.check_output(
            [ffprobe, "-v", "error",
             "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            timeout=20
        ).decode().strip()
        return max(0.0, float(out))
    except Exception:
        return 0.0

def _find_fontfile() -> str:
    for p in FONT_CANDIDATES:
        if os.path.exists(p):
            if platform.system().lower() == "windows":
                return p.replace("\\", "/").replace(":", r"\:")
            return p
    return ""

def _detect_chrome_major():
    candidates = ("google-chrome", "chromium-browser", "chromium", "/usr/bin/google-chrome")
    for cmd in candidates:
        if shutil.which(cmd):
            try:
                out = subprocess.check_output([cmd, "--version"], text=True).strip()
                m = re.search(r"(\d+)\.", out)
                if m: return int(m.group(1))
            except Exception:
                continue
    return None

def _write_textfile(dirpath: str, filename: str, content: str) -> str:
    p = os.path.join(dirpath, filename)
    with open(p, "w", encoding="utf-8") as f:
        f.write(content or "")
    if platform.system().lower() == "windows":
        return p.replace("\\", "/").replace(":", r"\:")
    return p

# ════════════════════════════════════════════════════════════════
#  IMAGE STANDARDISATION  (720×1280)
# ════════════════════════════════════════════════════════════════

def standardize_image_for_video(src_path: str, out_path: str, size=None) -> bool:
    if size is None:
        size = (VIDEO_W, VIDEO_H)
    try:
        img = Image.open(src_path)
        img = ImageOps.exif_transpose(img)
        img = img.convert("RGB")
        canvas = Image.new("RGB", size, (0, 0, 0))
        img.thumbnail(size, Image.Resampling.LANCZOS)
        x = (size[0] - img.width)  // 2
        y = (size[1] - img.height) // 2
        canvas.paste(img, (x, y))
        canvas.save(out_path, "PNG", optimize=True)
        return os.path.exists(out_path) and os.path.getsize(out_path) > 1024
    except Exception as e:
        log(f"  ⚠️ image standardization failed: {e}")
        return False

# ════════════════════════════════════════════════════════════════
#  TTS  — edge-tts (free) with OpenAI fallback
# ════════════════════════════════════════════════════════════════

def _sanitize_for_tts(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[@#][\w\-_]+", " ", text)
    text = re.sub(r"[\"""''«»‹›´`]+", "", text)
    text = text.replace("&", " and ").replace("+", " plus ")
    text = re.sub(r"%\s*", " percent ", text)
    text = re.sub(r"([!?]){2,}", r"\1", text)
    text = re.sub(r"\.{2,}", ".", text)
    def strip_emoji(s):
        out = []
        for ch in s:
            cp = ord(ch)
            if (0x1F1E6 <= cp <= 0x1FAD6) or (0x1F300 <= cp <= 0x1F5FF) or \
               (0x1F900 <= cp <= 0x1F9FF) or (0x2600 <= cp <= 0x27BF) or (cp == 0xFE0F):
                continue
            out.append(ch)
        return "".join(out)
    text = strip_emoji(text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    if len(text) > 350:
        text = text[:350].rsplit(" ", 1)[0] + "."
    return text

OPENAI_KEY_PATHS = ["/mnt/data/videokey.txt", os.path.expanduser("~/videokey.txt")]

def _read_openai_key():
    for path in OPENAI_KEY_PATHS:
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    k = f.read().strip()
                    if k:
                        return k
        except Exception:
            continue
    return None

def _tts_openai_to_mp3(text: str, out_mp3: str, voice: str = "nova") -> bool:
    """OpenAI TTS — kept as fallback."""
    key = os.environ.get("OPENAI_API_KEY") or _read_openai_key()
    if not key or not text:
        return False
    try:
        payload = {"model": "gpt-4o-mini-tts", "voice": voice, "input": text}
        r = requests.post(
            "https://api.openai.com/v1/audio/speech",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload, timeout=300
        )
        if not r.ok:
            try:    log(f"  ⚠️ TTS {r.status_code}: {r.text[:300]}")
            except: log(f"  ⚠️ TTS {r.status_code} (no body)")
            return False
        with open(out_mp3, "wb") as f:
            f.write(r.content)
        return os.path.exists(out_mp3) and os.path.getsize(out_mp3) > 1024
    except Exception as e:
        log(f"  ⚠️ OpenAI TTS error: {e}")
        return False

def _tts_to_mp3(text: str, out_path: str) -> bool:
    """
    Primary: edge-tts (free, runs locally, no API key).
    Fallback: OpenAI TTS.
    Install edge-tts once:  pip install edge-tts
    """
    if not text:
        return False

    # ── Try edge-tts ──────────────────────────────────────────
    try:
        import edge_tts  # noqa

        async def _run():
            communicate = edge_tts.Communicate(text, "en-US-EmmaNeural", rate="-8%")
            await communicate.save(out_path)

        asyncio.run(_run())
        if os.path.exists(out_path) and os.path.getsize(out_path) > 512:
            log("  ✓ TTS via edge-tts (free)")
            return True
    except ImportError:
        log("  ℹ️ edge-tts not installed — falling back to OpenAI TTS  (run: pip install edge-tts)")
    except Exception as e:
        log(f"  ⚠️ edge-tts failed: {e} — falling back to OpenAI TTS")

    # ── Fallback: OpenAI ──────────────────────────────────────
    return _tts_openai_to_mp3(text, out_path)

# ════════════════════════════════════════════════════════════════
#  SOCIAL POST  — Gemini free tier with OpenAI fallback
# ════════════════════════════════════════════════════════════════

GEMINI_KEY_PATHS = ["/mnt/data/geminikey.txt", os.path.expanduser("~/geminikey.txt")]
_GEMINI_DEAD_KEYS: set = set()   # keys that returned 400 (expired) — skip for rest of session

def _read_gemini_keys():
    """Return a list of Gemini API keys.
    Checks ~/geminikeys.txt first (one key per line, multiple accounts).
    Falls back to single-key geminikey.txt for backward compatibility."""
    multi_path = os.path.expanduser("~/geminikeys.txt")
    if os.path.exists(multi_path):
        try:
            with open(multi_path, "r", encoding="utf-8", errors="ignore") as f:
                keys = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
            if keys:
                return keys
        except Exception:
            pass
    # Legacy single-key fallback
    for path in GEMINI_KEY_PATHS:
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    k = f.read().strip()
                    if k:
                        return [k]
        except Exception:
            continue
    return []

# Opening styles — one is randomly chosen per post to break the "every post
# starts the same" repetition. Each Gemini call is stateless, so without this
# seed the model defaults to the same handful of openers.
_POST_OPENING_STYLES = [
    "a bold, surprising one-liner",
    "a playful rhetorical question",
    "a relatable everyday frustration this product fixes",
    "a witty exaggeration about how good the deal is",
    "a mock 'news flash' announcement",
    "a cheeky dare to the reader",
    "a warm friend-to-friend tip",
    "a funny 'plot twist' setup",
    "a tongue-in-cheek confession",
    "an unexpected comparison or metaphor",
]

# Overused openers the model must never start with
_BANNED_OPENERS = (
    "Calling all', 'Attention', 'Are you tired', 'Looking for', 'Hey there', "
    "'Tired of', 'Introducing', 'Get ready', 'Say goodbye', 'Stop scrolling"
)


def _build_post_prompt(code, discount_pct, expiry_date, title, price) -> str:
    style = random.choice(_POST_OPENING_STYLES)
    lines = [
        "Write ONE short, witty Facebook post for an Amazon deal.",
        f"Open with {style}.",
        f"Never begin with any of these: '{_BANNED_OPENERS}'.",
        "",
        f"Product (use only 2-4 key words, not the full name): {title}",
    ]
    if discount_pct:
        lines.append(f"Discount: {discount_pct} off")
    if price:
        lines.append(f"Final price after discount: {price}")
    if expiry_date:
        lines.append(f"Deal ends: {expiry_date}")
    if code:
        lines.append(f"Discount code: {code}")
    lines += [
        "",
        "Strict rules:",
        "- 40 words MAX. Tight and punchy.",
        "- Use smooth, natural sentences with commas for breathing, so it reads "
        "well aloud as a voiceover. No choppy one-word fragments.",
        f"- You MUST mention the price ({price}) and that the deal ends {expiry_date}.",
        ("- Refer to the discount code generically (e.g. 'use the code at checkout') "
         "— do NOT spell out the actual code letters."
         if code else "- No discount code is needed for this deal."),
        "- Do NOT mention any link, URL, 'link in bio', 'link below', or 'click here'.",
        "- No hashtags. At most one emoji. No labels, no preamble.",
        "This text is BOTH the post body and a spoken voiceover, so keep it clean.",
        "Return only the post text.",
    ]
    return "\n".join(lines)


def _clean_post_text(txt: str) -> str:
    """Strip any URL or link call-to-action the model added, so the prose stays
    clean and voiceover-ready. The real affiliate link is appended separately."""
    txt = re.sub(r"https?://\S+", "", txt)
    txt = re.sub(
        r"(?i)\b(link in bio|link below|link here|click (the )?link|"
        r"tap the link|shop now via|grab it (here|via the link)|"
        r"link\s*[👇⬇️🔗]+)\b[.! ]*", "", txt)
    # collapse blank lines and trailing whitespace left behind
    txt = re.sub(r"\n{3,}", "\n\n", txt).strip()
    return txt


def _finalize_post(txt: str, link: str) -> str:
    """Clean the generated prose and append the affiliate link on its own line."""
    clean = _clean_post_text(txt)
    return f"{clean}\n\n{link}" if link else clean


def generate_social_post(link, code, discount_pct, expiry, title, price):
    # Convert a relative expiry ("7 days") to an absolute date ("June 11") so the
    # post never says "ends in 2 days" — viewers may see it days later.
    expiry_date = expiry_to_date_text(expiry) or expiry

    # Col O is the VO/body copy ONLY — no link, no spelled-out code (FBP_ready
    # appends the code; the link goes in the FB link field).
    fb_text  = (f"{discount_pct} off — now just {price}, but only until {expiry_date}. "
                + ("Use the code at checkout. " if code else "")
                + "Grab yours before it's gone!")
    fallback = _clean_post_text(fb_text)

    if os.getenv("VIPON_DISABLE_GPT", "0") in ("1","true","TRUE","yes","YES"):
        return fallback

    prompt = _build_post_prompt(code, discount_pct, expiry_date, title, price)

    # ── Try Gemini keys in order, skip dead/rate-limited keys ────
    # When the server returns 503 (overloaded) on 3+ keys in a row it usually means
    # the whole fleet is momentarily busy — not that the keys are bad. Wait 5s then
    # restart from key 0 so fresh capacity is tried. Only do one such reset per call
    # to avoid an infinite loop; after the reset the loop runs to exhaustion normally.
    gemini_keys = _read_gemini_keys()
    _503_streak   = 0
    _did_503_reset = False
    kidx = 0
    while kidx < len(gemini_keys):
        gemini_key = gemini_keys[kidx]
        if gemini_key in _GEMINI_DEAD_KEYS:
            kidx += 1
            continue   # expired key — don't waste a round-trip
        try:
            api_url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
                       f"gemini-2.5-flash:generateContent?key={gemini_key}")
            payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": 0.95,
                    "maxOutputTokens": 200,
                    # 2.5-flash is a thinking model — disable thinking so the token
                    # budget produces the actual post, not hidden reasoning.
                    "thinkingConfig": {"thinkingBudget": 0},
                }
            }
            resp = requests.post(api_url, json=payload, timeout=25)
            if resp.ok:
                j   = resp.json()
                txt = (j.get("candidates", [{}])[0]
                         .get("content", {})
                         .get("parts", [{}])[0]
                         .get("text", "")).strip()
                if txt:
                    log(f"  ✓ Post generated via Gemini (key {kidx+1}/{len(gemini_keys)})")
                    return _clean_post_text(txt)
            elif resp.status_code == 429:
                log(f"  ⚠️ Gemini key {kidx+1}/{len(gemini_keys)} rate-limited — trying next")
                _503_streak = 0
            elif resp.status_code == 400:
                log(f"  ⚠️ Gemini key {kidx+1} expired/invalid — marking dead for this session")
                _GEMINI_DEAD_KEYS.add(gemini_key)
                _503_streak = 0
            elif resp.status_code == 503:
                _503_streak += 1
                log(f"  ⚠️ Gemini key {kidx+1} server busy (503) — trying next "
                    f"(503 streak: {_503_streak})")
                if _503_streak >= 3 and not _did_503_reset:
                    log(f"  ⏳ 3 consecutive 503s — waiting 5s then restarting from key 1…")
                    time.sleep(5)
                    _503_streak   = 0
                    _did_503_reset = True   # only one reset per generate_social_post call
                    kidx = 0               # restart from key 1
                    continue
            else:
                log(f"  ⚠️ Gemini key {kidx+1} error {resp.status_code}: {resp.text[:150]}")
                _503_streak = 0
        except Exception as e:
            log(f"  ⚠️ Gemini key {kidx+1} exception: {e}")
            _503_streak = 0
        kidx += 1

    # ── Fallback: OpenAI GPT-3.5-turbo ────────────────────────
    api_key = _read_openai_key()
    if api_key:
        try:
            payload = {
                "model": "gpt-3.5-turbo",
                "temperature": 0.8,
                "messages": [
                    {"role": "system", "content": "You are a witty social media marketer."},
                    {"role": "user",   "content": prompt}
                ]
            }
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            resp = requests.post("https://api.openai.com/v1/chat/completions",
                                 headers=headers, json=payload, timeout=25)
            if resp.ok:
                j   = resp.json()
                txt = (j.get("choices", [{}])[0]
                         .get("message", {})
                         .get("content", "")).strip()
                if txt:
                    log("  ✓ Post generated via OpenAI")
                    return _clean_post_text(txt)
        except Exception as e:
            log(f"  ⚠️ OpenAI post error: {e}")

    return fallback

# ════════════════════════════════════════════════════════════════
#  CLOUDINARY
# ════════════════════════════════════════════════════════════════

def cloudinary_url_exact(img_url: str, discount_pct_raw: str, code: str) -> str:
    pct_txt = (discount_pct_raw or "").strip().upper()
    if pct_txt and "OFF" not in pct_txt:
        pct_txt = (pct_txt.split()[0] + " OFF") if "%" in pct_txt else (pct_txt + " OFF")
    pct_enc  = quote(pct_txt, safe="")
    code_enc = quote(f"Code: {code}", safe="")
    img_enc  = quote(img_url or "", safe="")
    return (f"https://res.cloudinary.com/{CLOUDINARY_CLOUD_NAME}/image/fetch/"
            f"l_text:Arial_40_bold:{pct_enc},g_north_east,x_35,y_35/"
            f"l_text:Arial_40_bold:{code_enc},g_north_east,x_35,y_90/{img_enc}")

def _cloudinary_upload_video(mp4_path: str, public_id: str, max_retries: int = 5) -> str:
    ts       = str(int(time.time()))
    to_sign  = f"public_id={public_id}&timestamp={ts}{CLOUDINARY_API_SECRET}"
    signature = hashlib.sha1(to_sign.encode("utf-8")).hexdigest()
    url  = f"https://api.cloudinary.com/v1_1/{CLOUDINARY_CLOUD_NAME}/video/upload"
    data = {
        "api_key":   CLOUDINARY_API_KEY,
        "timestamp": ts,
        "public_id": public_id,
        "signature": signature,
    }
    headers = {"Connection": "close"}
    delay = 2.0
    for attempt in range(max_retries):
        try:
            with open(mp4_path, "rb") as f:
                files = {"file": (os.path.basename(mp4_path), f, "video/mp4")}
                r = requests.post(url, data=data, files=files, headers=headers, timeout=(30, 600))
            r.raise_for_status()
            j = r.json()
            return j.get("secure_url") or j.get("url") or ""
        except requests.RequestException as e:
            if attempt < max_retries - 1:
                log(f"  ⚠️ Cloudinary upload retry {attempt+1}/{max_retries}: {e}")
                time.sleep(delay); delay *= 1.8
            else:
                raise

# ── Code-failure diagnostics ─────────────────────────────────────
# On the FIRST no-code result per account, snapshot the page and push it to
# Cloudinary (named account + timestamp). Lets us see whether the reveal zone is
# empty, shows a cap/limit message, or a CAPTCHA — i.e. IP vs account vs something
# else. One image per account keeps noise + Cloudinary usage low.
_CODE_FAIL_CAPTURED = set()   # account indices already captured this run

def _cloudinary_upload_image(img_path: str, public_id: str, max_retries: int = 3) -> str:
    ts        = str(int(time.time()))
    to_sign   = f"public_id={public_id}&timestamp={ts}{CLOUDINARY_API_SECRET}"
    signature = hashlib.sha1(to_sign.encode("utf-8")).hexdigest()
    url  = f"https://api.cloudinary.com/v1_1/{CLOUDINARY_CLOUD_NAME}/image/upload"
    data = {"api_key": CLOUDINARY_API_KEY, "timestamp": ts,
            "public_id": public_id, "signature": signature}
    delay = 2.0
    for attempt in range(max_retries):
        try:
            with open(img_path, "rb") as f:
                files = {"file": (os.path.basename(img_path), f, "image/png")}
                r = requests.post(url, data=data, files=files,
                                  headers={"Connection": "close"}, timeout=(30, 120))
            r.raise_for_status()
            j = r.json()
            return j.get("secure_url") or j.get("url") or ""
        except requests.RequestException as e:
            if attempt < max_retries - 1:
                time.sleep(delay); delay *= 1.8
            else:
                log(f"  ⚠️ Cloudinary image upload failed: {e.__class__.__name__}")
    return ""

def _capture_code_failure(driver, pid):
    """First no-code result per account: log the reveal-zone text + bot-check markers,
    screenshot the page, and upload the image to Cloudinary for inspection."""
    idx = _account_index
    if idx in _CODE_FAIL_CAPTURED:
        return
    _CODE_FAIL_CAPTURED.add(idx)
    try:
        acc   = (_current_account() or {}).get("username", f"acct{idx}")
        safe  = re.sub(r"[^A-Za-z0-9]", "_", acc)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        try:    url = driver.current_url
        except Exception: url = "?"
        try:    zone = (driver.find_element(By.ID, "PC_240_jumpToAmzFromCodeZone")
                        .get_attribute("textContent") or "").strip()
        except Exception: zone = "(zone not found)"
        try:    html = (driver.page_source or "").lower()
        except Exception: html = ""
        markers = [m for m in ("captcha", "recaptcha", "cloudflare", "unusual traffic",
                               "verify", "limit", "too many", "rate", "blocked",
                               "access denied", "try again", "challenge") if m in html]
        log(f"  🔎 first code-failure [{acc}] PID {pid} url={url!r} "
            f"reveal-zone={zone[:60]!r} markers={markers or 'none'}")
        os.makedirs("debug_artifacts", exist_ok=True)
        png = os.path.join("debug_artifacts", f"codefail_{safe}_{stamp}.png")
        if driver.save_screenshot(png):
            sec = _cloudinary_upload_image(png, f"vipon_debug/codefail_{safe}_{stamp}")
            if sec:
                log(f"  📸 code-failure screenshot [{acc}] -> {sec}")
    except Exception as e:
        log(f"  ⚠️ _capture_code_failure error: {e.__class__.__name__}")

# ════════════════════════════════════════════════════════════════
#  AFFILIATE LINKS
# ════════════════════════════════════════════════════════════════

def _build_affiliate_dp_link(asin: str, tld: str = "com") -> str:
    if not asin: return ""
    tag  = AFFILIATE_ID_CA if tld == "ca" else AFFILIATE_ID
    base = f"https://www.amazon.{tld}/dp/{asin}"
    sep  = "&" if "?" in base else "?"
    return base + f"{sep}tag={tag}"

def _worker_smartlink(asin: str, tag: str, tld: str = "com") -> str:
    dp = _build_affiliate_dp_link(asin, tld)
    if not WORKER_BASE:
        return dp
    qs = urllib.parse.urlencode({"asin": asin.upper(), "tag": tag, "tld": tld})
    return f"{WORKER_BASE}/a?{qs}"

def get_affiliate_link(asin: str, tld: str = "com") -> str:
    link = _worker_smartlink(asin, AFFILIATE_ID, tld)
    if os.getenv("VIPON_SHORT_LINK", "0") in ("1","true","TRUE","yes","YES"):
        try:
            resp = requests.get("http://tinyurl.com/api-create.php",
                                params={"url": link}, timeout=8)
            if resp.ok and resp.text and resp.text.startswith("http"):
                return resp.text.strip()
        except Exception:
            pass
    return link

def get_platform_links(asin: str, tld: str = "com") -> dict:
    if not asin:
        return {"reel": "", "ig": "", "youtube": "", "tiktok": "", "pinterest": ""}
    asin = asin.upper()
    return {
        "reel":      _worker_smartlink(asin, TAG_REEL,      tld),
        "ig":        _worker_smartlink(asin, TAG_IG,        tld),
        "youtube":   _worker_smartlink(asin, TAG_YOUTUBE,   tld),
        "tiktok":    _worker_smartlink(asin, TAG_TIKTOK,    tld),
        "pinterest": _worker_smartlink(asin, TAG_PINTEREST, tld),
    }

# ════════════════════════════════════════════════════════════════
#  SELENIUM / CHROME
# ════════════════════════════════════════════════════════════════

def create_driver():
    log("▶ Launching browser…")
    common_args = [
        "--headless=new",
        "--window-size=1920,1080",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-blink-features=AutomationControlled",
        "--disable-extensions",
        "--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
        "--lang=en-US,en;q=0.9",
    ]
    try:
        opts_uc = uc.ChromeOptions()
        for a in common_args: opts_uc.add_argument(a)
        major = _detect_chrome_major()
        if major:
            log(f"  ↳ Detected Chrome major: {major}")
            driver = uc.Chrome(options=opts_uc, version_main=major)
        else:
            driver = uc.Chrome(options=opts_uc)
        driver.set_page_load_timeout(PAGELOAD_TIMEOUT)
        driver.set_script_timeout(SCRIPT_TIMEOUT)
        driver.implicitly_wait(IMPLICIT_WAIT)
        return driver
    except (SessionNotCreatedException, WebDriverException, Exception) as e:
        log(f"  ⚠️ UC failed ({e.__class__.__name__}): {e} — trying Selenium Manager")
    try:
        from selenium.webdriver.chrome.options import Options as ChromeOptions
        opts_sm = ChromeOptions()
        for a in common_args: opts_sm.add_argument(a)
        driver = webdriver.Chrome(options=opts_sm)
        driver.set_page_load_timeout(PAGELOAD_TIMEOUT)
        driver.set_script_timeout(SCRIPT_TIMEOUT)
        driver.implicitly_wait(IMPLICIT_WAIT)
        return driver
    except Exception as e:
        raise RuntimeError(f"Failed to start Chrome (UC & Selenium Manager both failed): {e}")

def _dismiss_overlays(driver):
    xpaths = [
        "//button[contains(.,'Accept') or contains(.,'I agree') or contains(.,'Got it')]",
        "//a[contains(.,'Accept') or contains(.,'Agree')]",
        "//*[@id='onetrust-accept-btn-handler']",
        "//*[@class='cookie' or contains(@class,'cookie')]//button",
        "//div[contains(@class,'modal')]//button[contains(.,'Close') or contains(.,'×')]",
    ]
    for xp in xpaths:
        try:
            el = WebDriverWait(driver, 3).until(EC.element_to_be_clickable((By.XPATH, xp)))
            el.click(); time.sleep(0.3)
        except Exception:
            continue

def logout(driver):
    """Navigate to the logout URL so the next login() starts from a clean state."""
    try:
        driver.get("https://www.myvipon.com/logout")
        time.sleep(2)
        log("✓ Logged out")
    except Exception as e:
        log(f"  ⚠️ Logout error (ignored): {e.__class__.__name__}")

def _capture_login_failure(driver, acc):
    """On a login failure, snapshot what the CI browser actually sees (CAPTCHA /
    Cloudflare / block page / blank). Saves PNG + HTML to debug_artifacts/ (uploaded
    as a GitHub Actions artifact) and logs the URL, title, and any bot-check markers
    found in the page text — the markers alone often answer 'why' without the image."""
    try:
        os.makedirs("debug_artifacts", exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        safe  = re.sub(r"[^A-Za-z0-9]", "_", (acc or {}).get("username", "acct"))
        base  = os.path.join("debug_artifacts", f"login_fail_{safe}_{stamp}")
        try:    url = driver.current_url
        except Exception: url = "?"
        try:    title = driver.title
        except Exception: title = "?"
        log(f"  📸 LOGIN FAILURE — url={url!r} title={title!r}")
        try:
            driver.save_screenshot(base + ".png")
            log(f"  📸 screenshot -> {base}.png")
        except Exception as e:
            log(f"  ⚠️ screenshot failed: {e.__class__.__name__}")
        html = ""
        try:
            html = driver.page_source or ""
            with open(base + ".html", "w", encoding="utf-8") as f:
                f.write(html)
            log(f"  📸 page source -> {base}.html ({len(html)} chars)")
        except Exception as e:
            log(f"  ⚠️ page source failed: {e.__class__.__name__}")
        low = html.lower()
        markers = [m for m in ("captcha", "recaptcha", "cloudflare", "unusual traffic",
                               "verify", "are you a robot", "access denied", "forbidden",
                               "blocked", "too many requests", "rate limit", "geetest",
                               "slider", "challenge") if m in low]
        log(f"  🚩 bot-check markers: {', '.join(markers) if markers else 'none found in page text'}")
    except Exception as e:
        log(f"  ⚠️ _capture_login_failure error: {e.__class__.__name__}")


def login(driver, wait):
    acc = _current_account()
    log(f"▶ Logging in as {acc['username']}…")
    try:
        driver.get("https://www.myvipon.com/login")
        wait.until(EC.presence_of_element_located((By.NAME, "LoginForm[email]"))).send_keys(acc["username"])
        wait.until(EC.presence_of_element_located((By.NAME, "LoginForm[password]"))).send_keys(acc["password"])
        for xp in [
            "//div[contains(@class,'google_test') and normalize-space(text())='Log In']",
            "//button[@type='submit']",
            "//button[contains(.,'Login') or contains(.,'Log in') or contains(.,'Sign in')]",
            "//input[@type='submit']",
        ]:
            try:
                btn = WebDriverWait(driver, 12).until(EC.element_to_be_clickable((By.XPATH, xp)))
                btn.click(); break
            except Exception:
                continue
        # Give post-login redirect more time than the default WAIT_SECS
        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.XPATH, "//a[contains(@href,'logout')]"))
        )
        _dismiss_overlays(driver)
        log("✓ Logged in")
    except Exception:
        _capture_login_failure(driver, acc)   # snapshot the block/CAPTCHA page, then re-raise
        raise

# ════════════════════════════════════════════════════════════════
#  GOOGLE SHEETS
# ════════════════════════════════════════════════════════════════

HEADER = [
    "Link", "Reel", "IG", "Youtube", "TikTok",
    "Discount Code", "Disc", "Expiry", "Product", "Price",
    "PID", "Image", "Pin Image", "Reel URL", "FB Post", "Reel Posted",
    "FB Text Posted", "YT Posted", "Social Score",
]
COL_S_SOCIAL = 19   # Social Score (1-based) — used by the reel publisher to rank

# ── Pinterest sheet (preserved, gated by ENABLE_PINTEREST) ──────
PINTEREST_SHEET_NAME       = "Pintrest"
PINTEREST_BOARD_DEFAULT    = "Daily Coupons and Discounts"
PINTEREST_KEYWORDS_DEFAULT = "Discount, Code, Coupon, Amazon, Deal"
PINTEREST_THUMBNAIL_DEFAULT = "2"

def generate_pinterest_description(title, discount_pct, code, expiry, price):
    parts = []
    if title:
        t = title.strip()
        if len(t) > 80: t = t[:77] + "..."
        parts.append(f"🎁 {t}")
    if discount_pct: parts.append(f"Save {discount_pct}")
    if code:         parts.append(f"Use code {code}")
    if price:        parts.append(f"Now {price}")
    if expiry:       parts.append(f"Ends {expiry}")
    desc = " • ".join(parts) or "Hot deal • Limited time"
    desc += "  #Amazon #Deal #Coupon"
    return desc

def open_pinterest_sheet_and_reset():
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDS_FILE, scope)
    client = gspread.authorize(creds)
    try:
        ws = client.open(GOOGLE_SHEET_NAME).worksheet(PINTEREST_SHEET_NAME)
    except Exception:
        ss = client.open(GOOGLE_SHEET_NAME)
        ws = ss.add_worksheet(title=PINTEREST_SHEET_NAME, rows=1000, cols=20)
    ws.clear()
    ws.append_row(
        ["Title", "Media URL", "Pinterest board", "Thumbnail",
         "Description", "Link", "Publish date", "Keywords"],
        value_input_option="USER_ENTERED"
    )
    return ws

def _delete_rows_batched(ws, row_indices: list, label: str = "") -> None:
    """Delete sheet rows in contiguous-range batches to minimise write API calls.

    Deleting each row individually burns one write quota unit per row.  Grouping
    consecutive rows (e.g. 5-7) into a single delete_rows(5, 7) call uses just
    one unit per contiguous range, dramatically reducing quota pressure.
    A 1-second pause is inserted between distinct ranges as a safety buffer.
    """
    if not row_indices:
        return

    # Build contiguous ranges from the sorted index list
    sorted_rows = sorted(row_indices)
    ranges: list[tuple[int, int]] = []
    start = prev = sorted_rows[0]
    for r in sorted_rows[1:]:
        if r == prev + 1:
            prev = r
        else:
            ranges.append((start, prev))
            start = prev = r
    ranges.append((start, prev))

    log(f"  {label}deleting {len(row_indices)} rows in {len(ranges)} batch(es)…")

    # Delete bottom-to-top so earlier row numbers stay valid after each deletion
    for start_r, end_r in reversed(ranges):
        try:
            ws.delete_rows(start_r, end_r)
        except Exception as e:
            log(f"  ⚠️ delete_rows({start_r},{end_r}) failed: {e}")
        time.sleep(1.1)   # 1-second gap between API write calls (quota guard)


def _rows_to_delete(values: list, *, col_q: int = 16) -> list[int]:
    """Return 1-based sheet row numbers that should be deleted.

    A row is deleted when:
      • Col Q (FB text posted) = "Yes"  — the final step; signals full promotion, OR
      • Col A (link) is empty AND any flag column (P/Q/R) has a value
        (orphaned "Yes" left behind after partial cleaning).
    """
    to_delete = []
    for idx, row in enumerate(values[1:], start=2):   # row 1 is header
        link  = row[0].strip() if row else ""
        q_val = row[col_q].strip().lower() if len(row) > col_q else ""

        # FB text posted = fully promoted, safe to clear
        fully_done = q_val == "yes"

        # Orphaned: no link in col A but "Yes" appears anywhere in the row
        any_flag = any(cell.strip().lower() == "yes" for cell in row)
        orphaned = not link and any_flag

        if fully_done or orphaned:
            to_delete.append(idx)

    return to_delete


def open_sheet_and_reset():
    log("▶ Opening Google Sheet and cleaning completed rows…")
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDS_FILE, scope)
    client = gspread.authorize(creds)
    ws = client.open(GOOGLE_SHEET_NAME).sheet1

    values = ws.get_all_values()
    if not values:
        ws.append_row(HEADER, value_input_option="USER_ENTERED")
        log("✓ Sheet was empty — header added")
        return ws

    if values[0] != HEADER:
        ws.update("A1", [HEADER], value_input_option="USER_ENTERED")
        time.sleep(1.1)
        log(f"✓ Sheet1 header repaired ({len(values[0])} → {len(HEADER)} cols)")

    # Delete rows where col Q (FB text posted) = "Yes", plus orphaned flag rows
    to_delete = _rows_to_delete(values)
    _delete_rows_batched(ws, to_delete, label="Sheet1: ")
    log(f"✓ Sheet ready — removed {len(to_delete)} completed/orphaned rows")
    return ws


def open_sheet2_and_reset():
    """Open Sheet2 (Canada) and remove completed or orphaned rows."""
    log("▶ Opening Sheet2 (Canada) and cleaning completed rows…")
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDS_FILE, scope)
    client = gspread.authorize(creds)
    ss = client.open(GOOGLE_SHEET_NAME)
    try:
        ws2 = ss.worksheet(SHEET2_TAB)
    except Exception:
        ws2 = ss.get_worksheet(1)

    values = ws2.get_all_values()
    if not values:
        ws2.update("A1", [HEADER], value_input_option="USER_ENTERED")
        log("✓ Sheet2 was empty — header added")
        return ws2

    if values[0] != HEADER:
        ws2.update("A1", [HEADER], value_input_option="USER_ENTERED")
        time.sleep(1.1)
        log(f"✓ Sheet2 header repaired ({len(values[0])} → {len(HEADER)} cols)")

    # Delete rows where col Q (FB text posted) = "Yes", plus orphaned flag rows
    to_delete = _rows_to_delete(values)
    _delete_rows_batched(ws2, to_delete, label="Sheet2: ")
    log(f"✓ Sheet2 ready — removed {len(to_delete)} completed/orphaned rows")
    return ws2


def switch_to_canada(driver):
    """Click the country flag dropdown on myvipon.com and select Canada."""
    try:
        dropdown = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.ID, "dropdownMenu2"))
        )
        dropdown.click()
        time.sleep(1)
        # Use data-domain attribute — confirmed in DevTools (li data-domain="www.amazon.ca")
        canada = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((
                By.XPATH,
                "//li[@data-domain='www.amazon.ca']/a"
            ))
        )
        canada.click()
        time.sleep(3)
        log("✓ Switched myvipon.com to Canada")
        return True
    except Exception as e:
        log(f"  ⚠️ Could not switch to Canada: {e}")
        return False


# ════════════════════════════════════════════════════════════════
#  PAGE HELPERS
# ════════════════════════════════════════════════════════════════

def _attr(el, name: str) -> str:
    try: return (el.get_attribute(name) or "").strip()
    except Exception: return ""

def _text(el) -> str:
    try:
        t = (el.text or "").strip()
        if t: return t
    except Exception: pass
    try: return (el.get_attribute("textContent") or "").strip()
    except Exception: return ""

# ════════════════════════════════════════════════════════════════
#  IMAGE RESOLVER
# ════════════════════════════════════════════════════════════════

def _looks_like_product_img(url: str) -> bool:
    if not url or not url.startswith("http"):
        return False
    low = url.lower()
    if any(b in low for b in ("vipon.com", "/favicon", "logo", "/static/", "data:image")):
        return False
    return any(h in url for h in AMAZON_HOST_HINTS)

def _extract_amazon_img_from_source(src: str) -> str:
    if not src: return ""
    m = re.search(r"(https://m\.media-amazon\.com[^\"'>]+\.(?:jpe?g|png))", src, re.I)
    return m.group(1) if m else ""

def resolve_cover_image_url(driver) -> str:
    xps = [
        "//div[contains(@class,'left-show-img')]//img",
        "//div[contains(@class,'product') and contains(@class,'img')]//img",
        "//div[contains(@class,'box-img')]//img",
        "//img[contains(@src,'m.media-amazon.com') or contains(@data-src,'m.media-amazon.com')]",
        "//img[contains(@src,'images-na') or contains(@data-src,'images-na')]",
    ]
    for xp in xps:
        try:
            els = driver.find_elements(By.XPATH, xp)
            for el in els:
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                time.sleep(0.25)
                for attr in ("src", "data-src", "data-original", "data-lazy"):
                    url = (el.get_attribute(attr) or "").strip()
                    if _looks_like_product_img(url):
                        return url
        except Exception:
            pass
    try:
        og = driver.find_element(By.XPATH, "//meta[@property='og:image' or @name='og:image']")
        url = (og.get_attribute("content") or "").strip()
        if _looks_like_product_img(url):
            return url
    except Exception:
        pass
    try:
        return _extract_amazon_img_from_source(driver.page_source or "")
    except Exception:
        return ""
# PA-API client cached at module level — created once, reused across all products
_PAAPI_CLIENT = None

def _get_paapi_client():
    global _PAAPI_CLIENT
    if _PAAPI_CLIENT is not None:
        return _PAAPI_CLIENT
    try:
        import csv
        from paapi5_python_sdk.api.default_api import DefaultApi
        csv_path = os.path.expanduser("~/PAAPI.csv")
        with open(csv_path) as f:
            row = next(csv.DictReader(f))
            access = row["Access Key"].strip()
            secret = row["Secret Key"].strip()
        _PAAPI_CLIENT = DefaultApi(
            access_key=access,
            secret_key=secret,
            host="webservices.amazon.com",
            region="us-east-1",
        )
        return _PAAPI_CLIENT
    except Exception as e:
        log(f"  ⚠️ PA-API client init failed: {e}")
        return None


def _fetch_images_http(asin: str, tld: str = "com", max_imgs: int = 9) -> list:
    """HTTP-only image fallback: fetch the Amazon product page with requests and extract
    product image URLs from the embedded JS colorImages JSON. No Selenium, no PA API."""
    url = f"https://www.amazon.{tld}/dp/{asin}?th=1&psc=1"
    try:
        r = requests.get(url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        src = r.text
        seen, imgs = set(), []
        # Amazon embeds all product images as {"hiRes":"URL",...} in the colorImages JS var
        for m in re.finditer(r'"hiRes"\s*:\s*"(https://[^"]+\.(?:jpe?g|png))"', src, re.I):
            u = m.group(1)
            if u not in seen:
                seen.add(u); imgs.append(u)
            if len(imgs) >= max_imgs:
                break
        if not imgs:
            # Broader fallback: any m.media-amazon.com product image (skip tiny thumbnails)
            for m in re.finditer(
                    r'(https://m\.media-amazon\.com/images/I/[A-Za-z0-9%._-]{20,}\.(?:jpe?g|png))',
                    src, re.I):
                u = m.group(1)
                if re.search(r'\._S[SX]\d{2,3}_\.', u):   # skip small variants (_SX38_, _SS40_)
                    continue
                if u not in seen:
                    seen.add(u); imgs.append(u)
                if len(imgs) >= max_imgs:
                    break
        log(f"  ↳ HTTP image fallback: {len(imgs)} for {asin}")
        return imgs[:max_imgs]
    except Exception as e:
        log(f"  ⚠️ HTTP image fallback failed for {asin}: {e}")
        return []


def fetch_amazon_images(driver, asin: str, tld: str = "com", max_imgs: int = 9) -> list:
    """
    Primary: PA-API 5. Fallback: lightweight HTTP scrape of the product page.
    The 'driver' param is kept for signature compatibility but is not used.
    """
    if not asin:
        return []
    imgs = []
    client = _get_paapi_client()
    if client:
        try:
            from paapi5_python_sdk.get_items_request import GetItemsRequest
            from paapi5_python_sdk.get_items_resource import GetItemsResource
            from paapi5_python_sdk.partner_type import PartnerType

            log(f"  ↳ Amazon PA-API: {asin}")
            request = GetItemsRequest(
                partner_tag=AFFILIATE_ID_CA if tld == "ca" else AFFILIATE_ID,
                partner_type=PartnerType.ASSOCIATES,
                marketplace=f"www.amazon.{tld}",
                item_ids=[asin],
                resources=[
                    GetItemsResource.IMAGES_PRIMARY_LARGE,
                    GetItemsResource.IMAGES_VARIANTS_LARGE,
                ],
            )
            response = client.get_items(request)
            if response and response.items_result and response.items_result.items:
                item = response.items_result.items[0]
                try:
                    if item.images and item.images.primary and item.images.primary.large:
                        imgs.append(item.images.primary.large.url)
                except Exception:
                    pass
                try:
                    if item.images and item.images.variants:
                        for v in item.images.variants:
                            if v.large and v.large.url and v.large.url not in imgs:
                                imgs.append(v.large.url)
                            if len(imgs) >= max_imgs:
                                break
                except Exception:
                    pass
                log(f"  ↳ PA-API images: {len(imgs)}")
            else:
                log(f"  ⚠️ PA-API returned no items for {asin}")
        except Exception as e:
            log(f"  ⚠️ PA-API fetch failed for {asin}: {e}")

    if not imgs:
        imgs = _fetch_images_http(asin, tld, max_imgs)

    return imgs[:max_imgs]

# ════════════════════════════════════════════════════════════════
#  TILE DISCOVERY
# ════════════════════════════════════════════════════════════════

def collect_promo_tiles_random(driver, wait, start_url: str = PROMO_URL):
    log(f"▶ Loading promotions (random scroll)… {start_url}")
    driver.get(start_url)
    _dismiss_overlays(driver)

    def _has_any_products(d):
        return (d.find_elements(By.XPATH, "//a[contains(@href,'/product')]") or
                d.find_elements(By.XPATH, "//div[contains(@class,'box') and contains(@class,'solid')]"))
    try:
        WebDriverWait(driver, WAIT_SECS).until(_has_any_products)
    except TimeoutException:
        pass

    def snapshot_pids():
        seen, out = set(), []
        for a in driver.find_elements(By.XPATH, "//a[contains(@href,'/product')]"):
            try:
                href = _attr(a, "href")
                if "/product" not in href: continue
                pid = href.split("/product")[-1].strip("/").split("?")[0].split("/")[0]
                pid = re.sub(r"[^0-9A-Za-z_-].*$", "", pid)
                # Keep only the leading numeric ID — strip the SEO slug (e.g. "13102489-Towel-Warmer-...")
                pid_num = re.match(r'^(\d+)', pid)
                if pid_num:
                    pid = pid_num.group(1)
                if pid and pid not in seen:
                    seen.add(pid); out.append(pid)
            except StaleElementReferenceException:
                continue
        for box in driver.find_elements(By.XPATH, "//div[contains(@class,'box') and contains(@class,'solid')]"):
            try:
                pid = _attr(box, "data-id") or _attr(box, "id")
                if pid and pid not in seen:
                    seen.add(pid); out.append(pid)
            except StaleElementReferenceException:
                continue
        return out

    total_scrolls = random.randint(SCROLL_MIN, SCROLL_MAX)
    last_count, stagnant = 0, 0
    for _ in range(total_scrolls):
        driver.execute_script("window.scrollBy(0, Math.floor(window.innerHeight*0.9));")
        time.sleep(random.uniform(*SCROLL_PAUSE_RANGE))
        now = len(snapshot_pids())
        if now >= MAX_DISCOVERY: break          # have enough tiles — stop scrolling
        if now <= last_count:
            stagnant += 1
            if stagnant >= 2: break
        else:
            stagnant = 0; last_count = now

    pids = snapshot_pids()
    if not pids:
        driver.get(PROMO_URL); _dismiss_overlays(driver); time.sleep(2)
        pids = snapshot_pids()

    random.shuffle(pids)
    pids = pids[:min(MAX_DISCOVERY, len(pids))]
    log(f"✓ Discovered {len(pids)} product IDs")
    return [(pid, "", "") for pid in pids]

# ════════════════════════════════════════════════════════════════
#  DISCOUNT CODE EXTRACTION
# ════════════════════════════════════════════════════════════════

def try_reveal_code(driver):
    # Only match the INITIAL "Get Code" button — never the post-reveal "Use code on Amazon" button.
    # btn-moved class is added to the button AFTER the code is revealed; exclude it explicitly.
    xps = [
        "//*[@id='PC_239_getCodeInDetail']",
        "//button[not(contains(@class,'btn-moved')) and "
            "(contains(., 'Get Code') or contains(., 'Reveal') or contains(., 'Show Code'))]",
        "//a[contains(., 'Get Code') or contains(., 'Reveal') or contains(., 'Show Code')]",
        "//*[@data-target='#PC_240_jumpToAmzFromCodeZone' and not(contains(@class,'btn-moved'))]",
        # NOTE: PC_241_useCodeOnAmazon intentionally excluded — it navigates away to Amazon
        "//button[contains(@class,'get-coupon-btn') and not(contains(@class,'btn-moved'))]",
    ]
    for xp in xps:
        try:
            el = WebDriverWait(driver, 3).until(EC.element_to_be_clickable((By.XPATH, xp)))
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
            time.sleep(0.5)
            try: driver.execute_script("arguments[0].click();", el)
            except Exception: el.click()
            # Wait for the code to be present in PC_240_jumpToAmzFromCodeZone.
            # Read textContent (not .text): the code can be in the DOM while the zone
            # stays hidden, so .text would block the full 6s for nothing.
            try:
                WebDriverWait(driver, 6).until(
                    lambda d: (d.find_element(By.ID, "PC_240_jumpToAmzFromCodeZone")
                               .get_attribute("textContent") or "").strip()
                )
            except Exception:
                time.sleep(3.0)   # fallback if element not found by ID
            break   # ← stop after first click — never loop into the post-reveal "Use on Amazon" button
        except Exception:
            continue

_ONETIME_SENTINEL = "__ONETIME__"   # returned by extract_code for dashed single-use codes

def extract_code(driver):
    """Return a shareable discount code string on success.
    Returns _ONETIME_SENTINEL when a dashed single-use code is detected (caller must
    exit immediately — no retry, no screenshot).
    Returns "" when no code was found (throttle / empty reveal)."""
    # 1) Authoritative reveal zone. The code is embedded in PC_240's <span> the whole
    #    time; the "Get Code" click only UN-HIDES it (display:flex). Vipon throttles
    #    that VISUAL reveal after ~8/session, but the code TEXT never leaves the DOM.
    #    Selenium's .text returns "" for hidden nodes — which silently dropped every
    #    product after the first 8 and triggered the endless account rotation. Read
    #    textContent so a throttled/blocked visual reveal can't hide the code from us.
    zone = ""
    try:
        WebDriverWait(driver, 5).until(
            lambda d: (d.find_element(By.ID, "PC_240_jumpToAmzFromCodeZone")
                       .get_attribute("textContent") or "").strip()
        )
    except Exception:
        pass
    try:
        _el  = driver.find_element(By.ID, "PC_240_jumpToAmzFromCodeZone")
        zone = (_el.get_attribute("textContent") or _el.text or "").strip().upper()
    except Exception:
        pass
    if zone and is_onetime_code(zone):
        log("  ✗ one-time (dashed) code — skipping"); return _ONETIME_SENTINEL
    if zone:
        m = CODE_RE.search(zone)
        if m and is_plausible_code(m.group(1)): return m.group(1)
        if is_plausible_code(zone): return zone
    # 2) Other explicit code IDs
    for i in ["PC_240_codeInDetail", "coupon_code"]:
        try:
            el  = WebDriverWait(driver, 3).until(EC.presence_of_element_located((By.ID, i)))
            txt = (_text(el) or "").strip().upper()
            if is_onetime_code(txt):
                log("  ✗ one-time (dashed) code — skipping"); return _ONETIME_SENTINEL
            m = CODE_RE.search(txt)
            if m and is_plausible_code(m.group(1)): return m.group(1)
            if is_plausible_code(txt): return txt
        except Exception:
            pass
    # 3) Copy-input field — the FULL value must be a clean single-token code.
    #    (We do NOT regex-search arbitrary text — that's what grabbed "CRAFTS"
    #     out of the category breadcrumb. Only an exact input value qualifies.)
    try:
        for inp in driver.find_elements(By.XPATH, "//input[@type='text' or not(@type)]")[:20]:
            val = (inp.get_attribute("value") or "").strip().upper()
            if is_onetime_code(val):
                log("  ✗ one-time (dashed) code — skipping"); return _ONETIME_SENTINEL
            if is_plausible_code(val):
                return val
    except Exception:
        pass
    # NOTE: the loose class/id="*code*" scan and broad page-text scans were removed —
    # they pulled category words ("CRAFTS"/"ELECTRONICS") and code fragments. No code
    # from the reveal zone/IDs/input = skip (also covers deal-only "Get Deal at
    # Amazon" products, which have no exclusive code).
    return ""

# ════════════════════════════════════════════════════════════════
#  PRODUCT PAGE SCRAPER
# ════════════════════════════════════════════════════════════════

# Skip-reason sentinels returned by scrape_product_page instead of bare None.
# The main loop uses them to decide whether to count against consecutive_fails /
# trigger account rotation / retry.
SKIP_THROTTLE  = "throttle"   # reveal returned nothing — rate-limited or capped account
SKIP_DEAL_ONLY = "deal-only"  # no exclusive code; product is a "Get Deal at Amazon" offer
SKIP_ONETIME   = "onetime"    # dashed single-use code — can't be shared
SKIP_BLOCKED   = "blocked"    # blocked keyword in title
SKIP_NO_IMAGE  = "no-image"   # couldn't resolve product image


def _check_deal_only(driver) -> bool:
    """Return True ONLY when this product genuinely has no exclusive code.

    The ONLY reliable marker is the PRESENCE of PC_239_getCodeInDetail (the GET CODE
    button). If that button exists the product has a code to reveal — return False
    immediately regardless of any other markers. The JS string 'GetDealatAmazon=true'
    lives in a function definition on EVERY Vipon page and must NOT be used as a
    signal (it matches every product and false-flags all of them as deal-only)."""
    try:
        # GET CODE button present → definitely has an exclusive code, not deal-only.
        driver.find_element(By.ID, "PC_239_getCodeInDetail")
        return False
    except Exception:
        pass
    # GET CODE button absent → check for the deal-only plummet button.
    try:
        driver.find_element(By.ID, "plummet-status")
        return True   # only "Get Deal at Amazon", no GET CODE → deal-only
    except Exception:
        pass
    # Neither button found (page still loading?) — let extract_code decide.
    return False


def _check_cap_toast(driver) -> bool:
    """Poll briefly for the '400 codes every 30 days' iView toast that Vipon shows for
    ~1.5s after a capped account clicks GET CODE. Returns True if detected."""
    _CAP_PHRASES = ("400 codes", "only claim", "codes every 30", "limit reached",
                    "you can only")
    deadline = time.time() + 2.0    # toast lasts ~1.5s; poll for 2s to be safe
    while time.time() < deadline:
        try:
            # iView notifications land in .ivu-notice-content or .ivu-message-content
            for sel in (".ivu-notice-content", ".ivu-message-content",
                        "[class*='notice']", "[class*='message']", "[class*='toast']"):
                try:
                    els = driver.find_elements(By.CSS_SELECTOR, sel)
                    for el in els:
                        txt = (el.text or "").lower()
                        if any(p in txt for p in _CAP_PHRASES):
                            log(f"  🚫 account cap toast detected: {el.text.strip()!r}")
                            return True
                except Exception:
                    pass
            # Also check for the phrase anywhere in the visible page text (fast).
            body = (driver.find_element(By.TAG_NAME, "body").text or "").lower()
            if any(p in body for p in _CAP_PHRASES):
                log(f"  🚫 account cap phrase detected in body text")
                return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


def scrape_product_page(driver, wait, pid, tld="com", allow_deal_only=False):
    """Scrape one product page. Returns a data dict on success, or one of the
    SKIP_* sentinel strings to let the caller distinguish *why* it was skipped:

      SKIP_DEAL_ONLY  — no exclusive code (price-drop deal); don't count against throttle.
      SKIP_ONETIME    — single-use dashed code; don't count against throttle.
      SKIP_BLOCKED    — title contains a blocked keyword.
      SKIP_THROTTLE   — reveal returned nothing (rate-limited / account capped).
    """
    url = f"https://www.myvipon.com/product/{pid}"
    log(f"→ PID {pid}: opening product page…")
    try:
        driver.get(url)
    except TimeoutException:
        log("  ⏱️ page load timeout, retry once…"); driver.get(url)
    wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "body")))
    time.sleep(2.0)   # let JS fully render the page before looking for the reveal button

    if "/product/" not in driver.current_url:
        driver.get(url)
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "body")))
        time.sleep(2.0)

    # ── Deal-only check BEFORE clicking GET CODE ──────────────────
    # These products have no exclusive code — clicking GET CODE wastes a reveal quota.
    # For CA (allow_deal_only=True) we still capture the product with code="" instead
    # of skipping, because the Amazon deal link alone has affiliate value.
    _is_deal_only = _check_deal_only(driver)
    account_capped = False
    if _is_deal_only:
        if not allow_deal_only:
            log(f"  ⏭ deal-only product (no exclusive code) — skipping PID {pid}")
            return SKIP_DEAL_ONLY
        log(f"  ↳ deal-only product — capturing with empty code (no quota slot used)")
        code = ""
    else:
        try_reveal_code(driver)

        # Poll immediately for the 400-cap toast (appears for ~1.5s right after click).
        account_capped = _check_cap_toast(driver)

        # Guard: if a reveal click navigated away from vipon (e.g. clicked "Use on Amazon"), come back
        if "myvipon.com/product/" not in driver.current_url:
            log(f"  ↩ navigated away during reveal — returning to product page")
            driver.get(url)
            wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "body")))
            time.sleep(2.0)

        code = extract_code(driver)

        # Dashed one-time code: exit immediately — no retry, no screenshot, no rotation.
        if code == _ONETIME_SENTINEL:
            return SKIP_ONETIME

        # Inner retry — AJAX may still be in flight (not a throttle; just timing).
        if not code or not is_plausible_code(code, strict=False):
            time.sleep(3.0)
            try_reveal_code(driver)
            if "myvipon.com/product/" not in driver.current_url:
                driver.get(url)
                wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "body")))
                time.sleep(2.0)
            code = extract_code(driver)
            # Dashed code on the retry too — exit cleanly.
            if code == _ONETIME_SENTINEL:
                return SKIP_ONETIME

        code = (code or "").strip().upper()

        # ── Classify the skip reason precisely ───────────────────────
        if not is_plausible_code(code, strict=False):
            _capture_code_failure(driver, pid)   # only for genuine throttle/empty reveal
            if account_capped:
                log(f"  🚫 no valid code — account at 400/30-day cap → rotate immediately")
                return SKIP_THROTTLE
            log("  ✗ no valid code — skipping (throttle/empty reveal)")
            return SKIP_THROTTLE

    def _safe_css(c):
        try: return (driver.find_element(By.CSS_SELECTOR, c).text or "").strip()
        except Exception: return ""
    def _safe_xpath(x):
        try: return (driver.find_element(By.XPATH, x).text or "").strip()
        except Exception: return ""

    discount = _safe_css(".product-percent-discount")
    expiry   = _safe_css(".minutes")
    title    = _safe_xpath("//p[contains(@class,'product-title')]//span")
    price    = _safe_css("p.product-price > span")

    # Filter blocked keywords (word-boundary match — see _blocked_keyword_hit)
    bad = _blocked_keyword_hit(title)
    if bad:
        log(f"  ✗ blocked keyword '{bad}' — skipping PID {pid}")
        return SKIP_BLOCKED

    image_url = resolve_cover_image_url(driver)
    log(f"  ↳ cover image: {image_url or 'NONE'}")

    src = driver.page_source or ""
    if tld == "ca":
        # For CA: extract ASIN specifically from Amazon.ca links.
        # If the page has no amazon.ca context at all, it's a US product that Vipon listed
        # in its CA section — using its ASIN for amazon.ca would give "Page Not Found".
        ca_m = re.search(r'amazon\.ca/(?:dp|product)/([A-Z0-9]{10})', src, re.I)
        if ca_m:
            asin = ca_m.group(1).upper()
        elif "amazon.ca" not in src.lower():
            log(f"  ⚠️ PID {pid}: no Amazon.ca link found — US product in CA section, skipping")
            return SKIP_BLOCKED
        else:
            # amazon.ca mentioned but no /dp/ASIN in source — fall back to any ASIN
            m = ASIN_RE.search(src)
            asin = m.group(0).upper() if m else ""
            if asin:
                log(f"  ℹ PID {pid}: using fallback ASIN {asin} (no explicit .ca/dp/ URL found)")
    else:
        m    = ASIN_RE.search(src)
        asin = m.group(0).upper() if m else ""

    images = []
    if asin:
        images = fetch_amazon_images(driver, asin, tld, max_imgs=MAX_AMAZON_IMAGES)
        if not images and image_url:
            images = [image_url]

    if tld == "ca":
        # Canada: single tag for all platforms
        link           = _worker_smartlink(asin, AFFILIATE_ID_CA, tld) if asin else ""
        platform_links = {k: (_worker_smartlink(asin, AFFILIATE_ID_CA, tld) if asin else "")
                          for k in ["reel", "ig", "youtube", "tiktok", "pinterest"]}
    else:
        link           = get_affiliate_link(asin, tld) if asin else ""
        platform_links = (get_platform_links(asin, tld) if asin
                          else {"reel":"","ig":"","youtube":"","tiktok":"","pinterest":""})

    return {
        "pid":            pid,
        "link":           link,
        "link_reel":      platform_links["reel"],
        "link_ig":        platform_links["ig"],
        "link_youtube":   platform_links["youtube"],
        "link_tiktok":    platform_links["tiktok"],
        "link_pinterest": platform_links.get("pinterest", ""),
        "code":           code,
        "discount":       discount,
        "expiry":         expiry,
        "title":          title,
        "price":          price,
        "image":          image_url,
        "images":         images,
        "deal_only":      _is_deal_only,
    }

# ════════════════════════════════════════════════════════════════
#  FFMPEG SEGMENT BUILDERS  (720×1280, ultrafast, no yellow lines)
# ════════════════════════════════════════════════════════════════

# Common FFmpeg flags for all encoding steps
_FF_ENCODE = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "28", "-threads", "1"]
_FF_LOG    = ["-loglevel", "error", "-hide_banner"]


def _ffmpeg_build_segment_from_image(img_path: str, out_seg: str, dur_sec: int = 5,
                                     overlay_big: str = "", overlay_small: str = ""):
    ffmpeg_bin = _which_ffmpeg()
    if not ffmpeg_bin: raise RuntimeError("ffmpeg not found")
    fontfile = _find_fontfile()
    if not fontfile: raise RuntimeError("No usable font file found")

    # y positions (proportional, same visual placement at 720×1280)
    y_big   = "h*0.74"
    y_small = "h*0.81"

    vf_parts = [
        f"scale={VIDEO_W}:{VIDEO_H}:force_original_aspect_ratio=decrease",
        f"pad={VIDEO_W}:{VIDEO_H}:(ow-iw)/2:(oh-ih)/2",
        "setsar=1",
    ]

    def _drawline(txtfile, y_expr, fontsize):
        return (f"drawtext=fontfile='{fontfile}':expansion=none:textfile='{txtfile}':"
                f"x=(w-text_w)/2:y={y_expr}:fontsize={fontsize}:fontcolor=white:"
                f"box=1:boxcolor=black@0.55:boxborderw=10:shadowcolor=black@0.7:shadowx=2:shadowy=2:"
                f"enable='1'")

    with tempfile.TemporaryDirectory(prefix="vipon_imgtxt_") as td:
        pct_file  = _write_textfile(td, "pct.txt",  (overlay_big   or "").strip())
        code_file = _write_textfile(td, "code.txt", (overlay_small or "").strip())

        filters = list(vf_parts)
        filters.append(_drawline(pct_file,  y_big,   56))   # 84 × 0.667 → 56
        filters.append(_drawline(code_file, y_small, 38))   # 56 × 0.667 → 38

        vf_full = ",".join(filters)
        cmd = (
            [ffmpeg_bin, "-y"] + _FF_LOG +
            ["-loop", "1", "-t", str(dur_sec), "-i", img_path,
             "-vf", vf_full, "-r", "30", "-pix_fmt", "yuv420p"]
            + _FF_ENCODE + ["-an", out_seg]
        )
        subprocess.run(cmd, check=True)


def _ffmpeg_build_black_text_segment(out_seg: str, discount_txt: str, code_txt: str, dur_sec: int = 3):
    ffmpeg_bin = _which_ffmpeg()
    if not ffmpeg_bin: raise RuntimeError("ffmpeg not found")
    fontfile = _find_fontfile()
    if not fontfile: raise RuntimeError("No usable font file found")

    with tempfile.TemporaryDirectory(prefix="vipon_black_") as td:
        pct_file  = _write_textfile(td, "pct.txt",  (discount_txt or "").strip())
        code_file = _write_textfile(td, "code.txt",
                                    (f"Code: {code_txt}" if code_txt else "").strip())

        draw1 = (f"drawtext=fontfile='{fontfile}':expansion=none:textfile='{pct_file}':"
                 f"x=(w-text_w)/2:y=(h/2)-80:fontsize=56:fontcolor=white:"
                 f"box=1:boxcolor=black@0.0:boxborderw=10:shadowcolor=black@0.7:shadowx=2:shadowy=2")
        draw2 = (f"drawtext=fontfile='{fontfile}':expansion=none:textfile='{code_file}':"
                 f"x=(w-text_w)/2:y=(h/2)+14:fontsize=48:fontcolor=white:"
                 f"box=1:boxcolor=black@0.0:boxborderw=10:shadowcolor=black@0.7:shadowx=2:shadowy=2")

        vf  = f"scale={VIDEO_W}:{VIDEO_H},setsar=1,{draw1},{draw2}"
        cmd = (
            [ffmpeg_bin, "-y"] + _FF_LOG +
            ["-f", "lavfi", "-i", f"color=c=black:s={VIDEO_W}x{VIDEO_H}:d={dur_sec}",
             "-vf", vf, "-r", "30", "-pix_fmt", "yuv420p"]
            + _FF_ENCODE + ["-an", out_seg]
        )
        subprocess.run(cmd, check=True)


def _ffmpeg_build_logo_segment(logo_path: str, out_seg: str, dur_sec: int = 2):
    ffmpeg_bin = _which_ffmpeg()
    if not ffmpeg_bin: raise RuntimeError("ffmpeg not found")
    if not os.path.exists(logo_path):
        raise FileNotFoundError(f"Logo not found at {logo_path}")
    vf  = (f"scale=540:-1:force_original_aspect_ratio=decrease,"
           f"pad={VIDEO_W}:{VIDEO_H}:({VIDEO_W}-iw)/2:({VIDEO_H}-ih)/2,setsar=1")
    cmd = (
        [ffmpeg_bin, "-y"] + _FF_LOG +
        ["-loop", "1", "-t", str(dur_sec), "-i", logo_path,
         "-vf", vf, "-r", "30", "-pix_fmt", "yuv420p"]
        + _FF_ENCODE + ["-an", out_seg]
    )
    subprocess.run(cmd, check=True)

# ════════════════════════════════════════════════════════════════
#  REEL BUILDER
# ════════════════════════════════════════════════════════════════

def _pick_music(path=MUSIC_DIR):
    if not os.path.isdir(path): return ""
    cand = glob.glob(os.path.join(path, "*.mp3")) + glob.glob(os.path.join(path, "*.wav"))
    random.shuffle(cand); return cand[0] if cand else ""


def make_and_upload_reel_from_images(pid: str, image_urls: list,
                                     discount_txt: str, code_txt: str,
                                     title_txt: str, price_txt: str,
                                     expiry_txt: str = "") -> str:
    norm_disc = _normalize_discount(discount_txt)

    if not image_urls:
        log(f"  ⚠️ no images for PID {pid} — skipping reel")
        return ""

    ffmpeg_bin = _which_ffmpeg()
    if not ffmpeg_bin:
        log("  ⚠️ ffmpeg not found — skipping reel")
        return ""

    # Deduplicate and cap
    dedup, seen = [], set()
    for u in image_urls:
        if u and u not in seen:
            dedup.append(u); seen.add(u)
        if len(dedup) >= MAX_AMAZON_IMAGES:
            break
    image_urls = dedup

    with tempfile.TemporaryDirectory(prefix="vipon_reel_") as td:
        local_imgs, segs = [], []

        # 1) Download & standardise images
        for idx, u in enumerate(image_urls, start=1):
            img_path = os.path.join(td, f"img{idx}.jpg")
            try:
                resp = requests.get(u, timeout=60, stream=True, headers={"User-Agent": UA})
                resp.raise_for_status()
                with open(img_path, "wb") as w:
                    for chunk in resp.iter_content(1 << 15):
                        if chunk: w.write(chunk)
            except Exception as e:
                log(f"  ⚠️ could not download image {idx}: {e}")
                continue

            fixed_path = os.path.join(td, f"img{idx}_fixed.png")
            if standardize_image_for_video(img_path, fixed_path):
                local_imgs.append(fixed_path)
            else:
                log(f"  ⚠️ using raw image (standardization failed): {img_path}")
                local_imgs.append(img_path)

        if not local_imgs:
            log(f"  ⚠️ no usable images for PID {pid} — skipping reel")
            return ""

        # 2) Build one segment per image
        for i, ip in enumerate(local_imgs, start=1):
            seg = os.path.join(td, f"seg_img_{i}.mp4")
            try:
                _ffmpeg_build_segment_from_image(
                    ip, seg,
                    dur_sec=IMG_SEG_DURATION_SEC,
                    overlay_big=norm_disc or "",
                    overlay_small=f"Code: {code_txt}" if code_txt else "",
                )
                segs.append(seg)
            except Exception as e:
                log(f"  ⚠️ segment {i} failed: {e}")

        if not segs:
            log(f"  ⚠️ no segments built for PID {pid}")
            return ""

        # 3) Optional logo screen
        if os.path.exists(LOGO_PATH):
            logo_seg = os.path.join(td, "seg_logo.mp4")
            try:
                _ffmpeg_build_logo_segment(LOGO_PATH, logo_seg, dur_sec=LOGO_SEG_DURATION_SEC)
                segs.append(logo_seg)
            except Exception as e:
                log(f"  ⚠️ logo segment failed: {e}")
        else:
            log(f"  ℹ️ No logo at {LOGO_PATH}; skipping logo segment")

        # 4) Concat all segments
        listfile    = os.path.join(td, "list.txt")
        concat_path = os.path.join(td, "concat.mp4")
        with open(listfile, "w", encoding="utf-8") as f:
            for s in segs:
                f.write(f"file '{Path(s).as_posix()}'\n")

        subprocess.run(
            [ffmpeg_bin, "-y"] + _FF_LOG +
            ["-f", "concat", "-safe", "0", "-i", listfile, "-c", "copy", concat_path],
            check=True
        )

        # 5) Voiceover (edge-tts free → OpenAI fallback)
        end_date_txt = expiry_to_date_text(expiry_txt)
        if norm_disc and end_date_txt:
            vo_line = (f"{title_txt}. {norm_disc}. "
                       f"This discount ends on {end_date_txt}. "
                       f"Price {price_txt}. Product link in description")
        elif norm_disc:
            vo_line = f"{title_txt}. {norm_disc}. Limited time. Price {price_txt}. Product link in description"
        else:
            vo_line = f"{title_txt}. Limited time offer. Price {price_txt}"

        vo_line    = _sanitize_for_tts(vo_line)
        voice_file = os.path.join(td, "voice.mp3")
        have_vo    = _tts_to_mp3(vo_line, voice_file)   # ← single call (bug fixed)

        # 6) Mix audio
        out_path = os.path.join(td, f"{pid}.mp4")
        if have_vo:
            cmd = (
                [ffmpeg_bin, "-y"] + _FF_LOG +
                ["-i", concat_path, "-i", voice_file,
                 "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-shortest", out_path]
            )
        else:
            vid_dur = _probe_duration(concat_path) or 10.0
            cmd = (
                [ffmpeg_bin, "-y"] + _FF_LOG +
                ["-i", concat_path,
                 "-f", "lavfi", "-t", f"{vid_dur:.2f}",
                 "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
                 "-c:v", "copy", "-c:a", "aac", "-b:a", "96k", "-shortest", out_path]
            )

        subprocess.run(cmd, check=True)

        # 7) Upload to Cloudinary
        public_id = f"{CLOUDINARY_VIDEO_FOLDER}/{pid}"
        vurl = _cloudinary_upload_video(out_path, public_id)
        if vurl:
            log(f"  ✓ reel URL: {vurl}")
        else:
            log("  ⚠️ Cloudinary returned no URL")
        return vurl

# ════════════════════════════════════════════════════════════════
#  SELLER FORM INTAKE — Phase 3 (runs after main scrape + videos)
#  Reads "Form Responses 2", processes unhandled rows, appends to
#  Sheet1 (same format as scraped products), marks status in the
#  form tab directly — no intermediate Seller tab needed.
# ════════════════════════════════════════════════════════════════

SELLER_FORM_TAB        = "Form Responses 2"
SELLER_STATUS_HEADER   = "Status"


def _open_form_tab():
    """Open Form Responses 2 in the same Google Sheet."""
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDS_FILE, scope)
    return gspread.authorize(creds).open(GOOGLE_SHEET_NAME).worksheet(SELLER_FORM_TAB)


def _ensure_status_col(form_ws) -> int:
    """Ensure a 'Status' column exists. Returns its 0-based index."""
    header = form_ws.row_values(1)
    for i, h in enumerate(header):
        if h.strip().lower() == "status":
            return i
    new_col = len(header) + 1
    form_ws.update_cell(1, new_col, SELLER_STATUS_HEADER)
    return new_col - 1   # 0-based


def _find_col(norm_header: list, *names) -> int | None:
    """Return 0-based index of first header matching any of the given names."""
    for name in names:
        key = re.sub(r"[^a-z0-9]+", "", name.lower())
        for i, h in enumerate(norm_header):
            hn = re.sub(r"[^a-z0-9]+", "", h)
            if key == hn or key in hn:
                return i
    return None


def _fetch_amazon_title_simple(asin: str, tld: str = "com") -> str:
    """Best-effort title from Amazon HTML (no Selenium).
    Tries the given TLD first; falls back to .com if needed.
    """
    import html as _html
    tlds = [tld] if tld == "com" else [tld, "com"]
    for t in tlds:
        try:
            r = requests.get(
                f"https://www.amazon.{t}/dp/{asin}?th=1&psc=1",
                headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.8"},
                timeout=20,
            )
            if r.ok:
                m = re.search(r'id="productTitle"[^>]*>\s*([^<]+)\s*<', r.text, re.I)
                if m:
                    return _html.unescape(m.group(1)).strip()
        except Exception:
            pass
    return ""


def process_seller_forms(ws_main) -> None:
    """Process new Google Form submissions and append rows to Sheet1."""
    log("\n═══ Phase 3: Seller Form Intake ═══")
    try:
        form_ws = _open_form_tab()
    except Exception as e:
        log(f"⚠️ Could not open '{SELLER_FORM_TAB}' — skipping seller forms: {e}")
        return

    all_values = form_ws.get_all_values()
    if not all_values or len(all_values) < 2:
        log("ℹ️ No form submissions found.")
        return

    header      = all_values[0]
    norm_header = [re.sub(r"[^a-z0-9]+", "", h.lower()) for h in header]

    ts_col     = _find_col(norm_header, "timestamp")
    asin_col   = _find_col(norm_header, "asin", "amazon link", "product link")
    code_col   = _find_col(norm_header, "discount code")
    disc_col   = _find_col(norm_header, "discount %", "discount percent", "discountpercent")
    expiry_col = _find_col(norm_header, "expiry", "expiration", "expirey")
    price_col  = _find_col(norm_header, "final price", "priceafterdiscount", "price")

    if ts_col is None or asin_col is None:
        log("⚠️ Form tab missing Timestamp or ASIN column — skipping.")
        return

    status_col = _ensure_status_col(form_ws)
    # Re-read after possible header update
    all_values = form_ws.get_all_values()

    processed = skipped = 0

    for row_idx, row in enumerate(all_values[1:], start=2):   # 1-based, row 1 = header
        max_needed = max(i for i in [asin_col, code_col, disc_col, expiry_col, price_col, status_col] if i is not None)
        while len(row) <= max_needed:
            row.append("")

        if row[status_col].strip().lower() in ("done", "blocked"):
            continue   # permanent — skip. Transient errors (no images, video failed, no asin) are retried.

        # ── Extract fields ────────────────────────────────────────
        asin_raw = row[asin_col] if asin_col is not None else ""
        m = ASIN_RE.search(asin_raw)
        if not m:
            m = re.search(r"\b([A-Z0-9]{10})\b", asin_raw, re.I)
        asin = m.group(0).upper() if m else ""

        if not asin:
            log(f"  ✗ Form row {row_idx}: no ASIN — skipping")
            form_ws.update_cell(row_idx, status_col + 1, "No ASIN")
            skipped += 1
            continue

        code = (row[code_col].strip().upper() if code_col is not None else "")
        if not is_plausible_code(code, strict=False):
            log(f"  ✗ Form row {row_idx}: invalid code '{code}' for {asin} — skipping")
            form_ws.update_cell(row_idx, status_col + 1, "Invalid Code")
            skipped += 1
            continue

        pct_raw  = (row[disc_col].strip()  if disc_col   is not None else "")
        expiry   = (row[expiry_col].strip() if expiry_col is not None else "")
        price    = (row[price_col].strip()  if price_col  is not None else "")

        m_pct     = re.search(r"(\d{1,3})", pct_raw)
        disc_txt  = f"{min(int(m_pct.group(1)), 95)}%" if m_pct else pct_raw
        disc_norm = _normalize_discount(disc_txt or pct_raw or "")

        # ── Title + blocked-keyword check ─────────────────────────
        log(f"  → Form row {row_idx}: ASIN {asin}…")
        title = _fetch_amazon_title_simple(asin) or f"Amazon Product {asin}"
        bad_hit = _blocked_keyword_hit(title)
        if bad_hit:
            log(f"  ✗ Form row {row_idx}: blocked by '{bad_hit}'")
            form_ws.update_cell(row_idx, status_col + 1, "Blocked")
            skipped += 1
            continue

        # ── Images ────────────────────────────────────────────────
        images = fetch_amazon_images(None, asin, "com", max_imgs=10)
        if not images:
            log(f"  ✗ Form row {row_idx}: no images for {asin}")
            form_ws.update_cell(row_idx, status_col + 1, "No Images")
            skipped += 1
            continue

        # Build-on-publish: no video here; the publisher builds it at post time.
        t_short  = shorten_title(title, MAX_TITLE_LEN)
        reel_url = ""

        # ── Links + post text ─────────────────────────────────────
        aff_link       = get_affiliate_link(asin, "com")
        platform_links = get_platform_links(asin, "com")
        post_text      = generate_social_post(aff_link, code, disc_txt, expiry, t_short, price)

        # ── Score (rewarded) + append to Sheet1 (same column order as scraped) ──
        # Real selection score + seller bonus so the publisher ranks/produces these
        # ahead of the regular batch. Col S must be populated or the publisher (which
        # ranks by Col S) treats the row as score 0 and never picks it.
        _sp = fetch_social_proof(asin, "com")
        _score = round(selection_score(_sp.get("units", 0), _sp.get("stars", 0.0),
                                       _sp.get("ratings", 0), price, disc_txt)
                       + SELLER_SCORE_BONUS, 1)
        ws_main.append_rows([[
            aff_link,
            platform_links.get("reel", ""),
            platform_links.get("ig", ""),
            platform_links.get("youtube", ""),
            platform_links.get("tiktok", ""),
            code,
            disc_txt,
            expiry,
            t_short,
            price,
            asin,
            images[0],
            images[0],
            reel_url,
            post_text,
            "", "", "", _score,
        ]], value_input_option="USER_ENTERED", table_range="A1")

        # ── Mark done in form tab ─────────────────────────────────
        form_ws.update_cell(row_idx, status_col + 1, "Done")
        processed += 1
        log(f"  ✓ Seller {asin} → Sheet1 row added (score {_score}, seller-boosted)")
        time.sleep(0.3)

    log(f"✅ Seller forms done — {processed} added to Sheet1, {skipped} skipped")


def process_seller_forms_ca(ws2_main) -> None:
    """Phase 4 — Canada seller form intake from 'Response Form 3' -> Sheet2."""
    log("\n═══ Phase 4: Canada Seller Form Intake ═══")
    try:
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDS_FILE, scope)
        form_ws = gspread.authorize(creds).open(GOOGLE_SHEET_NAME).worksheet(SELLER_FORM_TAB_CA)
    except Exception as e:
        log(f"⚠️ Could not open '{SELLER_FORM_TAB_CA}' — skipping CA seller forms: {e}")
        return

    # Reuse the same helper logic as process_seller_forms but writing to Sheet2
    # and using Canada TLD + affiliate tag
    form_ws2 = form_ws
    status_col = _ensure_status_col(form_ws2)
    rows = form_ws2.get_all_values()
    if len(rows) < 2:
        log("No CA seller form submissions found.")
        return

    # Detect all relevant columns dynamically (form columns differ from US form).
    # Pre-normalise header to lowercase so _find_col matches regardless of casing
    # (e.g. "ASIN" header matches search term "asin").
    header_raw  = rows[0]
    norm_hdr_ca = [re.sub(r"[^a-z0-9]+", "", h.lower()) for h in header_raw]

    log(f"  CA form headers (normalised): {norm_hdr_ca}")
    asin_col_ca   = _find_col(norm_hdr_ca, "asin", "amazon asin", "product asin", "amazon link", "product link")
    code_col_ca   = _find_col(norm_hdr_ca, "discount code", "promo code", "coupon", "code")
    # Disc% column: look for "%" in raw header but NOT "code" — avoids matching "Discount Code"
    disc_col_ca   = next(
        (i for i, h in enumerate(header_raw) if "%" in h and "code" not in h.lower()),
        _find_col(norm_hdr_ca, "discount percent", "discountpercent"),
    )
    expiry_col_ca = _find_col(norm_hdr_ca, "expiry", "expiration", "expirey")
    price_col_ca  = _find_col(norm_hdr_ca, "final price", "priceafterdiscount", "price")
    log(f"  CA cols — asin:{asin_col_ca} code:{code_col_ca} disc:{disc_col_ca} expiry:{expiry_col_ca} price:{price_col_ca}")

    if asin_col_ca is None:
        log("⚠️ CA form: cannot find ASIN column — skipping CA seller forms.")
        return

    _NO_CODE_PHRASES = {
        "NO CODE NEEDED", "NO CODE", "NONE", "N/A", "NA",
        "NO COUPON", "NO COUPON NEEDED", "NOT REQUIRED", "NOT APPLICABLE",
    }

    processed = skipped = 0
    for row_idx, row in enumerate(rows[1:], start=2):
        status_val = row[status_col].strip().lower() if len(row) > status_col else ""
        if status_val in ("done", "blocked"):
            continue  # permanent — skip. Transient errors (no images, video failed, no asin) are retried.

        # Pad row so column accesses are safe
        max_col = max(c for c in [asin_col_ca, code_col_ca, disc_col_ca, expiry_col_ca, price_col_ca, status_col] if c is not None)
        while len(row) <= max_col:
            row.append("")

        asin = row[asin_col_ca].strip().upper() if asin_col_ca is not None else ""
        # Extract ASIN from a URL if seller pasted a link instead of just the ASIN
        if asin and not re.match(r"^[A-Z0-9]{10}$", asin):
            m = ASIN_RE.search(asin) or re.search(r"\b([A-Z0-9]{10})\b", asin, re.I)
            asin = m.group(0).upper() if m else ""

        code_raw = row[code_col_ca].strip().upper() if code_col_ca is not None else ""
        code = "" if code_raw in _NO_CODE_PHRASES else code_raw

        pct_raw  = row[disc_col_ca].strip()  if disc_col_ca   is not None else ""
        expiry   = row[expiry_col_ca].strip() if expiry_col_ca is not None else ""
        price    = row[price_col_ca].strip()  if price_col_ca  is not None else ""

        # Normalise discount: "30% off" → "30%"
        m_pct    = re.search(r"(\d{1,3})", pct_raw)
        disc_txt = f"{min(int(m_pct.group(1)), 95)}%" if m_pct else pct_raw

        if not asin:
            form_ws2.update_cell(row_idx, status_col + 1, "No ASIN")
            skipped += 1
            continue
        # No "Invalid Code" gate — allow empty code (seller may have no discount code)

        images = fetch_amazon_images(None, asin, AMAZON_TLD_CA, max_imgs=10)
        if not images:
            form_ws2.update_cell(row_idx, status_col + 1, "No Images")
            skipped += 1
            continue

        title = _fetch_amazon_title_simple(asin, tld=AMAZON_TLD_CA)
        t_short = shorten_title(title, MAX_TITLE_LEN)
        aff_link = _worker_smartlink(asin, AFFILIATE_ID_CA, AMAZON_TLD_CA)
        platform_links = {k: aff_link for k in ["reel","ig","youtube","tiktok","pinterest"]}

        post_text = generate_social_post(aff_link, code, disc_txt, expiry, t_short, price)
        reel_url = ""   # build-on-publish

        _sp = fetch_social_proof(asin, AMAZON_TLD_CA)
        _score = round(selection_score(_sp.get("units", 0), _sp.get("stars", 0.0),
                                       _sp.get("ratings", 0), price, disc_txt)
                       + SELLER_SCORE_BONUS, 1)
        ws2_main.append_rows([[
            aff_link, platform_links.get("reel",""), platform_links.get("ig",""),
            platform_links.get("youtube",""), platform_links.get("tiktok",""),
            code, disc_txt, expiry, t_short, price, asin,
            images[0], images[0], reel_url, post_text,
            "", "", "", _score,
        ]], value_input_option="USER_ENTERED", table_range="A1")

        form_ws2.update_cell(row_idx, status_col + 1, "Done")
        processed += 1
        log(f"  ✓ CA Seller {asin} -> Sheet2 row added (score {_score}, seller-boosted)")
        time.sleep(0.3)

    log(f"✅ CA seller forms done — {processed} added to Sheet2, {skipped} skipped")


# ════════════════════════════════════════════════════════════════
#  PER-PRODUCT SHEET WRITE  (write-as-you-scrape)
# ════════════════════════════════════════════════════════════════
# Each scraped product is written to the sheet immediately, not batched at the end.
# This makes the batch durable (a crash/timeout/throttle can't erase what we got) and
# available to the publisher/FB jobs the moment it lands. We're paced anyway, so one
# write every several seconds stays well under the Sheets write quota.

def _append_row_retry(ws, row, retries: int = 4) -> bool:
    """Append one row anchored at column A, retrying on transient/quota errors."""
    delay = 2.0
    for attempt in range(retries):
        try:
            ws.append_rows([row], value_input_option="USER_ENTERED", table_range="A1")
            return True
        except Exception as e:
            if attempt < retries - 1:
                log(f"  ⚠️ Sheet append retry {attempt+1}/{retries}: {e.__class__.__name__}")
                time.sleep(delay); delay *= 1.8
            else:
                log(f"  ✗ Sheet append failed after {retries} tries: {e.__class__.__name__}")
    return False


def _write_product_row(ws, data, tld="com", ws_p=None) -> float:
    """Build and immediately write one scraped product's row. Computes the VO post
    (Col O) and social/selection score (Col S) inline so the row is fully usable by
    the publisher/FB jobs. Returns the score. tld picks US vs CA social proof."""
    pid     = data["pid"]
    t_short = shorten_title(data["title"], MAX_TITLE_LEN)
    reel_url = ""   # build-on-publish: the video is built by the publisher at post time

    _m_asin = re.search(r"asin=([A-Za-z0-9]{10})", data["link"], re.I)
    _sp = fetch_social_proof(_m_asin.group(1).upper(), tld) if _m_asin else {}
    _score = selection_score(_sp.get("units", 0), _sp.get("stars", 0.0),
                             _sp.get("ratings", 0), data["price"], data["discount"])
    log(f"  score: units={_sp.get('units',0)} stars={_sp.get('stars',0)} "
        f"price={data['price']} disc={data['discount']} → {_score}")

    post_text = generate_social_post(data["link"], data["code"], data["discount"],
                                     data["expiry"], t_short, data["price"])
    row = [
        data["link"], data["link_reel"], data["link_ig"], data["link_youtube"],
        data["link_tiktok"], data["code"], data["discount"], data["expiry"],
        t_short, data["price"], pid, data["image"], data["image"], reel_url,
        post_text, "", "", "", _score,
    ]
    if _append_row_retry(ws, row):
        log(f"✓ Row written for PID {pid} (score {_score})")
    if ENABLE_PINTEREST and ws_p is not None:
        try:
            ws_p.append_row([
                t_short, reel_url, PINTEREST_BOARD_DEFAULT, PINTEREST_THUMBNAIL_DEFAULT,
                generate_pinterest_description(t_short, data["discount"], data["code"],
                                               data["expiry"], data["price"]),
                data.get("link_pinterest", data["link"]), "", PINTEREST_KEYWORDS_DEFAULT,
            ], value_input_option="USER_ENTERED")
        except Exception as e:
            log(f"  ⚠️ Pinterest row write failed for PID {pid}: {e.__class__.__name__}")
    time.sleep(0.3)
    return _score


def _sheet_topup_state(ws):
    """For top-up: return (existing_data_row_count, set_of_existing_PIDs/ASINs in
    Col K). Lets a rerun fill each sheet UP TO PRODUCT_LIMIT instead of clearing and
    re-scraping (which re-burns the monthly code quota on products already banked)."""
    try:
        vals = ws.get_all_values()
    except Exception:
        return 0, set()
    pids, n = set(), 0
    for r in (vals[1:] if vals else []):          # skip header
        if not any((c or "").strip() for c in r): # skip blank rows
            continue
        n += 1
        if len(r) >= 11 and (r[10] or "").strip(): # Col K (11) = PID/ASIN
            pids.add((r[10] or "").strip())
    return n, pids


# ════════════════════════════════════════════════════════════════
#  MAIN  — Phase 1: scrape US (Chrome alive), writing each row as it lands
#          Phase 1b: scrape Canada (same Chrome session, switched to CA)
#          Phase 3: US seller form intake (Form Responses 2 -> Sheet1)
#          Phase 4: CA seller form intake (Response Form 3 -> Sheet2)
# ════════════════════════════════════════════════════════════════

def main():
    random.seed(time.time())

    ws     = open_sheet_and_reset()
    ws2    = open_sheet2_and_reset()
    ws_p   = open_pinterest_sheet_and_reset() if ENABLE_PINTEREST else None

    global _account_index
    _account_index = _read_account_state(ws.spreadsheet)
    log(f"══════════════════════════════════════════════════════════════")
    log(f"  STARTING ACCOUNT: {_account_index + 1}/{len(VIPON_ACCOUNTS)} — {_current_account()['username']}")
    log(f"══════════════════════════════════════════════════════════════")

    # Cross-day PID dedup: load PIDs scraped in the last 2 days so repeats are skipped.
    us_hist_pids, ca_hist_pids = _read_pid_history(ws.spreadsheet)

    # Top-up: fill each sheet UP TO PRODUCT_LIMIT rather than always scraping a fresh
    # full batch. After a short run, a rerun completes the day to 24/sheet without
    # re-scraping (and re-burning quota on) products already banked. Existing PIDs are
    # skipped so we never duplicate.
    us_existing, us_pids = _sheet_topup_state(ws)
    ca_existing, ca_pids = _sheet_topup_state(ws2)
    us_pids |= us_hist_pids   # exclude last-2-days PIDs from this run's tile scan
    ca_pids |= ca_hist_pids
    us_target = max(0, PRODUCT_LIMIT - us_existing)
    ca_target = max(0, PRODUCT_LIMIT - ca_existing)
    need_scrape = (us_target > 0 or ca_target > 0)
    log(f"▶ Top-up — US: have {us_existing}, need {us_target} more; "
        f"CA: have {ca_existing}, need {ca_target} more (limit {PRODUCT_LIMIT}).")

    driver = create_driver()
    wait   = WebDriverWait(driver, WAIT_SECS)

    # ── PHASE 1: Scrape US ───────────────────────────────────────
    scraped    = []
    scraped_ca = []
    scrape_start = time.time()                          # for the time-budget early stop
    _budget_sec  = EARLY_STOP_AFTER_MIN * 60
    def _time_up():
        return (time.time() - scrape_start) >= _budget_sec
    try:
        if not need_scrape:
            log("▶ Both sheets already at the limit — skipping scrape, running seller intake only.")
        # Try every account until one logs in successfully (only if we need to scrape)
        log(f"📅 Daily account rotation: starting with account "
            f"{_account_index + 1}/{len(VIPON_ACCOUNTS)} ({_current_account()['username']})")
        logged_in = False
        if need_scrape:
            for _attempt in range(len(VIPON_ACCOUNTS)):
                try:
                    login(driver, wait)
                    logged_in = True
                    break
                except Exception as e:
                    log(f"  ⚠️ Login failed for {_current_account()['username']}: {e.__class__.__name__}")
                    if _attempt < len(VIPON_ACCOUNTS) - 1:
                        logout(driver)              # clear session before trying next account
                        driver.delete_all_cookies()
                        _rotate_account()
            if not logged_in:
                log("✗ All accounts failed to login — exiting")
                raise RuntimeError("All Vipon accounts failed to login")

        tiles = collect_promo_tiles_random(driver, wait) if (need_scrape and us_target > 0) else []
        count = 0
        consecutive_fails = 0          # only throttle results count (not deal-only/onetime)
        rotations_no_success = 0       # account cycles since last successful code
        account_scraped   = 0          # successful codes from current account this run
        ROTATION_THRESHOLD = 3         # rotate after this many consecutive throttle fails
        # Proactive: spread codes evenly — rotate before hitting the 60/day cap
        PROACTIVE_ROTATE_AFTER = max(1, (us_target + ca_target + len(VIPON_ACCOUNTS) - 1) // len(VIPON_ACCOUNTS))
        log(f"  ↺ Proactive rotation every {PROACTIVE_ROTATE_AFTER} code(s) per account "
            f"(target {us_target + ca_target} across {len(VIPON_ACCOUNTS)} accounts)")

        def _do_rotate():
            nonlocal rotations_no_success, account_scraped
            _rotate_account()
            rotations_no_success += 1
            account_scraped = 0
            try:
                logout(driver)
                driver.delete_all_cookies()
                login(driver, wait)
                log(f"  ✓ Account rotated successfully")
            except Exception as _e:
                log(f"  ⚠️ Re-login after rotation failed: {_e.__class__.__name__} — continuing")

        for pid, _, _ in tiles:
            if count >= us_target:
                break
            if str(pid).strip() in us_pids:
                continue                 # already on the sheet — don't re-scrape
            # Fixed pace between reveals (rate-limit protection).
            time.sleep(REVEAL_PACE_SEC)
            try:
                data = scrape_product_page(driver, wait, pid)
            except TimeoutException:
                log(f"  ⏱️ hard timeout on PID {pid} — skip")
                data = SKIP_THROTTLE
            except WebDriverException as e:
                log(f"  ⚠️ webdriver error on PID {pid}: {e} — skip")
                data = SKIP_THROTTLE

            # ── Classified skip handling ──────────────────────────────
            if data in (SKIP_DEAL_ONLY, SKIP_BLOCKED, SKIP_ONETIME):
                # These are legit non-code products — don't penalise the account.
                continue

            if data == SKIP_THROTTLE:
                # Outer retry: wait another REVEAL_PACE_SEC then try the same PID once more.
                # A transient throttle often clears after a short pause; a genuine account
                # cap will fail again and we'll rotate on the second failure.
                log(f"  🔄 throttle on PID {pid} — waiting {REVEAL_PACE_SEC}s then retrying once…")
                time.sleep(REVEAL_PACE_SEC)
                try:
                    data = scrape_product_page(driver, wait, pid)
                except Exception:
                    data = SKIP_THROTTLE

                if data == SKIP_THROTTLE or data is None:
                    consecutive_fails += 1
                    # Rotate immediately if cap toast was detected OR after ROTATION_THRESHOLD
                    # real throttle failures.
                    if consecutive_fails >= ROTATION_THRESHOLD and len(VIPON_ACCOUNTS) > 1:
                        _do_rotate()
                        consecutive_fails = 0
                        if rotations_no_success >= len(VIPON_ACCOUNTS):
                            log(f"  ⛔ Cycled all {len(VIPON_ACCOUNTS)} account(s) — "
                                f"stopping US scrape at {count} product(s).")
                            break
                    continue
                if data in (SKIP_DEAL_ONLY, SKIP_BLOCKED, SKIP_ONETIME):
                    continue   # retry resolved to a legit skip — don't penalise

            if not isinstance(data, dict):
                continue   # unknown sentinel — skip

            consecutive_fails = 0
            rotations_no_success = 0
            account_scraped += 1
            scraped.append(data)
            count += 1
            log(f"✓ Scraped {count}/{us_target} (sheet total {us_existing+count}/{PRODUCT_LIMIT}): {data['title'][:60]}")
            if account_scraped >= PROACTIVE_ROTATE_AFTER and len(VIPON_ACCOUNTS) > 1 and count < us_target:
                log(f"  📊 Proactive rotation after {account_scraped} code(s) — spreading load evenly")
                _do_rotate()
            try:
                _write_product_row(ws, data, tld="com", ws_p=ws_p)
            except Exception as e:
                log(f"  ⚠️ inline sheet write failed for PID {data['pid']}: {e.__class__.__name__}")

        # ── PHASE 1b: Switch to Canada and scrape CA deals ───────
        log("\n═══ Phase 1b: Canada Scrape ═══")
        if ca_target <= 0:
            log(f"  ✓ Sheet2 already has {ca_existing} (≥ {PRODUCT_LIMIT}) — skipping CA scrape.")
            ca_tiles = []
        else:
            ca_switched = switch_to_canada(driver)
            if not ca_switched:
                log("⚠️  Canada switch failed — skipping CA scrape to avoid writing US products to Sheet2")
            if ca_switched and len(VIPON_ACCOUNTS) > 1:
                # Always start CA on a fresh account so it doesn't inherit an account that
                # already burned through its daily quota during the US phase.
                log(f"  ↺ CA: rotating to fresh account (was {_current_account()['username']})…")
                _do_rotate()
                account_scraped = 0
            ca_tiles = collect_promo_tiles_random(driver, wait, start_url=PROMO_URL_CA) if ca_switched else []
        ca_count = 0
        ca_consecutive_fails = 0     # throttle fails since last CA rotation
        ca_rotations_no_success = 0  # how many CA accounts have been tried with 0 codes
        for pid, _, _ in ca_tiles:
            if ca_count >= ca_target:
                break
            if str(pid).strip() in ca_pids:
                continue
            time.sleep(REVEAL_PACE_SEC)
            try:
                data_ca = scrape_product_page(driver, wait, pid, tld=AMAZON_TLD_CA,
                                              allow_deal_only=True)
            except (TimeoutException, WebDriverException) as e:
                log(f"  ⚠️ CA PID {pid} error: {e.__class__.__name__} — skip")
                data_ca = SKIP_THROTTLE

            # Legit skips don't count against the throttle.
            if data_ca in (SKIP_DEAL_ONLY, SKIP_BLOCKED, SKIP_ONETIME):
                continue

            if data_ca == SKIP_THROTTLE or data_ca is None:
                log(f"  🔄 CA throttle on PID {pid} — retrying once after {REVEAL_PACE_SEC}s…")
                time.sleep(REVEAL_PACE_SEC)
                try:
                    data_ca = scrape_product_page(driver, wait, pid, tld=AMAZON_TLD_CA)
                except Exception:
                    data_ca = SKIP_THROTTLE
                if data_ca in (SKIP_DEAL_ONLY, SKIP_BLOCKED, SKIP_ONETIME):
                    continue
                if data_ca == SKIP_THROTTLE or not isinstance(data_ca, dict):
                    ca_consecutive_fails += 1
                    if ca_consecutive_fails >= ROTATION_THRESHOLD and len(VIPON_ACCOUNTS) > 1:
                        # Mirror US behaviour: rotate on consecutive throttle fails instead of stopping.
                        log(f"  ↺ CA: {ca_consecutive_fails} consecutive fails — rotating to next account…")
                        _do_rotate()
                        ca_consecutive_fails = 0
                        account_scraped = 0
                        ca_rotations_no_success += 1
                        if ca_rotations_no_success >= len(VIPON_ACCOUNTS):
                            log(f"  ⛔ CA: cycled all {len(VIPON_ACCOUNTS)} accounts — "
                                f"stopping at {ca_count} products.")
                            break
                    elif len(VIPON_ACCOUNTS) == 1 and ca_consecutive_fails >= 8:
                        log(f"  ⛔ CA throttle: {ca_consecutive_fails} fails (single account) — "
                            f"stopping at {ca_count}.")
                        break
                    continue

            if not isinstance(data_ca, dict):
                continue

            ca_consecutive_fails = 0
            ca_rotations_no_success = 0
            if not data_ca.get("deal_only"):
                account_scraped += 1   # deal-only uses no quota slot → don't count for rotation
            scraped_ca.append(data_ca)
            ca_count += 1
            code_tag = "" if data_ca.get("deal_only") else f" code={data_ca.get('code','?')}"
            log(f"✓ CA Scraped {ca_count}/{ca_target} (sheet total {ca_existing+ca_count}/{PRODUCT_LIMIT}){code_tag}: {data_ca['title'][:60]}")
            if account_scraped >= PROACTIVE_ROTATE_AFTER and len(VIPON_ACCOUNTS) > 1 and ca_count < ca_target:
                log(f"  📊 Proactive rotation after {account_scraped} code(s) — spreading load evenly")
                _do_rotate()
                account_scraped = 0
            try:
                _write_product_row(ws2, data_ca, tld=AMAZON_TLD_CA)
            except Exception as e:
                log(f"  ⚠️ inline CA sheet write failed for PID {data_ca['pid']}: {e.__class__.__name__}")

    finally:
        _write_account_state(ws.spreadsheet)
        _write_pid_history(
            ws.spreadsheet,
            [str(d["pid"]) for d in scraped],
            [str(d["pid"]) for d in scraped_ca],
        )
        try:
            driver.quit()
            log("✓ Chrome closed — RAM freed, starting video production…")
        except Exception:
            pass

    # Rows are written inline during Phase 1/1b (write-as-you-scrape), so there's
    # nothing to bulk-write here — just report what landed. Note: we do NOT exit when
    # nothing was scraped, because seller intake (Phase 3/4) must still run — e.g. on
    # a top-up rerun where the sheets were already full of scraped products.
    if not scraped and not scraped_ca:
        log("▶ No new products scraped this run (sheets already topped up or none "
            "available) — proceeding to seller intake.")
    log(f"✅ US done — {len(scraped)} new product(s) written to Sheet1 "
        f"(sheet total ~{us_existing + len(scraped)})")
    log(f"✅ CA done — {len(scraped_ca)} new product(s) written to Sheet2 "
        f"(sheet total ~{ca_existing + len(scraped_ca)})")

    # Free the social-proof Chrome before the seller-form phases
    global _SOCIAL_DRIVER
    if _SOCIAL_DRIVER is not None:
        try: _SOCIAL_DRIVER.quit()
        except Exception: pass
        _SOCIAL_DRIVER = None

    # ── PHASE 3: US Seller Form Intake ───────────────────────────
    process_seller_forms(ws)

    # ── PHASE 4: Canada Seller Form Intake ───────────────────────
    process_seller_forms_ca(ws2)


if __name__ == "__main__":
    main()
