#!/usr/bin/env python3
"""
avatar_poc.py — Proof of concept for the "recognizable persona over a scrolling
Amazon page" video format.

Pipeline:
  1. Read Sheet1, pick the highest-commission-potential product
  2. Gemini 2.0 Flash → avatar SCENARIO (scene/persona brief) + VO SCRIPT
  3. Headless Chrome → full-page screenshot of the real Amazon mobile page
     → ffmpeg pans/zooms down it = the scrolling/zooming background video
  4. Stitch:
       - If AVATAR_CLIP_URL is provided (a green-screen avatar clip you made in
         CapCut/HeyGen that speaks the VO script): chroma-key it, overlay as a
         lower-third on the scrolling page, use the avatar's audio → final reel
       - Else: output the scrolling-page video + the scenario/script so you can
         generate the avatar clip, then re-run with AVATAR_CLIP_URL to stitch
  5. Upload all artifacts to Cloudinary and print the URLs

Standalone — touches nothing in the production pipeline.

Env / inputs:
  SECRETS_DIR        — folder with credential files (CI copies them here)
  AVATAR_CLIP_URL    — optional URL to a green-screen avatar clip to overlay
  PAGE_SECONDS       — optional override for scroll duration (default 25, or the
                       avatar clip's duration when AVATAR_CLIP_URL is given)
"""

import base64
import hashlib
import json
import math
import os
import re
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
CLOUD_FOLDER      = "vipon_avatar_poc"
AVATAR_CLIP_URL   = os.environ.get("AVATAR_CLIP_URL", "").strip()
PAGE_SECONDS_ENV  = os.environ.get("PAGE_SECONDS", "").strip()

# Amazon categories that pay higher affiliate commission — used to score products
_HIGH_COMMISSION_KEYWORDS = {
    # beauty / personal care (up to ~10%)
    "beauty", "skin", "serum", "cream", "facial", "makeup", "cosmetic", "lipstick",
    "fragrance", "perfume", "hair", "shampoo", "moisturizer", "lotion", "nail",
    # fashion / jewelry / watches
    "jewelry", "necklace", "ring", "bracelet", "earring", "watch", "handbag",
    "purse", "wallet", "scarf", "dress", "boots",
    # home / kitchen / furniture
    "kitchen", "home", "furniture", "decor", "bedding", "comforter", "rug",
    "cookware", "knife", "blender", "vacuum", "lamp", "organizer",
}

_FF_LOG    = ["-loglevel", "error", "-hide_banner"]
_FF_ENCODE = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "26", "-threads", "2"]

def log(m): print(m, flush=True)

# ─── CREDENTIALS / TOOLS ──────────────────────────────────────────────────────
def _read_gemini_keys():
    pro = os.path.expanduser("~/geminipro.txt")
    keys = []
    if os.path.exists(pro):
        k = open(pro).read().strip()
        if k: keys.append(k)
    multi = os.path.expanduser("~/geminikeys.txt")
    if os.path.exists(multi):
        keys += [l.strip() for l in open(multi) if l.strip() and not l.startswith("#")]
    single = os.path.expanduser("~/geminikey.txt")
    if os.path.exists(single):
        k = open(single).read().strip()
        if k and k not in keys: keys.append(k)
    return keys

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

