#!/usr/bin/env python3
"""
test_hook_hf.py — Hook image variant using HuggingFace Inference API (free)

Uses a free HuggingFace token (hftoken.txt) to generate images via
FLUX.1-schnell — the same model behind Pollinations.ai but with direct
API control over dimensions, steps, and seed.

Setup (one-time):
  1. Sign up free at huggingface.co
  2. Go to Settings → Access Tokens → New token (type: Read)
  3. Save the token to: C:\\Users\\<you>\\hftoken.txt

Models tried in order:
  1. black-forest-labs/FLUX.1-schnell  (4-step Flux, best speed/quality)
  2. black-forest-labs/FLUX.1-dev      (28-step Flux, higher quality, slower)
  3. stabilityai/stable-diffusion-xl-base-1.0  (SDXL fallback)

Usage:
  python test_hook_hf.py --url "https://www.amazon.com/dp/ASIN"
  python test_hook_hf.py --skip-concept          # reuse hook_concept_hf.json
  python test_hook_hf.py --skip-concept --seed 7 # try a different variation
  python test_hook_hf.py --skip-concept --model FLUX.1-dev  # try higher quality
"""

import os, re, sys, json, time, base64, argparse, subprocess
import requests
from pathlib import Path
from bs4 import BeautifulSoup

# ── Config ─────────────────────────────────────────────────────────────────────
GEMINI_KEYS_FILE = Path.home() / "geminikey.txt"
HF_TOKEN_FILE    = Path.home() / "hftoken.txt"
GEMINI_API_BASE  = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_TEXT_MODEL = "gemini-2.5-flash"
HF_API_BASE      = "https://api-inference.huggingface.co/models"

HF_MODELS = [
    "black-forest-labs/FLUX.1-schnell",
    "black-forest-labs/FLUX.1-dev",
    "stabilityai/stable-diffusion-xl-base-1.0",
]

OUTPUT_DIR = Path("hook_test_output")

HEADERS_AMAZON = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# ── Meta-prompt (same direct-scene approach as Gemini Imagen script) ───────────
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
Good: "A first-time homeowner who woke up at 3 AM to a noise outside and lay paralyzed"

STEP 3 — HOOK CONCEPT
A dramatic, real-world SCENE that exaggerates the pain point.
Must feel like a moment from a thriller movie or a dramatic news photo.

STEP 4 — IMAGE PROMPTS
Write 3 still-image prompts for FLUX (photorealistic AI image generation).
CRITICAL RULES:
• Each prompt = ONE frozen frame, like a film still or news photo
• DIRECTLY show the pain point the product solves — viewer must instantly think "I need that"
• No surreal metaphors, impossible scales, or abstract concepts
• No product shown yet
• Be hyper-specific: subject position, lighting direction, camera angle, mood
• Do NOT write "a scene of" — describe what is physically IN the image

Frame 1 — BEFORE: the calm state with foreshadowing (something slightly off)
Frame 2 — PEAK: the frozen most-dramatic moment, maximum tension (this becomes the thumbnail)
Frame 3 — UNRESOLVED: action has progressed, outcome unknown

Each prompt must end with:
"photorealistic, cinematic, dramatic lighting, ultra-sharp, 9:16 vertical"

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Respond ONLY in valid JSON. No markdown. No code fences.

