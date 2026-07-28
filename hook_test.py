#!/usr/bin/env python3
"""
hook_test.py — Standalone proof-of-concept for the Veo 2 hook clip pipeline.

Pipeline for ONE product (first row with a title in Sheet1):
  1. Read product data from Google Sheet
  2. Gemini 2.0 Flash → absurd-but-product-related Veo prompt
  3. Gemini 2.0 Flash → punchy VO script (mentions discount, price, expiry date, code)
  4. Veo 2 (v1alpha) → clip 1 (8s) + last-frame extension → clip 2 (8s) ≈ 16s hook
  5. Gemini Native Audio (gemini-2.0-flash) → VO audio
  6. FFmpeg: hook + carousel images (discount overlay) + beat tail
  7. Upload to Cloudinary, print URL

Touch NOTHING in the existing codebase — fully standalone.
"""

import base64
import hashlib
import json
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
IMG_DUR_SEC       = 3
MAX_IMGS          = 4
VEO_MODEL         = "veo-2.0-generate-001"
VEO_DURATION      = 8
VEO_POLL_INT      = 8
VEO_MAX_WAIT      = 300
CLOUDINARY_FOLDER = "vipon_hooks_test"

_FF_LOG    = ["-loglevel", "error", "-hide_banner"]
_FF_ENCODE = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "28", "-threads", "1"]

def log(msg): print(msg, flush=True)

# ─── CREDENTIALS ─────────────────────────────────────────────────────────────
def _read_pro_key() -> str:
    """Read the Gemini Pro API key — tried first for all API calls."""
    p = os.path.expanduser("~/geminipro.txt")
    if os.path.exists(p):
        k = open(p).read().strip()
        if k: return k
    return ""

def _read_gemini_keys() -> list:
    """Return all available keys — pro key first, then free-tier keys."""
    keys = []
    pro = _read_pro_key()
    if pro:
        keys.append(pro)
    for path in (os.path.expanduser("~/geminikeys.txt"),):
        if os.path.exists(path):
            keys += [l.strip() for l in open(path) if l.strip() and not l.startswith("#")]
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
    import shutil; return shutil.which("ffmpeg") or ""

def _find_font():
    for p in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ):
        if os.path.exists(p): return p
    return ""

# ─── GOOGLE SHEET ────────────────────────────────────────────────────────────
def read_first_product():
    scope  = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds  = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDS_FILE, scope)
    ws     = gspread.authorize(creds).open(GOOGLE_SHEET_NAME).sheet1
    rows   = ws.get_all_values()
    for row in rows[1:]:
        while len(row) < 18: row.append("")
        title = row[8].strip()
        if not title: continue
        return {
            "aff_link": row[0].strip(),
            "title":    title,
            "code":     row[5].strip(),
            "disc":     row[6].strip(),
            "expiry":   row[7].strip(),
            "price":    row[9].strip(),
            "post_txt": row[14].strip(),
            "imgs":     [u for u in (row[11].strip(), row[12].strip()) if u],
        }
    return None

# ─── EXPIRY NORMALISER ────────────────────────────────────────────────────────
def friendly_date(expiry: str) -> str:
    """Convert any expiry string to 'Month Day' format for the VO."""
    if not expiry: return ""
    # Absolute date M/D/YYYY
    dm = re.search(r"(\d{1,2})[/\-](\d{1,2})[/\-](\d{2,4})", expiry)
    if dm:
        try:
            a, b, c = int(dm.group(1)), int(dm.group(2)), int(dm.group(3))
            year = 2000 + c if c < 100 else c
            dt = datetime(year, a, b)
            return f"{dt.strftime('%B')} {dt.day}"
        except Exception: pass
    # Relative: "X days"
    m = re.search(r"(\d+)\s*day", expiry, re.I)
    if m:
        dt = datetime.now() + timedelta(days=int(m.group(1)))
        return f"{dt.strftime('%B')} {dt.day}"
    # Relative: "X hours"
    m = re.search(r"(\d+)\s*hour", expiry, re.I)
    if m:
        dt = datetime.now() + timedelta(hours=int(m.group(1)))
        return f"{dt.strftime('%B')} {dt.day}"
    return expiry

