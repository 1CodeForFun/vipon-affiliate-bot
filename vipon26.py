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
AFFILIATE_ID_CA    = "onamzfreshdea-20"   # single tag for all CA platforms
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
_account_index  = 0   # mutable via _rotate_account()

def _current_account():
    return VIPON_ACCOUNTS[_account_index % len(VIPON_ACCOUNTS)]

def _rotate_account():
    global _account_index
    _account_index = (_account_index + 1) % len(VIPON_ACCOUNTS)
    acc = _current_account()
    print(f"[rotate] switching to account {_account_index + 1}/{len(VIPON_ACCOUNTS)}: {acc['username']}")
    return acc

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

BAD_CODES = {"CATEGORIES","CATEGORY","DISCOUNT","PROMOTION","VOUCHER","COUPON","COLLECTION"}
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
    "lingerie",
    "sleepwear", "sleep ware", "sleep wear", "sleepware",
    "women's clothes", "womens clothes", "women clothes",
    "legging", "leggings", "pants", "sex", "neck",
    "panty", "panties", "underwear", "bra", "Skirt",
    "sexy", "lace", "wig",
    "hooka", "hookah", "shisha",
    "smoking", "tobacco", "tobaco",
    "Christian","bible", "christian", "nightgown",
    "dress", "Dress", "dressy", "blouse",
    "wine", "vodka", "whiskey", "whisky", "beer",
    "bikini", "swimsuit", "swimwear", "swim wear",
]

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

def _build_post_prompt(link, code, discount_pct, expiry, title, price) -> str:
    return (
        "Write a single humorous and engaging Facebook post.\n"
        f"- Product: {title} (pick 2–4 key words only, not the full title)\n"
        f"- Discount: {discount_pct} off\n"
        f"- Expires: {expiry}\n"
        f"- Price: {price} (highlight if it is a bargain)\n"
        f"- Discount code: {code} — tell readers to use it at checkout\n"
        f"- Affiliate link (put on its own last line): {link}\n"
        "Return only the final post text. No labels, no preamble."
    )

def generate_social_post(link, code, discount_pct, expiry, title, price):
    fallback = (f"🔥 {discount_pct} off! Use code {code} before {expiry}. "
                f"Price: {price}.\n{link}")

    if os.getenv("VIPON_DISABLE_GPT", "0") in ("1","true","TRUE","yes","YES"):
        return fallback

    prompt = _build_post_prompt(link, code, discount_pct, expiry, title, price)

    # ── Try Gemini keys in order, skip dead/rate-limited keys ────
    gemini_keys = _read_gemini_keys()
    for kidx, gemini_key in enumerate(gemini_keys):
        if gemini_key in _GEMINI_DEAD_KEYS:
            continue   # expired key — don't waste a round-trip
        try:
            api_url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
                       f"gemini-2.5-flash-lite:generateContent?key={gemini_key}")
            payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.8, "maxOutputTokens": 300}
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
                    return txt
            elif resp.status_code == 429:
                log(f"  ⚠️ Gemini key {kidx+1}/{len(gemini_keys)} rate-limited — trying next")
                continue
            elif resp.status_code == 400:
                log(f"  ⚠️ Gemini key {kidx+1} expired/invalid — marking dead for this session")
                _GEMINI_DEAD_KEYS.add(gemini_key)
                continue
            elif resp.status_code == 503:
                log(f"  ⚠️ Gemini key {kidx+1} server busy (503) — trying next")
                continue
            else:
                log(f"  ⚠️ Gemini key {kidx+1} error {resp.status_code}: {resp.text[:150]}")
        except Exception as e:
            log(f"  ⚠️ Gemini key {kidx+1} exception: {e}")

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
                    return txt
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

def login(driver, wait):
    acc = _current_account()
    log(f"▶ Logging in as {acc['username']}…")
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

# ════════════════════════════════════════════════════════════════
#  GOOGLE SHEETS
# ════════════════════════════════════════════════════════════════

