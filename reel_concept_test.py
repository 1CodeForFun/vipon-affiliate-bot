#!/usr/bin/env python3
"""
reel_concept_test.py — Standalone test of the NEW reel concept.

Frame sequence (each frame keeps the discount %/code overlay, as today):
  1. Cover image (the current cover)
  2. Amazon PHONE price screenshot, with a "was → now" discount banner
  3. Next product (gallery) image
  4. Reviews + "bought last month" zoom from the page
  5. The rest of the gallery images

Audio:
  VO = FB-post PIECE 1 (persuasive copy that does NOT read the URL or spell the
  code) via Gemini TTS (the voice you liked). Beats fill any tail silence.

FB post (printed for reference) is split into:
  PIECE 1 = spoken copy (no link, no spelled-out code)   → used as VO + FB body
  PIECE 2 = affiliate link + discount code               → appended to FB text only

Standalone — touches nothing in the production pipeline.

Env:
  SECRETS_DIR   — folder with credential files (CI copies them here)
  TTS_VOICE     — optional Gemini TTS voice name (default Achernar)
"""

import base64
import hashlib
import json
import math
import os
import re
import struct
import subprocess
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

import gspread
import requests
from oauth2client.service_account import ServiceAccountCredentials

# ─── CONFIG ──────────────────────────────────────────────────────────────────
SECRETS_DIR       = os.environ.get("SECRETS_DIR", ".")
GOOGLE_CREDS_FILE = os.path.join(SECRETS_DIR, "vipon_google_creds.json")
GOOGLE_SHEET_NAME = "vipon"

VIDEO_W, VIDEO_H  = 720, 1280
FPS               = 30
IMG_SECS          = 2.6      # seconds per gallery image
CROP_SECS         = 3.2      # seconds per page-crop frame (price / reviews)
CLOUD_FOLDER      = "vipon_concept_test"
TTS_VOICE         = os.environ.get("TTS_VOICE", "Achernar").strip() or "Achernar"

_TEXT_MODELS = ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.0-flash"]
_TTS_MODELS  = ["gemini-2.5-flash-preview-tts", "gemini-3.1-flash-tts-preview",
                "gemini-2.5-pro-preview-tts"]

_FF_LOG    = ["-loglevel", "error", "-hide_banner"]
_FF_ENCODE = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "26", "-threads", "2"]

def log(m): print(m, flush=True)

# ─── CREDENTIALS / TOOLS ──────────────────────────────────────────────────────
def _read_gemini_keys():
    out = []
    for path, _ in ((os.path.expanduser("~/geminipro.txt"), 1),):
        if os.path.exists(path):
            k = open(path).read().strip()
            if k: out.append(k)
    multi = os.path.expanduser("~/geminikeys.txt")
    if os.path.exists(multi):
        out += [l.strip() for l in open(multi) if l.strip() and not l.startswith("#")]
    single = os.path.expanduser("~/geminikey.txt")
    if os.path.exists(single):
        k = open(single).read().strip()
        if k and k not in out: out.append(k)
    return out

def _load_cloudinary():
    d = json.load(open(os.path.expanduser("~/cloudinary.json")))
    return d["cloud_name"], d["api_key"], d["api_secret"]

