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
        title = row[8].strip()       # I Product
        asin  = row[10].strip()      # K PID
        price = row[9].strip()       # J Price
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
def gemini_text(prompt: str, keys: list, max_tokens: int = 350) -> str:
    for key in keys:
        try:
            r = requests.post(
                "https://generativelanguage.googleapis.com/v1beta/models/"
                f"gemini-2.0-flash:generateContent?key={key}",
                json={"contents": [{"parts": [{"text": prompt}]}],
                      "generationConfig": {"temperature": 0.9, "maxOutputTokens": max_tokens}},
                timeout=30)
            if r.ok:
                return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            log(f"  Gemini {r.status_code}: {r.text[:120]}")
        except Exception as e:
            log(f"  Gemini error: {e}")
    return ""

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
    return scenario, script, expd

# ─── 3. SCROLLING/ZOOMING AMAZON PAGE ─────────────────────────────────────────
def record_amazon_page(asin: str, td: str, ffmpeg: str, seconds: float, font: str):
    """Headless Chrome full-page screenshot of the mobile Amazon page, then ffmpeg
    pans/zooms down it to create a vertical scrolling background video."""
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
    except ImportError:
        log("  selenium missing"); return None

    binary, driver_bin = _chrome_bits()
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--hide-scrollbars")
    opts.add_argument("--lang=en-US")
    # Mobile emulation → narrow, tall page that suits vertical scroll
    mobile_ua = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                 "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
                 "Mobile/15E148 Safari/604.1")
    opts.add_argument(f"--user-agent={mobile_ua}")
    opts.add_argument("--window-size=440,900")
    if binary: opts.binary_location = binary

    service = Service(executable_path=driver_bin) if driver_bin else Service()
    driver  = webdriver.Chrome(service=service, options=opts)

    shot = os.path.join(td, "page_full.png")
    try:
        url = f"https://www.amazon.com/dp/{asin}?th=1&psc=1"
        log(f"  Loading {url}")
        driver.set_window_size(440, 900)
        driver.get(url)
        time.sleep(5)

        page_txt = (driver.page_source or "").lower()
        if "robot check" in page_txt or "captchacharacters" in page_txt or "type the characters" in page_txt:
            log("  ⚠️ Amazon served a captcha/robot page — screenshot will show it")

        # Full-page screenshot via CDP
        metrics = driver.execute_cdp_cmd("Page.getLayoutMetrics", {})
        width  = math.ceil(metrics["cssContentSize"]["width"])
        height = min(math.ceil(metrics["cssContentSize"]["height"]), 9000)
        driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {
            "mobile": True, "width": width, "height": height,
            "deviceScaleFactor": 2, "screenWidth": width, "screenHeight": height})
        result = driver.execute_cdp_cmd("Page.captureScreenshot", {
            "captureBeyondViewport": True, "fromSurface": True, "format": "png"})
        with open(shot, "wb") as f:
            f.write(base64.b64decode(result["data"]))
        log(f"  ✓ Full-page screenshot saved ({os.path.getsize(shot):,} bytes)")
    except Exception as e:
        log(f"  page capture failed: {e}")
        try: driver.quit()
        except Exception: pass
        return None
    finally:
        try: driver.quit()
        except Exception: pass

    # ffmpeg: scale screenshot to target width, slow zoom-in, pan top→bottom
    page_vid = os.path.join(td, "page_scroll.mp4")
    try:
        # Scale to a bit wider than target so a slight zoom always has pixels
        scaled_w = int(VIDEO_W * 1.12)
        # crop window pans from top to bottom across the scaled-height image
        crop_x = f"(iw-{VIDEO_W})/2"
        crop_y = f"(ih-{VIDEO_H})*min(1\\,t/{max(0.1, seconds-0.5):.2f})"
        vf = (f"scale={scaled_w}:-1,"
              f"crop={VIDEO_W}:{VIDEO_H}:{crop_x}:'{crop_y}',"
              f"setsar=1")
        subprocess.run([ffmpeg, "-y"] + _FF_LOG + [
            "-loop", "1", "-t", f"{seconds:.2f}", "-i", shot,
            "-vf", vf, "-r", str(FPS), "-pix_fmt", "yuv420p",
        ] + _FF_ENCODE + ["-an", page_vid], check=True, timeout=180)
        log(f"  ✓ Scrolling page video: {seconds:.1f}s")
        return page_vid
    except Exception as e:
        log(f"  scroll video build failed: {e}"); return None

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
                seconds = max(8.0, probe_duration(tmp, ffmpeg) or 25.0)
            except Exception:
                seconds = 25.0
        else:
            seconds = 25.0
        log(f"[3] Recording scrolling/zooming Amazon page ({seconds:.0f}s)…")

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
