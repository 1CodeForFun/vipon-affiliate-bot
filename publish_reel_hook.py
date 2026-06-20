#!/usr/bin/env python3
"""
publish_reel_hook.py — Amazon deals hook reel publisher (US market only).

Flow per trigger:
  1. Load amazon.com/gp/goldbox with 60%+ discount pre-filter → pick product randomly
  2. Capture product page: gallery images + full-page screenshot + price text
  3. Generate Veo 3.1 Lite 6s video hook (billing key in ~/geminipro.txt)
  4. Generate humorous/witty VO script + Gemini TTS audio
  5. Build carousel: Ken Burns gallery images + animated red-circle price slide
  6. Assemble: Veo hook (FX@15%) + carousel + VO
  7. Upload to Cloudinary → post to FreshDeals + Ultafind (FB / IG / YT)

Triggered by cron-job.org via GitHub Actions workflow_dispatch.
Runs independently of vipon_publisher.py — does NOT touch the Google Sheet.
"""

import json, os, random, re, subprocess, tempfile, time
from pathlib import Path

import requests
from PIL import Image, ImageDraw
from google import genai
import google.genai.types as gtypes

# ── Reuse helpers / posting functions from the existing vipon stack ───────────
from reel_concept_test import (
    cloud_upload, _read_gemini_keys, _which_ffmpeg, _find_font,
    gemini_text, gemini_tts, gen_beats, probe_duration, pcm_to_wav,
    _esc, _chrome_bits, capture_page,
    VIDEO_W, VIDEO_H, FPS, _FF_LOG, _FF_ENCODE,
)
from vipon_publisher import (
    post_fb_reel, post_ig_reel, post_youtube_short, load_fb_token,
    IG_FRESHDEALS_USER_ID, IG_ULTAFIND_USER_ID,
    TIMEOUT, UPLOAD_TIMEOUT, IG_PROCESS_WAIT, IG_RETRY_WAIT, IG_MAX_RETRIES,
)

# ── CONFIG ────────────────────────────────────────────────────────────────────
SECRETS_DIR = os.environ.get("SECRETS_DIR", ".")
def _p(f): return os.path.join(SECRETS_DIR, f)

FB_FRESHDEALS_TOKEN    = _p("fb_page_token.json")
FB_ULTAFIND_TOKEN      = _p("fb_page_token-ultafind.json")
YT_TOKEN_FILE          = _p("token_youtube.json")
YT_ULTAFIND_TOKEN_FILE = _p("token_youtube_ultafind.json")

AFF_TAG_FB = "manus00-20"
AFF_TAG_YT = "youtubefdusa-20"

VEO_MODEL        = "veo-3.1-lite-generate-preview"
HOOK_SECS        = 6
MAX_GALLERY_IMGS = 4
GALLERY_SECS     = 3.0    # seconds per gallery image
CIRCLE_SECS      = 2.5    # seconds for the price-circle slide
DEAL_MIN_DISCOUNT = 60

# Goldbox URL: the Amazon deals page with the 60% discount filter already applied in the URL.
# (This is what amazon.com/deals looks like after moving the Discount slider to 60%.)
# Avoids Selenium having to find and drag the slider — page loads pre-filtered.
_GOLDBOX_URL = (
    "https://www.amazon.com/gp/goldbox/"
    "?discounts-widget=%22%7B%22state%22%3A%7B%22rangeRefinementFilters%22"
    "%3A%22%7B%22discountPercentage%22%3A%7B%22rangeRefinementFilters%22"
    "%3A%22%7B%22l%22%3A%2260%22%7D%22%7D%22%7D%22%7D%22"
)
_DEALS_FALLBACK_URL = "https://www.amazon.com/deals"  # fallback with JS filter

def log(m): print(m, flush=True)

