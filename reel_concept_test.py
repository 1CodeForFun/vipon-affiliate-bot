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
const out = {images: [], price: null, reviews: null};
function abs(el){ if(!el) return null; const r=el.getBoundingClientRect();
  if(r.width<8||r.height<4) return null;
  return {x:r.left+window.scrollX, y:r.top+window.scrollY, w:r.width, h:r.height}; }
// gallery image URLs — ONLY from the product image gallery, to avoid ad/sponsor
// banners (e.g. "Fanka") that live elsewhere on the page.
const GAL = ['#main-image-container', '#imageBlock', '#altImages',
             '#ivThumbViewport', '#imageBlockThumbs', '#imgTagWrapperId'];
let imgEls = [];
for (const sel of GAL){ const c = document.querySelector(sel);
  if (c) imgEls = imgEls.concat([...c.querySelectorAll('img')]); }
if (imgEls.length === 0)
  imgEls = [...document.querySelectorAll('#landingImage, #imgTagWrapperId img')];
const seen = new Set();
imgEls.forEach(im=>{
  let s = im.getAttribute('data-old-hires') || im.src || '';
  const m = s.match(/images\/I\/([A-Za-z0-9%+._-]+)\./);
  if(m && s.includes('media-amazon') && !seen.has(m[1])){ seen.add(m[1]); out.images.push(s); }
});
// Tight price element (the actual price number) so the zoom + the strike align.
out.price   = abs(document.querySelector('.priceToPay')
            || document.querySelector('.a-price')
            || document.querySelector('#corePrice_feature_div')
            || document.querySelector('#price'));
out.reviews = abs(document.querySelector('#averageCustomerReviews')
            || document.querySelector('#acrPopover')
            || document.querySelector("[data-hook='review']"));