HEADER = [
    "Link", "Reel", "IG", "Youtube", "TikTok",
    "Discount Code", "Disc", "Expiry", "Product", "Price",
    "PID", "Image", "Pin Image", "Reel URL", "FB Post", "Reel Posted",
    "FB Text Posted", "YT Posted",
]

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


def fetch_amazon_images(driver, asin: str, tld: str = "com", max_imgs: int = 9) -> list:
    """
    Official Amazon Product Advertising API (PA-API 5).
    No scraping, no IP blocking, no Chrome involved.
    The 'driver' and 'tld' params are kept for signature compatibility.
    """
    if not asin:
        return []
    client = _get_paapi_client()
    if not client:
        return []
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
        if not response or not response.items_result or not response.items_result.items:
            log(f"  ⚠️ PA-API returned no items for {asin}")
            return []

        item = response.items_result.items[0]
        imgs = []
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
        return imgs[:max_imgs]
    except Exception as e:
        log(f"  ⚠️ PA-API fetch failed for {asin}: {e}")
        return []

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
            # Wait for the code text to appear in PC_240_jumpToAmzFromCodeZone (AJAX result)
            try:
                WebDriverWait(driver, 6).until(
                    lambda d: (d.find_element(By.ID, "PC_240_jumpToAmzFromCodeZone").text or "").strip()
                )
            except Exception:
                time.sleep(3.0)   # fallback if element not found by ID
            break   # ← stop after first click — never loop into the post-reveal "Use on Amazon" button
        except Exception:
            continue

def extract_code(driver):
    # 1a) PC_240_jumpToAmzFromCodeZone — wait for NON-EMPTY text (element exists from page load
    #     but is empty until AJAX populates it after clicking GET CODE)
    try:
        WebDriverWait(driver, 5).until(
            lambda d: (d.find_element(By.ID, "PC_240_jumpToAmzFromCodeZone").text or "").strip()
        )
        el  = driver.find_element(By.ID, "PC_240_jumpToAmzFromCodeZone")
        txt = (_text(el) or "").upper()
        m   = CODE_RE.search(txt)
        if m and is_plausible_code(m.group(1), strict=False): return m.group(1)
        if is_plausible_code(txt, strict=False): return txt
    except Exception:
        pass
    # 1b) Other known IDs (presence check is fine — these only exist when code is ready)
    for i in ["PC_240_codeInDetail", "coupon_code"]:
        try:
            el = WebDriverWait(driver, 3).until(EC.presence_of_element_located((By.ID, i)))
            txt = (_text(el) or "").upper()
            m = CODE_RE.search(txt)
            if m and is_plausible_code(m.group(1), strict=False): return m.group(1)
            if is_plausible_code(txt, strict=False): return txt
        except Exception:
            pass
    # 2) Input fields — code is often pre-filled for easy copy
    try:
        for inp in driver.find_elements(By.XPATH,
                "//input[@type='text' or not(@type)]")[:20]:
            val = (inp.get_attribute("value") or "").strip().upper()
            if is_plausible_code(val, strict=False):
                return val
    except Exception:
        pass
    # 3) Elements whose class or id contains "coupon" or "code"
    try:
        for el in driver.find_elements(By.XPATH,
                "//*[contains(@class,'coupon') or contains(@class,'code') or "
                "contains(@id,'coupon') or contains(@id,'code') or "
                "contains(@class,'Coupon') or contains(@class,'Code')]")[:60]:
            txt = (_text(el) or "").strip().upper()
            if not txt or len(txt) > 20:
                continue   # skip empty or long blocks
            m = CODE_RE.search(txt)
            if m and is_plausible_code(m.group(1), strict=False):
                return m.group(1)
    except Exception:
        pass
    # 4) Page-source keyword scan
    try:
        src = (driver.page_source or "").upper()
        m = re.search(r"CODE[:\s]*([A-Z0-9]{6,12})", src)
        if m and is_plausible_code(m.group(1), strict=False): return m.group(1)
    except Exception:
        pass
    # 5) Broad text scan (last resort)
    try:
        for el in driver.find_elements(By.XPATH, "//strong|//b|//code|//span")[:250]:
            txt = (_text(el) or "").upper()
            m = CODE_RE.search(txt)
            if m and is_plausible_code(m.group(1), strict=True): return m.group(1)
    except Exception:
        pass
    return ""