def _which_ffmpeg():
    for p in ("/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        if os.path.exists(p): return p
    import shutil; return shutil.which("ffmpeg") or "ffmpeg"

def _find_font():
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"):
        if os.path.exists(p): return p
    return ""

def _chrome_bits():
    binary = next((b for b in ("/usr/bin/chromium-browser", "/usr/bin/chromium",
                               "/usr/bin/google-chrome") if os.path.exists(b)), "")
    driver = next((d for d in ("/usr/bin/chromedriver",
                               "/usr/lib/chromium-browser/chromedriver",
                               "/usr/bin/chromium-chromedriver") if os.path.exists(d)), "")
    return binary, driver

# ─── SMALL HELPERS ─────────────────────────────────────────────────────────────
def _num(s):
    m = re.search(r"(\d+(?:\.\d+)?)", (s or "").replace(",", ""))
    return float(m.group(1)) if m else None

def _esc(t):
    return (t.replace("\\", "\\\\").replace("'", "\\'")
             .replace(":", "\\:").replace("%", "\\%"))

def friendly_date(expiry):
    if not expiry: return ""
    dm = re.search(r"(\d{1,2})[/\-](\d{1,2})[/\-](\d{2,4})", expiry)
    if dm:
        try:
            a, b, c = int(dm.group(1)), int(dm.group(2)), int(dm.group(3))
            y = 2000 + c if c < 100 else c
            d = datetime(y, a, b); return f"{d.strftime('%B')} {d.day}"
        except Exception: pass
    for unit, kw in (("days", "day"), ("hours", "hour")):
        m = re.search(rf"(\d+)\s*{kw}", expiry, re.I)
        if m:
            d = datetime.now() + timedelta(**{unit: int(m.group(1))})
            return f"{d.strftime('%B')} {d.day}"
    return expiry

def parse_units_sold(text):
    """'100+ bought in past month' → 100 ; '1K+ bought' → 1000 ; '2.5K' → 2500."""
    if not text: return 0
    m = re.search(r"(\d+(?:\.\d+)?)\s*([KkMm]?)", text)
    if not m: return 0
    n = float(m.group(1))
    return int(n * {"k": 1_000, "m": 1_000_000}.get(m.group(2).lower(), 1))

def parse_stars(text):
    """'4.3 out of 5 stars' → 4.3."""
    if not text: return 0.0
    m = re.search(r"(\d(?:\.\d)?)", text)
    return float(m.group(1)) if m else 0.0

def parse_count(text):
    """'12,394 ratings' → 12394."""
    if not text: return 0
    m = re.search(r"([\d,]+)", text)
    return int(m.group(1).replace(",", "")) if m else 0

def social_score(units_sold, stars, rating_count):
    """Compound recent sales velocity with rating quality (and a little volume).
    Velocity is the strongest signal; stars scale it; a log of rating_count adds
    a small, saturating bump so a huge review base helps but doesn't dominate.
    """
    import math as _m
    quality = (stars / 5.0) if stars else 0.6          # neutral-ish if unknown
    volume  = 1 + _m.log10(rating_count + 1) / 4.0     # ~1.0 .. ~2.0
    base    = units_sold if units_sold else 1
    return round(base * quality * volume, 1)

def probe_duration(path, ffmpeg):
    try:
        r = subprocess.run([ffmpeg, "-i", path], stderr=subprocess.PIPE, stdout=subprocess.PIPE)
        m = re.search(r"Duration: (\d+):(\d+):(\d+\.?\d*)", r.stderr.decode())
        if m: return int(m.group(1))*3600 + int(m.group(2))*60 + float(m.group(3))
    except Exception: pass
    return 0.0

def cloud_upload(path, public_id, kind="video"):
    if not path or not os.path.exists(path):
        log(f"  ⚠️ upload skipped — missing {path}"); return ""
    size = os.path.getsize(path)
    cn, ak, asec = _load_cloudinary()
    ts = int(time.time())
    sig = hashlib.sha1(f"public_id={public_id}&timestamp={ts}{asec}".encode()).hexdigest()
    try:
        with open(path, "rb") as f:
            r = requests.post(f"https://api.cloudinary.com/v1_1/{cn}/{kind}/upload",
                              data={"public_id": public_id, "timestamp": ts,
                                    "api_key": ak, "signature": sig},
                              files={"file": f}, timeout=300)
    except Exception as e:
        log(f"  ⚠️ upload error: {e}"); return ""
    if r.ok:
        url = r.json().get("secure_url", "")
        log(f"  ☁️ uploaded {size:,} bytes → {url}")
        return url
    log(f"  ⚠️ Cloudinary {r.status_code}: {r.text[:160]}"); return ""

def pcm_to_wav(pcm, rate=24000, ch=1, bits=16):
    """Gemini TTS returns raw PCM — wrap it with a WAV header so ffmpeg reads it."""
    byte_rate = rate * ch * bits // 8
    block = ch * bits // 8
    return (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE" +
            b"fmt " + struct.pack("<IHHIIHH", 16, 1, ch, rate, byte_rate, block, bits) +
            b"data" + struct.pack("<I", len(pcm)) + pcm)

# ─── PRODUCT ───────────────────────────────────────────────────────────────────
def pick_product():
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDS_FILE, scope)
    ws    = gspread.authorize(creds).open(GOOGLE_SHEET_NAME).sheet1
    for row in ws.get_all_values()[1:]:
        if len(row) < 14: continue
        title, aff = row[8].strip(), row[0].strip()
        if not title or not aff: continue
        m = re.search(r"asin=([A-Za-z0-9]{10})", aff, re.I) or re.search(r"\b(B0[A-Z0-9]{8})\b", aff, re.I)
        if not m: continue
        return {"title": title, "asin": m.group(1).upper(), "price": row[9].strip(),
                "code": row[5].strip(), "disc": row[6].strip(), "expiry": row[7].strip(),
                "aff_link": aff, "cover": row[11].strip() if len(row) > 11 else ""}
    return None