{{
  "pinpoint": "...",
  "persona": "...",
  "hook_concept": "one dramatic paragraph — a real-world scene the viewer instantly relates to",
  "image_prompts": [
    "BEFORE frame description",
    "PEAK frame description",
    "UNRESOLVED frame description"
  ]
}}
"""


# ── Helpers ────────────────────────────────────────────────────────────────────

def log(msg: str):
    print(msg, flush=True)


def load_gemini_keys() -> list:
    if not GEMINI_KEYS_FILE.exists():
        sys.exit(f"Key file not found: {GEMINI_KEYS_FILE}")
    keys = [k.strip() for k in GEMINI_KEYS_FILE.read_text().splitlines() if k.strip()]
    log(f"  Loaded {len(keys)} Gemini keys")
    return keys


def load_hf_token() -> str:
    if not HF_TOKEN_FILE.exists():
        sys.exit(
            f"\n  HuggingFace token not found at {HF_TOKEN_FILE}\n"
            "  Steps:\n"
            "    1. Sign up free at https://huggingface.co\n"
            "    2. Go to Settings → Access Tokens → New token (Read)\n"
            f"    3. Save the token text to {HF_TOKEN_FILE}"
        )
    token = HF_TOKEN_FILE.read_text().strip()
    if not token:
        sys.exit(f"hftoken.txt is empty.")
    return token


# ── Amazon product ─────────────────────────────────────────────────────────────

def get_first_deal_url() -> str:
    resp = requests.get("https://www.amazon.com/deals", headers=HEADERS_AMAZON, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    for a in soup.find_all("a", href=True):
        m = re.search(r"/dp/([A-Z0-9]{10})", a["href"])
        if m:
            return f"https://www.amazon.com/dp/{m.group(1)}"
    raise RuntimeError("No /dp/ links found — run with --url")


def _parse_bullets(soup) -> tuple:
    title_el = soup.select_one("#productTitle")
    title    = title_el.get_text(strip=True) if title_el else "Unknown Product"
    bullets  = []
    for selector in ["#feature-bullets li span.a-list-item", "#productFactsDesktop_feature_div li"]:
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
        resp = requests.get(url, headers=HEADERS_AMAZON, timeout=20)
        resp.raise_for_status()
        title, bullets, price = _parse_bullets(BeautifulSoup(resp.text, "html.parser"))
        if bullets:
            return title, bullets, price
    except Exception as e:
        log(f"  requests failed ({e}) — trying Selenium...")
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
        WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.ID, "feature-bullets")))
        return _parse_bullets(BeautifulSoup(driver.page_source, "html.parser"))
    finally:
        try:
            driver.quit()
        except Exception:
            pass


# ── Gemini concept generation ──────────────────────────────────────────────────

def generate_hook_concept(title: str, bullets: list, keys: list) -> dict:
    bullets_text = "\n".join(f"• {b}" for b in bullets)
    payload = {
        "contents": [{"parts": [{"text": META_PROMPT.format(product_name=title, bullets=bullets_text)}]}],
        "generationConfig": {"temperature": 0.9, "maxOutputTokens": 8192},
    }
    for i, key in enumerate(keys):
        log(f"  Gemini (key {i+1}/{len(keys)})...")
        resp = requests.post(
            f"{GEMINI_API_BASE}/models/{GEMINI_TEXT_MODEL}:generateContent?key={key}",
            json=payload, timeout=60,
        )
        if resp.status_code == 429:
            log(f"  Key {i+1} → 429 — next...")
            continue
        if resp.status_code != 200:
            log(f"  Key {i+1} → {resp.status_code} — next...")
            continue
        raw = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE).strip()
        return json.loads(raw)
    raise RuntimeError("All Gemini keys exhausted.")


# ── HuggingFace image generation ──────────────────────────────────────────────

def generate_hf_image(prompt: str, hf_token: str, output_path: Path,
                      model_override: str = None, seed: int = None) -> Path:
    """Generate one image via HuggingFace Inference API."""
    models_to_try = [model_override] if model_override else HF_MODELS
    auth_headers  = {"Authorization": f"Bearer {hf_token}"}

    for model in models_to_try:
        url = f"{HF_API_BASE}/{model}"
        log(f"  HF Inference ({model})...")

        # Build payload — parameters vary slightly by model family
        is_flux_schnell = "schnell" in model.lower()
        params = {
            "width":  720,
            "height": 1280,
            "num_inference_steps": 4 if is_flux_schnell else 20,
        }
        if not is_flux_schnell:
            params["guidance_scale"] = 7.5
        if seed is not None:
            params["seed"] = seed

        payload = {"inputs": prompt, "parameters": params}

        for attempt in range(4):
            resp = requests.post(url, headers=auth_headers, json=payload, timeout=120)

            if resp.status_code == 200:
                output_path.write_bytes(resp.content)
                log(f"  Saved: {output_path}  ({len(resp.content) // 1024} KB)")
                return output_path

            if resp.status_code == 503:
                try:
                    wait = float(resp.json().get("estimated_time", 20))
                except Exception:
                    wait = 20
                wait = min(wait, 45)
                log(f"  Model loading — waiting {wait:.0f}s (attempt {attempt+1}/4)...")
                time.sleep(wait)
                continue

            if resp.status_code == 404:
                log(f"  {model} → 404, trying next model...")
                break

            if resp.status_code == 401:
                sys.exit(
                    "\n  HuggingFace token rejected (401).\n"
                    f"  Check token in {HF_TOKEN_FILE} — it must be a valid Read token."
                )

            if resp.status_code == 429:
                log(f"  Rate limited (429) — waiting 30s...")
                time.sleep(30)
                continue

            log(f"  {model} → {resp.status_code}: {resp.text[:200]}")
            break

    raise RuntimeError(
        "All HuggingFace models failed.\n"
        "  Check https://status.huggingface.co or try again later.\n"
        "  Free tier may be temporarily overloaded — Pollinations is the no-auth fallback."
    )


# ── FFmpeg Ken Burns animation ─────────────────────────────────────────────────

def build_image_hook(image_paths: list, output_path: Path, thumb_path: Path) -> Path:
    """5-second Ken Burns hook: zoom-in (1.5s) + static hold (1.5s) + pan-right (2s)."""
    tmp   = output_path.parent
    clips = []

    specs = [
        # (input, clip_path, duration, vf)
        (image_paths[0], tmp / "_hf_c1.mp4", "1.5",
         "scale=720:1280:force_original_aspect_ratio=decrease,"
         "pad=720:1280:(ow-iw)/2:(oh-ih)/2,"
         "zoompan=z='min(zoom+0.00267,1.12)':d=45"
         ":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=720x1280,fps=30"),

        (image_paths[1], tmp / "_hf_c2.mp4", "1.5",
         "scale=720:1280:force_original_aspect_ratio=decrease,"
         "pad=720:1280:(ow-iw)/2:(oh-ih)/2,fps=30"),

        (image_paths[2], tmp / "_hf_c3.mp4", "2.0",
         "scale=720:1280:force_original_aspect_ratio=decrease,"
         "pad=720:1280:(ow-iw)/2:(oh-ih)/2,"
         "zoompan=z='1.12':d=60"
         ":x='min(x+1,iw-iw/zoom)':y='ih/2-(ih/zoom/2)':s=720x1280,fps=30"),
    ]

    for src, dst, dur, vf in specs:
        r = subprocess.run([
            "ffmpeg", "-y", "-loop", "1", "-i", str(src),
            "-vf", vf, "-t", dur, "-pix_fmt", "yuv420p", str(dst),
        ], capture_output=True)
        if r.returncode != 0:
            raise RuntimeError(f"FFmpeg failed for {dst.name}:\n{r.stderr.decode()[-500:]}")
        clips.append(dst)

    list_file = tmp / "_hf_clips.txt"
    list_file.write_text("\n".join(f"file '{p.resolve()}'" for p in clips))
    r = subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", str(list_file), "-c", "copy", str(output_path),
    ], capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"FFmpeg concat failed:\n{r.stderr.decode()[-500:]}")
    log(f"  Hook video: {output_path}  ({output_path.stat().st_size // 1024} KB)")

    r = subprocess.run([
        "ffmpeg", "-y", "-i", str(clips[1]),
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
    parser = argparse.ArgumentParser(description="Amazon product → HuggingFace FLUX hook clip")
    parser.add_argument("--url",          help="Amazon product URL")
    parser.add_argument("--skip-concept", action="store_true",
                        help="Reuse existing hook_concept_hf.json")
    parser.add_argument("--seed",         type=int, default=None,
                        help="Image generation seed for reproducibility")
    parser.add_argument("--model",        default=None,
                        help="Override HF model (e.g. black-forest-labs/FLUX.1-dev)")
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(exist_ok=True)
    concept_out = OUTPUT_DIR / "hook_concept_hf.json"
    hf_token    = load_hf_token()

    # ── Step 1 & 2: product + concept ────────────────────────────────────────
    if args.skip_concept:
        # Also accept the imagen concept since the prompts are equivalent
        for candidate in [concept_out, OUTPUT_DIR / "hook_concept_imagen.json"]:
            if candidate.exists():
                concept = json.loads(candidate.read_text())
                title   = concept.get("product_title", "Unknown")
                log(f"  Reusing concept: {title[:80]}")
                break
        else:
            sys.exit(f"--skip-concept set but no concept file found in {OUTPUT_DIR}.")
    else:
        keys = load_gemini_keys()

        log("\n═══ Step 1: Get Amazon product ═══")
        product_url = args.url or get_first_deal_url()
        log(f"  URL: {product_url}")
        title, bullets, price = get_product_info(product_url)
        log(f"  Title: {title[:90]}")
        if price:
            log(f"  Price: {price}")
        for b in bullets:
            log(f"    • {b[:100]}")

        log("\n═══ Step 2: Hook concept (Gemini) ═══")
        concept = generate_hook_concept(title, bullets, keys)
        log(f"\n  Pinpoint: {concept['pinpoint']}")
        log(f"  Persona:  {concept['persona']}")
        log(f"  Concept:  {concept['hook_concept']}")
        log("\n  Image prompts:")
        for idx, p in enumerate(concept.get("image_prompts", []), 1):
            log(f"    [{idx}] {p[:120]}")

        concept_out.write_text(json.dumps(
            {"product_title": title, "product_url": product_url, **concept},
            indent=2, ensure_ascii=False,
        ))
        log(f"\n  Concept saved → {concept_out}")

    # ── Step 3: Generate images via HuggingFace ───────────────────────────────
    log("\n═══ Step 3: HuggingFace FLUX image generation ═══")
    image_prompts = concept.get("image_prompts", [])
    if len(image_prompts) != 3:
        sys.exit("Concept missing 'image_prompts' (3 items). Delete concept file and rerun.")

    image_paths = []
    for i, prompt in enumerate(image_prompts, 1):
        img_path = OUTPUT_DIR / f"hook_hf_{i}.jpg"
        seed     = (args.seed + i) if args.seed is not None else None
        generate_hf_image(prompt, hf_token, img_path, model_override=args.model, seed=seed)
        image_paths.append(img_path)

    # ── Step 4: Assemble hook clip ────────────────────────────────────────────
    log("\n═══ Step 4: Assemble Ken Burns hook clip ═══")
    hook_path  = OUTPUT_DIR / "hook_hf.mp4"
    thumb_path = OUTPUT_DIR / "hook_hf_thumbnail.jpg"
    build_image_hook(image_paths, hook_path, thumb_path)

    log("\n" + "═" * 60)
    log("✓ COMPLETE  (HuggingFace FLUX hook)")
    log(f"  hook_hf.mp4            → 5-second animated hook clip")
    log(f"  hook_hf_thumbnail.jpg  → peak-drama frame for thumbnail")
    log(f"  hook_hf_1/2/3.jpg      → individual frames for quality review")
    log("═" * 60)


if __name__ == "__main__":
    main()