# ─── 1. PRODUCT SELECTION ─────────────────────────────────────────────────────
def pick_product():
    """Pick the product with the highest commission potential from Sheet1.
    Score = price (AOV proxy) × category multiplier (high-commission categories).
    """
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDS_FILE, scope)
    ws    = gspread.authorize(creds).open(GOOGLE_SHEET_NAME).sheet1
    rows  = ws.get_all_values()

    best, best_score = None, -1.0
    for row in rows[1:]:
        if len(row) < 14: continue
        title    = row[8].strip()       # I Product
        price    = row[9].strip()       # J Price
        aff_link = row[0].strip()       # A Link (carries the real ASIN)
        # Column K is the vipon PID for scraped rows, NOT the Amazon ASIN.
        # The reliable ASIN is the asin= param inside the affiliate link.
        m_asin = re.search(r"asin=([A-Za-z0-9]{10})", aff_link, re.I)
        if not m_asin:
            m_asin = re.search(r"\b(B0[A-Z0-9]{8})\b", aff_link, re.I)
        asin = m_asin.group(1).upper() if m_asin else ""
        if not title or not asin:
            continue
        m = re.search(r"(\d+(?:\.\d+)?)", price.replace(",", ""))
        price_val = float(m.group(1)) if m else 0.0
        t_low = title.lower()
        cat_hit = next((kw for kw in _HIGH_COMMISSION_KEYWORDS if kw in t_low), None)
        multiplier = 2.5 if cat_hit else 1.0
        score = price_val * multiplier
        if score > best_score:
            best_score = score
            best = {
                "title": title, "asin": asin, "price": price,
                "code": row[5].strip(), "disc": row[6].strip(),
                "expiry": row[7].strip(), "aff_link": row[0].strip(),
                "image": row[11].strip() if len(row) > 11 else "",
                "category_hit": cat_hit, "score": round(score, 2),
            }
    return best

# ─── EXPIRY → FRIENDLY DATE ────────────────────────────────────────────────────
def friendly_date(expiry: str) -> str:
    if not expiry: return ""
    dm = re.search(r"(\d{1,2})[/\-](\d{1,2})[/\-](\d{2,4})", expiry)
    if dm:
        try:
            a, b, c = int(dm.group(1)), int(dm.group(2)), int(dm.group(3))
            y = 2000 + c if c < 100 else c
            return f"{datetime(y, a, b).strftime('%B')} {datetime(y, a, b).day}"
        except Exception: pass
    for unit, kw in (("days", "day"), ("hours", "hour")):
        m = re.search(rf"(\d+)\s*{kw}", expiry, re.I)
        if m:
            delta = timedelta(**{unit: int(m.group(1))})
            d = datetime.now() + delta
            return f"{d.strftime('%B')} {d.day}"
    return expiry

# ─── 2. GEMINI: SCENARIO + SCRIPT ─────────────────────────────────────────────
# Try several models — each has a separate free-tier quota pool, so a 429 on one
# may still succeed on another.
_TEXT_MODELS = ["gemini-2.0-flash", "gemini-2.5-flash", "gemini-1.5-flash", "gemini-1.5-flash-8b"]

def gemini_text(prompt: str, keys: list, max_tokens: int = 350) -> str:
    for model in _TEXT_MODELS:
        for key in keys:
            try:
                r = requests.post(
                    "https://generativelanguage.googleapis.com/v1beta/models/"
                    f"{model}:generateContent?key={key}",
                    json={"contents": [{"parts": [{"text": prompt}]}],
                          "generationConfig": {"temperature": 0.9, "maxOutputTokens": max_tokens}},
                    timeout=30)
                if r.ok:
                    return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                # 429 → try next key/model silently; log other errors once
                if r.status_code != 429:
                    log(f"  {model} {r.status_code}: {r.text[:100]}")
            except Exception as e:
                log(f"  {model} error: {e}")
    log("  ⚠️ All Gemini models/keys exhausted — using template fallback")
    return ""

def _fallback_scenario(p):
    return ("A warm, upbeat female creator, filmed selfie-style, gesturing toward the "
            "screen behind her. As the page tours each spot she points to it — the rating "
            "stars, the 'bought last month' badge, the reviews — friendly and trustworthy, "
            "with a touch of genuine excitement.")

def _fallback_script(p, expd):
    name = p["title"].split(",")[0].strip()
    disc = f"{p['disc']} off" if p["disc"] else "a great deal"
    code = f"Use code {p['code']} at checkout. " if p["code"] else ""
    return (f"Okay, I did not expect to love this {name} as much as I do. Look at these "
            f"reviews — people are obsessed, and thousands sold just last month. Right now "
            f"it is {disc}, down to just {p['price']}. {code}But that price only lasts until "
            f"{expd}, so do not sit on this one. Go grab it before it is gone.")

