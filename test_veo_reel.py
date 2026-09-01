#!/usr/bin/env python3
"""
test_veo_reel.py — Full affiliate reel pipeline

Steps:
  1. Scrape Amazon deals page for a random product ≥70% off
  2. Gemini generates: Veo visual prompt + humorous VO script with CTA
  3. Veo 3.1 Lite → 6s hook clip (FX audio, no speech)
  4. Download product images → animated carousel (Ken Burns + discount badge)
  5. edge-tts → VO audio covering full video duration
  6. FFmpeg → final_reel.mp4 (hook + carousel, Veo FX at 15% vol + VO at 100%)

Usage:
  pip install google-genai edge-tts pillow
  python test_veo_reel.py
  python test_veo_reel.py --url "https://www.amazon.com/dp/ASIN"
  python test_veo_reel.py --skip-concept          # reuse saved concept, skip deals scrape
  python test_veo_reel.py --skip-veo              # also skip Veo (reuse hook.mp4)
  python test_veo_reel.py --veo-key 2             # key index with billing (default: 2)
"""

import os, re, sys, json, time, random, asyncio, argparse, subprocess
import requests
from pathlib import Path
from bs4 import BeautifulSoup

# ── Optional deps check ───────────────────────────────────────────────────────

def _require(pkg, install):
    print(f"ERROR: {pkg} not installed. Run: pip install {install}")
    sys.exit(1)

try:
    from google import genai
    from google.genai import types as gtypes
except ImportError:
    _require("google-genai", "google-genai")

try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_OK = True
except ImportError:
    PIL_OK = False
    print("WARNING: Pillow not installed — discount badge skipped (pip install pillow)")

try:
    import edge_tts
    EDGE_OK = True
except ImportError:
    EDGE_OK = False

try:
    import undetected_chromedriver as uc
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    SELENIUM_OK = True
except ImportError:
    SELENIUM_OK = False

# ── Config ────────────────────────────────────────────────────────────────────

KEYS_FILE         = Path.home() / "geminikey.txt"
GEMINI_API_BASE   = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_TEXT_MODEL = "gemini-2.5-flash"
VEO_MODEL         = "veo-3.1-lite-generate-preview"

HOOK_DURATION    = 6    # seconds — must be 5, 6, or 8 for Veo
IMAGE_PER_SEC    = 3    # seconds per carousel image
MAX_IMAGES       = 4    # max carousel images
MIN_DISCOUNT_PCT = 70   # Amazon deals filter

OUTPUT_DIR = Path("reel_output")

# (voice, rate, pitch) — varied per run for natural feel
VO_VOICES = [
    ("en-US-AriaNeural",    "+15%", "+5%"),
    ("en-US-GuyNeural",     "+10%", "0%"),
    ("en-US-JennyNeural",   "+20%", "+5%"),
    ("en-GB-SoniaNeural",   "+5%",  "0%"),
    ("en-AU-NatashaNeural", "0%",   "+5%"),
    ("en-US-EricNeural",    "+10%", "-5%"),
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# ── Meta-prompt ───────────────────────────────────────────────────────────────

META_PROMPT = """\
You are a viral affiliate marketing video creator.
Given a product on sale, you produce two things: a Veo video prompt and a VO script.

PRODUCT: {product_name}
CURRENT PRICE: {price}
ORIGINAL PRICE: {orig_price}
DISCOUNT: {discount_pct}% OFF
KEY FEATURES:
{bullets}

VIDEO STRUCTURE (total ~{total_sec} seconds):
  PART 1 — HOOK (first 6s): AI-generated cinematic clip, visuals + FX sounds only, NO speech.
  PART 2 — CAROUSEL (~{carousel_sec}s): Product images scroll by while VO plays.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TASK A — VEO PROMPT (Part 1 visual)
Write a Veo 3.1 prompt for a 6-second, 9:16 cinematic clip.
Rules:
• Show the PAIN POINT the product solves — dramatic real-world scene
• NO narration, NO dialogue, NO speech, NO text — ambient FX and environment sounds only
• Think: opening shot of a thriller or high-end commercial
• End the prompt with: "9:16 vertical portrait, cinematic lighting, no text, no speech"

TASK B — VO SCRIPT (covers all {total_sec} seconds)
Write a humorous voiceover narration for the FULL video.
• [0-6s]   Reacts to the dramatic hook scene with a punchy, witty opener (1-2 sentences)
• [6-{total_sec}s] Benefits pitch: 2-3 key features, drop the {discount_pct}% off, build desire
• Last line: punchy CTA (e.g. "Link's in the bio — move fast before it sells out!")
• Tone: enthusiastic infomercial meets Gen Z energy — funny, slightly over-the-top
• Word count: {word_count} words total (fits {total_sec}s at natural speaking pace)
• No filler words, no "um", no "uh"

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Respond ONLY in valid JSON. No markdown. No code fences.

{{
  "veo_prompt": "...",
  "vo_script": "...",
  "voice_style": "excited|deadpan|warm|dramatic"
}}
"""

# ── Helpers ───────────────────────────────────────────────────────────────────

def log(msg):
    print(msg, flush=True)

def load_keys():
    if not KEYS_FILE.exists():
        sys.exit(f"Key file not found: {KEYS_FILE}")
    keys = [k.strip() for k in KEYS_FILE.read_text().splitlines() if k.strip()]
    if not keys:
        sys.exit(f"No keys in {KEYS_FILE}")
    log(f"  Loaded {len(keys)} Gemini keys")
    return keys

def _clean_amz_img(url: str) -> str:
    """Convert Amazon thumbnail URL to full-size by removing size suffixes."""
    return re.sub(r'\._[A-Z0-9_,]+_\.', '.', url)

def get_video_duration(path: Path) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(path)],
        capture_output=True,
    )
    if r.returncode != 0:
        return 0.0
    return float(json.loads(r.stdout).get("format", {}).get("duration", 0))