# ─── GEMINI TEXT ────────────────────────────────────────────────────────────────
def gemini_text(prompt, keys, max_tokens=300):
    for model in _TEXT_MODELS:
        for key in keys:
            try:
                r = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
                    json={"contents": [{"parts": [{"text": prompt}]}],
                          "generationConfig": {"temperature": 0.9, "maxOutputTokens": max_tokens,
                                               "thinkingConfig": {"thinkingBudget": 0}}},
                    timeout=30)
                if r.ok:
                    return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                if r.status_code != 429:
                    log(f"  {model} {r.status_code}: {r.text[:90]}")
            except Exception as e:
                log(f"  {model} err: {e}")
    return ""

def build_fb_pieces(p, keys):
    expd = friendly_date(p["expiry"])
    piece1 = gemini_text(
        f'Product: "{p["title"]}"\n'
        f"Discount: {p['disc']} off | Price after discount: {p['price']} | Ends: {expd}\n\n"
        "Write a warm, punchy 45-65 word first-person social post for this Amazon find.\n"
        "Rules:\n"
        "- Open with something surprising/relatable, NOT 'Are you looking for'\n"
        "- Mention what it is, why it's loved, the price, and that the deal ends "
        f"{expd}\n"
        "- Refer to the code as 'the code on screen' or 'the code in the link' — do "
        "NOT spell out the actual code letters.\n"
        "- Do NOT include any link or URL.\n"
        "- Smooth sentences with commas (it will be read aloud). No hashtags.\n"
        "Return only the post text.", keys, max_tokens=180)
    if not piece1:
        disc = f"{p['disc']} off" if p["disc"] else "a big discount"
        piece1 = (f"Okay, this {p['title'].split(',')[0]} is the upgrade I didn't know I "
                  f"needed. Right now it's {disc}, just {p['price']}, and the deal ends "
                  f"{expd}. Use the code on screen at checkout — don't sleep on this one.")
    code_line = f"\n\n🏷️ Code: {p['code']}" if p["code"] else ""
    piece2 = f"🛒 {p['aff_link']}{code_line}"
    return piece1, piece2, expd

# ─── GEMINI TTS ──────────────────────────────────────────────────────────────────
def gemini_tts(text, keys, voice=TTS_VOICE):
    for model in _TTS_MODELS:
        for key in keys:
            try:
                r = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
                    json={"contents": [{"parts": [{"text": text}]}],
                          "generationConfig": {
                              "responseModalities": ["AUDIO"],
                              "speechConfig": {"voiceConfig": {
                                  "prebuiltVoiceConfig": {"voiceName": voice}}}}},
                    timeout=120)
                if r.ok:
                    raw = (r.json()["candidates"][0]["content"]["parts"][0]
                           .get("inlineData", {}).get("data", ""))
                    if raw:
                        log(f"  ✓ TTS via {model} ({voice})")
                        return pcm_to_wav(base64.b64decode(raw))
                else:
                    log(f"  TTS {model} {r.status_code}: {r.text[:90]}")
            except Exception as e:
                log(f"  TTS {model} err: {e}")
    return None

# ─── CHROME: capture gallery images + price/reviews regions ──────────────────────
_GALLERY_JS = r"""
const out = {images: [], price: null, reviews: null, title: null,
             bought: "", rating: "", rating_count: ""};
function abs(el){ if(!el) return null; const r=el.getBoundingClientRect();
  if(r.width<8||r.height<4) return null;
  return {x:r.left+window.scrollX, y:r.top+window.scrollY, w:r.width, h:r.height}; }
function txt(el){ return el ? (el.innerText||el.textContent||'').trim() : ''; }
// product gallery images: square-ish media-amazon images (incl. alt hi-res from
// data-a-dynamic-image), skipping wide ad banners (e.g. "Fanka") by aspect ratio.
const seen = new Set();
function add(s){ if(!s) return; const m = s.match(/images\/I\/([A-Za-z0-9%+._-]+)\./);
  if(m && s.includes('media-amazon') && !seen.has(m[1])){ seen.add(m[1]); out.images.push(s); } }
document.querySelectorAll('img').forEach(im=>{
  const r = im.getBoundingClientRect();
  const ar = r.height > 0 ? r.width / r.height : 99;
  if (ar > 2.2 || ar < 0.45) return;                 // skip wide/thin banners (ads)
  if (im.closest('[data-cel-widget*="sp_"], [class*="adholder"], [aria-label*="ponsored"], [id^="ad-"]')) return;
  const dyn = im.getAttribute('data-a-dynamic-image');   // alt hi-res image set
  if (dyn){ try{ Object.keys(JSON.parse(dyn)).forEach(add); }catch(e){} }
  add(im.getAttribute('data-old-hires'));
  add(im.src);
});
out.title   = abs(document.querySelector('#title')
            || document.querySelector('#productTitle')
            || document.querySelector('#titleSection'));
// Tight price element (the actual price number) so the zoom + the strike align.
out.price   = abs(document.querySelector('.priceToPay')
            || document.querySelector('.a-price')
            || document.querySelector('#corePrice_feature_div')
            || document.querySelector('#price'));
out.reviews = abs(document.querySelector('#averageCustomerReviews')
            || document.querySelector('#acrPopover')
            || document.querySelector("[data-hook='review']"));
// ── social proof ──
// units sold this month ("100+ bought in past month")
{ const els = document.querySelectorAll('span,div,a');
  for (const e of els){ const t=(e.innerText||'').trim();
    if(/bought in past/i.test(t) && t.length<60){ out.bought=t; break; } } }
// star rating + how many ratings
out.rating       = txt(document.querySelector("[data-hook='rating-out-of-text']")
                 || document.querySelector('#acrPopover'));
out.rating_count = txt(document.querySelector('#acrCustomerReviewText'));
// Diagnostic: when price_box is missing, what did Amazon actually serve to this IP?
// (interstitial / "continue shopping" / robot-check vs. a normal product page)
out.doc_title = (document.title || '').slice(0, 120);
out.body_head = (document.body ? (document.body.innerText || '').slice(0, 300) : '');
return out;
"""

