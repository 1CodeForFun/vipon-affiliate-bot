#!/usr/bin/env python3
"""
test_hook.py — Prototype: Amazon Daily Deal → Gemini concept → Veo 3 hook video

Pipeline:
  1. Scrape first product from amazon.com/deals (requests + BS4)
  2. Pull top-3 bullet points from the product page
  3. Gemini Pro: bullets → pinpoint + persona + structured Veo 3 prompt
  4. Veo 3 API: prompt → 5-second hook video (9:16 vertical)
  5. FFmpeg: extract frame at 2.5s → thumbnail (peak-motion frame)

Outputs to: hook_test_output/
  hook.mp4            — the Veo 3 hook clip
  hook_thumbnail.jpg  — frame at 2.5s, use as FB/YT custom thumbnail
  hook_concept.json   — full Gemini output for review/debugging

Usage:
  python test_hook.py
  python test_hook.py --url "https://www.amazon.com/dp/ASIN"  # skip deals scrape
"""

import os, re, sys, json, time, base64, argparse, subprocess, urllib.parse
import requests
from pathlib import Path
from bs4 import BeautifulSoup

# ── Config ─────────────────────────────────────────────────────────────────────
# Uses the same free-tier key file as the main scraper — no credits consumed
GEMINI_KEYS_FILE    = Path.home() / "geminikey.txt"
AMAZON_DEALS_URL    = "https://www.amazon.com/deals"

# Veo 3 model name — verify at https://ai.google.dev/api if this returns 404
VEO_MODEL           = "veo-003"
HOOK_DURATION_SEC   = 5
HOOK_THUMB_OFFSET   = 2.5    # seconds — where peak-motion frame lives

# Gemini model — flash works on free-tier keys and is fast enough for this task
GEMINI_MODEL        = "gemini-2.5-flash"

GEMINI_API_BASE     = "https://generativelanguage.googleapis.com/v1beta"
POLLINATIONS_BASE   = "https://image.pollinations.ai/prompt"
OUTPUT_DIR          = Path("hook_test_output")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
}


# ── Helpers ────────────────────────────────────────────────────────────────────

def load_keys() -> list[str]:
    """Load all Gemini keys from geminikey.txt (one per line)."""
    if not GEMINI_KEYS_FILE.exists():
        sys.exit(f"Key file not found: {GEMINI_KEYS_FILE}")
    keys = [k.strip() for k in GEMINI_KEYS_FILE.read_text().splitlines() if k.strip()]
    if not keys:
        sys.exit(f"No keys found in {GEMINI_KEYS_FILE}")
    log(f"  Loaded {len(keys)} Gemini keys from {GEMINI_KEYS_FILE}")
    return keys


def log(msg: str):
    print(msg, flush=True)


# ── Step 1: Get one product from Amazon Deals ──────────────────────────────────

def get_first_deal_url() -> str:
    """Return the URL of the first /dp/ product found on the Amazon deals page."""
    log("  GET amazon.com/deals ...")
    resp = requests.get(AMAZON_DEALS_URL, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = re.search(r"/dp/([A-Z0-9]{10})", href)
        if m and m.group(1) not in seen:
            seen.add(m.group(1))
            return f"https://www.amazon.com/dp/{m.group(1)}"

    raise RuntimeError(
        "No /dp/ links found on deals page.\n"
        "Amazon may have served a CAPTCHA or JS-heavy page.\n"
        "Run with: python test_hook.py --url https://www.amazon.com/dp/YOUR_ASIN"
    )


# ── Step 2: Bullet points from product page ────────────────────────────────────

def _parse_bullets(soup: "BeautifulSoup") -> tuple[str, list[str], str]:
    """Extract (title, bullets, price) from a BeautifulSoup page."""
    title_el = soup.select_one("#productTitle")
    title = title_el.get_text(strip=True) if title_el else "Unknown Product"

    bullets: list[str] = []
    for selector in [
        "#feature-bullets li span.a-list-item",
        "#productFactsDesktop_feature_div li",
        "#detailBullets_feature_div li",
    ]:
        for li in soup.select(selector):
            text = li.get_text(" ", strip=True)
            if len(text) > 25 and not text.lower().startswith("make sure"):
                bullets.append(text)
            if len(bullets) >= 3:
                break
        if len(bullets) >= 3:
            break

    price = ""
    for sel in [".a-price .a-offscreen", "#priceblock_ourprice", "#priceblock_dealprice"]:
        el = soup.select_one(sel)
        if el:
            price = el.get_text(strip=True)
            break

    return title, bullets[:3], price


def _get_bullets_via_selenium(product_url: str) -> tuple[str, list[str], str]:
    """Fallback: use undetected_chromedriver (already in project) when requests gets blocked."""
    log("  Retrying with Selenium (Amazon blocked plain requests)...")
    import undetected_chromedriver as uc
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    opts = uc.ChromeOptions()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1280,900")

    driver = uc.Chrome(options=opts)
    try:
        driver.get(product_url)
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.ID, "feature-bullets"))
        )
        soup = BeautifulSoup(driver.page_source, "html.parser")
        return _parse_bullets(soup)
    finally:
        try:
            driver.quit()
        except Exception:
            pass