# ════════════════════════════════════════════════════════════════
#  PRODUCT PAGE SCRAPER
# ════════════════════════════════════════════════════════════════

def scrape_product_page(driver, wait, pid, tld="com"):
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

    try_reveal_code(driver)
    # Guard: if a reveal click navigated away from vipon (e.g. clicked "Use on Amazon"), come back
    if "myvipon.com/product/" not in driver.current_url:
        log(f"  ↩ navigated away during reveal — returning to product page")
        driver.get(url)
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "body")))
        time.sleep(2.0)
    code = extract_code(driver)
    # Retry once if code not found — AJAX may still be in flight
    if not code or not is_plausible_code(code, strict=False):
        time.sleep(3.0)
        try_reveal_code(driver)
        if "myvipon.com/product/" not in driver.current_url:
            driver.get(url)
            wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "body")))
            time.sleep(2.0)
        code = extract_code(driver)
    code = (code or "").strip().upper()
    if not is_plausible_code(code, strict=False):
        log("  ✗ no valid code — skipping")
        return None

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

    # Filter blocked keywords
    t_low = (title or "").lower()
    for bad in BLOCKED_TITLE_KEYWORDS:
        if bad in t_low:
            log(f"  ✗ blocked keyword '{bad}' — skipping PID {pid}")
            return None

    image_url = resolve_cover_image_url(driver)
    log(f"  ↳ cover image: {image_url or 'NONE'}")

    src  = driver.page_source or ""
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
        t_low = title.lower()
        bad_hit = next(
            (b for b in BLOCKED_TITLE_KEYWORDS
             if b and (b in t_low if (" " in b or "-" in b)
                       else re.search(rf"\b{re.escape(b)}\b", t_low))),
            None,
        )
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

        # ── Build reel ────────────────────────────────────────────
        t_short  = shorten_title(title, MAX_TITLE_LEN)
        reel_url = ""
        try:
            reel_url = make_and_upload_reel_from_images(asin, images, disc_norm, code, title, price)
        except Exception as e:
            log(f"  ⚠️ Reel failed for seller {asin}: {e}")

        if not reel_url:
            log(f"  ✗ Form row {row_idx}: video failed for {asin}")
            form_ws.update_cell(row_idx, status_col + 1, "Video Failed")
            skipped += 1
            continue

        # ── Links + post text ─────────────────────────────────────
        aff_link       = get_affiliate_link(asin, "com")
        platform_links = get_platform_links(asin, "com")
        post_text      = generate_social_post(aff_link, code, disc_txt, expiry, t_short, price)

        # ── Append to Sheet1 (same column order as scraped products) ──
        ws_main.append_row([
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
        ], value_input_option="USER_ENTERED")

        # ── Mark done in form tab ─────────────────────────────────
        form_ws.update_cell(row_idx, status_col + 1, "Done")
        processed += 1
        log(f"  ✓ Seller {asin} → Sheet1 row added")
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

        try:
            reel_url = make_and_upload_reel_from_images(
                asin, images, disc_txt, code, t_short, price, expiry
            )
        except Exception as e:
            log(f"  ⚠️ Reel failed for CA seller {asin}: {e}")
            form_ws2.update_cell(row_idx, status_col + 1, "Video Failed")
            skipped += 1
            continue

        ws2_main.append_rows([[
            aff_link, platform_links.get("reel",""), platform_links.get("ig",""),
            platform_links.get("youtube",""), platform_links.get("tiktok",""),
            code, disc_txt, expiry, t_short, price, asin,
            images[0], images[0], reel_url, post_text,
        ]], value_input_option="USER_ENTERED", table_range="A1")

        form_ws2.update_cell(row_idx, status_col + 1, "Done")
        processed += 1
        log(f"  ✓ CA Seller {asin} -> Sheet2 row added")
        time.sleep(0.3)

    log(f"✅ CA seller forms done — {processed} added to Sheet2, {skipped} skipped")