# ─── PA API CLIENT (primary image source — no scraping, no IP blocking) ─────
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
        log(f"  PA-API client init failed: {e}")
        return None

def _paapi_get_images(asin, tld="com", max_imgs=6):
    """Primary image source: Amazon PA API. Returns [] on any failure."""
    client = _get_paapi_client()
    if not client:
        return []
    try:
        from paapi5_python_sdk.get_items_request import GetItemsRequest
        from paapi5_python_sdk.get_items_resource import GetItemsResource
        from paapi5_python_sdk.partner_type import PartnerType
        partner_tag = "fdcanada00-20" if tld == "ca" else "freshdeal00cc-20"
        request = GetItemsRequest(
            partner_tag=partner_tag,
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
            return []
        item = response.items_result.items[0]
        imgs = []
        if item.images and item.images.primary and item.images.primary.large:
            imgs.append(item.images.primary.large.url)
        if item.images and item.images.variants:
            for v in item.images.variants:
                if v.large and v.large.url and v.large.url not in imgs:
                    imgs.append(v.large.url)
                if len(imgs) >= max_imgs:
                    break
        log(f"  PA-API images: {len(imgs)}")
        return imgs[:max_imgs]
    except Exception as e:
        log(f"  PA-API fetch failed: {e}")
        return []


def paapi_get_product_info(asin, tld="com"):
    """Fetch clean title + bullet features + price from PA API. Returns dict (empty on failure)."""
    client = _get_paapi_client()
    if not client:
        return {}
    try:
        from paapi5_python_sdk.get_items_request import GetItemsRequest
        from paapi5_python_sdk.get_items_resource import GetItemsResource
        from paapi5_python_sdk.partner_type import PartnerType
        partner_tag = "fdcanada00-20" if tld == "ca" else "freshdeal00cc-20"
        request = GetItemsRequest(
            partner_tag=partner_tag,
            partner_type=PartnerType.ASSOCIATES,
            marketplace=f"www.amazon.{tld}",
            item_ids=[asin],
            resources=[
                GetItemsResource.ITEMINFO_TITLE,
                GetItemsResource.ITEMINFO_FEATURES,
                GetItemsResource.OFFERS_LISTINGS_PRICE,
                GetItemsResource.OFFERS_LISTINGS_SAVINGBASIS,
            ],
        )
        response = client.get_items(request)
        if not response or not response.items_result or not response.items_result.items:
            return {}
        item = response.items_result.items[0]
        info = {}
        if item.item_info and item.item_info.title:
            info['title_text'] = (item.item_info.title.display_value or '').strip()
        if item.item_info and item.item_info.features:
            info['bullets'] = [f for f in (item.item_info.features.display_values or []) if f][:5]
        # Current price
        try:
            listing = item.offers.listings[0]
            info['price_text'] = listing.price.display_amount or ''
            if listing.saving_basis:
                info['orig_price_text'] = listing.saving_basis.display_amount or ''
        except Exception:
            pass
        log(f"  PA-API info: title={'yes' if info.get('title_text') else 'no'} | "
            f"bullets={len(info.get('bullets', []))} | price={info.get('price_text', '-')}")
        return info
    except Exception as e:
        log(f"  PA-API product info failed: {e}")
        return {}


def capture_page(asin, td, ffmpeg, tld="com"):
    """Return (gallery_image_urls, screenshot_path, img_w, img_h, price_box, reviews_box)."""
    # PA API is primary for images — reliable, no bot-detection
    imgs = _paapi_get_images(asin, tld)

    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
    except ImportError:
        log("  selenium missing"); return imgs, None, 0, 0, None, None, None, {}
    binary, drv = _chrome_bits()
    opts = Options()
    for a in ("--headless=new", "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
              "--hide-scrollbars", "--lang=en-US", "--window-size=440,950"):
        opts.add_argument(a)
    opts.add_argument("--user-agent=Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")
    if binary: opts.binary_location = binary
    driver = webdriver.Chrome(service=(Service(executable_path=drv) if drv else Service()), options=opts)
    shot = os.path.join(td, "page.png")
    pw, ph, price_box, rev_box, title_box = 0, 0, None, None, None
    social = {"bought": "", "rating": "", "rating_count": ""}
    try:
        driver.set_window_size(440, 950)
        driver.get(f"https://www.amazon.{tld}/dp/{asin}?th=1&psc=1")
        time.sleep(5)
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight*0.55);"); time.sleep(1.3)
        driver.execute_script("window.scrollTo(0,0);"); time.sleep(0.7)
        data = driver.execute_script(_GALLERY_JS) or {}
        if not imgs:  # PA API failed — fall back to Selenium images
            imgs = data.get("images", [])[:6]
            log(f"  Selenium fallback images: {len(imgs)}")
        price_box, rev_box, title_box = data.get("price"), data.get("reviews"), data.get("title")
        social = {"bought": data.get("bought", ""), "rating": data.get("rating", ""),
                  "rating_count": data.get("rating_count", "")}
        log(f"  gallery imgs: {len(imgs)} | price box: {'y' if price_box else 'n'} | reviews box: {'y' if rev_box else 'n'}")
        if not price_box:
            log(f"  ⚠️ price_box MISSING — page served: title={data.get('doc_title','')!r} | "
                f"body[:140]={(data.get('body_head','') or '')[:140]!r}")
        log(f"  social: bought='{social['bought']}' rating='{social['rating']}' count='{social['rating_count']}'")
        metrics = driver.execute_cdp_cmd("Page.getLayoutMetrics", {})
        pw = math.ceil(metrics["cssContentSize"]["width"])
        ph = min(math.ceil(metrics["cssContentSize"]["height"]), 7000)
        driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {
            "mobile": True, "width": pw, "height": ph, "deviceScaleFactor": 2,
            "screenWidth": pw, "screenHeight": ph})
        res = driver.execute_cdp_cmd("Page.captureScreenshot",
                                     {"captureBeyondViewport": True, "fromSurface": True, "format": "png"})
        with open(shot, "wb") as f: f.write(base64.b64decode(res["data"]))
        # CDP captured at deviceScaleFactor 2 → screenshot px = css px * 2
        pw, ph = pw * 2, ph * 2
        for b in (price_box, rev_box, title_box):
            if b:
                for k in b: b[k] *= 2
        log(f"  screenshot {pw}x{ph}")
    except Exception as e:
        log(f"  capture failed: {e}")
    finally:
        try: driver.quit()
        except Exception: pass
    return imgs, (shot if os.path.exists(shot) else None), pw, ph, price_box, rev_box, title_box, social