def build_scenario(p, keys):
    expd = friendly_date(p["expiry"])
    scenario = gemini_text(
        f'Product: "{p["title"]}"\n\n'
        "Write a SHORT creative brief (3-4 sentences) for a female AI presenter "
        "who will appear as a lower-third overlay while an Amazon product page "
        "scrolls behind her. Describe: her vibe/energy, what she's doing with her "
        "hands or expression, and the single emotional angle she'll sell this "
        "product on. Keep it natural and relatable, not salesy. No script yet.",
        keys, max_tokens=200)

    code_line = (f"Tell viewers to use code {p['code']} at checkout."
                 if p["code"] else "No code needed — just the link.")
    script = gemini_text(
        f'Product: "{p["title"]}"\n'
        f"Discount: {p['disc']} off | Price after discount: {p['price']} | "
        f"Deal ends: {expd}\n\n"
        "Write a 50-70 word first-person voiceover script for a female creator "
        "reviewing this as an Amazon find. Rules:\n"
        "- Open with a surprising/relatable hook, NOT 'Are you looking for'\n"
        "- Sound like a real person who genuinely uses it, warm and a little funny\n"
        "- Naturally mention what it is, why she loves it, the price and that the "
        f"deal ends {expd}\n"
        f"- {code_line}\n"
        "- Smooth sentences with commas (it will be read aloud). No hashtags, no "
        "'link in bio'. Return only the script.",
        keys, max_tokens=180)
    return (scenario or _fallback_scenario(p)), (script or _fallback_script(p, expd)), expd

# ─── 3. GUIDED-TOUR AMAZON PAGE (zoom/hover to key spots) ─────────────────────
# JS that locates the key elements on a desktop Amazon product page and returns
# their absolute page coordinates (CSS px). These become the camera's stops.
_ANCHOR_JS = r"""
function abs(el){
  if(!el) return null;
  const r = el.getBoundingClientRect();
  if(r.width < 8 || r.height < 4) return null;
  return {x: r.left + window.scrollX, y: r.top + window.scrollY,
          w: r.width, h: r.height};
}
function firstText(needle){
  needle = needle.toLowerCase();
  const els = document.querySelectorAll('span,div,a');
  for(const e of els){
    const t = (e.innerText||'').trim().toLowerCase();
    if(t && t.length < 70 && t.includes(needle)) return e;
  }
  return null;
}
const out = {};
out.image   = abs(document.querySelector('#landingImage')
              || document.querySelector('#imgTagWrapperId img')
              || document.querySelector('#main-image-container img'));
out.rating  = abs(document.querySelector('#acrPopover')
              || document.querySelector('#averageCustomerReviews')
              || document.querySelector("[data-hook='rating-out-of-text']"));
out.count   = abs(document.querySelector('#acrCustomerReviewText'));
out.bought  = abs(firstText('bought in past'));
out.price   = abs(document.querySelector('.priceToPay')
              || document.querySelector('#corePriceDisplay_desktop_feature_div')
              || document.querySelector('#price'));
out.reviews = abs(document.querySelector('#reviewsMedley')
              || document.querySelector("[data-hook='review']")
              || document.querySelector('#cm-cr-dp-review-list'));
return out;
"""

# Order the camera visits its stops, with how tall a window to frame each
# (smaller window = tighter zoom). Height is in source px.
_SHOT_PLAN = [
    ("image",   1100),   # the product, generous frame
    ("price",    520),   # zoom to the deal price
    ("bought",   360),   # tight zoom on "10K+ bought last month"
    ("rating",   360),   # tight zoom on the stars
    ("count",    420),   # "12,394 ratings"
    ("reviews",  900),   # a written review
]