# ════════════════════════════════════════════════════════════════
#  MAIN  — Phase 1: scrape US (Chrome alive)
#          Phase 1b: scrape Canada (same Chrome session, switched to CA)
#          Phase 2: build US videos + write Sheet1 (Chrome closed)
#          Phase 2b: build Canada videos + write Sheet2
#          Phase 3: US seller form intake (Form Responses 2 -> Sheet1)
#          Phase 4: CA seller form intake (Response Form 3 -> Sheet2)
# ════════════════════════════════════════════════════════════════

def main():
    random.seed(time.time())

    ws     = open_sheet_and_reset()
    ws2    = open_sheet2_and_reset()
    driver = create_driver()
    wait   = WebDriverWait(driver, WAIT_SECS)

    # ── PHASE 1: Scrape US ───────────────────────────────────────
    scraped    = []
    scraped_ca = []
    try:
        # Try every account until one logs in successfully
        logged_in = False
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

        tiles = collect_promo_tiles_random(driver, wait)
        count = 0
        consecutive_fails = 0
        ROTATION_THRESHOLD = 8  # switch account after this many consecutive no-code results

        for pid, _, _ in tiles:
            if count >= PRODUCT_LIMIT:
                break
            try:
                data = scrape_product_page(driver, wait, pid)
            except TimeoutException:
                log(f"  ⏱️ hard timeout on PID {pid} — skip")
                data = None
            except WebDriverException as e:
                log(f"  ⚠️ webdriver error on PID {pid}: {e} — skip")
                data = None

            if data is None:
                consecutive_fails += 1
                # Auto-rotate account when we hit the daily code limit
                if consecutive_fails >= ROTATION_THRESHOLD and len(VIPON_ACCOUNTS) > 1:
                    _rotate_account()
                    # Always reset counter so we don't loop endlessly on failed re-login
                    consecutive_fails = 0
                    try:
                        logout(driver)                # must log out or /login redirects to home
                        driver.delete_all_cookies()   # clear any remaining session cookies
                        login(driver, wait)
                        log(f"  ✓ Account rotated successfully")
                    except Exception as e:
                        log(f"  ⚠️ Re-login after rotation failed: {e.__class__.__name__} — continuing with current session")
                continue

            consecutive_fails = 0
            scraped.append(data)
            count += 1
            log(f"✓ Scraped {count}/{PRODUCT_LIMIT}: {data['title'][:60]}")

        # ── PHASE 1b: Switch to Canada and scrape CA deals ───────
        log("\n═══ Phase 1b: Canada Scrape ═══")
        ca_switched = switch_to_canada(driver)
        if not ca_switched:
            log("⚠️  Canada switch failed — skipping CA scrape to avoid writing US products to Sheet2")
        ca_tiles = collect_promo_tiles_random(driver, wait, start_url=PROMO_URL_CA) if ca_switched else []
        ca_count = 0
        ca_fails = 0
        for pid, _, _ in ca_tiles:
            if ca_count >= PRODUCT_LIMIT:
                break
            try:
                data_ca = scrape_product_page(driver, wait, pid, tld=AMAZON_TLD_CA)
            except (TimeoutException, WebDriverException) as e:
                log(f"  ⚠️ CA PID {pid} error: {e.__class__.__name__} — skip")
                ca_fails += 1
                data_ca = None
            if data_ca is None:
                ca_fails += 1
                continue
            ca_fails = 0
            scraped_ca.append(data_ca)
            ca_count += 1
            log(f"✓ CA Scraped {ca_count}/{PRODUCT_LIMIT}: {data_ca['title'][:60]}")

    finally:
        try:
            driver.quit()
            log("✓ Chrome closed — RAM freed, starting video production…")
        except Exception:
            pass

    if not scraped:
        log("✗ No products scraped — exiting")
        return

    # ── PHASE 2: Videos + Sheet ───────────────────────────────────
    if ENABLE_PINTEREST:
        ws_p = open_pinterest_sheet_and_reset()

    for data in scraped:
        pid       = data["pid"]
        t_short   = shorten_title(data["title"], MAX_TITLE_LEN)
        pin_img_url = data["image"]

        try:
            reel_url = make_and_upload_reel_from_images(
                pid,
                data.get("images") or ([data["image"]] if data.get("image") else []),
                data["discount"],
                data["code"],
                data["title"],
                data["price"],    # ← comma present (bug fixed)
                data["expiry"],
            )
        except Exception as e:
            log(f"  ⚠️ reel failed for PID {pid}: {e}")
            reel_url = ""

        # Write main sheet row
        ws.append_row([
            data["link"],
            data["link_reel"],
            data["link_ig"],
            data["link_youtube"],
            data["link_tiktok"],
            data["code"],
            data["discount"],
            data["expiry"],
            t_short,
            data["price"],
            pid,
            data["image"],
            pin_img_url,
            reel_url,
            generate_social_post(
                data["link"],
                data["code"],
                data["discount"],
                data["expiry"],
                t_short,
                data["price"],
            ),
        ], value_input_option="USER_ENTERED")

        # Write Pinterest row (only if enabled)
        if ENABLE_PINTEREST:
            ws_p.append_row([
                t_short,
                reel_url,
                PINTEREST_BOARD_DEFAULT,
                PINTEREST_THUMBNAIL_DEFAULT,
                generate_pinterest_description(
                    t_short,
                    data["discount"],
                    data["code"],
                    data["expiry"],
                    data["price"],
                ),
                data["link_pinterest"],
                "",
                PINTEREST_KEYWORDS_DEFAULT,
            ], value_input_option="USER_ENTERED")

        time.sleep(0.3)
        log(f"✓ Row written for PID {pid}")

    log(f"✅ US done — {len(scraped)} products processed")

    # ── PHASE 2b: Canada Videos + Sheet2 ─────────────────────────
    log("\n═══ Phase 2b: Canada Videos + Sheet2 ═══")
    for data in scraped_ca:
        pid     = data["pid"]
        t_short = shorten_title(data["title"], MAX_TITLE_LEN)

        try:
            reel_url = make_and_upload_reel_from_images(
                pid,
                data.get("images") or ([data["image"]] if data.get("image") else []),
                data["discount"],
                data["code"],
                data["title"],
                data["price"],
                data["expiry"],
            )
        except Exception as e:
            log(f"  ⚠️ CA reel failed for PID {pid}: {e}")
            reel_url = ""

        ws2.append_rows([[
            data["link"],
            data["link_reel"],
            data["link_ig"],
            data["link_youtube"],
            data["link_tiktok"],
            data["code"],
            data["discount"],
            data["expiry"],
            t_short,
            data["price"],
            pid,
            data["image"],
            data["image"],
            reel_url,
            generate_social_post(
                data["link"],
                data["code"],
                data["discount"],
                data["expiry"],
                t_short,
                data["price"],
            ),
        ]], value_input_option="USER_ENTERED", table_range="A1")

        time.sleep(0.3)
        log(f"✓ CA row written for PID {pid}")

    log(f"✅ CA done — {len(scraped_ca)} products processed")

    # ── PHASE 3: US Seller Form Intake ───────────────────────────
    process_seller_forms(ws)

    # ── PHASE 4: Canada Seller Form Intake ───────────────────────
    process_seller_forms_ca(ws2)


if __name__ == "__main__":
    main()