# ─── SEGMENT BUILDERS ────────────────────────────────────────────────────────────
def _overlay_codepct(disc, code, font):
    """drawtext chain for the persistent discount %/code overlay (bottom area)."""
    parts = []
    if font and disc:
        parts.append(f"drawtext=fontfile='{font}':expansion=none:text='{_esc(disc)}':x=(w-text_w)/2:y=h*0.80:"
                     f"fontsize=58:fontcolor=white:box=1:boxcolor=0xCC2200@0.85:boxborderw=12:"
                     f"shadowcolor=black@0.7:shadowx=2:shadowy=2")
    if font and code:
        parts.append(f"drawtext=fontfile='{font}':expansion=none:text='Code\\: {_esc(code)}':x=(w-text_w)/2:y=h*0.875:"
                     f"fontsize=40:fontcolor=white:box=1:boxcolor=black@0.6:boxborderw=10:"
                     f"shadowcolor=black@0.7:shadowx=2:shadowy=2")
    return parts

def seg_from_image(img_url, dst, td, ffmpeg, disc, code, font, idx, secs):
    ip = os.path.join(td, f"gimg_{idx}.jpg")
    try:
        r = requests.get(img_url, timeout=30, headers={"User-Agent": "Mozilla/5.0"}); r.raise_for_status()
        open(ip, "wb").write(r.content)
    except Exception as e:
        log(f"  image {idx} dl failed: {e}"); return False
    vf = (f"scale={VIDEO_W}:{VIDEO_H}:force_original_aspect_ratio=decrease,"
          f"pad={VIDEO_W}:{VIDEO_H}:(ow-iw)/2:(oh-ih)/2,setsar=1")
    chain = [vf] + _overlay_codepct(disc, code, font)
    try:
        subprocess.run([ffmpeg, "-y"] + _FF_LOG + ["-loop", "1", "-i", ip,
            "-vf", ",".join(chain), "-r", str(FPS), "-t", f"{secs:.2f}", "-pix_fmt", "yuv420p"]
            + _FF_ENCODE + ["-an", dst], check=True, timeout=90)
        return True
    except Exception as e:
        log(f"  image seg {idx} failed: {e}"); return False