def record_amazon_page(asin: str, td: str, ffmpeg: str, seconds: float, font: str):
    """Load the desktop Amazon page, find key elements, and build a 'guided tour'
    video that cuts/zooms from the product image to the price, the bought-count,
    the star rating, and the reviews — like a camera hovering over each spot."""
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
    except ImportError:
        log("  selenium missing"); return None

    binary, driver_bin = _chrome_bits()
    opts = Options()
    for a in ("--headless=new", "--no-sandbox", "--disable-dev-shm-usage",
              "--disable-gpu", "--hide-scrollbars", "--lang=en-US",
              "--window-size=1280,1400"):
        opts.add_argument(a)
    opts.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    if binary: opts.binary_location = binary
    service = Service(executable_path=driver_bin) if driver_bin else Service()
    driver  = webdriver.Chrome(service=service, options=opts)

    shot = os.path.join(td, "page_full.png")
    anchors, img_w, img_h = {}, VIDEO_W, 4000
    try:
        url = f"https://www.amazon.com/dp/{asin}?th=1&psc=1"
        log(f"  Loading desktop {url}")
        driver.set_window_size(1280, 1400)
        driver.get(url)
        time.sleep(5)
        # nudge lazy-loaded sections (reviews) into the DOM
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight*0.6);")
        time.sleep(1.5)
        driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(0.8)

        page_txt = (driver.page_source or "").lower()
        if "robot check" in page_txt or "captchacharacters" in page_txt:
            log("  ⚠️ Amazon served a captcha page — tour anchors will be missing")

        try:
            anchors = {k: v for k, v in (driver.execute_script(_ANCHOR_JS) or {}).items() if v}
        except Exception as e:
            log(f"  anchor lookup failed: {e}")
        log(f"  Found anchors: {', '.join(anchors.keys()) or 'none'}")

        metrics = driver.execute_cdp_cmd("Page.getLayoutMetrics", {})
        img_w = math.ceil(metrics["cssContentSize"]["width"])
        img_h = min(math.ceil(metrics["cssContentSize"]["height"]), 7000)
        driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {
            "mobile": False, "width": img_w, "height": img_h,
            "deviceScaleFactor": 1, "screenWidth": img_w, "screenHeight": img_h})
        result = driver.execute_cdp_cmd("Page.captureScreenshot", {
            "captureBeyondViewport": True, "fromSurface": True, "format": "png"})
        with open(shot, "wb") as f:
            f.write(base64.b64decode(result["data"]))
        log(f"  ✓ Desktop screenshot {img_w}x{img_h} ({os.path.getsize(shot):,} bytes)")
    except Exception as e:
        log(f"  page capture failed: {e}")
        try: driver.quit()
        except Exception: pass
        return None
    finally:
        try: driver.quit()
        except Exception: pass

    # Decide the camera stops (only those found, in plan order)
    stops = [(name, anchors[name], win) for name, win in _SHOT_PLAN
             if name in anchors and anchors[name]["y"] < img_h - 40]
    if not stops:
        log("  No anchors found — falling back to a simple eased scroll")
        return _fallback_scroll(shot, td, ffmpeg, seconds)

    per = max(2.2, seconds / len(stops))
    log(f"  Guided tour: {len(stops)} stops × {per:.1f}s "
        f"({', '.join(n for n, _, _ in stops)})")

    seg_paths = []
    for i, (name, a, win_h) in enumerate(stops):
        seg = _build_zoom_shot(shot, td, ffmpeg, i, a, win_h, per, img_w, img_h)
        if seg: seg_paths.append(seg)
    if not seg_paths:
        return _fallback_scroll(shot, td, ffmpeg, seconds)

    # concat the shots
    page_vid = os.path.join(td, "page_tour.mp4")
    listf = os.path.join(td, "tour_list.txt")
    with open(listf, "w") as f:
        for s in seg_paths:
            f.write(f"file '{Path(s).as_posix()}'\n")
    try:
        subprocess.run([ffmpeg, "-y"] + _FF_LOG +
                       ["-f", "concat", "-safe", "0", "-i", listf,
                        "-c", "copy", page_vid], check=True, timeout=120)
        log(f"  ✓ Guided-tour video: {probe_duration(page_vid, ffmpeg):.1f}s")
        return page_vid
    except Exception as e:
        log(f"  tour concat failed: {e}")
        return seg_paths[0]


