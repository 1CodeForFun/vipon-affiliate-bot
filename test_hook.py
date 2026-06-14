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

import os, re, sys, json, time, base64, argparse, subprocess
import requests
from pathlib import Path
from bs4 import BeautifulSoup

# ── Config ─────────────────────────────────────────────────────────────────────
GEMINI_PRO_KEY_FILE = Path.home() / "geminipro.txt"
AMAZON_DEALS_URL    = "https://www.amazon.com/deals"

# Veo 3 model name — verify at https://ai.google.dev/api if this returns 404
VEO_MODEL           = "veo-003"
HOOK_DURATION_SEC   = 5
HOOK_THUMB_OFFSET   = 2.5    # seconds — where peak-motion frame lives

# Gemini model for concept generation
GEMINI_MODEL        = "gemini-2.5-pro"    # Pro key; swap to "gemini-2.0-flash" for free-tier keys

GEMINI_API_BASE     = "https://generativelanguage.googleapis.com/v1beta"
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

def load_key() -> str:
    if not GEMINI_PRO_KEY_FILE.exists():
        sys.exit(f"Key file not found: {GEMINI_PRO_KEY_FILE}")
    key = GEMINI_PRO_KEY_FILE.read_text().strip().splitlines()[0].strip()
    if not key:
        sys.exit(f"Key file is empty: {GEMINI_PRO_KEY_FILE}")
    return key


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

STEP 4 — VEO 3 VIDEO PROMPT
Write a Veo 3 generation prompt using EXACTLY this structure. Be specific and cinematic.

OPENING (0–2s): [Describe the scene and building action. Set the world. Show the problem escalating.
                 The viewer must feel something is about to go very wrong or very right.]

PEAK (2–3s): [THIS IS THE THUMBNAIL FRAME — the frame at exactly 2.5 seconds.
              An object or person must be SUSPENDED MID-MOTION: mid-fall, mid-spin, mid-crash,
              mid-explosion, mid-tip. The composition must look like a freeze-frame that makes
              any viewer ask "what happens in the next second?"
              Camera: extreme close-up OR wide dramatic angle. Maximum visual tension.
              Do NOT resolve the action here — just hold it at the peak of uncertainty.]

RESOLUTION (3–5s): [The motion continues but does NOT fully complete. End the clip at the
                    most suspenseful possible moment before resolution. Leave it hanging.
                    The viewer must feel compelled to click Play to see what happens.]