# ─── GEMINI TEXT ─────────────────────────────────────────────────────────────
def gemini_text(prompt: str, keys: list, max_tokens: int = 300) -> str:
    for key in keys:
        try:
            r = requests.post(
                "https://generativelanguage.googleapis.com/v1beta/models/"
                f"gemini-2.0-flash:generateContent?key={key}",
                json={"contents": [{"parts": [{"text": prompt}]}],
                      "generationConfig": {"temperature": 0.95, "maxOutputTokens": max_tokens}},
                timeout=30,
            )
            if r.ok:
                return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            log(f"  Gemini text {r.status_code}: {r.text[:100]}")
        except Exception as e:
            log(f"  Gemini text error: {e}")
    return ""

# ─── GEMINI NATIVE AUDIO TTS ─────────────────────────────────────────────────
# Try stable model first, then experimental fallback
_TTS_MODELS = ["gemini-2.0-flash", "gemini-2.0-flash-exp"]

def gemini_tts(script: str, keys: list, voice: str = "Charon") -> bytes | None:
    """Generate speech via Gemini native audio. Returns raw audio bytes."""
    for key in keys:
        for model in _TTS_MODELS:
            try:
                r = requests.post(
                    "https://generativelanguage.googleapis.com/v1beta/models/"
                    f"{model}:generateContent?key={key}",
                    json={
                        "contents": [{"parts": [{"text": script}]}],
                        "generationConfig": {
                            "responseModalities": ["AUDIO"],
                            "speechConfig": {
                                "voiceConfig": {
                                    "prebuiltVoiceConfig": {"voiceName": voice}
                                }
                            },
                        },
                    },
                    timeout=60,
                )
                if r.ok:
                    part = r.json()["candidates"][0]["content"]["parts"][0]
                    raw  = part.get("inlineData", {}).get("data", "")
                    if raw:
                        log(f"  ✓ TTS: {model} / {voice}")
                        return base64.b64decode(raw)
                else:
                    log(f"  TTS {model}: {r.status_code} {r.text[:100]}")
            except Exception as e:
                log(f"  TTS {model} error: {e}")
    return None

# ─── VEO 2 ───────────────────────────────────────────────────────────────────
# Try multiple API versions — Veo 2 may be in v1alpha before v1beta
_VEO_BASES = [
    "https://generativelanguage.googleapis.com/v1alpha",
    "https://generativelanguage.googleapis.com/v1beta",
]

