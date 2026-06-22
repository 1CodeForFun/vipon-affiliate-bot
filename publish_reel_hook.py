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

import json, os, random, re, subprocess, time
from pathlib import Path

import requests
from PIL import Image, ImageDraw
from google import genai
import google.genai.types as gtypes

# ── Reuse helpers / posting functions from the existing vipon stack ───────────
from reel_concept_test import (
    cloud_upload, _read_gemini_keys, _which_ffmpeg, _find_font,
    gemini_text, gemini_tts, gen_beats, probe_duration, pcm_to_wav,
    _esc, _chrome_bits, capture_page, paapi_get_product_info,
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

# Goldbox URL with a working percentOff discount filter, built programmatically.
# Reverse-engineered from the live deals-page slider URL: the discounts-widget value
# is a JSON object, json-stringified again (escaped quotes), then double URL-encoded.
def _build_goldbox_url(min_pct=DEAL_MIN_DISCOUNT, max_pct=100):
    from urllib.parse import quote
    obj = {"state": {"rangeRefinementFilters": {"percentOff": {"min": min_pct, "max": max_pct}}},
           "version": 1}
    inner        = json.dumps(obj, separators=(",", ":"))
    widget_value = json.dumps(inner)                        # wrap + escape inner quotes
    encoded      = quote(quote(widget_value, safe=""), safe="")  # double URL-encode
    return f"https://www.amazon.com/gp/goldbox/?discounts-widget={encoded}"

_GOLDBOX_URL        = _build_goldbox_url(DEAL_MIN_DISCOUNT, 100)
_DEALS_FALLBACK_URL = "https://www.amazon.com/deals"  # fallback (unfiltered)

def log(m): print(m, flush=True)

# ── DEALS SCRAPING ────────────────────────────────────────────────────────────
def _parse_deals_from_html(html):
    """Parse deal cards from rendered HTML using BeautifulSoup (regex fallback).
    Targets data-csa-c-item-type='deal' cards — same structure Amazon uses on /deals."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        log("  beautifulsoup4 not installed — using regex fallback")
        return _parse_deals_regex(html)

    soup = BeautifulSoup(html, 'html.parser')
    results = []
    seen = set()

    # Iterate deal cards. Each <div data-testid="product-card" data-asin="ASIN">
    # contains a <span class="a-size-mini">X% off</span> badge and a /dp/ASIN link.
    # (Walking the card — not the <a> — avoids html.parser mangling Amazon's
    # invalid nesting of block <div>s inside <a>.)
    cards = soup.find_all('div', attrs={'data-testid': 'product-card'})
    if not cards:
        cards = soup.find_all('div', attrs={'data-asin': True})

    for c in cards:
        asin = c.get('data-asin', '').strip()
        if not asin or not re.match(r'^[A-Z0-9]{10}$', asin):
            link0 = c.find('a', href=re.compile(r'/dp/([A-Z0-9]{10})'))
            mm = re.search(r'/dp/([A-Z0-9]{10})', link0.get('href', '')) if link0 else None
            asin = mm.group(1) if mm else ''
        if not asin or asin in seen:
            continue

        badge = c.find('span', class_=re.compile(r'\ba-size-mini\b'),
                       string=re.compile(r'\d+\s*%\s*off', re.I))
        if not badge:
            continue
        pct = int(re.match(r'(\d+)', badge.get_text(strip=True)).group(1))

        link  = c.find('a', href=re.compile(r'/dp/[A-Z0-9]{10}'))
        href  = link.get('href', '') if link else ''
        slug  = re.search(r'amazon\.com/([^/]+)/dp/', href) or re.match(r'/([^/]+)/dp/', href)
        title = slug.group(1).replace('-', ' ')[:120] if slug else asin

        seen.add(asin)
        results.append({'asin': asin, 'pct': pct, 'title': title})

    return results


def _parse_deals_regex(html):
    """Regex fallback for deal parsing (used when BeautifulSoup not installed)."""
    results = []
    seen = set()
    for m in re.finditer(r'/dp/([A-Z0-9]{10})', html):
        asin = m.group(1)
        if asin in seen:
            continue
        ctx = html[max(0, m.start() - 1200): m.start() + 1200]
        pct_m = re.search(r'(\d+)%\s*off', ctx, re.I)
        if not pct_m:
            continue
        pct = int(pct_m.group(1))
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
    """Scroll the deals page collecting a page-source SNAPSHOT at each position.
    The page virtualizes (off-screen cards are removed from the DOM), so the final
    page_source alone misses most deals. Merge unique deals across all snapshots."""
    driver = _init_driver()
    try:
        driver.get(url)
        time.sleep(8)
        snapshots = [driver.page_source]
        for px in (1500, 3000, 5000, 7000, 9000, 12000, 15000, 18000):
            driver.execute_script(f"window.scrollTo(0, {px})")
            time.sleep(1.5)
            snapshots.append(driver.page_source)
        log(f"  Captured {len(snapshots)} snapshots ({sum(len(s) for s in snapshots):,} chars)")

        merged = {}
        for snap in snapshots:
            for d in _parse_deals_from_html(snap):
                if d['asin'] not in merged or d['pct'] > merged[d['asin']]['pct']:
                    merged[d['asin']] = d
        return list(merged.values())
    finally:
        try: driver.quit()
        except: pass


# Products whose images could be explicit or inappropriate for family-friendly feeds
_EXPLICIT_KEYWORDS = {
    "bra", "bras", "bralette", "bikini", "bikinis", "panty", "panties", "underwear",
    "underwire", "underwired", "wirefree", "wirefree", "brassiere", "lingerie",
    "thong", "thongs", "corset", "negligee", "g-string", "camisole", "shapewear",
    "swimsuit", "swimwear", "bodysuit", "boyshort", "boyshorts",
}

# Over-saturated categories — the deals page is flooded with the security-camera /
# doorbell cluster (Blink, Arlo, Ring, etc.), so blocking them stops the random picker
# from statistically over-selecting the same product type every run.
_SKIP_CATEGORY_KEYWORDS = {
    "doorbell", "blink", "floodlight", "arlo", "wyze", "eufy", "surveillance", "cctv",
}
_CAM_WORDS = {"camera", "cameras", "cam", "cams"}

def _is_safe(deal):
    """Return False if the title has a restricted (explicit) keyword OR belongs to an
    over-saturated category (doorbell / security camera / floodlight cam)."""
    words = set(re.findall(r'\b\w+\b', deal.get("title", "").lower()))
    blocked = words & _EXPLICIT_KEYWORDS
    if blocked:
        log(f"  Skipping {deal['asin']} — restricted keyword(s): {blocked}")
        return False
    over = words & _SKIP_CATEGORY_KEYWORDS
    if not over and "security" in words and (words & _CAM_WORDS):
        over = {"security camera"}
    if over:
        log(f"  Skipping {deal['asin']} — over-saturated category: {over}")
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
        # Fallback: pick from the highest-discount safe deals (top 5 to avoid repetition)
        safe = sorted((d for d in deals if _is_safe(d)), key=lambda x: -x.get("pct", 0))
        if safe:
            top_pct = safe[0]["pct"]
            log(f"  No deals at {min_pct}%+ today — using highest available ({top_pct}%)")
            qualified = safe[:5]
        else:
            qualified = deals

    pick = random.choice(qualified)
    log(f"  Selected ASIN={pick['asin']} ({pick['pct']}% off) — {pick['title'][:60]}")

    # Enrich with clean title + bullet points + price from PA API
    log("  Fetching product details via PA API...")
    info = paapi_get_product_info(pick["asin"])
    pick.update(info)   # adds title_text, bullets, price_text, orig_price_text if available
    return pick




# ── VEO HOOK ─────────────────────────────────────────────────────────────────
def _extract_hook_thumbnail(hook_path, td, ffmpeg):
    """Extract the frame at 3s from the Veo hook clip as a JPEG for YouTube thumbnail."""
    thumb = os.path.join(td, "hook_thumb.jpg")
    try:
        subprocess.run([ffmpeg, "-y"] + _FF_LOG + [
            "-ss", "3", "-i", str(hook_path), "-vframes", "1",
            "-vf", f"scale={VIDEO_W}:{VIDEO_H}:force_original_aspect_ratio=increase,crop={VIDEO_W}:{VIDEO_H}",
            "-q:v", "2", thumb],
            check=True, timeout=30)
        if os.path.exists(thumb):
            log(f"  Thumbnail: extracted frame@3s ({os.path.getsize(thumb):,} bytes)")
            return thumb
    except Exception as e:
        log(f"  Thumbnail extract failed: {e}")
    return None


# Meta-prompt that tells Gemini how to write the actual Veo video prompt. Iterated
# locally in test_hook_prompt.py until the output was consistently strong, then ported
# here verbatim. Tightened for: FACELESS, MID-ACTION TENSION, PRODUCT-IN-USE, NO SPEECH,
# a loud opening pattern-interrupt sound, and a thumbnail-worthy curiosity peak (~3s).
# Keep in sync with test_hook_prompt.py::META_PROMPT.
_VEO_META_PROMPT = """You are a viral short-form video director writing a Veo video prompt for the OPENING HOOK of a product deal reel. The hook must make someone STOP scrolling within the first second.

PRODUCT: "{title}"
{features}

Write the Veo prompt in the EXACT STRUCTURED FORMAT below. Do NOT collapse it into one flowing sentence — Veo follows labeled, structured prompts far more reliably. Fill every field with vivid, concrete, photorealistic detail specific to THIS product. Open IN MEDIAS RES: the clip must START already mid-action at the peak of a developing situation — never a calm, static or establishing shot. The "Avoid" block is MANDATORY and must appear verbatim with the listed items.

OUTPUT THIS EXACT TEMPLATE (keep the labels):

Scene: <a charged real-life moment where the product is actively in use; describe the setting, the faceless person, and the product clearly>
First frame (0s): <the literal opening frame — already mid-motion at a moment of peak tension, something actively happening or about to snap; NO calm setup, NO establishing wide, NO product sitting still>
Peak moment (~3s): <the single most dramatic, curiosity-driving instant — the frame that must work as a thumbnail; freeze-worthy tension, something happening or about to>
Subject framing: faceless — <choose: shot from directly behind / over-the-shoulder / hands and wrists only / silhouette / cropped at the shoulders>; the product is held or used by the hands
Camera: <specific movement and framing that keeps the face hidden — e.g. tight handheld push-in, low over-shoulder, fast whip-pan into the action>
Lighting & mood: <dramatic, specific lighting that heightens tension>
Opening sound (0-2s): <a sudden, weird, LOUD non-musical sound effect in the first one to two seconds that fits the scene and jolts a scrolling viewer to stop — e.g. a sharp whoosh, glass shatter, metallic clang, deep bass boom, record scratch, snapping/cracking; it must NOT be speech>
Pacing: start HOT on the very first frame, then escalate across 6 seconds and peak around second 3 — no slow build-up
Avoid: human faces, eyes or facial expressions; on-screen text, captions or subtitles; dialogue, speech or talking; calm, static or staged "lifestyle" shots; brand logos or watermarks; the product sitting unused
Style: photorealistic, cinematic, 9:16 vertical; loud striking sound design with a jarring sound in the first second; no spoken words

GOOD EXAMPLE (Outdoor security camera):
Scene: A homeowner at dusk on a quiet suburban porch, hands pressing a small white security camera onto the wall beside the front door as the street darkens behind them.
First frame (0s): hands already jamming the camera against the wall, knuckles tense, the mounting bracket half-twisted mid-turn.
Peak moment (~3s): The camera's motion spotlight SNAPS on, harshly lighting a hooded figure frozen mid-step at the open back gate, caught in the act.
Subject framing: faceless — low over-the-shoulder from directly behind the homeowner; their hands mount and angle the camera.
Camera: tight handheld over-the-shoulder, a quick push-in toward the gate as the light triggers.
Lighting & mood: deep blue dusk, then a harsh white spotlight burst; tense, alarming.
Opening sound (0-2s): a sudden loud electronic ALARM CHIRP and motion-sensor beep bursting out of near silence.
Pacing: start HOT on the very first frame, then escalate across 6 seconds and peak around second 3.
Avoid: human faces, eyes or facial expressions; on-screen text, captions or subtitles; dialogue, speech or talking; calm, static or staged "lifestyle" shots; brand logos or watermarks; the product sitting unused.
Style: photorealistic, cinematic, 9:16 vertical; loud striking sound design with a jarring sound in the first second; no spoken words.

Now produce the structured prompt for the PRODUCT above. Output ONLY the filled template — no preamble, no explanation, no markdown bold."""

# Model-level reinforcement of the Avoid block — passed to Veo as negative_prompt so the
# constraints hold even if the generated text prompt softens them.
_VEO_NEGATIVE_PROMPT = (
    "human faces, eyes, facial expressions; on-screen text, captions, subtitles; "
    "dialogue, speech, talking, voiceover; brand logos, watermarks; "
    "calm, static or staged lifestyle shots; the product sitting unused; "
    "slow establishing shot, empty opening frame"
)


def generate_veo_hook(deal, veo_key, keys, output_path):
    """Generate 6s Veo hook video using veo_key (billing key from ~/geminikey.txt index 1).
    keys is used only for the cheap text prompt. Returns path on success, None on failure."""
    bullets = "; ".join(deal.get("bullets", [])[:3])
    title   = deal.get("title_text") or deal.get("title", "product")
    features = f"KEY FEATURES: {bullets}" if bullets else ""

    # Structured meta-prompt (ported from test_hook_prompt.py). A labeled, multi-field
    # template — NOT one flowing sentence — because Veo follows structured prompts far
    # more reliably, and it lets us pin the mandatory "Avoid" block (faces / speech /
    # on-screen text) plus a thumbnail-worthy peak moment around second 3.
    veo_prompt = gemini_text(
        _VEO_META_PROMPT.format(title=title, features=features),
        keys[2:] if len(keys) > 2 else keys, max_tokens=700,
    )
    if not veo_prompt:
        veo_prompt = (
            f"Close-up of hands confidently using {title.split(',')[0]}, "
            f"cinematic lighting, photorealistic, faceless, 9:16 vertical, no speech"
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
                resolution="720p",          # Lite defaults to 1080p — 720p halves the per-second cost
                negative_prompt=_VEO_NEGATIVE_PROMPT,
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


# ── PRICE CARD (fallback when no screenshot/price_box) ───────────────────────
def make_price_card_clip(deal, gallery_urls, dst, td, ffmpeg, font, secs=CIRCLE_SECS):
    """Price slide built entirely from deal data + PA API product image.
    No screenshot or price_box needed. Animated red oval + strikethrough + flashing new price."""
    from PIL import ImageFont
    price_text = deal.get("price_text") or ""
    orig_price = deal.get("orig_price_text") or ""
    pct        = deal.get("pct", 0)
    disc_label = f"{pct}% OFF"

    if not price_text:
        log("  price card: no price_text in deal — skipping"); return False
    if not gallery_urls:
        log("  price card: no images for background — skipping"); return False

    # Background: first product image center-cropped to 9:16
    bg_path = os.path.join(td, "price_bg.jpg")
    try:
        r = requests.get(gallery_urls[0], timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        open(bg_path, "wb").write(r.content)
    except Exception as e:
        log(f"  price card bg download failed: {e}"); return False

    bg = Image.open(bg_path).convert("RGB")
    bg_w, bg_h = bg.size
    target_ar  = VIDEO_W / VIDEO_H
    if bg_w / bg_h > target_ar:
        crop_w = int(bg_h * target_ar)
        bg = bg.crop(((bg_w - crop_w) // 2, 0, (bg_w + crop_w) // 2, bg_h))
    else:
        crop_h = int(bg_w / target_ar)
        bg = bg.crop((0, (bg_h - crop_h) // 2, bg_w, (bg_h + crop_h) // 2))
    bg = bg.resize((VIDEO_W, VIDEO_H), Image.LANCZOS)

    # Dim the background so text is readable
    dim = Image.new("RGBA", (VIDEO_W, VIDEO_H), (0, 0, 0, 140))
    bg = Image.alpha_composite(bg.convert("RGBA"), dim).convert("RGB")

    # Fonts
    try:
        sz_old   = 54
        sz_new   = 96
        sz_badge = 52
        fnt_old   = ImageFont.truetype(font, sz_old)   if font else ImageFont.load_default()
        fnt_new   = ImageFont.truetype(font, sz_new)   if font else ImageFont.load_default()
        fnt_badge = ImageFont.truetype(font, sz_badge) if font else ImageFont.load_default()
    except Exception:
        fnt_old = fnt_new = fnt_badge = ImageFont.load_default()

    # Price text areas (centered in middle of frame)
    y_orig  = int(VIDEO_H * 0.38)
    y_new   = int(VIDEO_H * 0.52)
    y_badge = int(VIDEO_H * 0.74)

    # Oval bounds (around the price area, will animate)
    pad = 30
    oval = (pad, y_orig - pad, VIDEO_W - pad, y_new + sz_new + pad)

    total_frames = max(int(secs * FPS), 15)
    draw_frames  = total_frames // 2   # first half: oval draws in
    frames_dir   = os.path.join(td, "price_card_frames")
    os.makedirs(frames_dir, exist_ok=True)

    for i in range(total_frames):
        frame = bg.copy()
        draw  = ImageDraw.Draw(frame)

        # Animated red oval
        progress  = min(i / max(draw_frames, 1), 1.0)
        end_angle = -90 + progress * 360
        for w_off in range(4):
            o = [oval[0]-w_off, oval[1]-w_off, oval[2]+w_off, oval[3]+w_off]
            if i < draw_frames:
                draw.arc(o, start=-90, end=end_angle, fill=(220, 20, 20), width=max(1, 7 - w_off*2))
            else:
                draw.ellipse(o, outline=(220, 20, 20), width=max(1, 7 - w_off*2))

        # Original price with strikethrough (shown from first frame, strikethrough after oval completes)
        if orig_price:
            try:
                bb = draw.textbbox((0, 0), orig_price, font=fnt_old)
                tw = bb[2] - bb[0]
            except Exception:
                tw = VIDEO_W // 3
            tx = (VIDEO_W - tw) // 2
            draw.text((tx, y_orig), orig_price, font=fnt_old, fill=(200, 200, 200))
            if i >= draw_frames:
                mid_y = y_orig + sz_old // 2
                draw.line([(tx - 5, mid_y), (tx + tw + 5, mid_y)], fill=(220, 20, 20), width=5)

        # New price — flash on/off after oval completes
        show_new = i < draw_frames or (((i - draw_frames) // 5) % 2 == 0)
        if show_new:
            try:
                bb2 = draw.textbbox((0, 0), price_text, font=fnt_new)
                tw2 = bb2[2] - bb2[0]
            except Exception:
                tw2 = VIDEO_W // 2
            tx2 = (VIDEO_W - tw2) // 2
            draw.text((tx2, y_new), price_text, font=fnt_new, fill=(255, 255, 255),
                      stroke_width=3, stroke_fill=(0, 0, 0))

        # Discount badge at bottom
        badge_w, badge_h = 200, 60
        bx0 = (VIDEO_W - badge_w) // 2
        bx1 = bx0 + badge_w
        by0 = y_badge - badge_h // 2
        by1 = y_badge + badge_h // 2
        draw.rectangle([(bx0, by0), (bx1, by1)], fill=(204, 12, 57))
        try:
            bb3 = draw.textbbox((0, 0), disc_label, font=fnt_badge)
            bl_w = bb3[2] - bb3[0]
            draw.text(((VIDEO_W - bl_w) // 2, by0 + 5), disc_label, font=fnt_badge, fill=(255, 255, 255))
        except Exception:
            draw.text((bx0 + 10, by0 + 5), disc_label, font=fnt_badge, fill=(255, 255, 255))

        frame.save(os.path.join(frames_dir, f"pc_{i:05d}.png"))

    raw = os.path.join(td, "price_card_raw.mp4")
    try:
        subprocess.run([ffmpeg, "-y"] + _FF_LOG + [
            "-framerate", str(FPS),
            "-i", os.path.join(frames_dir, "pc_%05d.png"),
            "-pix_fmt", "yuv420p"] + _FF_ENCODE + ["-an", raw],
            check=True, timeout=120)
    except Exception as e:
        log(f"  price card frames→video failed: {e}"); return False

    os.rename(raw, dst)
    log(f"  ✓ price card clip ({secs:.1f}s, {os.path.getsize(dst):,} bytes)")
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

    # Pick a random style so each run sounds different even for the same product
    style = random.choice([
        "SARCASTIC and self-aware — like you can't believe how good this deal is",
        "OVER-THE-TOP infomercial parody — think 90s late-night TV but self-aware",
        "DEADPAN and dry — deliver the deal like it's the most obvious thing in the world",
        "STORYTELLING — set up a relatable 1-sentence scenario, then reveal the deal",
        "DISBELIEVING — you found this price and you're still not sure it's real",
        "HIGH-ENERGY HYPE — short punchy sentences, maximum excitement",
    ])

    # Use keys[2:] for text + TTS to avoid depleted key[0] and Veo billing key[1]
    text_keys = keys[2:] if len(keys) > 2 else keys

    script = gemini_text(
        f'Product: "{title}"\n'
        f'{price_info}\n'
        f'Discount: {deal["pct"]}% off — LIMITED TIME DEAL\n'
        f'Features:\n{bullets}\n\n'
        f"Write a 40-55 word voiceover script for a social media reel.\n"
        f"Style: {style}\n"
        "Rules:\n"
        "- Open with a hook that is NOT 'Are you looking for'\n"
        "- Name the product and its main benefit in a punchy way\n"
        "- End with urgency CTA: e.g. 'limited time — link below!'\n"
        "- Write for speech: smooth pacing, reads in ~12 seconds\n"
        "- No hashtags, no URLs, no discount codes\n"
        "Return ONLY the script text.",
        text_keys, max_tokens=200,
    )
    if not script:
        short = title.split(",")[0]
        script = (
            f"Wait, did I just find this? {short} just dropped {deal['pct']}% and I "
            f"genuinely cannot believe this price. This is the deal that shows up once "
            f"and never again. Limited time — link below before it's gone!"
        )
    log(f"  VO: {script[:80]}…")
    wav = gemini_tts(script, text_keys)
    return script, wav


# ── REEL ASSEMBLY ─────────────────────────────────────────────────────────────
def build_hook_reel(deal, page_data, veo_path, script, wav_bytes, ffmpeg, font, td):
    """Assemble the full reel. Returns local mp4 path or None on failure."""
    disc_label = f"{deal['pct']}% OFF"

    # 1. VO audio (script + wav_bytes pre-generated in main() before Veo)
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

    # Price circle slide — try screenshot-based first, fall back to data-driven card
    circle_dst = os.path.join(td, "price_circle.mp4")
    circle_ok  = False
    if page_data and make_price_circle_clip(page_data, circle_dst, td, ffmpeg, disc_label, font):
        circle_ok = True
    if not circle_ok:
        # Fallback: build price card from deal data + first product image (no screenshot needed)
        if make_price_card_clip(deal, gallery_urls, circle_dst, td, ffmpeg, font):
            circle_ok = True
    if circle_ok:
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

    # Extend carousel if VO is longer — freeze last frame rather than cutting the VO
    if vo_dur and carousel_dur < vo_dur - 0.5:
        deficit  = vo_dur - carousel_dur + 1.0   # 1s breathing room after VO ends
        log(f"  VO ({vo_dur:.1f}s) > carousel ({carousel_dur:.1f}s) — extending by {deficit:.1f}s")
        extended = os.path.join(td, "carousel_ext.mp4")
        subprocess.run([ffmpeg, "-y"] + _FF_LOG + [
            "-i", carousel,
            "-filter_complex", f"[0:v]tpad=stop_mode=clone:stop_duration={deficit:.2f}[v]",
            "-map", "[v]",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26", "-pix_fmt", "yuv420p", "-an",
            extended], check=True, timeout=120)
        carousel     = extended
        carousel_dur = probe_duration(carousel, ffmpeg)
        log(f"  Extended carousel: {carousel_dur:.1f}s")

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

    # 5. Stitch: Veo hook then carousel. Audio is SEQUENTIAL — the hook's own Veo
    #    audio plays at 100% (the jarring pattern-interrupt sound that stops the
    #    scroll), THEN the carousel/VO audio at 100%. No ducking/overlap.
    out = os.path.join(td, "final.mp4")
    if veo_path and os.path.exists(str(veo_path)):
        hook_dur  = probe_duration(veo_path, ffmpeg)
        total_dur = hook_dur + carousel_dur
        subprocess.run([ffmpeg, "-y"] + _FF_LOG + [
            "-i", str(veo_path), "-i", c_audio,
            "-filter_complex",
            "[0:v][1:v]concat=n=2:v=1:a=0[vout];"
            # hook audio @100%, trimmed/padded to exactly the hook length
            f"[0:a]aformat=sample_rates=44100:channel_layouts=stereo,apad,"
            f"atrim=end={hook_dur:.3f},asetpts=PTS-STARTPTS,volume=1.0[ha];"
            # carousel+VO audio @100%
            "[1:a]aformat=sample_rates=44100:channel_layouts=stereo,"
            "asetpts=PTS-STARTPTS,volume=1.0[ca];"
            # play hook audio first, then VO audio; pad/trim to full video length
            f"[ha][ca]concat=n=2:v=0:a=1,apad,atrim=end={total_dur:.3f}[aout]",
            "-map", "[vout]", "-map", "[aout]",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
            "-c:a", "aac", "-b:a", "128k",
            "-pix_fmt", "yuv420p", out],
            check=True, timeout=300)
    else:
        log("  ⚠️ No Veo hook — publishing carousel only")
        os.rename(c_audio, out)

    log(f"  ✓ Final reel: {probe_duration(out, ffmpeg):.1f}s ({os.path.getsize(out):,} bytes)")
    return out


# ── PUBLISHING ────────────────────────────────────────────────────────────────
def publish_platforms(video_url, deal, script, thumbnail_path=None):
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
            post_youtube_short(video_url, title, yt_desc, yt_token_file=yt_file,
                               thumbnail_path=thumbnail_path)
        except Exception as e:
            log(f"ERROR YT {label}: {e}"); errors.append(f"YT-{label}: {e}")

    if errors:
        log(f"\n⚠️  {len(errors)} posting error(s): {'; '.join(map(str, errors))}")
    else:
        log("\n✓ All US platforms posted successfully")


def _load_veo_key():
    """Load the Veo billing key from ~/billed_gemini.txt (standalone file in secrets repo)."""
    p = os.path.expanduser("~/billed_gemini.txt")
    if not os.path.exists(p):
        log("  ~/billed_gemini.txt not found — check secrets repo")
        return None
    key = open(p).read().strip()
    log(f"  Veo billing key: loaded from billed_gemini.txt")
    return key or None


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

    td = os.path.join(os.getcwd(), "reel_output")
    os.makedirs(td, exist_ok=True)
    log(f"Output folder: {td}")
    if True:

        # 1. Pick a deal
        log("\n[1] Scraping Amazon deals (60%+ off)...")
        deal = scrape_amazon_deals()

        # 2. Capture product page
        log(f"\n[2] Capturing product page — ASIN {deal['asin']}...")
        # PA API is primary for images; capture_page runs Selenium for screenshot +
        # price_box and also provides Selenium images as automatic fallback if PA API fails
        imgs, shot, pw, ph, price_box, _, title_box, _ = capture_page(
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
        if not imgs:
            raise RuntimeError("No product images retrieved — aborting before Veo to avoid wasted spend")

        # 3. Generate VO script + TTS audio (must succeed before spending on Veo)
        log("\n[3] Generating VO script + TTS...")
        script, wav_bytes = generate_hook_vo(deal, keys)
        if not wav_bytes:
            raise RuntimeError("VO TTS generation failed — aborting before Veo to avoid wasted spend")
        log(f"  VO: confirmed ({len(wav_bytes):,} bytes)")

        # 4. Veo hook — only runs after images AND VO are confirmed
        log("\n[4] Generating Veo 6s hook...")
        veo_out    = os.path.join(td, "hook.mp4")
        veo_path   = generate_veo_hook(deal, veo_key, keys, veo_out)
        thumb_path = None
        if veo_path:
            thumb_path = _extract_hook_thumbnail(veo_path, td, ffmpeg)
        else:
            log("  Veo failed — will publish carousel-only reel")

        # 5. Build reel (carousel + VO + stitch)
        log("\n[5] Building hook reel...")
        final_path = build_hook_reel(deal, page_data, veo_path, script, wav_bytes, ffmpeg, font, td)
        if not final_path:
            raise RuntimeError("Reel build failed — no output produced")

        # 6. Upload
        log("\n[6] Uploading to Cloudinary...")
        pub_id    = f"vipon_hook_reels/{deal['asin']}_{int(time.time())}"
        video_url = cloud_upload(final_path, pub_id)
        if not video_url:
            raise RuntimeError("Cloudinary upload failed")
        log(f"  URL: {video_url}")

        # 7. Publish
        vo_script = deal.get("_vo_script", f"This deal on {deal.get('title','this product')[:40]} is real. "
                                            f"{deal['pct']}% off — limited time, link below!")
        log("\n[7] Publishing to platforms...")
        publish_platforms(video_url, deal, vo_script, thumbnail_path=thumb_path)

    log("=== publish_reel_hook.py done ===")


if __name__ == "__main__":
    main()