# ── Step 1: Amazon deals scrape ───────────────────────────────────────────────

def _parse_deals_html(html: str) -> list[dict]:
    """Extract deals ≥ MIN_DISCOUNT_PCT from raw HTML. Returns [{url, discount_pct}]."""
    found = {}
    for m in re.finditer(r'(\d{2,3})%\s*off', html, re.I):
        pct = int(m.group(1))
        if pct < MIN_DISCOUNT_PCT:
            continue
        # Find nearest /dp/ASIN within ±2000 chars
        window = html[max(0, m.start() - 2000): m.end() + 500]
        asin_m = re.search(r'/dp/([A-Z0-9]{10})', window)
        if asin_m:
            url = f"https://www.amazon.com/dp/{asin_m.group(1)}"
            if url not in found or pct > found[url]:
                found[url] = pct
    return [{"url": u, "discount_pct": p} for u, p in found.items()]


def get_filtered_deal() -> tuple[str, int]:
    """Returns (product_url, discount_pct) for a random deal ≥ MIN_DISCOUNT_PCT."""
    deals_url = "https://www.amazon.com/deals"

    # Try requests first (works if Amazon serves enough SSR content)
    try:
        resp = requests.get(deals_url, headers=HEADERS, timeout=20)
        deals = _parse_deals_html(resp.text)
        if deals:
            log(f"  Found {len(deals)} deals ≥{MIN_DISCOUNT_PCT}% via requests")
            pick = random.choice(deals)
            return pick["url"], pick["discount_pct"]
    except Exception as e:
        log(f"  requests failed: {e}")

    # Selenium fallback
    if not SELENIUM_OK:
        raise RuntimeError(
            "Selenium not available — install undetected_chromedriver, "
            "or pass --url manually."
        )
    log("  Falling back to Selenium for deals page...")
    opts = uc.ChromeOptions()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    driver = uc.Chrome(options=opts)
    try:
        driver.get(deals_url)
        time.sleep(5)
        deals = _parse_deals_html(driver.page_source)
    finally:
        try:
            driver.quit()
        except Exception:
            pass

    if not deals:
        raise RuntimeError(
            f"No deals ≥{MIN_DISCOUNT_PCT}% off found. Try --url with a specific product."
        )
    log(f"  Found {len(deals)} deals ≥{MIN_DISCOUNT_PCT}% via Selenium")
    pick = random.choice(deals)
    return pick["url"], pick["discount_pct"]

# ── Step 2: Product info + images ─────────────────────────────────────────────