def veo_generate(prompt: str, key: str, start_image_b64: str = None) -> bytes | None:
    payload = {
        "prompt": {"text": prompt},
        "generationConfig": {"durationSeconds": VEO_DURATION, "aspectRatio": "9:16"},
    }
    if start_image_b64:
        payload["prompt"]["image"] = {"imageBytes": start_image_b64, "mimeType": "image/jpeg"}

    resp = op_name = None
    for base in _VEO_BASES:
        resp = requests.post(
            f"{base}/models/{VEO_MODEL}:generateVideo?key={key}",
            json=payload, timeout=60,
        )
        if resp.ok:
            op_name = resp.json().get("name", "")
            if op_name:
                log(f"  Veo accepted via {base.split('/')[-1]}")
                break
        log(f"  Veo {base.split('/')[-1]}: {resp.status_code} {resp.text[:120]}")

    if not op_name:
        return None

    log(f"  Polling Veo job ({op_name[:50]}…)")
    # Use same base that worked
    base = _VEO_BASES[0] if "v1alpha" in op_name else _VEO_BASES[1]
    for _ in range(VEO_MAX_WAIT // VEO_POLL_INT):
        time.sleep(VEO_POLL_INT)
        poll = requests.get(f"{base}/{op_name}?key={key}", timeout=30)
        if not poll.ok: break
        data = poll.json()
        if not data.get("done"): continue
        samples = (data.get("response", {})
                   .get("generateVideoResponse", {})
                   .get("generatedSamples", []))
        if not samples:
            log("  Veo: done but no samples"); return None
        uri = samples[0].get("video", {}).get("uri", "")
        if not uri: return None
        log("  Veo clip ready, downloading…")
        for dl in (f"{uri}?key={key}&alt=media", uri):
            try:
                r = requests.get(dl, timeout=120)
                if r.ok and len(r.content) > 10_000:
                    log(f"  ✓ Veo: {len(r.content):,} bytes")
                    return r.content
            except Exception: pass
        return None
    log("  Veo: timed out"); return None


def _upload_screenshot(img_path: str) -> str:
    """Upload a debug screenshot to Cloudinary and return the URL."""
    try:
        cloud_name, api_key, api_secret = _load_cloudinary()
        ts  = int(time.time())
        pid = f"hook_debug/screenshot_{ts}"
        sig = hashlib.sha1(f"public_id={pid}&timestamp={ts}{api_secret}".encode()).hexdigest()
        with open(img_path, "rb") as f:
            r = requests.post(
                f"https://api.cloudinary.com/v1_1/{cloud_name}/image/upload",
                data={"public_id": pid, "timestamp": ts, "api_key": api_key, "signature": sig},
                files={"file": f}, timeout=60,
            )
        return r.json().get("secure_url", "") if r.ok else ""
    except Exception: return ""


def veo_via_browser(prompt: str, td: str) -> bytes | None:
    """Drive Google AI Studio headlessly using saved cookies to generate a Veo 2 clip."""
    cookie_path = os.path.expanduser("~/glabcookie.json")
    if not os.path.exists(cookie_path):
        log("  ⚠️ glabcookie.json not found — skipping browser Veo")
        return None

    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
    except ImportError:
        log("  ⚠️ selenium not installed"); return None

    download_dir = os.path.join(td, "veo_dl")
    os.makedirs(download_dir, exist_ok=True)

    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    opts.add_experimental_option("prefs", {
        "download.default_directory": download_dir,
        "download.prompt_for_download": False,
        "safebrowsing.enabled": True,
    })

    # Locate Chromium binary (Ubuntu installs as chromium-browser, not google-chrome)
    for chrome_bin in ("/usr/bin/chromium-browser", "/usr/bin/chromium",
                       "/snap/bin/chromium", "/usr/bin/google-chrome"):
        if os.path.exists(chrome_bin):
            opts.binary_location = chrome_bin
            log(f"  Browser: using binary {chrome_bin}")
            break
    else:
        log("  ⚠️ No Chrome/Chromium binary found"); return None

    # Locate chromedriver
    driver_bin = None
    for d in ("/usr/bin/chromedriver", "/usr/lib/chromium-browser/chromedriver",
              "/usr/bin/chromium-chromedriver", "/snap/bin/chromium.chromedriver"):
        if os.path.exists(d):
            driver_bin = d
            break

    service = Service(executable_path=driver_bin) if driver_bin else Service()
    driver  = webdriver.Chrome(service=service, options=opts)
    wait   = WebDriverWait(driver, 30)

    def screenshot(label: str):
        try:
            p = os.path.join(td, f"{label}.png")
            driver.save_screenshot(p)
            url = _upload_screenshot(p)
            if url: log(f"  📸 {label}: {url}")
        except Exception: pass

    try:
        # ── 1. Load cookies on google.com domain ─────────────────────────────
        log("  Browser: seeding cookies on google.com…")
        driver.get("https://google.com")
        time.sleep(2)

        with open(cookie_path) as f:
            raw = json.load(f)

        driver.delete_all_cookies()
        for c in raw:
            cookie = {
                "name":   c["name"],
                "value":  c["value"],
                "domain": c.get("domain", ".google.com"),
                "path":   c.get("path", "/"),
                "secure": c.get("secure", False),
            }
            if "expirationDate" in c:
                cookie["expiry"] = int(c["expirationDate"])
            try:
                driver.add_cookie(cookie)
            except Exception:
                pass

        # ── 2. Navigate to Veo Studio ─────────────────────────────────────────
        VEO_URLS = [
            "https://aistudio.google.com/generate/video",
            "https://aistudio.google.com/veo",
        ]
        for veo_url in VEO_URLS:
            log(f"  Browser: trying {veo_url} …")
            driver.get(veo_url)
            time.sleep(5)
            screenshot(f"nav_{veo_url.split('/')[-1]}")
            log(f"  Browser: title = {driver.title[:80]}")
            if "Sign in" in driver.title or "accounts.google" in driver.current_url:
                log("  Browser: not authenticated — cookies may be expired"); return None
            # If we land on a valid page (not a redirect to /prompts), proceed
            if "video" in driver.current_url or "veo" in driver.current_url.lower():
                break
        else:
            # Try navigating from the main page
            driver.get("https://aistudio.google.com")
            time.sleep(4)
            screenshot("main_page")
            log(f"  Browser: main page title = {driver.title[:60]}")

        # ── 3. Find the prompt input ──────────────────────────────────────────
        log("  Browser: looking for prompt input…")
        prompt_el = None
        for sel in ["textarea", "[contenteditable='true']", "input[type='text']"]:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            for el in els:
                if el.is_displayed() and el.is_enabled():
                    prompt_el = el; break
            if prompt_el: break

        if not prompt_el:
            screenshot("no_prompt_input")
            log("  Browser: prompt input not found"); return None

        log("  Browser: typing prompt…")
        prompt_el.click()
        time.sleep(0.5)
        prompt_el.clear()
        prompt_el.send_keys(prompt)
        time.sleep(1)
        screenshot("prompt_entered")

        # ── 4. Click Generate ─────────────────────────────────────────────────
        log("  Browser: looking for Generate button…")
        gen_btn = None
        for sel in [
            "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
            "'abcdefghijklmnopqrstuvwxyz'), 'generate')]",
            "//button[contains(@aria-label, 'enerate')]",
            "//button[contains(@data-testid, 'enerate')]",
        ]:
            els = driver.find_elements(By.XPATH, sel)
            for e in els:
                if e.is_displayed() and e.is_enabled():
                    gen_btn = e; break
            if gen_btn: break

        if not gen_btn:
            screenshot("no_generate_btn")
            log("  Browser: Generate button not found"); return None

        gen_btn.click()
        log("  Browser: generation started — polling up to 5 min…")
        screenshot("after_generate_click")

        # ── 5. Wait for video / download button ───────────────────────────────
        for tick in range(60):
            time.sleep(5)

            # Check for a video element
            vids = driver.find_elements(By.CSS_SELECTOR, "video[src]")
            for v in vids:
                src = v.get_attribute("src") or ""
                if src.startswith("http"):
                    log(f"  Browser: video src found → {src[:60]}…")
                    screenshot("video_found")
                    cookies_dict = {c["name"]: c["value"] for c in driver.get_cookies()}
                    r = requests.get(src, cookies=cookies_dict, timeout=120)
                    if r.ok and len(r.content) > 10_000:
                        log(f"  ✓ Veo via browser: {len(r.content):,} bytes")
                        return r.content

            # Check for download button
            for dl_sel in [
                "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
                "'abcdefghijklmnopqrstuvwxyz'), 'download')]",
                "//button[@aria-label[contains(., 'ownload')]]",
                "//*[@data-testid[contains(., 'ownload')]]",
            ]:
                btns = driver.find_elements(By.XPATH, dl_sel)
                for b in btns:
                    if b.is_displayed():
                        log("  Browser: download button found — clicking…")
                        b.click()
                        time.sleep(8)
                        # Check download dir
                        files = [f for f in os.listdir(download_dir) if f.endswith(".mp4")]
                        if files:
                            with open(os.path.join(download_dir, files[0]), "rb") as f:
                                data = f.read()
                            log(f"  ✓ Veo downloaded: {len(data):,} bytes")
                            return data

            if tick % 6 == 0:  # screenshot every 30s
                screenshot(f"waiting_{tick * 5}s")

        screenshot("timeout")
        log("  Browser: timed out waiting for video"); return None

    except Exception as e:
        log(f"  Browser Veo error: {e}")
        try: screenshot("exception")
        except Exception: pass
        return None
    finally:
        try: driver.quit()
        except Exception: pass


def extract_last_frame(video_path: str, td: str, ffmpeg_bin: str) -> str | None:
    frame = os.path.join(td, "last_frame.jpg")
    try:
        subprocess.run([ffmpeg_bin, "-y"] + _FF_LOG +
                       ["-sseof", "-1", "-i", video_path, "-vframes", "1", "-q:v", "2", frame],
                       check=True)
        return frame if os.path.exists(frame) else None
    except Exception as e:
        log(f"  last-frame extract failed: {e}"); return None

# ─── FFMPEG HELPERS ───────────────────────────────────────────────────────────
def _escape_drawtext(text: str) -> str:
    """Escape characters that break FFmpeg drawtext."""
    return (text
            .replace("\\", "\\\\")
            .replace("'",  "\\'")
            .replace(":",  "\\:")
            .replace("%",  "\\%"))   # % is a special char in drawtext

def reencode(src: str, dst: str, ffmpeg_bin: str) -> bool:
    try:
        subprocess.run([ffmpeg_bin, "-y"] + _FF_LOG + [
            "-i", src,
            "-vf", (f"scale={VIDEO_W}:{VIDEO_H}:force_original_aspect_ratio=decrease,"
                    f"pad={VIDEO_W}:{VIDEO_H}:(ow-iw)/2:(oh-ih)/2,setsar=1"),
            "-r", str(FPS), "-pix_fmt", "yuv420p",
        ] + _FF_ENCODE + ["-an", dst], check=True)
        return True
    except Exception as e:
        log(f"  reencode failed: {e}"); return False

def probe_duration(path: str, ffmpeg_bin: str) -> float:
    try:
        r = subprocess.run([ffmpeg_bin, "-i", path],
                           stderr=subprocess.PIPE, stdout=subprocess.PIPE)
        m = re.search(r"Duration: (\d+):(\d+):(\d+\.?\d*)", r.stderr.decode())
        if m: return int(m.group(1))*3600 + int(m.group(2))*60 + float(m.group(3))
    except Exception: pass
    return 10.0

def build_carousel_seg(img_url: str, seg_path: str, td: str, ffmpeg_bin: str,
                       disc: str, code: str, idx: int, font: str) -> bool:
    img_path = os.path.join(td, f"cimg_{idx}.jpg")
    try:
        r = requests.get(img_url, timeout=30, headers={"User-Agent": "Mozilla/5.0"}); r.raise_for_status()
        with open(img_path, "wb") as f: f.write(r.content)
    except Exception as e:
        log(f"  image {idx} download failed: {e}"); return False

    vf = (f"scale={VIDEO_W}:{VIDEO_H}:force_original_aspect_ratio=decrease,"
          f"pad={VIDEO_W}:{VIDEO_H}:(ow-iw)/2:(oh-ih)/2,setsar=1")

    if font:
        def _dt(txt, y, size):
            safe = _escape_drawtext(txt)
            return (f"drawtext=fontfile='{font}':text='{safe}':x=(w-text_w)/2:y={y}:"
                    f"fontsize={size}:fontcolor=white:box=1:boxcolor=black@0.6:"
                    f"boxborderw=10:shadowcolor=black@0.7:shadowx=2:shadowy=2")
        parts = [vf]
        if disc: parts.append(_dt(disc, "h*0.74", 56))
        if code: parts.append(_dt(f"Code: {code}", "h*0.81", 38))
        vf = ",".join(parts)

    try:
        subprocess.run([ffmpeg_bin, "-y"] + _FF_LOG + [
            "-loop", "1", "-t", str(IMG_DUR_SEC), "-i", img_path,
            "-vf", vf, "-r", str(FPS), "-pix_fmt", "yuv420p",
        ] + _FF_ENCODE + ["-an", seg_path], check=True)
        return True
    except Exception as e:
        log(f"  carousel seg {idx} failed: {e}"); return False

def make_ffmpeg_hook_seg(img_path: str, dst: str, ffmpeg_bin: str, font: str) -> bool:
    """Fast zoom-in hook using scale+crop (avoids slow zoompan filter).
    Upscales image to 130%, then crops from centre — renders in seconds not minutes.
    """
    try:
        # Scale up to 130% of target, then crop window that drifts toward centre
        # x starts at 0, ends at (1.3*W - W)/2 = 0.15*W over 8 seconds
        crop_x = f"(iw-{VIDEO_W})*t/8"
        crop_y = f"(ih-{VIDEO_H})*t/8"
        vf = (f"scale={int(VIDEO_W*1.3)}:{int(VIDEO_H*1.3)}:force_original_aspect_ratio=increase,"
              f"crop={VIDEO_W}:{VIDEO_H}:{crop_x}:{crop_y},"
              f"setsar=1")
        if font:
            vf += (f",drawtext=fontfile='{font}':text='WAIT FOR IT\\.\\.\\.':"
                   f"x=(w-text_w)/2:y=h*0.10:fontsize=68:fontcolor=white:"
                   f"box=1:boxcolor=0xFF4500@0.85:boxborderw=16:"
                   f"shadowcolor=black@0.8:shadowx=3:shadowy=3")
        subprocess.run([ffmpeg_bin, "-y"] + _FF_LOG + [
            "-loop", "1", "-t", "8", "-i", img_path,
            "-vf", vf, "-r", str(FPS), "-pix_fmt", "yuv420p",
        ] + _FF_ENCODE + ["-an", dst], check=True, timeout=60)
        return True
    except Exception as e:
        log(f"  FFmpeg hook failed: {e}"); return False

def gen_beat_audio(duration: float, td: str, ffmpeg_bin: str) -> str:
    """Simple synthesised beat for tail silence."""
    out = os.path.join(td, "beat.aac")
    try:
        bpm = 100
        beat = 60.0 / bpm
        # aevalsrc takes a sample EXPRESSION, not a filter graph — see the same
        # fix in reel_concept_test.gen_beats. Decaying tones on a modulo clock
        # reproduce the intended kick (~80 ms) and hat (~17 ms) hits.
        kicks = f"sin(2*PI*60*t)*exp(-12*mod(t,{beat:.4f}))"
        hats  = f"sin(2*PI*6000*t)*exp(-60*mod(t,{beat/2:.4f}))"
        fc = (f"[0][1]amix=inputs=2:duration=shortest,volume=0.2,"
              f"afade=t=out:st={max(0,duration-1):.1f}:d=1")
        subprocess.run([ffmpeg_bin, "-y"] + _FF_LOG + [
            "-f", "lavfi", "-i", f"aevalsrc='{kicks}':s=44100:c=mono",
            "-f", "lavfi", "-i", f"aevalsrc='{hats}':s=44100:c=mono",
            "-filter_complex", fc,
            "-t", str(duration), "-c:a", "aac", "-b:a", "96k", out,
        ], check=True)
        return out
    except Exception as e:
        log(f"  beat gen failed: {e}")
        silent = os.path.join(td, "silent.aac")
        subprocess.run([ffmpeg_bin, "-y"] + _FF_LOG + [
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
            "-t", str(duration), "-c:a", "aac", "-b:a", "64k", silent,
        ], check=True, capture_output=True)
        return silent

def cloudinary_upload(video_path: str, public_id: str) -> str:
    cloud_name, api_key, api_secret = _load_cloudinary()
    ts  = int(time.time())
    sig = hashlib.sha1(f"public_id={public_id}&timestamp={ts}{api_secret}".encode()).hexdigest()
    with open(video_path, "rb") as f:
        r = requests.post(
            f"https://api.cloudinary.com/v1_1/{cloud_name}/video/upload",
            data={"public_id": public_id, "timestamp": ts, "api_key": api_key, "signature": sig},
            files={"file": f}, timeout=300,
        )
    if r.ok: return r.json().get("secure_url", "")
    log(f"  Cloudinary failed: {r.text[:200]}"); return ""

# ─── MAIN ────────────────────────────────────────────────────────────────────
def main():
    log("=== hook_test.py starting ===")
    keys   = _read_gemini_keys()
    ffmpeg = _which_ffmpeg()
    font   = _find_font()
    if not keys:   raise RuntimeError("No Gemini keys found")
    if not ffmpeg: raise RuntimeError("ffmpeg not found")
    log(f"Keys: {len(keys)}  ffmpeg: {ffmpeg}  font: {'yes' if font else 'no'}")

    # 1) Read product
    log("\n[1] Reading product…")
    p = read_first_product()
    if not p: raise RuntimeError("No product found in Sheet1")
    expiry_friendly = friendly_date(p["expiry"])
    log(f"  {p['title'][:80]}")
    log(f"  Disc: {p['disc']}  Code: {p['code']}  Price: {p['price']}  Expiry: {expiry_friendly}")

    with tempfile.TemporaryDirectory(prefix="hook_test_") as td:

        # 2) Generate Veo hook prompt — absurd but product-related
        log("\n[2] Generating Veo hook prompt…")
        hook_req = (
            f'Product title: "{p["title"]}"\n\n'
            "Write a Veo 2 prompt for an 8-second ABSURD social media hook video. Rules:\n"
            "• The visual MUST relate thematically to the product (not literally show it)\n"
            "• It must show something IMPOSSIBLE or SURREAL — never seen in daily life\n"
            "• No text, logos, or labels in the scene\n"
            "• Vivid cinematic colors, dynamic camera motion, 9:16 vertical frame\n"
            "• Example: for an umbrella product → 'A person floats upward through a "
            "ceiling made of storm clouds using only a tiny umbrella, confetti raining up'\n"
            "• Example: for a kitchen knife → 'A chef's knife slices through a mountain "
            "like butter, an avalanche of vegetables tumbling out'\n"
            "Return only the prompt, 2-3 sentences, ready to paste into Veo."
        )
        veo_prompt = gemini_text(hook_req, keys, max_tokens=150)
        if not veo_prompt:
            veo_prompt = (f"An impossibly large version of the product falls from a golden sky "
                          f"onto a miniature city, everything bouncing cartoonishly. "
                          f"Vivid saturated colors, slow-motion, cinematic 9:16.")
        log(f"  Prompt: {veo_prompt}")

        # 3) Generate VO script
        log("\n[3] Generating VO script…")
        code_line = f"Use code {p['code']} at checkout." if p["code"] else ""
        vo_req = (
            f'Product: "{p["title"]}"\n'
            f"Discount: {p['disc']} off — price after discount: {p['price']}\n"
            f"Deal ends: {expiry_friendly}\n"
            f"Discount code: {p['code'] or 'none needed'}\n\n"
            "Write a punchy, humorous 50-70 word voiceover script for a social media reel.\n"
            "Rules:\n"
            "• Open with something surprising or funny — NOT 'Are you looking for'\n"
            "• Clearly mention what the product IS and what it does\n"
            f"• Say the discount ({p['disc']}) and that the deal ends {expiry_friendly}\n"
            f"• {'Mention the code ' + p['code'] + ' at checkout.' if p['code'] else 'No code needed, just grab the link.'}\n"
            "• End with urgency: move fast, link in bio, don't miss it\n"
            "Return only the script text, ready to read aloud."
        )
        vo_script = gemini_text(vo_req, keys, max_tokens=120)
        if not vo_script:
            disc_str = f"{p['disc']} off" if p["disc"] else "a huge discount"
            vo_script = (f"Stop scrolling — this is {disc_str} on {p['title'].split(',')[0]}! "
                         f"Down to just {p['price']} but only until {expiry_friendly}. "
                         f"{code_line} Grab the link in bio before it's gone!")
        log(f"  Script: {vo_script}")

        # 4) Generate Veo clip 1
        log("\n[4] Generating Veo hook clip 1…")
        clip1_raw = os.path.join(td, "clip1_raw.mp4")
        clip1_seg = os.path.join(td, "clip1.mp4")
        # Try API first, fall back to browser-based generation
        clip1_bytes = veo_generate(veo_prompt, keys[0]) if keys else None
        if not clip1_bytes:
            log("  API failed — trying browser-based Veo (glabcookie.json)…")
            clip1_bytes = veo_via_browser(veo_prompt, td)

        if clip1_bytes:
            with open(clip1_raw, "wb") as f: f.write(clip1_bytes)

            # 4b) Extend via last-frame → clip 2
            log("[4b] Extending clip via last-frame continuation…")
            last_frame = extract_last_frame(clip1_raw, td, ffmpeg)
            clip2_bytes = None
            if last_frame:
                with open(last_frame, "rb") as f:
                    frame_b64 = base64.b64encode(f.read()).decode()
                cont = (veo_prompt.split(".")[0] +
                        ". Continue seamlessly from this frame, "
                        "same surreal scene, slight camera drift, same vivid cinematic style.")
                clip2_bytes = veo_generate(cont, keys[0], start_image_b64=frame_b64)

            if clip2_bytes:
                clip2_raw = os.path.join(td, "clip2_raw.mp4")
                with open(clip2_raw, "wb") as f: f.write(clip2_bytes)
                hook_concat = os.path.join(td, "hook_concat.mp4")
                list_f = os.path.join(td, "hook_list.txt")
                with open(list_f, "w") as f:
                    f.write(f"file '{clip1_raw}'\nfile '{clip2_raw}'\n")
                try:
                    subprocess.run([ffmpeg, "-y"] + _FF_LOG +
                                   ["-f", "concat", "-safe", "0", "-i", list_f,
                                    "-c", "copy", hook_concat], check=True)
                    reencode(hook_concat, clip1_seg, ffmpeg)
                    log("  ✓ Hook: ~16s (2 clips)")
                except Exception as e:
                    log(f"  concat clip2 failed: {e}")
                    reencode(clip1_raw, clip1_seg, ffmpeg)
            else:
                reencode(clip1_raw, clip1_seg, ffmpeg)
                log("  Hook: 8s (clip2 skipped)")
        else:
            # FFmpeg fallback — Ken Burns zoom on first product image
            log("  ⚠️ Veo unavailable — using FFmpeg Ken Burns hook")
            if p["imgs"]:
                img_dl = os.path.join(td, "hook_img.jpg")
                r = requests.get(p["imgs"][0], timeout=30); r.raise_for_status()
                with open(img_dl, "wb") as f: f.write(r.content)
                make_ffmpeg_hook_seg(img_dl, clip1_seg, ffmpeg, font)
            else:
                # Black fallback
                subprocess.run([ffmpeg, "-y"] + _FF_LOG + [
                    "-f", "lavfi", "-i", f"color=c=black:s={VIDEO_W}x{VIDEO_H}:r={FPS}",
                    "-t", "8", "-pix_fmt", "yuv420p",
                ] + _FF_ENCODE + ["-an", clip1_seg], check=True)

        hook_dur = probe_duration(clip1_seg, ffmpeg)
        log(f"  Hook duration: {hook_dur:.1f}s")

        # 5) Generate VO audio
        log("\n[5] Generating VO audio…")
        vo_wav = os.path.join(td, "vo.wav")
        vo_aac = os.path.join(td, "vo.aac")
        vo_bytes = gemini_tts(vo_script, keys)
        have_vo = False
        vo_dur  = 0.0

        if vo_bytes:
            with open(vo_wav, "wb") as f: f.write(vo_bytes)
            try:
                subprocess.run([ffmpeg, "-y"] + _FF_LOG +
                               ["-i", vo_wav, "-c:a", "aac", "-b:a", "128k", vo_aac],
                               check=True)
                vo_dur  = probe_duration(vo_aac, ffmpeg)
                have_vo = True
                log(f"  ✓ VO: {vo_dur:.1f}s")
            except Exception as e:
                log(f"  VO convert failed: {e}")
        else:
            log("  ⚠️ Gemini TTS failed — will use beats only")

        # 6) Build carousel segments
        log("\n[6] Building carousel…")
        carousel_segs = []
        for i, url in enumerate(p["imgs"][:MAX_IMGS], start=1):
            seg = os.path.join(td, f"cseg_{i}.mp4")
            if build_carousel_seg(url, seg, td, ffmpeg, p["disc"], p["code"], i, font):
                carousel_segs.append(seg)
                log(f"  ✓ Image {i}")

        # 7) Concat all video
        log("\n[7] Concatenating…")
        all_segs  = [clip1_seg] + carousel_segs
        list_all  = os.path.join(td, "all.txt")
        concat_mp4 = os.path.join(td, "concat.mp4")
        with open(list_all, "w") as f:
            for s in all_segs:
                f.write(f"file '{Path(s).as_posix()}'\n")
        subprocess.run([ffmpeg, "-y"] + _FF_LOG +
                       ["-f", "concat", "-safe", "0", "-i", list_all,
                        "-c", "copy", concat_mp4], check=True)
        total_dur = probe_duration(concat_mp4, ffmpeg)
        log(f"  Total video: {total_dur:.1f}s")

        # 8) Mix audio
        log("\n[8] Mixing audio…")
        out_mp4   = os.path.join(td, "final.mp4")
        tail_sil  = max(0.0, total_dur - vo_dur)

        if have_vo and tail_sil < 1.0:
            subprocess.run([ffmpeg, "-y"] + _FF_LOG +
                           ["-i", concat_mp4, "-i", vo_aac,
                            "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-shortest", out_mp4],
                           check=True)
            log("  VO covers full video")
        elif have_vo:
            beat = gen_beat_audio(tail_sil, td, ffmpeg)
            mixed = os.path.join(td, "mixed.aac")
            delay_ms = int(vo_dur * 1000)
            fc = (f"[0]adelay=0|0[a0];"
                  f"[1]adelay={delay_ms}|{delay_ms}[a1];"
                  f"[a0][a1]amix=inputs=2:duration=longest[out]")
            subprocess.run([ffmpeg, "-y"] + _FF_LOG +
                           ["-i", vo_aac, "-i", beat,
                            "-filter_complex", fc,
                            "-map", "[out]", "-c:a", "aac", "-b:a", "128k", mixed],
                           check=True)
            subprocess.run([ffmpeg, "-y"] + _FF_LOG +
                           ["-i", concat_mp4, "-i", mixed,
                            "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-shortest", out_mp4],
                           check=True)
            log(f"  VO ({vo_dur:.1f}s) + beats tail ({tail_sil:.1f}s)")
        else:
            beat = gen_beat_audio(total_dur, td, ffmpeg)
            subprocess.run([ffmpeg, "-y"] + _FF_LOG +
                           ["-i", concat_mp4, "-i", beat,
                            "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-shortest", out_mp4],
                           check=True)
            log("  Beats only")

        size = os.path.getsize(out_mp4)
        log(f"  ✓ Final: {probe_duration(out_mp4, ffmpeg):.1f}s  {size:,} bytes")

        # 9) Upload
        log("\n[9] Uploading to Cloudinary…")
        pid    = re.sub(r"[^a-zA-Z0-9]", "_", p["title"][:30])
        pub_id = f"{CLOUDINARY_FOLDER}/hook_{pid}_{int(time.time())}"
        url    = cloudinary_upload(out_mp4, pub_id)
        if url:
            log(f"\n✅ SUCCESS\n{url}")
        else:
            log("\n⚠️ Upload failed")

    log("=== hook_test.py done ===")


if __name__ == "__main__":
    main()