return out;
"""

def capture_page(asin, td, ffmpeg):
    """Return (gallery_image_urls, screenshot_path, img_w, img_h, price_box, reviews_box)."""
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
    except ImportError:
        log("  selenium missing"); return [], None, 0, 0, None, None
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
    imgs, pw, ph, price_box, rev_box = [], 0, 0, None, None
    try:
        driver.set_window_size(440, 950)
        driver.get(f"https://www.amazon.com/dp/{asin}?th=1&psc=1")
        time.sleep(5)
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight*0.55);"); time.sleep(1.3)
        driver.execute_script("window.scrollTo(0,0);"); time.sleep(0.7)
        data = driver.execute_script(_GALLERY_JS) or {}
        imgs = data.get("images", [])[:6]
        price_box, rev_box = data.get("price"), data.get("reviews")
        log(f"  gallery imgs: {len(imgs)} | price box: {'y' if price_box else 'n'} | reviews box: {'y' if rev_box else 'n'}")
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
        for b in (price_box, rev_box):
            if b:
                for k in b: b[k] *= 2
        log(f"  screenshot {pw}x{ph}")
    except Exception as e:
        log(f"  capture failed: {e}")
    finally:
        try: driver.quit()
        except Exception: pass
    return imgs, (shot if os.path.exists(shot) else None), pw, ph, price_box, rev_box

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


def seg_price(shot, box, dst, td, ffmpeg, img_w, img_h, disc, code, font, new_price, secs):
    """Price frame: tight static crop on the real price, a red strike aligned to the
    on-page (old) price, and the NEW price beside it with a blinking highlight."""
    if not box: return False
    # tight, readable window centred on the price element
    win_h = float(min(max(box["h"] * 3.5, 620), min(img_h, 1050)))
    win_w = win_h * VIDEO_W / VIDEO_H
    if win_w > img_w:
        win_w = float(img_w); win_h = win_w * VIDEO_H / VIDEO_W
    cx, cy = box["x"] + box["w"]/2, box["y"] + box["h"]/2
    x = min(max(cx - win_w/2, 0), img_w - win_w)
    y = min(max(cy - win_h/2, 0), img_h - win_h)
    # map the price box into the final 720x1280 frame (static crop → exact alignment)
    sx, sy = VIDEO_W / win_w, VIDEO_H / win_h
    px, py = (box["x"] - x) * sx, (box["y"] - y) * sy
    pw, ph = box["w"] * sx, box["h"] * sy
    chain = [f"crop={int(win_w)}:{int(win_h)}:{int(x)}:{int(y)}",
             f"scale={VIDEO_W}:{VIDEO_H}:flags=bicubic", "setsar=1"]
    # red strike through the old price, aligned to its bbox
    strike_y = int(py + ph/2 - 5)
    chain.append(f"drawbox=x={int(px)-6}:y={strike_y}:w={int(pw)+12}:h=10:color=red@0.95:t=fill")
    # NEW price — place to the right if there's room, else just below the old price
    nptxt = f"${_num(new_price):.2f}" if _num(new_price) else (new_price or "")
    if px + pw + 230 <= VIDEO_W:
        nx, ny = int(px + pw + 18), int(max(py - 6, 0))
    else:
        nx, ny = int(max(px, 12)), int(py + ph + 14)
    if font and nptxt:
        # base (always visible)
        chain.append(f"drawtext=fontfile='{font}':expansion=none:text='{_esc(nptxt)}':x={nx}:y={ny}:"
                     f"fontsize=72:fontcolor=white:box=1:boxcolor=0x008000@0.95:boxborderw=14:"
                     f"shadowcolor=black@0.8:shadowx=2:shadowy=2")
        # blinking emphasis copy on top (flash highlight)
        chain.append(f"drawtext=fontfile='{font}':expansion=none:text='{_esc(nptxt)}':x={nx-4}:y={ny-4}:"
                     f"fontsize=80:fontcolor=yellow:enable='lt(mod(t\\,0.8)\\,0.4)':"
                     f"shadowcolor=black@0.8:shadowx=2:shadowy=2")
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

# ─── MAIN ────────────────────────────────────────────────────────────────────────
def main():
    log("=== reel_concept_test.py starting ===")
    keys, ffmpeg, font = _read_gemini_keys(), _which_ffmpeg(), _find_font()
    log(f"Gemini keys: {len(keys)} | ffmpeg: {ffmpeg} | font: {'yes' if font else 'no'}")

    with tempfile.TemporaryDirectory(prefix="reel_concept_") as td:
        log("\n[1] Picking product…")
        p = pick_product()
        if not p: raise RuntimeError("No product found in Sheet1")
        log(f"  {p['title'][:70]} | ASIN {p['asin']} | {p['disc']} off | {p['price']}")

        # discount banner: was → now
        new_v = _num(p["price"]); pct = _num(p["disc"])
        old_v = (new_v / (1 - pct/100)) if (new_v and pct and pct < 100) else None
        banner = (f"WAS ${old_v:.2f}   NOW ${new_v:.2f}" if (old_v and new_v)
                  else (f"NOW {p['price']}" if p["price"] else ""))

        log("\n[2] Generating FB post (2 pieces)…")
        piece1, piece2, expd = build_fb_pieces(p, keys)
        log("\n--- FB POST PIECE 1 (spoken VO + body) ---\n" + piece1)
        log("\n--- FB POST PIECE 2 (link + code, text only) ---\n" + piece2)
        log("\n--- FULL FB POST (piece1 + piece2) ---\n" + piece1 + "\n\n" + piece2 + "\n")

        log("[3] Capturing Amazon phone page (gallery + price + reviews)…")
        imgs, shot, pw, ph, price_box, rev_box = capture_page(p["asin"], td, ffmpeg)
        if p["cover"]:
            imgs = [p["cover"]] + [u for u in imgs if u != p["cover"]]
        if not imgs:
            raise RuntimeError("No product images available")

        log("\n[4] Building frames…")
        segs = []
        # 1) cover image — held a little longer so it registers
        s = os.path.join(td, "s_cover.mp4")
        if seg_from_image(imgs[0], s, td, ffmpeg, p["disc"], p["code"], font, 0, IMG_SECS + 0.8):
            segs.append(s); log("  ✓ cover")
        # 2) second product image (BEFORE switching to the Amazon page)
        if len(imgs) > 1:
            s = os.path.join(td, "s_img1.mp4")
            if seg_from_image(imgs[1], s, td, ffmpeg, p["disc"], p["code"], font, 1, IMG_SECS):
                segs.append(s); log("  ✓ gallery image 2")
        # 3) price screenshot — tight crop, red strike on old price, new price flashing
        if shot and price_box:
            s = os.path.join(td, "s_price.mp4")
            if seg_price(shot, price_box, s, td, ffmpeg, pw, ph, p["disc"], p["code"],
                         font, p["price"], CROP_SECS):
                segs.append(s); log("  ✓ price frame")
        # 4) reviews + sold zoom (tight)
        if shot and rev_box:
            s = os.path.join(td, "s_rev.mp4")
            if seg_from_crop(shot, rev_box, s, td, ffmpeg, pw, ph, p["disc"], p["code"],
                             font, "LOVED BY REAL BUYERS", 2, CROP_SECS,
                             zoom_mult=6.0, win_min=950, win_max=1500):
                segs.append(s); log("  ✓ reviews frame")
        # 5) rest of gallery
        for j, u in enumerate(imgs[2:5], start=2):
            s = os.path.join(td, f"s_img{j}.mp4")
            if seg_from_image(u, s, td, ffmpeg, p["disc"], p["code"], font, j, IMG_SECS):
                segs.append(s); log(f"  ✓ gallery image {j+1}")

        if not segs:
            raise RuntimeError("No frames built")

        # concat
        log("\n[5] Concatenating frames…")
        listf = os.path.join(td, "list.txt")
        concat = os.path.join(td, "concat.mp4")
        with open(listf, "w") as f:
            for s in segs: f.write(f"file '{Path(s).as_posix()}'\n")
        subprocess.run([ffmpeg, "-y"] + _FF_LOG + ["-f", "concat", "-safe", "0", "-i", listf,
                        "-c", "copy", concat], check=True, timeout=120)
        vid_dur = probe_duration(concat, ffmpeg)
        log(f"  video: {vid_dur:.1f}s ({len(segs)} frames)")

        # VO via Gemini TTS (piece 1)
        log("\n[6] Generating VO (Gemini TTS, piece 1)…")
        vo_wav = os.path.join(td, "vo.wav"); vo_aac = os.path.join(td, "vo.aac")
        wav = gemini_tts(piece1, keys)
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

        # If VO longer than the video, extend the last frame so video covers the VO
        if have_vo and vo_dur > vid_dur + 0.3:
            pad = vo_dur - vid_dur
            ext = os.path.join(td, "ext.mp4")
            subprocess.run([ffmpeg, "-y"] + _FF_LOG + ["-i", concat,
                "-vf", f"tpad=stop_mode=clone:stop_duration={pad:.2f}", "-r", str(FPS),
                "-pix_fmt", "yuv420p"] + _FF_ENCODE + ["-an", ext], check=True, timeout=120)
            concat = ext; vid_dur = probe_duration(concat, ffmpeg)
            log(f"  extended video to {vid_dur:.1f}s to fit VO")

        # mix audio
        log("\n[7] Mixing audio…")
        out = os.path.join(td, "final.mp4")
        if have_vo:
            tail = max(0.0, vid_dur - vo_dur)
            if tail < 1.0:
                subprocess.run([ffmpeg, "-y"] + _FF_LOG + ["-i", concat, "-i", vo_aac,
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-shortest", out], check=True, timeout=120)
            else:
                beats = gen_beats(tail, td, ffmpeg)
                mixed = os.path.join(td, "mix.aac")
                dly = int(vo_dur*1000)
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

        url = cloud_upload(out, f"{CLOUD_FOLDER}/reel_{p['asin']}_{int(time.time())}")
        log("\n" + "=" * 60)
        log("  ARTIFACTS")
        log("=" * 60)
        log(f"  REEL : {url or '⚠️ upload failed (see log)'}")
        log("=" * 60)
    log("=== reel_concept_test.py done ===")


if __name__ == "__main__":
    main()