def _parse_product_page(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")

    title_el = soup.select_one("#productTitle")
    title = title_el.get_text(strip=True) if title_el else "Unknown Product"

    bullets = []
    for sel in ["#feature-bullets li span.a-list-item", "#productFactsDesktop_feature_div li"]:
        for li in soup.select(sel):
            t = li.get_text(" ", strip=True)
            if len(t) > 25 and not t.lower().startswith("make sure"):
                bullets.append(t)
        if len(bullets) >= 4:
            break

    price = orig_price = ""
    for sel in [".a-price .a-offscreen", "#priceblock_ourprice"]:
        el = soup.select_one(sel)
        if el:
            price = el.get_text(strip=True)
            break
    for sel in [".a-text-price .a-offscreen", "#priceblock_listprice",
                ".a-price.a-text-price .a-offscreen"]:
        el = soup.select_one(sel)
        if el:
            orig_price = el.get_text(strip=True)
            break

    discount_pct = 0
    for sel in [".savingsPercentage", "#savingPercentage"]:
        el = soup.select_one(sel)
        if el:
            m = re.search(r'(\d+)', el.get_text())
            if m:
                discount_pct = int(m.group(1))
                break
    if not discount_pct and price and orig_price:
        try:
            p  = float(re.sub(r'[^0-9.]', '', price))
            op = float(re.sub(r'[^0-9.]', '', orig_price))
            if op > p > 0:
                discount_pct = int((1 - p / op) * 100)
        except Exception:
            pass

    # Images — main + gallery
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
        if len(images) >= MAX_IMAGES:
            break

    # Also try to extract from embedded JS colorImages data
    for script in soup.find_all("script", type="text/javascript"):
        text = script.string or ""
        if "colorImages" in text or "hiRes" in text:
            for hi in re.findall(r'"hiRes"\s*:\s*"([^"]+)"', text):
                if hi not in images:
                    images.append(hi)
            if len(images) >= MAX_IMAGES:
                break

    return {
        "title": title,
        "price": price,
        "orig_price": orig_price,
        "discount_pct": discount_pct,
        "bullets": bullets[:4],
        "images": [i for i in images if i][:MAX_IMAGES],
        "url": url,
    }


def get_product_info(url: str) -> dict:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        info = _parse_product_page(resp.text, url)
        if info["bullets"]:
            return info
    except Exception as e:
        log(f"  requests failed ({e}) — trying Selenium...")

    if not SELENIUM_OK:
        raise RuntimeError("Selenium unavailable — cannot scrape product page")

    opts = uc.ChromeOptions()
    opts.add_argument("--headless=new")
    driver = uc.Chrome(options=opts)
    try:
        driver.get(url)
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.ID, "productTitle"))
        )
        return _parse_product_page(driver.page_source, url)
    finally:
        try:
            driver.quit()
        except Exception:
            pass

# ── Step 3: Gemini concept + VO script ───────────────────────────────────────

def generate_concept(product: dict, keys: list, n_images: int) -> dict:
    carousel_sec = n_images * IMAGE_PER_SEC
    total_sec    = HOOK_DURATION + carousel_sec
    word_count   = int(total_sec * 2.5)

    bullets_text = "\n".join(f"• {b[:120]}" for b in product["bullets"][:4])
    prompt_text  = META_PROMPT.format(
        product_name = product["title"][:120],
        price        = product["price"] or "N/A",
        orig_price   = product["orig_price"] or "N/A",
        discount_pct = product["discount_pct"],
        bullets      = bullets_text,
        total_sec    = total_sec,
        carousel_sec = carousel_sec,
        word_count   = word_count,
    )
    payload = {
        "contents":        [{"parts": [{"text": prompt_text}]}],
        "generationConfig": {"temperature": 0.9, "maxOutputTokens": 2048},
    }
    for i, key in enumerate(keys):
        url = f"{GEMINI_API_BASE}/models/{GEMINI_TEXT_MODEL}:generateContent?key={key}"
        log(f"  Gemini concept (key {i+1}/{len(keys)})...")
        resp = requests.post(url, json=payload, timeout=60)
        if resp.status_code == 429:
            log(f"  Key {i+1} → 429, trying next...")
            continue
        if resp.status_code != 200:
            log(f"  Key {i+1} → {resp.status_code}")
            continue
        raw = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE).strip()
        concept = json.loads(raw)
        concept["total_sec"]    = total_sec
        concept["carousel_sec"] = carousel_sec
        return concept
    raise RuntimeError("All Gemini keys exhausted for concept generation")

# ── Step 4: Veo hook clip ─────────────────────────────────────────────────────

