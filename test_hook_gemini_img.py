#!/usr/bin/env python3
"""
test_hook_gemini_img.py — Hook image variant using Gemini Imagen (free-tier keys)

Uses the SAME geminikey.txt keys as the main scraper.
Generates 3 photorealistic stills via Gemini image generation, then
assembles them into a 5-second Ken Burns hook clip with FFmpeg.

Image model tried in order:
  1. imagen-3.0-generate-001     (Imagen 3, highest quality)
  2. imagen-3.0-fast-generate-001 (faster, still good)

The key difference from Pollinations: prompts are DIRECT product scenes
(the actual pain-point situation), not surreal abstract metaphors.

Usage:
  python test_hook_gemini_img.py --url "https://www.amazon.com/dp/ASIN"
  python test_hook_gemini_img.py --skip-concept          # reuse hook_concept.json
  python test_hook_gemini_img.py --skip-concept --seed 7 # different image variation
"""

import os, re, sys, json, time, base64, argparse, subprocess, urllib.parse
import requests
from pathlib import Path
from bs4 import BeautifulSoup

# ── Config ─────────────────────────────────────────────────────────────────────
GEMINI_KEYS_FILE = Path.home() / "geminikey.txt"
GEMINI_API_BASE  = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_TEXT_MODEL = "gemini-2.5-flash"

IMAGEN_MODELS = [
    "imagen-4.0-generate-preview-05-20",   # Imagen 4 (newest)
    "imagen-3.0-generate-001",
    "imagen-3.0-fast-generate-001",
]

# Gemini multimodal image generation via generateContent + responseModalities.
# gemini-2.5-flash is already confirmed working for text on these free keys —
# it also supports image output via the same endpoint.
GEMINI_IMG_MODELS = [
    "gemini-2.0-flash-preview-image-generation",
    "gemini-2.0-flash-exp",
]

OUTPUT_DIR = Path("hook_test_output")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# ── Meta-prompt ────────────────────────────────────────────────────────────────
# KEY DIFFERENCE: STEP 5 asks for DIRECT product-related scenes —
# the actual pain point in a dramatic, staged-photo style — not surreal metaphors.

META_PROMPT = """\
You are a viral social media video strategist and product photographer.
Your job: given a product, produce a 5-second hook concept that stops scrollers cold,
using 3 still images animated with Ken Burns motion.

PRODUCT: {product_name}
TOP BENEFITS:
{bullets}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STEP 1 — PINPOINT
The single most universal pain point, frustration, or fear this product addresses.
Something a random person mid-scroll recognizes INSTANTLY.

STEP 2 — PERSONA
The SPECIFIC person who feels this most intensely. Be vivid and concrete.
Bad: "homeowners"
Good: "A first-time homeowner who woke up at 3 AM to a noise outside and lay paralyzed, afraid to check"

STEP 3 — HOOK CONCEPT
A dramatic, real-world SCENE that exaggerates the pain point.
Rules:
• Must feel like a moment from a thriller movie or a dramatic news photo
• Must involve VISIBLE TENSION — something about to happen or just happening
• Do NOT show the product yet
• A person mid-scroll must think: "wait, is this real? what happens next?"

STEP 4 — THREE-FRAME STORY
Describe the scene as 3 sequential photographic moments (BEFORE / PEAK / UNRESOLVED).
Think of it like 3 frames from a film strip:
  BEFORE: calm, but something is slightly off — foreshadowing
  PEAK: the most dramatic frozen moment — maximum visual tension
  UNRESOLVED: action has progressed but outcome unknown — the clip cuts here

STEP 5 — IMAGE PROMPTS
Write 3 still-image prompts for Imagen 3 (photorealistic AI image generation).
CRITICAL RULES:
• Each prompt must describe a SINGLE FROZEN FRAME, like a film still or press photo
• The scene must be DIRECTLY recognizable as the pain point for this product category
• Do NOT use surreal metaphors, impossible scales, or abstract concepts
• Do NOT show the actual product
• Think: dramatic product advertisement photography meets thriller movie still
• Every viewer must immediately think "I need that product" — not "what is this?"
• Be hyper-specific: exact subject, exact position in frame, exact lighting, camera angle
• Do NOT write "a scene of" or "phase" — just describe what is physically in the image

Each prompt must end with:
"photorealistic, cinematic, dramatic lighting, ultra-sharp focus, 9:16 vertical portrait"

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Respond ONLY in valid JSON. No markdown. No code fences. No explanation outside the JSON.

{{
  "pinpoint": "...",
  "persona": "...",
  "hook_concept": "one paragraph — a dramatic real-world scene that a viewer instantly relates to",
  "image_prompts": [
    "BEFORE frame: hyper-specific frozen-frame description for Imagen 3",
    "PEAK frame: hyper-specific frozen-frame description for Imagen 3",
    "UNRESOLVED frame: hyper-specific frozen-frame description for Imagen 3"
  ]
}}
"""