def seg_from_crop(shot, box, dst, td, ffmpeg, img_w, img_h, disc, code, font, banner, idx, secs,
                  zoom_mult=6.0, win_min=900, win_max=1500):
    """Crop a tight 9:16 window around a page region (e.g. reviews), gentle zoom, overlays."""
    if not box: return False
    win_h = float(min(max(box["h"] * zoom_mult, win_min), min(img_h, win_max)))
    win_w = win_h * VIDEO_W / VIDEO_H
    if win_w > img_w:
        win_w = float(img_w); win_h = win_w * VIDEO_H / VIDEO_W
    cx, cy = box["x"] + box["w"]/2, box["y"] + box["h"]/2
    x = min(max(cx - win_w/2, 0), img_w - win_w)
    y = min(max(cy - win_h/2, 0), img_h - win_h)
    frames = max(2, int(secs * FPS))
    up_w, up_h = VIDEO_W*2, VIDEO_H*2
    chain = [f"crop={int(win_w)}:{int(win_h)}:{int(x)}:{int(y)}",
             f"scale={up_w}:{up_h}:flags=bicubic",
             f"zoompan=z='min(1+0.05*on/{frames},1.05)':d={frames}"
             f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={VIDEO_W}x{VIDEO_H}:fps={FPS}",
             "setsar=1"]
    if banner and font:
        chain.append(f"drawtext=fontfile='{font}':expansion=none:text='{_esc(banner)}':x=(w-text_w)/2:y=h*0.10:"
                     f"fontsize=46:fontcolor=white:box=1:boxcolor=0x008000@0.85:boxborderw=14:"
                     f"shadowcolor=black@0.8:shadowx=2:shadowy=2")
    chain += _overlay_codepct(disc, code, font)
    try:
        subprocess.run([ffmpeg, "-y"] + _FF_LOG + ["-loop", "1", "-i", shot,
            "-vf", ",".join(chain), "-r", str(FPS), "-t", f"{secs:.2f}", "-pix_fmt", "yuv420p"]
            + _FF_ENCODE + ["-an", dst], check=True, timeout=120)
        return True
    except Exception as e:
        log(f"  crop seg {idx} failed: {e}"); return False


def seg_price(shot, box, dst, td, ffmpeg, img_w, img_h, disc, code, font, old_txt, new_txt, secs,
              title_box=None):
    """Price frame: zoomed-out page (title + price + variations for context) with a
    RELIABLE 'WAS $x  ✗   NOW $y' overlay drawn at a fixed spot — the WAS is struck
    through and the NOW flashes. Independent of where the page price sits (works even
    for variant-priced products)."""
    if not box: return False
    # Zoomed-out context window: from the title down past the price.
    if title_box and title_box["y"] < box["y"]:
        top    = max(title_box["y"] - 50, 0)
        bottom = box["y"] + box["h"] + 420
    else:
        top    = max(box["y"] - 650, 0)
        bottom = box["y"] + box["h"] + 350
    win_h = float(min(max(bottom - top, 1100), img_h))
    win_w = win_h * VIDEO_W / VIDEO_H
    if win_w > img_w:
        win_w = float(img_w); win_h = win_w * VIDEO_H / VIDEO_W
    cx = box["x"] + box["w"]/2
    cy = (top + bottom) / 2.0
    x = min(max(cx - win_w/2, 0), img_w - win_w)
    y = min(max(cy - win_h/2, 0), img_h - win_h)
    chain = [f"crop={int(win_w)}:{int(win_h)}:{int(x)}:{int(y)}",
             f"scale={VIDEO_W}:{VIDEO_H}:flags=bicubic", "setsar=1"]

    # ── Reliable WAS ✗ / NOW (flashing) overlay at a fixed position ──
    if font and (old_txt or new_txt):
        yW = int(VIDEO_H * 0.40)      # WAS line
        yN = int(VIDEO_H * 0.475)     # NOW line
        if old_txt:
            F = 52
            est = int(len(old_txt) * F * 0.52)          # estimated text width
            sx0 = (VIDEO_W - est) // 2
            chain.append(f"drawtext=fontfile='{font}':expansion=none:text='WAS {_esc(old_txt)}':"
                         f"x=(w-text_w)/2:y={yW}:fontsize={F}:fontcolor=white:"
                         f"box=1:boxcolor=black@0.55:boxborderw=10")
            # red strike across the WAS text
            chain.append(f"drawbox=x={sx0-30}:y={yW + F//2 + 4}:w={est+60}:h=9:color=red@0.95:t=fill")
        if new_txt:
            # base NOW (always visible) + blinking yellow copy on top
            chain.append(f"drawtext=fontfile='{font}':expansion=none:text='NOW {_esc(new_txt)}':"
                         f"x=(w-text_w)/2:y={yN}:fontsize=84:fontcolor=white:"
                         f"box=1:boxcolor=0x008000@0.95:boxborderw=16:"
                         f"shadowcolor=black@0.8:shadowx=2:shadowy=2")
            chain.append(f"drawtext=fontfile='{font}':expansion=none:text='NOW {_esc(new_txt)}':"
                         f"x=(w-text_w)/2:y={yN-3}:fontsize=90:fontcolor=yellow:"
                         f"enable='lt(mod(t\\,0.8)\\,0.4)':shadowcolor=black@0.9:shadowx=2:shadowy=2")
    chain += _overlay_codepct(disc, code, font)
    try:
        subprocess.run([ffmpeg, "-y"] + _FF_LOG + ["-loop", "1", "-i", shot,
            "-vf", ",".join(chain), "-r", str(FPS), "-t", f"{secs:.2f}", "-pix_fmt", "yuv420p"]
            + _FF_ENCODE + ["-an", dst], check=True, timeout=120)
        return True
    except Exception as e:
        log(f"  price seg failed: {e}"); return False