def generate_veo_hook(veo_prompt: str, key: str, output_path: Path) -> Path:
    log(f"  Model: {VEO_MODEL}  |  {HOOK_DURATION}s  |  9:16  |  720p")
    log(f"  Prompt: {veo_prompt[:120]}...")

    client = genai.Client(api_key=key)
    # generate_audio / negative_prompt are Enterprise-only — not available on AI Studio keys.
    # "No speech" is enforced via the prompt text instead (see META_PROMPT Task A rules).
    operation = client.models.generate_videos(
        model=VEO_MODEL,
        prompt=veo_prompt,
        config=gtypes.GenerateVideosConfig(
            aspect_ratio     = "9:16",
            duration_seconds = HOOK_DURATION,
        ),
    )

    log("  Submitted — polling every 20s (typically 2-5 min)...")
    spinner = 0
    while not operation.done:
        time.sleep(20)
        operation = client.operations.get(operation)
        spinner += 1
        log(f"  Generating{'.' * (spinner % 5 + 1)}")

    if not operation.response or not operation.response.generated_videos:
        err = getattr(operation, "error", "unknown")
        raise RuntimeError(f"Veo generation failed: {err}")

    video = operation.response.generated_videos[0]

    # Download
    try:
        client.files.download(file=video.video)
        video.video.save(str(output_path))
    except Exception as e:
        log(f"  SDK save failed ({e}), trying URI download...")
        uri = getattr(video.video, "uri", None)
        if not uri:
            raise RuntimeError(f"No video URI in response: {e}")
        resp = requests.get(f"{uri}&key={key}", timeout=120)
        resp.raise_for_status()
        output_path.write_bytes(resp.content)

    log(f"  Hook saved: {output_path}  ({output_path.stat().st_size // 1024} KB)")
    return output_path

# ── Step 5: Download product images ──────────────────────────────────────────

def download_images(image_urls: list, out_dir: Path) -> list[Path]:
    paths = []
    for i, url in enumerate(image_urls):
        ext  = ".jpg" if ".jpg" in url.lower() else ".png"
        dest = out_dir / f"product_{i+1}{ext}"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            dest.write_bytes(resp.content)
            log(f"  Image {i+1}: {dest.stat().st_size // 1024} KB")
            paths.append(dest)
        except Exception as e:
            log(f"  Image {i+1} failed: {e}")
    return paths

# ── Step 6: Carousel with Ken Burns + discount badge ─────────────────────────