# ── DEALS SCRAPING ────────────────────────────────────────────────────────────
def _parse_deals_from_html(html):
    """Extract deal cards from rendered HTML using regex on page source.
    Looks for /dp/ASIN occurrences and finds 'X% off' in the surrounding HTML."""
    results = []
    seen = set()
    for m in re.finditer(r'/dp/([A-Z0-9]{10})', html):
        asin = m.group(1)
        if asin in seen:
            continue
        # Check ±600 chars around the ASIN link for a discount badge
        ctx = html[max(0, m.start() - 600): m.start() + 600]
        pct_m = re.search(r'(\d+)%\s*off', ctx, re.I)
        if not pct_m:
            continue
        pct = int(pct_m.group(1))
        # Best-effort title: strip tags from the text before the ASIN
        raw = re.sub(r'<[^>]+>', ' ', html[max(0, m.start() - 300): m.start()])
        title = re.sub(r'\s+', ' ', raw).strip()[-120:]
        seen.add(asin)
        results.append({'asin': asin, 'pct': pct, 'title': title})
    return results


def _init_driver():
    """Return a headless Selenium driver using the system chromium + chromedriver."""
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    opts = Options()
    for a in ("--headless=new", "--no-sandbox", "--disable-dev-shm-usage",
              "--disable-gpu", "--window-size=1280,900",
              "--user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"):
        opts.add_argument(a)
    binary, drv = _chrome_bits()
    if binary: opts.binary_location = binary
    return webdriver.Chrome(
        service=(Service(executable_path=drv) if drv else Service()), options=opts)


def _scrape_url(url):
    driver = _init_driver()
    try:
        driver.get(url)
        time.sleep(8)
        # Scroll progressively so lazy-loaded cards further down the page render
        for px in (1500, 3000, 5000, 7000, 9000, 12000):
            driver.execute_script(f"window.scrollTo(0, {px})")
            time.sleep(1.5)
        driver.execute_script("window.scrollTo(0, 0)")
        time.sleep(1)
        html = driver.page_source
        log(f"  Page source: {len(html):,} chars")
        return _parse_deals_from_html(html)
    finally:
        try: driver.quit()
        except: pass


# Products whose images could be explicit or inappropriate for family-friendly feeds
_EXPLICIT_KEYWORDS = {
    "bra", "bras", "bikini", "bikinis", "panty", "panties", "underwear",
    "lingerie", "thong", "thongs", "corset", "negligee", "g-string",
    "camisole", "shapewear", "swimsuit", "swimwear", "bodysuit",
}

def _is_safe(deal):
    """Return False if the deal title contains any explicit/adult keyword."""
    words = set(re.findall(r'\b\w+\b', deal.get("title", "").lower()))
    blocked = words & _EXPLICIT_KEYWORDS
    if blocked:
        log(f"  Skipping {deal['asin']} — blocked keyword(s): {blocked}")
        return False
    return True


def scrape_amazon_deals(min_pct=DEAL_MIN_DISCOUNT):
    """Return a randomly chosen deal dict {asin, pct, title} with ≥ min_pct% off."""
    log(f"Scraping goldbox (60%+ pre-filtered URL)...")
    deals = _scrape_url(_GOLDBOX_URL)
    qualified = [d for d in deals if d.get("pct", 0) >= min_pct and _is_safe(d)]
    log(f"  {len(deals)} deals found, {len(qualified)} at {min_pct}%+ and safe")

    if not qualified:
        log(f"  Goldbox yielded nothing — falling back to amazon.com/deals...")
        deals = _scrape_url(_DEALS_FALLBACK_URL)
        qualified = [d for d in deals if d.get("pct", 0) >= min_pct and _is_safe(d)]
        log(f"  Fallback: {len(deals)} deals, {len(qualified)} at {min_pct}%+ and safe")

    if not qualified:
        if not deals:
            raise RuntimeError("No deals found — check Selenium / network")
        log(f"  No deals at {min_pct}%+ today — picking from all safe deals")
        qualified = [d for d in deals if _is_safe(d)] or deals

    pick = random.choice(qualified)
    log(f"  Selected ASIN={pick['asin']} ({pick['pct']}% off) — {pick['title'][:60]}")
    return pick


# ── PRODUCT IMAGE FETCH (requests, no Selenium) ───────────────────────────────
_DESKTOP_UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

def _clean_amz_img(url):
    return re.sub(r'\._[A-Z0-9_,]+_\.', '.', url)