def gen_beats(duration, td, ffmpeg):
    out = os.path.join(td, "beats.aac")
    try:
        beat = 0.5  # 120 bpm
        kicks = "+".join(f"sine=f=60:d=0.08:delay={i*beat:.3f}" for i in range(int(duration/beat)+2))
        subprocess.run([ffmpeg, "-y"] + _FF_LOG + [
            "-f", "lavfi", "-i", f"aevalsrc='{kicks}':s=44100:c=mono",
            "-af", f"volume=0.18,afade=t=out:st={max(0,duration-1):.1f}:d=1",
            "-t", str(duration), "-c:a", "aac", "-b:a", "96k", out], check=True, timeout=60)
        return out
    except Exception:
        out = os.path.join(td, "sil.aac")
        subprocess.run([ffmpeg, "-y"] + _FF_LOG + ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
            "-t", str(duration), "-c:a", "aac", out], check=True, capture_output=True)
        return out

# ─── REUSABLE BUILDER ──────────────────────────────────────────────────────────
def build_concept_video(p, keys, ffmpeg, font, td, vo_text=None, tld="com"):
    """Build the v4 concept reel and return the LOCAL mp4 path (no upload).

    p: dict with title, asin, price, code, disc, expiry, cover, and either
       'aff_link' or 'link'. vo_text = the VO script (Col O / piece 1); if None,
       it's generated. Used by both this test and the production publisher.
    """
    new_v = _num(p["price"]); pct = _num(p["disc"])
    old_v = (new_v / (1 - pct/100)) if (new_v and pct and pct < 100) else None

    if not vo_text:
        vo_text, _piece2, _expd = build_fb_pieces(p, keys)
    log(f"  VO text: {vo_text[:90]}…")

    imgs, shot, pw, ph, price_box, rev_box, title_box, _social = capture_page(
        p["asin"], td, ffmpeg, tld=tld)
    if p.get("cover"):
        imgs = [p["cover"]] + [u for u in imgs if u != p["cover"]]
    if not imgs:
        log("  ⚠️ no product images"); return None

    # VO FIRST so frames are sized to it (no freezing)
    vo_wav = os.path.join(td, "vo.wav"); vo_aac = os.path.join(td, "vo.aac")
    wav = gemini_tts(vo_text, keys)
    have_vo, vo_dur = False, 0.0
    if wav:
        open(vo_wav, "wb").write(wav)
        try:
            subprocess.run([ffmpeg, "-y"] + _FF_LOG + ["-i", vo_wav, "-c:a", "aac", "-b:a", "128k", vo_aac],
                           check=True, timeout=60)
            vo_dur = probe_duration(vo_aac, ffmpeg); have_vo = vo_dur > 0
            log(f"  ✓ VO {vo_dur:.1f}s")
        except Exception as e:
            log(f"  VO convert failed: {e}")
    else:
        log("  ⚠️ Gemini TTS unavailable — beats only")

    plan = [("image", imgs[0], 1.5)]
    if len(imgs) > 1:      plan.append(("image", imgs[1], 1.0))
    if shot and price_box: plan.append(("price", None, 2.6))
    if shot and rev_box:   plan.append(("reviews", None, 1.5))
    for u in imgs[2:6]:    plan.append(("image", u, 1.0))

    target = (vo_dur + 0.4) if have_vo else 16.0
    PRICE_CAP, MIN_SECS = 6.5, 1.8
    tw = sum(w for _, _, w in plan) or 1
    durs = [target * w / tw for _, _, w in plan]
    for i, (kind, _, _) in enumerate(plan):
        if kind == "price" and durs[i] > PRICE_CAP:
            excess = durs[i] - PRICE_CAP; durs[i] = PRICE_CAP
            others = [j for j in range(len(plan)) if j != i]
            ow = sum(plan[j][2] for j in others) or 1
            for j in others: durs[j] += excess * plan[j][2] / ow
    durs = [max(d, MIN_SECS) for d in durs]

    segs = []
    for i, (kind, payload, _) in enumerate(plan):
        s = os.path.join(td, f"f{i}.mp4"); secs = durs[i]; ok = False
        if kind == "image":
            ok = seg_from_image(payload, s, td, ffmpeg, p["disc"], p["code"], font, i, secs)
        elif kind == "price":
            old_txt = f"${old_v:.2f}" if old_v else ""
            new_txt = f"${new_v:.2f}" if new_v else (p["price"] or "")
            ok = seg_price(shot, price_box, s, td, ffmpeg, pw, ph, p["disc"], p["code"],
                           font, old_txt, new_txt, secs, title_box=title_box)
        elif kind == "reviews":
            ok = seg_from_crop(shot, rev_box, s, td, ffmpeg, pw, ph, p["disc"], p["code"],
                               font, "LOVED BY REAL BUYERS", i, secs,
                               zoom_mult=6.0, win_min=950, win_max=1500)
        if ok: segs.append(s); log(f"  ✓ {kind} ({secs:.1f}s)")
    if not segs:
        log("  ⚠️ no frames built"); return None

    listf = os.path.join(td, "list.txt"); concat = os.path.join(td, "concat.mp4")
    with open(listf, "w") as f:
        for s in segs: f.write(f"file '{Path(s).as_posix()}'\n")
    subprocess.run([ffmpeg, "-y"] + _FF_LOG + ["-f", "concat", "-safe", "0", "-i", listf,
                    "-c", "copy", concat], check=True, timeout=120)
    vid_dur = probe_duration(concat, ffmpeg)

    out = os.path.join(td, "final.mp4")
    if have_vo:
        tail = max(0.0, vid_dur - vo_dur)
        if tail < 1.0:
            subprocess.run([ffmpeg, "-y"] + _FF_LOG + ["-i", concat, "-i", vo_aac,
                "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-shortest", out], check=True, timeout=120)
        else:
            beats = gen_beats(tail, td, ffmpeg)
            mixed = os.path.join(td, "mix.aac"); dly = int(vo_dur*1000)
            subprocess.run([ffmpeg, "-y"] + _FF_LOG + ["-i", vo_aac, "-i", beats,
                "-filter_complex", f"[1]adelay={dly}|{dly}[b];[0][b]amix=inputs=2:duration=longest[a]",
                "-map", "[a]", "-c:a", "aac", "-b:a", "128k", mixed], check=True, timeout=120)
            subprocess.run([ffmpeg, "-y"] + _FF_LOG + ["-i", concat, "-i", mixed,
                "-c:v", "copy", "-c:a", "aac", "-shortest", out], check=True, timeout=120)
    else:
        beats = gen_beats(vid_dur, td, ffmpeg)
        subprocess.run([ffmpeg, "-y"] + _FF_LOG + ["-i", concat, "-i", beats,
            "-c:v", "copy", "-c:a", "aac", "-shortest", out], check=True, timeout=120)
    log(f"  ✓ final {probe_duration(out, ffmpeg):.1f}s ({os.path.getsize(out):,} bytes)")
    return out


# ─── MAIN (test harness) ───────────────────────────────────────────────────────
def main():
    log("=== reel_concept_test.py starting ===")
    keys, ffmpeg, font = _read_gemini_keys(), _which_ffmpeg(), _find_font()
    log(f"Gemini keys: {len(keys)} | ffmpeg: {ffmpeg} | font: {'yes' if font else 'no'}")
    with tempfile.TemporaryDirectory(prefix="reel_concept_") as td:
        log("\n[1] Picking product…")
        p = pick_product()
        if not p: raise RuntimeError("No product found in Sheet1")
        log(f"  {p['title'][:70]} | ASIN {p['asin']} | {p['disc']} off | {p['price']}")
        out = build_concept_video(p, keys, ffmpeg, font, td)
        if not out: raise RuntimeError("build failed")
        url = cloud_upload(out, f"{CLOUD_FOLDER}/reel_{p['asin']}_{int(time.time())}")
        log("\n" + "=" * 60 + f"\n  REEL : {url or '⚠️ upload failed'}\n" + "=" * 60)
    log("=== reel_concept_test.py done ===")


if __name__ == "__main__":
    main()