def get_product_info(product_url: str) -> tuple[str, list[str], str]:
    """Return (title, [bullet1, bullet2, bullet3], price_text).
    Tries plain requests first; falls back to Selenium if Amazon blocks it."""
    log(f"  GET {product_url}")
    try:
        resp = requests.get(product_url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        title, bullets, price = _parse_bullets(soup)
        if bullets:
            return title, bullets, price
    except Exception as e:
        log(f"  requests failed ({e}) — trying Selenium...")

    # Selenium fallback
    title, bullets, price = _get_bullets_via_selenium(product_url)

    if not bullets:
        raise RuntimeError(
            f"Could not find product bullet points at {product_url}\n"
            "Even Selenium found no bullets — the page may require login or is geo-blocked."
        )

    return title, bullets, price


# ── Step 3: Gemini Pro → pinpoint + persona + Veo 3 prompt ───────────────────

# This is the core meta-prompt. It is intentionally verbose and structured
# because Veo 3 needs precise direction to guarantee peak motion at 2.5s
# (the frame we extract as thumbnail). Every section maps to a timeline segment.
META_PROMPT = """\
You are a viral social media video strategist and Veo 3 prompt engineer.
Your job: given a product, produce a 5-second hook video concept that stops scrollers cold.

PRODUCT: {product_name}
TOP BENEFITS:
{bullets}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STEP 1 — PINPOINT
Identify the single most universal pain point, frustration, or desire this product addresses.
This must be something relatable enough that a random person mid-scroll recognizes it instantly.

STEP 2 — PERSONA
Name the SPECIFIC person who feels this most intensely. Be vivid and concrete.
Bad: "busy parents"
Good: "A frazzled dad in a supermarket at 6 PM, three kids pulling on him, phone ringing"

STEP 3 — HOOK CONCEPT
Take the pinpoint and ABSURDLY EXAGGERATE it — push it past the point of realism into drama or comedy.
Rules:
• Must involve a PHYSICAL OBJECT IN MOTION (falling, spinning, crashing, exploding, flying, tipping)
• The motion must build from 0–2s, reach PEAK DRAMA at exactly second 2–3, and leave unresolved at 5s
• Do NOT show the product yet — the hook is the exaggerated PROBLEM, not the solution
• A person mid-scroll must freeze and ask: "wait... WHAT is happening right now?"
• Think: something teetering on an edge, a tower about to fall, a ball mid-flight toward glass,
  a stack of things collapsing in slow motion, an avalanche of items about to hit someone

STEP 4 — VEO PROMPT
Write a SINGLE FLOWING PARAGRAPH that Veo can use directly as a generation prompt.
Do NOT use section headers like "OPENING:" or "PEAK:" — Veo ignores them and only renders
the first concept it reads. Instead write one cohesive scene description that naturally
flows through all three phases:

Phase 1 (build): describe what is happening at the start and how tension rises
Phase 2 (peak): describe the suspended mid-motion moment at maximum drama — THIS is the
  thumbnail frame, so make it visually unmistakable: object frozen mid-fall, mid-crash,
  mid-collision — maximum visual tension in a single frame
Phase 3 (cut): the action continues but the clip ends abruptly before resolution

Weave in sound design naturally: ambient sounds building to a dramatic silence or impact
sound that cuts off unresolved. End the prompt with: "Cinematic 4K, ultra-dramatic
slow-motion at the peak moment, high contrast, 9:16 vertical aspect ratio."

STEP 5 — IMAGE PROMPTS
Write 3 still-image prompts for Flux/Pollinations (image generation, NOT video).
Each prompt describes ONE frozen frame — like a film still.
Be hyper-specific: exact subject, exact position in frame, exact lighting source and angle,
foreground/background relationship, camera angle, depth of field.
Do NOT write "a scene of" or "phase 1" or narrative — just describe what is physically IN the image.
Do NOT mention the product.

Image 1 — BEFORE (calm, foreshadowing just starting):
What does the viewer see in the very first second before tension arrives?
Describe the peaceful state in complete detail.

Image 2 — PEAK (the frozen money-shot, the thumbnail):
The single most dramatic frame. Maximum visual tension.
Describe exactly what is frozen mid-motion, its exact position, the lighting, the shadow.

Image 3 — UNRESOLVED (action progressing, cut imminent):
The moment just before the clip cuts — action has advanced but no resolution.
Describe exactly what changed from the peak frame.

Each prompt must end with: "cinematic, hyperrealistic, dramatic chiaroscuro lighting, 9:16 vertical portrait"

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Respond ONLY in valid JSON. No markdown. No code fences. No explanation outside the JSON.

{{
  "pinpoint": "...",
  "persona": "...",
  "hook_concept": "one paragraph describing the visual narrative",
  "veo3_prompt": "single flowing paragraph — no section headers, no newlines inside, written as one continuous scene description Veo can render start to finish",
  "image_prompts": [
    "image 1 BEFORE — specific frozen-frame photography description",
    "image 2 PEAK — specific frozen-frame photography description",
    "image 3 UNRESOLVED — specific frozen-frame photography description"
  ]
}}
"""


def generate_hook_concept(title: str, bullets: list[str], keys: list[str]) -> dict:
    """Call Gemini to get hook concept. Rotates through keys on 429 — 1 attempt per key."""
    bullets_text = "\n".join(f"• {b}" for b in bullets)
    prompt_text = META_PROMPT.format(product_name=title, bullets=bullets_text)
    payload = {
        "contents": [{"parts": [{"text": prompt_text}]}],
        "generationConfig": {"temperature": 0.9, "maxOutputTokens": 8192},
    }

    for i, key in enumerate(keys):
        url = f"{GEMINI_API_BASE}/models/{GEMINI_MODEL}:generateContent?key={key}"
        log(f"  Calling Gemini (key {i + 1}/{len(keys)})...")
        resp = requests.post(url, json=payload, timeout=60)

        if resp.status_code == 429:
            try:
                msg = resp.json().get("error", {}).get("message", "")
            except Exception:
                msg = resp.text[:120]
            log(f"  Key {i + 1} → 429: {msg[:120]} — trying next key...")
            continue

        if resp.status_code != 200:
            log(f"  Key {i + 1} → {resp.status_code} — trying next key...")
            continue

        raw = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE).strip()
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            log(f"  ⚠️  JSON parse failed: {e}\n  Raw:\n{raw}")
            raise

    raise RuntimeError(f"All {len(keys)} Gemini keys returned 429/error — quota exhausted for today.")