# ── Helpers ────────────────────────────────────────────────────────────────────

def log(msg: str):
    print(msg, flush=True)


def load_keys() -> list[str]:
    if not GEMINI_KEYS_FILE.exists():
        sys.exit(f"Key file not found: {GEMINI_KEYS_FILE}")
    keys = [k.strip() for k in GEMINI_KEYS_FILE.read_text().splitlines() if k.strip()]
    if not keys:
        sys.exit(f"No keys in {GEMINI_KEYS_FILE}")
    log(f"  Loaded {len(keys)} Gemini keys")
    return keys


# ── Step 1: Amazon product ─────────────────────────────────────────────────────

def get_first_deal_url() -> str:
    resp = requests.get("https://www.amazon.com/deals", headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    for a in soup.find_all("a", href=True):
        m = re.search(r"/dp/([A-Z0-9]{10})", a["href"])
        if m:
            return f"https://www.amazon.com/dp/{m.group(1)}"
    raise RuntimeError("No /dp/ links found — run with --url")


def _parse_bullets(soup) -> tuple:
    title_el = soup.select_one("#productTitle")
    title = title_el.get_text(strip=True) if title_el else "Unknown Product"
    bullets = []
    for selector in [
        "#feature-bullets li span.a-list-item",
        "#productFactsDesktop_feature_div li",
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
    for sel in [".a-price .a-offscreen", "#priceblock_ourprice"]:
        el = soup.select_one(sel)
        if el:
            price = el.get_text(strip=True)
            break
    return title, bullets[:3], price


def get_product_info(url: str) -> tuple:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        title, bullets, price = _parse_bullets(BeautifulSoup(resp.text, "html.parser"))
        if bullets:
            return title, bullets, price
    except Exception as e:
        log(f"  requests failed ({e}) — trying Selenium...")
    # Selenium fallback
    import undetected_chromedriver as uc
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    opts = uc.ChromeOptions()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    driver = uc.Chrome(options=opts)
    try:
        driver.get(url)
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.ID, "feature-bullets"))
        )
        return _parse_bullets(BeautifulSoup(driver.page_source, "html.parser"))
    finally:
        try:
            driver.quit()
        except Exception:
            pass


# ── Step 2: Hook concept via Gemini text ──────────────────────────────────────

def generate_hook_concept(title: str, bullets: list, keys: list) -> dict:
    bullets_text = "\n".join(f"• {b}" for b in bullets)
    prompt_text  = META_PROMPT.format(product_name=title, bullets=bullets_text)
    payload = {
        "contents": [{"parts": [{"text": prompt_text}]}],
        "generationConfig": {"temperature": 0.9, "maxOutputTokens": 8192},
    }
    for i, key in enumerate(keys):
        url = f"{GEMINI_API_BASE}/models/{GEMINI_TEXT_MODEL}:generateContent?key={key}"
        log(f"  Gemini text (key {i+1}/{len(keys)})...")
        resp = requests.post(url, json=payload, timeout=60)
        if resp.status_code == 429:
            log(f"  Key {i+1} → 429 — trying next...")
            continue
        if resp.status_code != 200:
            log(f"  Key {i+1} → {resp.status_code} — trying next...")
            continue
        raw = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE).strip()
        return json.loads(raw)
    raise RuntimeError("All Gemini keys exhausted for concept generation.")


# ── Step 3: Imagen 3 still images ─────────────────────────────────────────────

def _save_img_bytes(b64: str, output_path: Path) -> Path:
    output_path.write_bytes(base64.b64decode(b64))
    log(f"  Saved: {output_path}  ({output_path.stat().st_size // 1024} KB)")
    return output_path