def add_discount_badge(src: Path, discount_pct: int, dst: Path) -> Path:
    """Overlay a red circular discount badge on the image (top-right)."""
    if not PIL_OK:
        import shutil
        shutil.copy(src, dst)
        return dst

    img = Image.open(src).convert("RGB")
    W, H = img.size
    r    = max(55, min(W, H) // 7)
    cx, cy = W - r - 16, r + 16

    draw = ImageDraw.Draw(img)
    draw.ellipse([cx - r, cy - r, cx + r, cy + r],
                 fill=(215, 15, 50), outline=(160, 0, 30), width=4)

    fs_large = max(14, r // 2)
    fs_small = max(10, r // 3)
    for path_guess in ["C:/Windows/Fonts/arialbd.ttf",
                       "C:/Windows/Fonts/arial.ttf",
                       "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]:
        try:
            font_large = ImageFont.truetype(path_guess, fs_large)
            font_small = ImageFont.truetype(path_guess, fs_small)
            break
        except OSError:
            continue
    else:
        font_large = font_small = ImageFont.load_default()

    draw.text((cx, cy - fs_large // 2), f"{discount_pct}%",
              fill="white", font=font_large, anchor="mm")
    draw.text((cx, cy + fs_large // 2 + 2), "OFF",
              fill="white", font=font_small, anchor="mm")

    img.save(dst, quality=92)
    return dst


# Ken Burns effects (90 frames = 3s at 30fps, matching IMAGE_PER_SEC=3)
_KEN_BURNS = [
    # zoom in
    "zoompan=z='min(zoom+0.001333,1.12)':d=90:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=720x1280:fps=30",
    # zoom out
    "zoompan=z='max(1.001,1.12-on*0.001333)':d=90:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=720x1280:fps=30",
    # pan right
    "zoompan=z='1.12':d=90:x='min(iw-iw/zoom,on*0.857)':y='ih/2-(ih/zoom/2)':s=720x1280:fps=30",
    # pan left
    "zoompan=z='1.12':d=90:x='max(0,(iw-iw/zoom)*(1-on/90))':y='ih/2-(ih/zoom/2)':s=720x1280:fps=30",
]


def build_carousel(img_paths: list[Path], discount_pct: int, out: Path, tmp: Path) -> Path:
    clips = []
    for i, src in enumerate(img_paths):
        badged = tmp / f"_badge_{i}.jpg"
        add_discount_badge(src, discount_pct, badged)

        vf = (
            "scale=720:1280:force_original_aspect_ratio=decrease,"
            "pad=720:1280:(ow-iw)/2:(oh-ih)/2,"
            + _KEN_BURNS[i % len(_KEN_BURNS)]
        )
        clip = tmp / f"_cc_{i}.mp4"
        r = subprocess.run([
            "ffmpeg", "-y", "-loop", "1", "-i", str(badged),
            "-vf", vf, "-t", str(IMAGE_PER_SEC),
            "-pix_fmt", "yuv420p", "-an", str(clip),
        ], capture_output=True)
        if r.returncode != 0:
            raise RuntimeError(f"Carousel clip {i+1} failed:\n{r.stderr.decode()[-400:]}")
        clips.append(clip)
        log(f"  Carousel clip {i+1}/{len(img_paths)} done")

    list_file = tmp / "_cc_list.txt"
    list_file.write_text("\n".join(f"file '{p.resolve()}'" for p in clips))
    r = subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", str(list_file), "-c", "copy", str(out),
    ], capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"Carousel concat failed:\n{r.stderr.decode()[-400:]}")
    log(f"  Carousel: {out}  ({out.stat().st_size // 1024} KB)")
    return out

# ── Step 7: VO audio via edge-tts ─────────────────────────────────────────────

async def _tts(script: str, voice: str, rate: str, pitch: str, path: Path):
    await edge_tts.Communicate(script, voice, rate=rate, pitch=pitch).save(str(path))


def generate_vo(script: str, voice_style: str, out: Path) -> Path:
    if not EDGE_OK:
        sys.exit("edge-tts not installed. Run: pip install edge-tts")
    style_idx = {"excited": 0, "warm": 0, "deadpan": 1,
                 "dramatic": 4}.get(voice_style.lower(), random.randrange(len(VO_VOICES)))
    voice, rate, pitch = VO_VOICES[style_idx]
    log(f"  Voice: {voice}  rate={rate}  pitch={pitch}")
    asyncio.run(_tts(script, voice, rate, pitch, out))
    log(f"  VO saved: {out}  ({out.stat().st_size // 1024} KB)")
    return out

# ── Step 8: Final assembly ────────────────────────────────────────────────────

def assemble_reel(hook: Path, carousel: Path, vo: Path, out: Path) -> Path:
    hook_dur     = get_video_duration(hook)
    carousel_dur = get_video_duration(carousel)
    total_dur    = hook_dur + carousel_dur
    log(f"  hook={hook_dur:.1f}s  carousel={carousel_dur:.1f}s  total={total_dur:.1f}s")

    # Concat hook+carousel video; mix Veo FX (15%) under VO (100%)
    r = subprocess.run([
        "ffmpeg", "-y",
        "-i", str(hook),
        "-i", str(carousel),
        "-i", str(vo),
        "-filter_complex",
        (
            "[0:v][1:v]concat=n=2:v=1:a=0[vout];"
            f"[0:a]apad=whole_dur={total_dur:.2f},volume=0.15[fx];"
            "[2:a]volume=1.0[vo];"
            "[fx][vo]amix=inputs=2:duration=longest[aout]"
        ),
        "-map", "[vout]",
        "-map", "[aout]",
        "-shortest",
        "-pix_fmt", "yuv420p",
        str(out),
    ], capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"Final assembly failed:\n{r.stderr.decode()[-600:]}")
    log(f"  Final reel: {out}  ({out.stat().st_size // 1024} KB)")
    return out

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Affiliate reel: Veo hook + carousel + VO")
    parser.add_argument("--url",          help="Amazon product URL (skip deals scrape)")
    parser.add_argument("--skip-concept", action="store_true",
                        help="Reuse reel_concept.json — skip deals scrape & Gemini call")
    parser.add_argument("--skip-veo",     action="store_true",
                        help="Also skip Veo — reuse existing hook.mp4 (saves billing)")
    parser.add_argument("--veo-key",      type=int, default=2,
                        help="1-based index of the key with Veo billing credits (default: 2)")
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(exist_ok=True)
    concept_file = OUTPUT_DIR / "reel_concept.json"

    keys = load_keys()
    veo_key = keys[args.veo_key - 1] if 1 <= args.veo_key <= len(keys) else keys[0]

    # ── Step 1-2: Product info + concept ─────────────────────────────────────
    if args.skip_concept and concept_file.exists():
        log("\n═══ Loading saved concept ═══")
        concept = json.loads(concept_file.read_text())
        product = concept["product"]
        log(f"  {product['title'][:80]}")
    else:
        log(f"\n═══ Step 1: Amazon deals (≥{MIN_DISCOUNT_PCT}% off) ═══")
        if args.url:
            product_url  = args.url
            deal_discount = 0
        else:
            product_url, deal_discount = get_filtered_deal()
        log(f"  URL: {product_url}")

        log("\n═══ Step 2: Product info + images ═══")
        product = get_product_info(product_url)
        if deal_discount and not product["discount_pct"]:
            product["discount_pct"] = deal_discount
        log(f"  Title:    {product['title'][:80]}")
        log(f"  Discount: {product['discount_pct']}% off")
        log(f"  Price:    {product['price']} (was {product['orig_price']})")
        log(f"  Images:   {len(product['images'])} found")
        if not product["images"]:
            log("  WARNING: No images found — carousel will be skipped")

        n_images = max(len(product["images"]), 1)

        log("\n═══ Step 3: Gemini concept + VO script ═══")
        concept = generate_concept(product, keys, n_images)
        concept["product"] = product
        concept_file.write_text(json.dumps(concept, indent=2, ensure_ascii=False))
        log(f"  Veo prompt:  {concept['veo_prompt'][:100]}...")
        log(f"  VO script:   {concept['vo_script'][:100]}...")
        log(f"  Voice style: {concept.get('voice_style','excited')}")
        log(f"  Total video: {concept['total_sec']}s (hook 6s + carousel {concept['carousel_sec']}s)")

    product = concept["product"]

    # ── Step 4: Veo hook ──────────────────────────────────────────────────────
    hook_path = OUTPUT_DIR / "hook.mp4"
    if args.skip_veo and hook_path.exists():
        log(f"\n═══ Skipping Veo — reusing {hook_path} ═══")
    else:
        log(f"\n═══ Step 4: Veo 3.1 Lite hook clip (billing key #{args.veo_key}) ═══")
        generate_veo_hook(concept["veo_prompt"], veo_key, hook_path)

    # ── Step 5: Product images ────────────────────────────────────────────────
    log("\n═══ Step 5: Download product images ═══")
    img_paths = download_images(product["images"], OUTPUT_DIR)
    if not img_paths:
        raise RuntimeError(
            "No product images downloaded. Try --url with a specific Amazon product page."
        )

    # ── Step 6: Carousel ──────────────────────────────────────────────────────
    log(f"\n═══ Step 6: Carousel ({len(img_paths)} images × {IMAGE_PER_SEC}s) ═══")
    carousel_path = OUTPUT_DIR / "carousel.mp4"
    build_carousel(img_paths, product["discount_pct"], carousel_path, OUTPUT_DIR)

    # ── Step 7: VO audio ──────────────────────────────────────────────────────
    log("\n═══ Step 7: VO audio (edge-tts) ═══")
    vo_path = OUTPUT_DIR / "vo.mp3"
    generate_vo(concept["vo_script"], concept.get("voice_style", "excited"), vo_path)

    # ── Step 8: Final reel ────────────────────────────────────────────────────
    log("\n═══ Step 8: Assemble final reel ═══")
    reel_path = OUTPUT_DIR / "final_reel.mp4"
    assemble_reel(hook_path, carousel_path, vo_path, reel_path)

    total = concept.get("total_sec", "?")
    log("\n" + "═" * 60)
    log("✓ COMPLETE")
    log(f"  reel_output/final_reel.mp4  ← full reel (~{total}s)")
    log(f"  reel_output/hook.mp4        ← Veo 6s hook (FX audio)")
    log(f"  reel_output/carousel.mp4    ← product image carousel")
    log(f"  reel_output/vo.mp3          ← voiceover narration")
    log(f"  reel_output/reel_concept.json ← Gemini concept + scripts")
    log("═" * 60)


if __name__ == "__main__":
    main()