# ── Step 4: Veo 3 → generate hook video ───────────────────────────────────────

def generate_veo3_hook(veo3_prompt: str, api_key: str, output_path: Path) -> Path:
    """Submit a Veo generation job, polling until done. Tries model names in order."""

    veo_models_to_try = [
        "veo-3.0-generate-001",
        "veo-2.0-generate-001",
        "veo-003",
    ]

    payload = {
        "prompt": veo3_prompt,
        "generationConfig": {
            "durationSeconds": HOOK_DURATION_SEC,
            "aspectRatio": "9:16",
        },
    }

    resp = None
    used_model = None
    for model in veo_models_to_try:
        submit_url = f"{GEMINI_API_BASE}/models/{model}:generateVideo?key={api_key}"
        log(f"  Submitting to Veo ({model})...")
        resp = requests.post(submit_url, json=payload, timeout=30)
        if resp.status_code == 404:
            log(f"  {model} not found — trying next...")
            continue
        if resp.status_code == 403:
            log(f"\n  ✗ {model} returned 403 — key does not have Veo access.")
            log("    Apply at: https://aistudio.google.com")
            log("    Concept saved — paste the Veo3 prompt manually into AI Studio to test.")
            return None
        used_model = model
        break

    if used_model is None:
        log("\n  ✗ No Veo model responded — all returned 404.")
        log("    These free-tier keys may not have Veo access yet.")
        log("    Paste hook_test_output/hook_concept.json → veo3_prompt into https://aistudio.google.com")
        return None

    resp.raise_for_status()
    data = resp.json()
    operation_name = data.get("name")
    if not operation_name:
        raise RuntimeError(f"No operation name in Veo response: {json.dumps(data)}")

    log(f"  Operation: {operation_name}")
    log("  Polling for completion (may take 1–3 minutes)...")

    poll_url = f"{GEMINI_API_BASE}/{operation_name}?key={api_key}"
    for attempt in range(72):   # up to 6 minutes
        time.sleep(5)
        poll_resp = requests.get(poll_url, timeout=15)
        poll_resp.raise_for_status()
        poll = poll_resp.json()

        if poll.get("done"):
            log(f"  ✓ Done after {attempt * 5}s")
            break
        if attempt % 6 == 5:
            log(f"    ... {(attempt + 1) * 5}s elapsed")
    else:
        raise TimeoutError("Veo 3 generation timed out after 6 minutes")

    # Extract video bytes — API response shape may vary; try both known formats
    response_body = poll.get("response", {})
    videos = (
        response_body.get("videos")
        or response_body.get("generatedVideos")
        or []
    )
    if not videos:
        raise RuntimeError(f"No videos in Veo response:\n{json.dumps(poll, indent=2)}")

    video_entry = videos[0]
    # Could be nested under "video" key or flat
    video_data = video_entry.get("video") or video_entry

    if "videoBytes" in video_data:
        output_path.write_bytes(base64.b64decode(video_data["videoBytes"]))
    elif "uri" in video_data:
        log(f"  Downloading from URI: {video_data['uri'][:80]}...")
        dl = requests.get(video_data["uri"], timeout=120)
        dl.raise_for_status()
        output_path.write_bytes(dl.content)
    else:
        raise RuntimeError(f"Unknown video format in response: {list(video_data.keys())}")

    log(f"  Saved: {output_path}  ({output_path.stat().st_size // 1024} KB)")
    return output_path