AUDIO: [Frame-by-frame sound design — e.g.: "0-1s: distant rumble building; 1-2s: slow-motion
        whoosh, items sliding; 2-3s: a split-second of silence + audible gasp; 3-5s: bass drop
        building but cutting off before impact". Be specific — Veo 3 generates sync audio.]

STYLE: Cinematic 4K. Ultra-dramatic slow-motion at peak (2–3s). High contrast.
       Dramatic lighting — either golden-hour warmth or harsh studio spotlight on subject.
       Shot style: mix of close-up for detail and wide for scale. High-budget commercial look.
       Color grade: vivid, punchy, slightly over-exposed highlights.

ASPECT RATIO: 9:16 vertical (social media / Reels / Shorts / TikTok).

CRITICAL CONSTRAINT: The thumbnail is automatically extracted at exactly 2.5 seconds.
The composition at that precise moment must be the most dramatic, emotion-triggering,
curiosity-inducing frame in the entire clip. Design the entire video around this frame.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Respond ONLY in valid JSON. No markdown. No code fences. No explanation outside the JSON.

{{
  "pinpoint": "...",
  "persona": "...",
  "hook_concept": "one paragraph describing the visual narrative",
  "veo3_prompt": "OPENING (0-2s): ...\\n\\nPEAK (2-3s): ...\\n\\nRESOLUTION (3-5s): ...\\n\\nAUDIO: ...\\n\\nSTYLE: ...\\n\\nASPECT RATIO: 9:16 vertical"
}}
"""


def generate_hook_concept(title: str, bullets: list[str], api_key: str) -> dict:
    bullets_text = "\n".join(f"• {b}" for b in bullets)
    prompt_text = META_PROMPT.format(product_name=title, bullets=bullets_text)

    # Try models in order — Pro first, then fallbacks if key lacks access
    models_to_try = [GEMINI_MODEL, "gemini-1.5-pro-latest", "gemini-2.0-flash", "gemini-1.5-flash"]
    payload = {
        "contents": [{"parts": [{"text": prompt_text}]}],
        "generationConfig": {"temperature": 0.9, "maxOutputTokens": 1500},
    }

    for model in models_to_try:
        url = f"{GEMINI_API_BASE}/models/{model}:generateContent?key={api_key}"
        for attempt in range(3):
            log(f"  Calling Gemini ({model}) attempt {attempt + 1}...")
            resp = requests.post(url, json=payload, timeout=60)
            if resp.status_code == 429:
                wait = 15 * (attempt + 1)
                log(f"  429 rate-limit — waiting {wait}s before retry...")
                time.sleep(wait)
                continue
            if resp.status_code in (400, 404):
                log(f"  Model {model} not available ({resp.status_code}) — trying next...")
                break  # try next model
            resp.raise_for_status()
            # Success
            raw = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE).strip()
            try:
                return json.loads(raw)
            except json.JSONDecodeError as e:
                log(f"  ⚠️  JSON parse failed: {e}\n  Raw:\n{raw}")
                raise
        else:
            log(f"  {model} exhausted retries — trying next model...")

    raise RuntimeError("All Gemini models failed — check API key and quota.")


# ── Step 4: Veo 3 → generate hook video ───────────────────────────────────────

def generate_veo3_hook(veo3_prompt: str, api_key: str, output_path: Path) -> Path:
    """Submit a Veo 3 generation job, poll until done, save to output_path."""

    submit_url = f"{GEMINI_API_BASE}/models/{VEO_MODEL}:generateVideo?key={api_key}"
    payload = {
        "prompt": veo3_prompt,
        "generationConfig": {
            "durationSeconds": HOOK_DURATION_SEC,
            "aspectRatio": "9:16",
        },
    }

    log(f"  Submitting to Veo 3 ({VEO_MODEL})...")
    resp = requests.post(submit_url, json=payload, timeout=30)

    if resp.status_code == 403:
        log("\n  ✗ Veo 3 returned 403 Forbidden.")
        log("    This key does not have video generation access.")
        log("    Apply for Veo 3 access at: https://aistudio.google.com")
        log("    Concept + Veo3 prompt are saved — you can paste the prompt manually.")
        return None

    if resp.status_code == 404:
        log(f"\n  ✗ Veo 3 model '{VEO_MODEL}' not found (404).")
        log("    Try changing VEO_MODEL in this script to: veo-2.0-generate-001 or veo-3.0-generate-001")
        log("    Check available models: https://ai.google.dev/api/generate-content#models")
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


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Amazon deal → Veo 3 hook video prototype")
    parser.add_argument("--url", help="Skip deals scrape, use this Amazon product URL directly")
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(exist_ok=True)
    api_key = load_key()

    # ── Step 1: Get product ───────────────────────────────────────────────────
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

    # ── Step 2: Hook concept via Gemini ──────────────────────────────────────
    log("\n═══ Step 2: Generate hook concept (Gemini) ═══")
    concept = generate_hook_concept(title, bullets, api_key)

    log(f"\n  Pinpoint:     {concept['pinpoint']}")
    log(f"  Persona:      {concept['persona']}")
    log(f"  Hook concept: {concept['hook_concept']}")
    log(f"\n  ── Veo 3 Prompt ──────────────────────────")
    for line in concept["veo3_prompt"].split("\n"):
        log(f"  {line}")
    log("  ──────────────────────────────────────────")

    concept_out = OUTPUT_DIR / "hook_concept.json"
    concept_out.write_text(json.dumps(
        {"product_title": title, "product_url": product_url, **concept},
        indent=2,
        ensure_ascii=False,
    ))
    log(f"\n  Concept saved → {concept_out}")

    # ── Step 3: Veo 3 hook video ──────────────────────────────────────────────
    log("\n═══ Step 3: Generate Veo 3 hook video ═══")
    hook_path = OUTPUT_DIR / "hook.mp4"
    video_result = generate_veo3_hook(concept["veo3_prompt"], api_key, hook_path)

    if video_result is None:
        log("\n  Stopped after hook concept — Veo 3 access not available on this key.")
        log(f"  Paste the Veo 3 prompt from {concept_out} into https://aistudio.google.com to test manually.")
        return

    # ── Step 4: Thumbnail ─────────────────────────────────────────────────────
    log("\n═══ Step 4: Extract peak-motion thumbnail ═══")
    thumb_path = OUTPUT_DIR / "hook_thumbnail.jpg"
    extract_thumbnail(hook_path, thumb_path)

    # ── Summary ───────────────────────────────────────────────────────────────
    log("\n" + "═" * 60)
    log("✓ COMPLETE")
    log(f"  hook.mp4           → upload as the first 5s of your product video")
    log(f"  hook_thumbnail.jpg → set as custom thumbnail on FB and YouTube")
    log(f"  hook_concept.json  → review/adjust prompt for next iteration")
    log("═" * 60)


if __name__ == "__main__":
    main()