def _build_zoom_shot(shot, td, ffmpeg, idx, a, win_h, secs, img_w, img_h):
    """One camera stop: frame a 9:16 window around the element, scaled to fill,
    with a slow push-in (zoom) for life."""
    cx = a["x"] + a["w"] / 2.0
    cy = a["y"] + a["h"] / 2.0
    win_h = float(min(win_h, img_h))
    win_w = win_h * VIDEO_W / VIDEO_H
    if win_w > img_w:                      # element wider than a 9:16 slice
        win_w = float(img_w)
        win_h = win_w * VIDEO_H / VIDEO_W
    x = min(max(cx - win_w / 2, 0), img_w - win_w)
    y = min(max(cy - win_h / 2, 0), img_h - win_h)
    seg = os.path.join(td, f"shot_{idx}.mp4")
    # crop the framed window, scale to target, then a gentle 1.0→1.08 push-in
    vf = (f"crop={int(win_w)}:{int(win_h)}:{int(x)}:{int(y)},"
          f"scale={VIDEO_W}:{VIDEO_H},"
          f"zoompan=z='min(zoom+0.0009,1.08)':d={int(secs*FPS)}"
          f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={VIDEO_W}x{VIDEO_H}:fps={FPS},"
          f"setsar=1")
    try:
        subprocess.run([ffmpeg, "-y"] + _FF_LOG + [
            "-loop", "1", "-t", f"{secs:.2f}", "-i", shot,
            "-vf", vf, "-r", str(FPS), "-pix_fmt", "yuv420p",
        ] + _FF_ENCODE + ["-an", seg], check=True, timeout=90)
        return seg
    except Exception as e:
        log(f"  shot {idx} ({int(win_w)}x{int(win_h)}) failed: {e}")
        return None


def _fallback_scroll(shot, td, ffmpeg, seconds):
    """Eased top→bottom scroll — used only when no anchors are found."""
    page_vid = os.path.join(td, "page_scroll.mp4")
    hold = 1.0
    span = max(0.1, seconds - hold)
    prog = f"max(0\\,(t-{hold:.2f}))/{span:.2f}"
    ease = f"(1-cos(PI*min(1\\,{prog})))/2"
    vf = (f"scale={VIDEO_W}:-1,"
          f"crop={VIDEO_W}:{VIDEO_H}:(iw-{VIDEO_W})/2:'(ih-{VIDEO_H})*{ease}',setsar=1")
    try:
        subprocess.run([ffmpeg, "-y"] + _FF_LOG + [
            "-loop", "1", "-t", f"{seconds:.2f}", "-i", shot,
            "-vf", vf, "-r", str(FPS), "-pix_fmt", "yuv420p",
        ] + _FF_ENCODE + ["-an", page_vid], check=True, timeout=180)
        return page_vid
    except Exception as e:
        log(f"  fallback scroll failed: {e}"); return None

# ─── HELPERS ──────────────────────────────────────────────────────────────────
def probe_duration(path: str, ffmpeg: str) -> float:
    try:
        r = subprocess.run([ffmpeg, "-i", path], stderr=subprocess.PIPE, stdout=subprocess.PIPE)
        m = re.search(r"Duration: (\d+):(\d+):(\d+\.?\d*)", r.stderr.decode())
        if m: return int(m.group(1))*3600 + int(m.group(2))*60 + float(m.group(3))
    except Exception: pass
    return 0.0

def cloud_upload(path: str, public_id: str, kind: str = "video") -> str:
    cn, ak, asec = _load_cloudinary()
    ts  = int(time.time())
    sig = hashlib.sha1(f"public_id={public_id}&timestamp={ts}{asec}".encode()).hexdigest()
    with open(path, "rb") as f:
        r = requests.post(f"https://api.cloudinary.com/v1_1/{cn}/{kind}/upload",
                          data={"public_id": public_id, "timestamp": ts,
                                "api_key": ak, "signature": sig},
                          files={"file": f}, timeout=300)
    return r.json().get("secure_url", "") if r.ok else ""

# ─── 4. STITCH ────────────────────────────────────────────────────────────────
def stitch(page_vid: str, avatar_url: str, td: str, ffmpeg: str, font: str,
           deal_text: str) -> str:
    """Overlay a green-screen avatar (lower-third) onto the scrolling page,
    using the avatar's own audio (the spoken VO)."""
    avatar_raw = os.path.join(td, "avatar_raw.mp4")
    try:
        r = requests.get(avatar_url, timeout=180); r.raise_for_status()
        with open(avatar_raw, "wb") as f: f.write(r.content)
    except Exception as e:
        log(f"  avatar download failed: {e}"); return ""

    out = os.path.join(td, "final.mp4")
    # chroma-key common green (0x00D400-ish range covered by similarity), scale to
    # ~58% width, sit bottom-centre as a lower-third; keep avatar audio
    fc = (
        "[1:v]chromakey=0x00FF00:0.30:0.10,"
        f"scale={int(VIDEO_W*0.58)}:-1[av];"
        "[0:v][av]overlay=(W-w)/2:H-h-40[outv]"
    )
    try:
        subprocess.run([ffmpeg, "-y"] + _FF_LOG + [
            "-i", page_vid, "-i", avatar_raw,
            "-filter_complex", fc,
            "-map", "[outv]", "-map", "1:a?",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "24",
            "-c:a", "aac", "-b:a", "128k", "-shortest", out,
        ], check=True, timeout=240)
        log("  ✓ Stitched avatar overlay onto scrolling page")
        return out
    except Exception as e:
        log(f"  stitch failed: {e}"); return ""