def _fetch_product_images(asin, max_imgs=6):
    """Fetch Amazon product images from static HTML — same approach as test_veo_reel.py."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        log("  beautifulsoup4 not installed — pip install beautifulsoup4"); return []

    url = f"https://www.amazon.com/dp/{asin}?th=1&psc=1"
    try:
        resp = requests.get(url, headers=_DESKTOP_UA, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        log(f"  requests fetch failed: {e}"); return []

    soup = BeautifulSoup(resp.text, "html.parser")
    images = []

    main = soup.select_one("#landingImage, #imgTagWrappingDiv img")
    if main:
        src = main.get("data-old-hires") or main.get("src", "")
        if src and "amazon" in src:
            images.append(_clean_amz_img(src))

    for img in soup.select("#altImages ul li.item img"):
        src = img.get("src", "")
        if src and "amazon" in src:
            full = _clean_amz_img(src)
            if full not in images:
                images.append(full)
        if len(images) >= max_imgs:
            break

    for script in soup.find_all("script", type="text/javascript"):
        text = script.string or ""
        if "colorImages" in text or "hiRes" in text:
            for hi in re.findall(r'"hiRes"\s*:\s*"([^"]+)"', text):
                if hi not in images:
                    images.append(hi)
            if len(images) >= max_imgs:
                break

    images = [i for i in images if i][:max_imgs]
    log(f"  requests image fetch: {len(images)} images")
    return images


# ── VEO HOOK ─────────────────────────────────────────────────────────────────
def generate_veo_hook(deal, veo_key, keys, output_path):
    """Generate 6s Veo hook video using veo_key (billing key from ~/geminikey.txt index 1).
    keys is used only for the cheap text prompt. Returns path on success, None on failure."""
    bullets = "; ".join(deal.get("bullets", [])[:3])
    title   = deal.get("title_text") or deal.get("title", "product")

    veo_prompt = gemini_text(
        f'Write a 1-sentence visual description for a 6-second cinematic hook VIDEO (no speech, no text overlays).\n'
        f'Product: "{title}"\n'
        f'Discount: {deal["pct"]}% off\n'
        f'Key benefits: {bullets}\n\n'
        'Rules:\n'
        '- Show the PAIN POINT this product solves — no product in frame yet\n'
        '- Dramatic, high-energy, photorealistic, 9:16 vertical\n'
        '- Feel like a thriller or documentary B-roll\n'
        'Output ONLY the video prompt sentence.',
        keys, max_tokens=120,
    )
    if not veo_prompt:
        veo_prompt = (
            f"Dramatic cinematic close-up of someone struggling with the everyday problem "
            f"that {title.split(',')[0]} solves, photorealistic, 9:16 vertical"
        )
    log(f"  Veo prompt: {veo_prompt[:100]}")

    try:
        client    = genai.Client(api_key=veo_key)
        operation = client.models.generate_videos(
            model=VEO_MODEL,
            prompt=veo_prompt,
            config=gtypes.GenerateVideosConfig(
                aspect_ratio="9:16",
                duration_seconds=HOOK_SECS,
            ),
        )
        log("  Veo: waiting for generation (polls every 20s)...")
        while not operation.done:
            time.sleep(20)
            operation = client.operations.get(operation)
        vid = operation.response.generated_videos[0]
        client.files.download(file=vid.video)
        vid.video.save(str(output_path))
        log(f"  Veo: saved {os.path.getsize(output_path):,} bytes → {output_path}")
        return output_path
    except Exception as e:
        log(f"  Veo failed: {e}")
        return None


# ── KEN BURNS SEGMENT ─────────────────────────────────────────────────────────
def seg_ken_burns(img_url, dst, td, ffmpeg, disc_label, font, idx, secs):
    """Download product image and build a Ken Burns animated clip."""
    ip = os.path.join(td, f"kb_{idx}.jpg")
    try:
        r = requests.get(img_url, timeout=30, headers={"User-Agent": "Mozilla/5.0"}); r.raise_for_status()
        open(ip, "wb").write(r.content)
    except Exception as e:
        log(f"  img {idx} download failed: {e}"); return False

    frames = max(2, int(secs * FPS))
    # Even index → zoom in; odd → zoom out; both pan slightly
    if idx % 2 == 0:
        zoom_expr = f"min(zoom+0.0008,1.25)"
        x_expr    = "iw/2-(iw/zoom/2)"
        y_expr    = "ih/2-(ih/zoom/2)"
    else:
        zoom_expr = f"if(eq(on,1),1.25,max(1,zoom-0.0008))"
        x_expr    = "iw/2-(iw/zoom/2)"
        y_expr    = "ih/2-(ih/zoom/2)"

    vf_parts = [
        f"scale={VIDEO_W*2}:{VIDEO_H*2}:force_original_aspect_ratio=decrease:flags=bicubic",
        f"pad={VIDEO_W*2}:{VIDEO_H*2}:(ow-iw)/2:(oh-ih)/2",
        f"zoompan=z='{zoom_expr}':d={frames}:x='{x_expr}':y='{y_expr}':s={VIDEO_W}x{VIDEO_H}:fps={FPS}",
        "setsar=1",
    ]
    if font and disc_label:
        vf_parts.append(
            f"drawtext=fontfile='{font}':expansion=none:text='{_esc(disc_label)}':"
            f"x=(w-text_w)/2:y=h*0.80:fontsize=58:fontcolor=white:"
            f"box=1:boxcolor=0xCC2200@0.85:boxborderw=12:shadowcolor=black@0.7:shadowx=2:shadowy=2"
        )
    try:
        subprocess.run(
            [ffmpeg, "-y"] + _FF_LOG
            + ["-loop", "1", "-i", ip,
               "-vf", ",".join(vf_parts),
               "-r", str(FPS), "-t", f"{secs:.2f}", "-pix_fmt", "yuv420p"]
            + _FF_ENCODE + ["-an", dst],
            check=True, timeout=120)
        return True
    except Exception as e:
        log(f"  ken burns seg {idx} failed: {e}"); return False


# ── PRICE CIRCLE ANIMATION ────────────────────────────────────────────────────
def make_price_circle_clip(page_data, dst, td, ffmpeg, disc_label, font, secs=CIRCLE_SECS):
    """Crop screenshot to price area then draw an animated red oval using PIL frames."""
    shot      = page_data.get("screenshot")
    price_box = page_data.get("price_box")
    img_w     = page_data.get("img_w", 0)
    img_h     = page_data.get("img_h", 0)
    if not shot or not price_box or not img_w:
        log("  price circle: missing screenshot or price_box — skipping"); return False

    # Determine crop window (title down past price, 9:16)
    title_box = page_data.get("title_box")
    if title_box and title_box["y"] < price_box["y"]:
        top    = max(title_box["y"] - 50, 0)
        bottom = price_box["y"] + price_box["h"] + 420
    else:
        top    = max(price_box["y"] - 650, 0)
        bottom = price_box["y"] + price_box["h"] + 350

    win_h = float(min(max(bottom - top, 1100), img_h))
    win_w = win_h * VIDEO_W / VIDEO_H
    if win_w > img_w:
        win_w = float(img_w); win_h = win_w * VIDEO_H / VIDEO_W

    cx = price_box["x"] + price_box["w"] / 2
    cy = (top + bottom) / 2.0
    x0 = int(min(max(cx - win_w / 2, 0), img_w - win_w))
    y0 = int(min(max(cy - win_h / 2, 0), img_h - win_h))

    try:
        full    = Image.open(shot).convert("RGB")
        cropped = full.crop((x0, y0, x0 + int(win_w), y0 + int(win_h)))
        cropped = cropped.resize((VIDEO_W, VIDEO_H), Image.LANCZOS)
    except Exception as e:
        log(f"  price circle: PIL open/crop failed: {e}"); return False

    # Map price_box into the cropped+scaled coordinate space
    scale_x = VIDEO_W / win_w
    scale_y = VIDEO_H / win_h
    margin  = max(int(price_box["h"] * scale_y * 0.6), 20)
    px0 = int((price_box["x"] - x0) * scale_x) - margin
    py0 = int((price_box["y"] - y0) * scale_y) - margin
    px1 = int((price_box["x"] + price_box["w"] - x0) * scale_x) + margin
    py1 = int((price_box["y"] + price_box["h"] - y0) * scale_y) + margin
    # Clamp to frame
    px0, py0 = max(px0, 6), max(py0, 6)
    px1, py1 = min(px1, VIDEO_W - 6), min(py1, VIDEO_H - 6)

    total_frames = max(int(secs * FPS), 15)
    draw_frames  = total_frames // 2     # first half: arc draws itself
    frames_dir   = os.path.join(td, "circle_frames")
    os.makedirs(frames_dir, exist_ok=True)

    for i in range(total_frames):
        frame = cropped.copy()
        draw  = ImageDraw.Draw(frame)
        if i < draw_frames:
            # Arc that reveals from 0° → 360° (start = -90 = top of oval)
            progress  = i / draw_frames
            end_angle = -90 + progress * 360
            for w_off in range(4):
                draw.arc([px0 - w_off, py0 - w_off, px1 + w_off, py1 + w_off],
                         start=-90, end=end_angle, fill=(220, 20, 20), width=max(1, 7 - w_off * 2))
        else:
            # Full oval, held
            for w_off in range(4):
                draw.ellipse([px0 - w_off, py0 - w_off, px1 + w_off, py1 + w_off],
                             outline=(220, 20, 20), width=max(1, 7 - w_off * 2))
        frame.save(os.path.join(frames_dir, f"frame_{i:05d}.png"))

    # Assemble frames → raw video
    raw = os.path.join(td, "circle_raw.mp4")
    try:
        subprocess.run(
            [ffmpeg, "-y"] + _FF_LOG
            + ["-framerate", str(FPS),
               "-i", os.path.join(frames_dir, "frame_%05d.png"),
               "-pix_fmt", "yuv420p"] + _FF_ENCODE + ["-an", raw],
            check=True, timeout=120)
    except Exception as e:
        log(f"  circle frames→video failed: {e}"); return False

    # Add discount text overlay
    if font and disc_label:
        dt = (f"drawtext=fontfile='{font}':expansion=none:text='{_esc(disc_label)}':"
              f"x=(w-text_w)/2:y=h*0.80:fontsize=58:fontcolor=white:"
              f"box=1:boxcolor=0xCC2200@0.85:boxborderw=12:shadowcolor=black@0.7:shadowx=2:shadowy=2")
        try:
            subprocess.run(
                [ffmpeg, "-y"] + _FF_LOG + ["-i", raw, "-vf", dt]
                + _FF_ENCODE + ["-an", dst],
                check=True, timeout=60)
        except Exception as e:
            log(f"  circle text overlay failed: {e}")
            os.rename(raw, dst)
    else:
        os.rename(raw, dst)

    log(f"  ✓ price circle clip ({secs:.1f}s, {os.path.getsize(dst):,} bytes)")
    return True


# ── VO GENERATION ─────────────────────────────────────────────────────────────
def generate_hook_vo(deal, keys):
    """Generate humorous VO script and Gemini TTS audio. Returns (script, wav_bytes)."""
    title      = deal.get("title_text") or deal.get("title", "this product")
    bullets    = "\n".join(f"• {b}" for b in deal.get("bullets", [])[:3]) or "Amazing deal!"
    price_info = ""
    if deal.get("price_text"):
        price_info = f"Current price: {deal['price_text']}"
        if deal.get("orig_price_text"):
            price_info += f" (was {deal['orig_price_text']})"

    script = gemini_text(
        f'Product: "{title}"\n'
        f'{price_info}\n'
        f'Discount: {deal["pct"]}% off — LIMITED TIME DEAL\n'
        f'Features:\n{bullets}\n\n'
        "Write a 40-55 word voiceover script for a social media reel.\n"
        "Style: HUMOROUS and WITTY — make the viewer laugh AND want to buy.\n"
        "Rules:\n"
        "- Open with a funny relatable hook (not 'Are you looking for')\n"
        "- Mention the product and its main benefit in a punchy, fun way\n"
        "- End with urgency CTA: something like 'limited time deal — link below!'\n"
        "- Write for speech: smooth, comma-separated, reads in ~12 seconds\n"
        "- No hashtags, no URLs, no discount codes\n"
        "Return ONLY the script text.",
        keys, max_tokens=200,
    )
    if not script:
        short = title.split(",")[0]
        script = (
            f"Wait, did I just find this? {short} just dropped {deal['pct']}% and I "
            f"genuinely cannot believe this price. This is the deal that shows up once "
            f"and never again. Limited time — link below before it's gone!"
        )
    log(f"  VO: {script[:80]}…")
    wav = gemini_tts(script, keys)
    return script, wav


# ── REEL ASSEMBLY ─────────────────────────────────────────────────────────────
def build_hook_reel(deal, page_data, veo_path, keys, ffmpeg, font, td):
    """Assemble the full reel. Returns local mp4 path or None on failure."""
    disc_label = f"{deal['pct']}% OFF"

    # 1. VO audio
    log("  Generating VO...")
    script, wav_bytes = generate_hook_vo(deal, keys)
    deal["_vo_script"] = script   # saved for captions
    vo_aac, vo_dur = None, 0.0
    if wav_bytes:
        vo_wav     = os.path.join(td, "vo.wav")
        vo_aac_path= os.path.join(td, "vo.aac")
        open(vo_wav, "wb").write(wav_bytes)
        try:
            subprocess.run([ffmpeg, "-y"] + _FF_LOG
                           + ["-i", vo_wav, "-c:a", "aac", "-b:a", "128k", vo_aac_path],
                           check=True, timeout=60)
            vo_dur = probe_duration(vo_aac_path, ffmpeg)
            if vo_dur > 0:
                vo_aac = vo_aac_path
                log(f"  VO: {vo_dur:.1f}s")
        except Exception as e:
            log(f"  VO convert failed: {e}")

    # 2. Carousel plan
    gallery_urls = (page_data or {}).get("images", [])[:MAX_GALLERY_IMGS]
    if not gallery_urls:
        log("  ⚠️ No gallery images — cannot build carousel"); return None

    target = max(vo_dur + 0.5 if vo_dur else 12.0, 8.0)
    n      = len(gallery_urls)
    img_s  = max(2.0, (target - CIRCLE_SECS) / n)

    segs = []
    for i, url in enumerate(gallery_urls):
        s  = os.path.join(td, f"kb_{i}.mp4")
        ok = seg_ken_burns(url, s, td, ffmpeg, disc_label, font, i, img_s)
        if ok:
            segs.append(s)
            log(f"  ✓ gallery {i+1}/{n} ({img_s:.1f}s)")

    if not segs:
        log("  ⚠️ All gallery segments failed"); return None

    # Price circle slide
    circle_dst = os.path.join(td, "price_circle.mp4")
    if page_data and make_price_circle_clip(page_data, circle_dst, td, ffmpeg, disc_label, font):
        segs.append(circle_dst)

    # 3. Concatenate carousel
    list_f   = os.path.join(td, "list.txt")
    carousel = os.path.join(td, "carousel.mp4")
    with open(list_f, "w") as f:
        for s in segs: f.write(f"file '{Path(s).as_posix()}'\n")
    subprocess.run([ffmpeg, "-y"] + _FF_LOG
                   + ["-f", "concat", "-safe", "0", "-i", list_f, "-c", "copy", carousel],
                   check=True, timeout=120)
    carousel_dur = probe_duration(carousel, ffmpeg)

    # 4. Mix VO over carousel
    c_audio = os.path.join(td, "carousel_audio.mp4")
    if vo_aac:
        tail = max(0.0, carousel_dur - vo_dur)
        if tail < 1.0:
            subprocess.run([ffmpeg, "-y"] + _FF_LOG
                           + ["-i", carousel, "-i", vo_aac,
                              "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-shortest", c_audio],
                           check=True, timeout=120)
        else:
            beats  = gen_beats(tail, td, ffmpeg)
            dly    = int(vo_dur * 1000)
            mixed  = os.path.join(td, "mix.aac")
            subprocess.run([ffmpeg, "-y"] + _FF_LOG
                           + ["-i", vo_aac, "-i", beats,
                              "-filter_complex",
                              f"[1]adelay={dly}|{dly}[b];[0][b]amix=inputs=2:duration=longest[a]",
                              "-map", "[a]", "-c:a", "aac", "-b:a", "128k", mixed],
                           check=True, timeout=120)
            subprocess.run([ffmpeg, "-y"] + _FF_LOG
                           + ["-i", carousel, "-i", mixed,
                              "-c:v", "copy", "-c:a", "aac", "-shortest", c_audio],
                           check=True, timeout=120)
    else:
        beats = gen_beats(carousel_dur, td, ffmpeg)
        subprocess.run([ffmpeg, "-y"] + _FF_LOG
                       + ["-i", carousel, "-i", beats,
                          "-c:v", "copy", "-c:a", "aac", "-shortest", c_audio],
                       check=True, timeout=120)

    # 5. Stitch: Veo hook (FX@15%) + carousel+VO
    out = os.path.join(td, "final.mp4")
    if veo_path and os.path.exists(str(veo_path)):
        hook_dur  = probe_duration(veo_path, ffmpeg)
        total_dur = hook_dur + carousel_dur
        subprocess.run([ffmpeg, "-y"] + _FF_LOG + [
            "-i", str(veo_path), "-i", c_audio,
            "-filter_complex",
            "[0:v][1:v]concat=n=2:v=1:a=0[vout];"
            f"[0:a]apad=whole_dur={total_dur:.2f},volume=0.15[fx];"
            "[1:a]volume=1.0[ca];"
            "[fx][ca]amix=inputs=2:duration=longest[aout]",
            "-map", "[vout]", "-map", "[aout]",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
            "-c:a", "aac", "-b:a", "128k",
            "-pix_fmt", "yuv420p", "-shortest", out],
            check=True, timeout=300)
    else:
        log("  ⚠️ No Veo hook — publishing carousel only")
        os.rename(c_audio, out)

    log(f"  ✓ Final reel: {probe_duration(out, ffmpeg):.1f}s ({os.path.getsize(out):,} bytes)")
    return out


# ── PUBLISHING ────────────────────────────────────────────────────────────────
def publish_platforms(video_url, deal, script):
    """Post the reel to FreshDeals + Ultafind on FB / IG / YT."""
    title   = (deal.get("title_text") or deal.get("title") or "Deal Alert!")[:100]
    asin    = deal["asin"]
    fb_link = f"https://www.amazon.com/dp/{asin}?tag={AFF_TAG_FB}"
    yt_link = f"https://www.amazon.com/dp/{asin}?tag={AFF_TAG_YT}"

    fb_cap  = f"{script}\n\n🛒 Grab the deal → {fb_link}"
    yt_desc = (f"{script}\n\n"
               f"⚡ {deal['pct']}% off — limited time!\n"
               f"🛒 {yt_link}")

    errors = []

    for fb_file, label in [(FB_FRESHDEALS_TOKEN, "FreshDeals"), (FB_ULTAFIND_TOKEN, "Ultafind")]:
        log(f"--- Facebook ({label}) ---")
        try:
            pid, tok, _ = load_fb_token(fb_file)
            post_fb_reel(pid, tok, video_url, title, fb_cap)
        except Exception as e:
            log(f"ERROR FB {label}: {e}"); errors.append(f"FB-{label}: {e}")

    for ig_uid, label, fb_file in [
        (IG_FRESHDEALS_USER_ID, "freshdealsus", FB_FRESHDEALS_TOKEN),
        (IG_ULTAFIND_USER_ID,   "ultafind",     FB_ULTAFIND_TOKEN),
    ]:
        log(f"--- Instagram ({label}) ---")
        try:
            _, tok, _ = load_fb_token(fb_file)
            post_ig_reel(ig_uid, tok, video_url, fb_cap)
        except Exception as e:
            log(f"ERROR IG {label}: {e}"); errors.append(f"IG-{label}: {e}")

    for yt_file, label in [(YT_TOKEN_FILE, "FreshDeals YT"), (YT_ULTAFIND_TOKEN_FILE, "Ultafind YT")]:
        log(f"--- YouTube ({label}) ---")
        if not os.path.exists(yt_file):
            log(f"  {yt_file} not found — skipping"); continue
        try:
            post_youtube_short(video_url, title, yt_desc, yt_token_file=yt_file)
        except Exception as e:
            log(f"ERROR YT {label}: {e}"); errors.append(f"YT-{label}: {e}")

    if errors:
        log(f"\n⚠️  {len(errors)} posting error(s): {'; '.join(map(str, errors))}")
    else:
        log("\n✓ All US platforms posted successfully")


def _load_veo_key():
    """Load the Veo billing key from ~/geminikey.txt index 1 (0-based) — mirrors test_veo_reel.py."""
    p = os.path.expanduser("~/geminikey.txt")
    if not os.path.exists(p):
        return None
    lines = [l.strip() for l in open(p) if l.strip()]
    key = lines[1] if len(lines) > 1 else (lines[0] if lines else None)
    log(f"  Veo billing key: geminikey.txt index 1 of {len(lines)} lines")
    return key


# ── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    log("=== publish_reel_hook.py starting ===")
    keys   = _read_gemini_keys()
    ffmpeg = _which_ffmpeg()
    font   = _find_font()
    log(f"Gemini keys: {len(keys)} | ffmpeg: {ffmpeg} | font: {'yes' if font else 'no'}")

    if not keys:
        raise RuntimeError("No Gemini keys found — check ~/geminikey.txt and ~/geminipro.txt")

    # Load Veo billing key the same way test_veo_reel.py does:
    # ~/geminikey.txt line 2 (index 1). This is independent of how geminipro.txt
    # or geminikeys.txt shift the main key list.
    veo_key = _load_veo_key() or (keys[1] if len(keys) > 1 else keys[0])

    with tempfile.TemporaryDirectory(prefix="hook_reel_") as td:

        # 1. Pick a deal
        log("\n[1] Scraping Amazon deals (60%+ off)...")
        deal = scrape_amazon_deals()

        # 2. Capture product page
        log(f"\n[2] Capturing product page — ASIN {deal['asin']}...")
        # Images: use requests + BeautifulSoup on static HTML (same as test_veo_reel.py — proven)
        imgs = _fetch_product_images(deal["asin"])
        # Screenshot + price_box: still need Selenium for the full-page CDP screenshot
        _, shot, pw, ph, price_box, _, title_box, _ = capture_page(
            deal["asin"], td, ffmpeg
        )
        page_data = {
            "images":     imgs,
            "screenshot": shot,
            "img_w":      pw,
            "img_h":      ph,
            "price_box":  price_box,
            "title_box":  title_box,
        }
        log(f"  Images: {len(imgs)} | price_box: {'y' if price_box else 'n'}")

        # 3. Veo hook — billing key loaded from ~/geminikey.txt index 1 (same as test_veo_reel.py)
        log("\n[3] Generating Veo 6s hook...")
        veo_out  = os.path.join(td, "hook.mp4")
        veo_path = generate_veo_hook(deal, veo_key, keys, veo_out)
        if not veo_path:
            log("  Veo failed — will publish carousel-only reel")

        # 4. Build reel (carousel + VO + stitch)
        log("\n[4] Building hook reel...")
        final_path = build_hook_reel(deal, page_data, veo_path, keys, ffmpeg, font, td)
        if not final_path:
            raise RuntimeError("Reel build failed — no output produced")

        # 5. Upload
        log("\n[5] Uploading to Cloudinary...")
        pub_id    = f"vipon_hook_reels/{deal['asin']}_{int(time.time())}"
        video_url = cloud_upload(final_path, pub_id)
        if not video_url:
            raise RuntimeError("Cloudinary upload failed")
        log(f"  URL: {video_url}")

        # 6. Publish
        script = deal.get("_vo_script", f"This deal on {deal.get('title','this product')[:40]} is real. "
                                         f"{deal['pct']}% off — limited time, link below!")
        log("\n[6] Publishing to platforms...")
        publish_platforms(video_url, deal, script)

    log("=== publish_reel_hook.py done ===")


if __name__ == "__main__":
    main()