# ── Step 5: Extract thumbnail at peak-motion frame ─────────────────────────────

def extract_thumbnail(video_path: Path, thumb_path: Path):
    """Extract a single frame at HOOK_THUMB_OFFSET seconds using FFmpeg."""
    result = subprocess.run(
        [
            "ffmpeg", "-y",
            "-ss", str(HOOK_THUMB_OFFSET),
            "-i", str(video_path),
            "-frames:v", "1",
            "-q:v", "2",
            str(thumb_path),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        log(f"  ⚠️  FFmpeg thumbnail extraction failed:\n{result.stderr[-500:]}")
        return
    log(f"  Thumbnail saved: {thumb_path}  (frame at {HOOK_THUMB_OFFSET}s)")


# ── Image hook: Pollinations.ai + FFmpeg Ken Burns ────────────────────────────

def _image_prompts(concept: dict) -> list[str]:
    """Return the 3 still-image prompts generated by Gemini in the concept step."""
    prompts = concept.get("image_prompts")
    if prompts and len(prompts) == 3:
        return prompts
    # Fallback for old concept files that predate the image_prompts field
    base  = concept.get("hook_concept", "")
    style = "cinematic, hyperrealistic, dramatic chiaroscuro lighting, 9:16 vertical portrait"
    return [
        f"Peaceful empty suburban front porch at night, warm glowing porch light, potted plants, welcome mat, white door. {style}",
        f"{base} The frozen peak tension moment, maximum drama, object suspended mid-motion. {style}",
        f"{base} Action progressing, resolution unseen, moment before the cut. {style}",
    ]


def fetch_pollinations_image(prompt: str, output_path: Path, seed: int = 42) -> Path:
    """Download one image from Pollinations.ai (Flux model, free, no API key)."""
    encoded = urllib.parse.quote(prompt)
    url = f"{POLLINATIONS_BASE}/{encoded}?width=720&height=1280&model=flux&seed={seed}&nologo=true"
    log(f"  Fetching from Pollinations.ai (seed={seed})...")
    resp = requests.get(url, timeout=90)
    resp.raise_for_status()
    output_path.write_bytes(resp.content)
    log(f"  Saved: {output_path}  ({len(resp.content) // 1024} KB)")
    return output_path


def build_image_hook(image_paths: list, output_path: Path, thumb_path: Path) -> Path:
    """
    Assemble a 5-second Ken Burns hook video from 3 still images:
      clip 1 (1.5 s) — slow zoom in
      clip 2 (1.5 s) — static hold, the peak-drama frame  → thumbnail
      clip 3 (2.0 s) — slow pan right
    """
    clips = []
    tmp = output_path.parent

    # ── clip 1: slow zoom in (1.0x → 1.12x) ─────────────────────────────────
    c1 = tmp / "_c1.mp4"
    r = subprocess.run([
        "ffmpeg", "-y", "-loop", "1", "-i", str(image_paths[0]),
        "-vf", (
            "scale=720:1280:force_original_aspect_ratio=decrease,"
            "pad=720:1280:(ow-iw)/2:(oh-ih)/2,"
            "zoompan=z='min(zoom+0.00267,1.12)':d=45"
            ":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=720x1280,fps=30"
        ),
        "-t", "1.5", "-pix_fmt", "yuv420p", str(c1),
    ], capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"FFmpeg clip 1 failed:\n{r.stderr.decode()[-600:]}")
    clips.append(c1)

    # ── clip 2: static hold (peak-drama, thumbnail source) ───────────────────
    c2 = tmp / "_c2.mp4"
    r = subprocess.run([
        "ffmpeg", "-y", "-loop", "1", "-i", str(image_paths[1]),
        "-vf", (
            "scale=720:1280:force_original_aspect_ratio=decrease,"
            "pad=720:1280:(ow-iw)/2:(oh-ih)/2,fps=30"
        ),
        "-t", "1.5", "-pix_fmt", "yuv420p", str(c2),
    ], capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"FFmpeg clip 2 failed:\n{r.stderr.decode()[-600:]}")
    clips.append(c2)

    # ── clip 3: slow pan right at 1.12x zoom ─────────────────────────────────
    c3 = tmp / "_c3.mp4"
    r = subprocess.run([
        "ffmpeg", "-y", "-loop", "1", "-i", str(image_paths[2]),
        "-vf", (
            "scale=720:1280:force_original_aspect_ratio=decrease,"
            "pad=720:1280:(ow-iw)/2:(oh-ih)/2,"
            "zoompan=z='1.12':d=60"
            ":x='min(x+1,iw-iw/zoom)':y='ih/2-(ih/zoom/2)':s=720x1280,fps=30"
        ),
        "-t", "2.0", "-pix_fmt", "yuv420p", str(c3),
    ], capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"FFmpeg clip 3 failed:\n{r.stderr.decode()[-600:]}")
    clips.append(c3)

    # ── concat ────────────────────────────────────────────────────────────────
    list_file = tmp / "_clip_list.txt"
    # Use absolute paths so FFmpeg doesn't resolve relative to the list file location
    list_file.write_text("\n".join(f"file '{p.resolve()}'" for p in clips))
    r = subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
        "-c", "copy", str(output_path),
    ], capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"FFmpeg concat failed:\n{r.stderr.decode()[-600:]}")
    log(f"  Hook video: {output_path}  ({output_path.stat().st_size // 1024} KB)")

    # ── thumbnail from the first frame of clip 2 (peak-drama still) ──────────
    r = subprocess.run([
        "ffmpeg", "-y", "-i", str(c2),
        "-frames:v", "1", "-q:v", "2", str(thumb_path),
    ], capture_output=True)
    if r.returncode == 0:
        log(f"  Thumbnail: {thumb_path}")

    # cleanup temp files
    for f in clips + [list_file]:
        try:
            f.unlink()
        except Exception:
            pass

    return output_path


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Amazon deal → hook video prototype")
    parser.add_argument("--url", help="Skip deals scrape, use this Amazon product URL directly")
    parser.add_argument("--image", action="store_true",
                        help="Use Pollinations.ai images + FFmpeg instead of Veo 3")
    parser.add_argument("--skip-concept", action="store_true",
                        help="Reuse existing hook_concept.json (skip Gemini call)")
    parser.add_argument("--seed", type=int, default=42,
                        help="Seed for Pollinations.ai image generation (default: 42)")
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(exist_ok=True)
    concept_out = OUTPUT_DIR / "hook_concept.json"

    # ── Step 1: Get product (skip if reusing existing concept) ───────────────
    if args.skip_concept:
        if not concept_out.exists():
            sys.exit(f"--skip-concept set but {concept_out} not found. Run without it first.")
        concept = json.loads(concept_out.read_text())
        title   = concept.get("product_title", "Unknown")
        log(f"  Reusing existing concept for: {title[:80]}")
    else:
        keys    = load_keys()
        api_key = keys[0]

        log("\n═══ Step 1: Get Amazon product ═══")
        if args.url:
            product_url = args.url
            log(f"  Using provided URL: {product_url}")
        else:
            product_url = get_first_deal_url()
            log(f"  Found deal: {product_url}")

        title, bullets, price = get_product_info(product_url)
        log(f"  Title: {title[:90]}")
        if price:
            log(f"  Price: {price}")
        log("  Bullets:")
        for b in bullets:
            log(f"    • {b[:100]}")

        # ── Step 2: Hook concept via Gemini ──────────────────────────────────
        log("\n═══ Step 2: Generate hook concept (Gemini) ═══")
        concept = generate_hook_concept(title, bullets, keys)

        log(f"\n  Pinpoint:     {concept['pinpoint']}")
        log(f"  Persona:      {concept['persona']}")
        log(f"  Hook concept: {concept['hook_concept']}")

        concept_out.write_text(json.dumps(
            {"product_title": title, "product_url": product_url, **concept},
            indent=2,
            ensure_ascii=False,
        ))
        log(f"\n  Concept saved → {concept_out}")

    # ── Step 3a: Image hook (Pollinations.ai + FFmpeg) ────────────────────────
    if args.image:
        log("\n═══ Step 3: Generate image hook (Pollinations.ai + FFmpeg) ═══")
        prompts     = _image_prompts(concept)
        image_paths = []
        for i, prompt in enumerate(prompts, 1):
            img_path = OUTPUT_DIR / f"hook_img_{i}.jpg"
            fetch_pollinations_image(prompt, img_path, seed=args.seed + i)
            image_paths.append(img_path)

        log("\n  Assembling Ken Burns animation...")
        hook_path  = OUTPUT_DIR / "hook_image.mp4"
        thumb_path = OUTPUT_DIR / "hook_image_thumbnail.jpg"
        build_image_hook(image_paths, hook_path, thumb_path)

        log("\n" + "═" * 60)
        log("✓ COMPLETE  (image hook)")
        log(f"  hook_image.mp4            → 5s animated hook clip")
        log(f"  hook_image_thumbnail.jpg  → peak-drama frame, use as FB thumbnail")
        log(f"  hook_img_1/2/3.jpg        → individual frames for review")
        log("═" * 60)
        return

    # ── Step 3b: Veo 3 hook video (original path) ────────────────────────────
    log("\n═══ Step 3: Generate Veo 3 hook video ═══")
    if args.skip_concept:
        keys    = load_keys()
        api_key = keys[0]
    hook_path = OUTPUT_DIR / "hook.mp4"
    video_result = generate_veo3_hook(concept["veo3_prompt"], api_key, hook_path)

    if video_result is None:
        log("\n  Stopped after hook concept — Veo 3 access not available on this key.")
        log(f"  Tip: rerun with --image to generate a free animated image hook instead.")
        return

    # ── Step 4: Thumbnail ─────────────────────────────────────────────────────
    log("\n═══ Step 4: Extract peak-motion thumbnail ═══")
    thumb_path = OUTPUT_DIR / "hook_thumbnail.jpg"
    extract_thumbnail(hook_path, thumb_path)

    log("\n" + "═" * 60)
    log("✓ COMPLETE")
    log(f"  hook.mp4           → upload as the first 5s of your product video")
    log(f"  hook_thumbnail.jpg → set as custom thumbnail on FB and YouTube")
    log(f"  hook_concept.json  → review/adjust prompt for next iteration")
    log("═" * 60)


if __name__ == "__main__":
    main()