# ─── MAIN ────────────────────────────────────────────────────────────────────
def main():
    log("=== avatar_poc.py starting ===")
    keys   = _read_gemini_keys()
    ffmpeg = _which_ffmpeg()
    font   = _find_font()
    log(f"Gemini keys: {len(keys)} | ffmpeg: {ffmpeg} | font: {'yes' if font else 'no'}")

    with tempfile.TemporaryDirectory(prefix="avatar_poc_") as td:
        # 1) Pick product
        log("\n[1] Picking high-commission product…")
        p = pick_product()
        if not p:
            raise RuntimeError("No usable product found in Sheet1")
        log(f"  Chosen: {p['title'][:80]}")
        log(f"  ASIN {p['asin']} | price {p['price']} | category match: "
            f"{p['category_hit'] or 'none'} | score {p['score']}")

        # 2) Scenario + script
        log("\n[2] Generating avatar scenario + VO script…")
        scenario, script, expd = build_scenario(p, keys)
        log("\n--- AVATAR SCENARIO ---")
        log(scenario or "(scenario generation failed)")
        log("\n--- VO SCRIPT (read this in CapCut/HeyGen) ---")
        log(script or "(script generation failed)")
        log("-----------------------\n")

        # Determine page scroll duration
        if PAGE_SECONDS_ENV:
            seconds = float(PAGE_SECONDS_ENV)
        elif AVATAR_CLIP_URL:
            # match the avatar clip length
            tmp = os.path.join(td, "probe.mp4")
            try:
                r = requests.get(AVATAR_CLIP_URL, timeout=180); r.raise_for_status()
                with open(tmp, "wb") as f: f.write(r.content)
                seconds = max(8.0, probe_duration(tmp, ffmpeg) or 18.0)
            except Exception:
                seconds = 18.0
        else:
            seconds = 18.0
        log(f"[3] Building guided-tour Amazon page video ({seconds:.0f}s)…")

        # 3) Scrolling page video
        page_vid = record_amazon_page(p["asin"], td, ffmpeg, seconds, font)
        if not page_vid:
            raise RuntimeError("Failed to build the scrolling page video")
        page_url = cloud_upload(page_vid, f"{CLOUD_FOLDER}/page_{p['asin']}_{int(time.time())}")
        log(f"  📄 Scrolling page video: {page_url}")

        # 4) Stitch (only if an avatar clip was provided)
        deal_text = f"{p['disc']} off · {p['price']} · ends {expd}"
        if AVATAR_CLIP_URL:
            log("\n[4] Stitching avatar overlay onto scrolling page…")
            final = stitch(page_vid, AVATAR_CLIP_URL, td, ffmpeg, font, deal_text)
            if final:
                final_url = cloud_upload(final, f"{CLOUD_FOLDER}/final_{p['asin']}_{int(time.time())}")
                log(f"\n✅ FINAL STITCHED REEL:\n{final_url}")
            else:
                log("\n⚠️ Stitch failed — see logs")
        else:
            log("\n[4] No AVATAR_CLIP_URL provided — POC stops at the page video.")
            log("    Next: make ONE green-screen avatar clip in CapCut/HeyGen using")
            log("    the VO SCRIPT above, upload it somewhere public, then re-run this")
            log("    workflow with AVATAR_CLIP_URL set to stitch the final reel.")

    log("=== avatar_poc.py done ===")


if __name__ == "__main__":
    main()