def generate_imagen(prompt: str, keys: list, output_path: Path, seed: int = None) -> Path:
    """Generate one image. Tries Imagen 3 first, falls back to Gemini Flash image gen."""

    # ── Path A: Imagen 3 (high quality, needs special access) ────────────────
    imagen_payload = {
        "instances": [{"prompt": prompt}],
        "parameters": {
            "sampleCount": 1,
            "aspectRatio": "9:16",
            "safetyFilterLevel": "block_only_high",
            "personGeneration": "allow_adult",
        },
    }
    if seed is not None:
        imagen_payload["parameters"]["seed"] = seed

    for model in IMAGEN_MODELS:
        url_tpl = f"{GEMINI_API_BASE}/models/{model}:predict?key={{key}}"
        all_404_for_model = True
        for i, key in enumerate(keys):
            log(f"  Imagen ({model}, key {i+1}/{len(keys)})...")
            resp = requests.post(url_tpl.format(key=key), json=imagen_payload, timeout=90)
            if resp.status_code == 404:
                # 404 may be tier-dependent (billing unlocks the model) — try all keys
                log(f"  Key {i+1} → 404 (tier/billing restriction — trying next key)")
                continue
            all_404_for_model = False
            if resp.status_code == 429:
                log(f"  Key {i+1} → 429 — trying next key...")
                continue
            if resp.status_code not in (200,):
                log(f"  Key {i+1} → {resp.status_code}: {resp.text[:300]}")
                continue
            predictions = resp.json().get("predictions", [])
            if predictions and predictions[0].get("bytesBase64Encoded"):
                return _save_img_bytes(predictions[0]["bytesBase64Encoded"], output_path)
        if all_404_for_model:
            log(f"  All keys → 404 for {model} — trying next model...")

    log("  Imagen exhausted — falling back to Gemini Flash image generation...")

    # ── Path B: Gemini Flash native image generation (works on free-tier keys) ─
    # Append vertical format hint to the prompt since we can't pass aspectRatio
    flash_prompt = prompt.rstrip(".") + ", 9:16 vertical portrait format."
    flash_payload = {
        "contents": [{"parts": [{"text": flash_prompt}]}],
        "generationConfig": {"responseModalities": ["IMAGE"], "temperature": 1.0},
    }
    for model in GEMINI_IMG_MODELS:
        url_tpl = f"{GEMINI_API_BASE}/models/{model}:generateContent?key={{key}}"
        for i, key in enumerate(keys):
            log(f"  Gemini Flash img ({model}, key {i+1}/{len(keys)})...")
            resp = requests.post(url_tpl.format(key=key), json=flash_payload, timeout=90)
            if resp.status_code == 404:
                log(f"  {model} → 404, trying next model...")
                break
            if resp.status_code == 429:
                log(f"  Key {i+1} → 429 — trying next key...")
                continue
            if resp.status_code not in (200,):
                log(f"  Key {i+1} → {resp.status_code}: {resp.text[:200]}")
                continue
            # Extract inlineData image from response parts
            parts = resp.json().get("candidates", [{}])[0].get("content", {}).get("parts", [])
            for part in parts:
                inline = part.get("inlineData", {})
                if inline.get("data"):
                    return _save_img_bytes(inline["data"], output_path)
            log(f"  No image in response parts — trying next key...")

    raise RuntimeError(
        "All image generation paths failed.\n"
        "  • Imagen 3: needs special API access (apply at aistudio.google.com)\n"
        "  • Gemini Flash img: model may not be available on your key tier\n"
        "Try running test_hook_pollinations.py as a no-key fallback."
    )


# ── Step 4: FFmpeg Ken Burns animation ────────────────────────────────────────

def build_image_hook(image_paths: list, output_path: Path, thumb_path: Path) -> Path:
    """
    5-second Ken Burns hook from 3 stills:
      clip 1 (1.5s) — slow zoom in
      clip 2 (1.5s) — static hold  → thumbnail
      clip 3 (2.0s) — slow pan right
    """
    tmp = output_path.parent
    clips = []

    # clip 1: slow zoom in 1.0x → 1.12x
    c1 = tmp / "_gi_c1.mp4"
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

    # clip 2: static hold (peak-drama — use as thumbnail)
    c2 = tmp / "_gi_c2.mp4"
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

    # clip 3: slow pan right at 1.12x zoom
    c3 = tmp / "_gi_c3.mp4"
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

    # concat
    list_file = tmp / "_gi_clip_list.txt"
    list_file.write_text("\n".join(f"file '{p.resolve()}'" for p in clips))
    r = subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
        "-c", "copy", str(output_path),
    ], capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"FFmpeg concat failed:\n{r.stderr.decode()[-600:]}")
    log(f"  Hook video: {output_path}  ({output_path.stat().st_size // 1024} KB)")

    # thumbnail from first frame of clip 2
    r = subprocess.run([
        "ffmpeg", "-y", "-i", str(c2),
        "-frames:v", "1", "-q:v", "2", str(thumb_path),
    ], capture_output=True)
    if r.returncode == 0:
        log(f"  Thumbnail: {thumb_path}")

    for f in clips + [list_file]:
        try:
            f.unlink()
        except Exception:
            pass

    return output_path


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Amazon product → Gemini Imagen hook clip")
    parser.add_argument("--url",          help="Amazon product URL (skip deals scrape)")
    parser.add_argument("--skip-concept", action="store_true",
                        help="Reuse existing hook_concept_imagen.json (no Gemini text call)")
    parser.add_argument("--seed",         type=int, default=None,
                        help="Imagen seed for reproducible images")
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(exist_ok=True)
    concept_out = OUTPUT_DIR / "hook_concept_imagen.json"

    # ── Step 1 & 2: product + concept ────────────────────────────────────────
    if args.skip_concept:
        if not concept_out.exists():
            sys.exit(f"--skip-concept set but {concept_out} not found.")
        concept = json.loads(concept_out.read_text())
        title   = concept.get("product_title", "Unknown")
        log(f"  Reusing concept: {title[:80]}")
    else:
        keys = load_keys()

        log("\n═══ Step 1: Get Amazon product ═══")
        if args.url:
            product_url = args.url
            log(f"  URL: {product_url}")
        else:
            product_url = get_first_deal_url()
            log(f"  Found: {product_url}")

        title, bullets, price = get_product_info(product_url)
        log(f"  Title: {title[:90]}")
        if price:
            log(f"  Price: {price}")
        for b in bullets:
            log(f"    • {b[:100]}")

        log("\n═══ Step 2: Hook concept (Gemini text) ═══")
        concept = generate_hook_concept(title, bullets, keys)
        log(f"\n  Pinpoint:  {concept['pinpoint']}")
        log(f"  Persona:   {concept['persona']}")
        log(f"  Concept:   {concept['hook_concept']}")
        log("\n  Image prompts:")
        for idx, p in enumerate(concept.get("image_prompts", []), 1):
            log(f"    [{idx}] {p[:120]}")

        concept_out.write_text(json.dumps(
            {"product_title": title, "product_url": product_url, **concept},
            indent=2, ensure_ascii=False,
        ))
        log(f"\n  Concept saved → {concept_out}")

    # ── Step 3: Generate images via Imagen ───────────────────────────────────
    log("\n═══ Step 3: Imagen 3 still images ═══")
    keys = load_keys()
    image_prompts = concept.get("image_prompts", [])
    if len(image_prompts) != 3:
        sys.exit("Concept missing 'image_prompts' array of 3. Delete concept file and rerun.")

    image_paths = []
    for i, prompt in enumerate(image_prompts, 1):
        img_path = OUTPUT_DIR / f"hook_gi_{i}.jpg"
        seed = (args.seed + i) if args.seed is not None else None
        generate_imagen(prompt, keys, img_path, seed=seed)
        image_paths.append(img_path)

    # ── Step 4: Assemble hook video ───────────────────────────────────────────
    log("\n═══ Step 4: Assemble Ken Burns hook clip ═══")
    hook_path  = OUTPUT_DIR / "hook_gemini_img.mp4"
    thumb_path = OUTPUT_DIR / "hook_gi_thumbnail.jpg"
    build_image_hook(image_paths, hook_path, thumb_path)

    log("\n" + "═" * 60)
    log("✓ COMPLETE  (Gemini Imagen hook)")
    log(f"  hook_gemini_img.mp4    → 5-second animated hook clip")
    log(f"  hook_gi_thumbnail.jpg  → peak-drama frame for thumbnail")
    log(f"  hook_gi_1/2/3.jpg      → individual frames for quality review")
    log("═" * 60)


if __name__ == "__main__":
    main()
