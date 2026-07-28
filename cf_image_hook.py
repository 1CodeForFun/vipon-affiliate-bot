#!/usr/bin/env python3
"""
cf_image_hook.py — Generate 2 AI hook images via Cloudflare Workers AI REST API.

Calls Cloudflare's AI REST API directly — no Worker deployment required.
Generates two cinematic persona images, animates them as a 2-second Ken Burns
clip, and returns both the clip and the peak-moment image (YouTube/Pinterest thumb).

Credentials (loaded from home dir or SECRETS_DIR):
  cf_account_id.txt  — Cloudflare Account ID (from the URL after logging in)
  cf_api_token.txt   — API token with "Workers AI" permission (workers-ai:read)

Create the token at: dash.cloudflare.com/profile/api-tokens → "Workers AI" template.
Falls back to (None, None) silently so the reel publisher continues without the hook.
"""

import json
import os
import re
import subprocess
from pathlib import Path

import requests

_FF_LOG          = ["-loglevel", "error"]
_GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
_GEMINI_MODEL    = "gemini-2.5-flash"

_CONCEPT_PROMPT = """\
You are a viral short-form video director and product photographer.
Given a product, write TWO cinematic image prompts for a photorealistic AI image generator.

PRODUCT: {title}
KEY FEATURES:
{features}

Your goal: two SEQUENTIAL frozen moments of the SAME PERSONA actively using or benefiting
from this product. Together they tell a mini-story that makes a viewer stop scrolling.

IMAGE 1 — SETUP/ACTION:
The persona is in the middle of using the product, experiencing effort or engagement.
Visible action or concentration. The product is clearly in their hands or in active use.

IMAGE 2 — PEAK BENEFIT (this frame becomes the video thumbnail):
The single most dramatic, satisfying moment — peak relief, power, or delight.
The product is prominently visible. Compelling enough that a viewer instantly wants it.

RULES for BOTH prompts:
• FACELESS — shoot from behind, over-shoulder, hands/wrists only, or cropped at shoulders
• Product must be clearly identifiable in the frame
• 9:16 vertical portrait, photorealistic, cinematic dramatic lighting, ultra-sharp focus
• No on-screen text, no captions, no visible logos
• Describe only what is physically in the frame — no abstract concepts

Respond ONLY in valid JSON. No markdown fences. No explanation.
{{
  "image_1": "hyper-specific frozen-frame prompt for IMAGE 1 (setup/action)",
  "image_2": "hyper-specific frozen-frame prompt for IMAGE 2 (peak benefit — the thumbnail)"
}}
"""


def log(m):
    print(m, flush=True)


_CF_API_BASE = "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model}"

# ── Credentials ───────────────────────────────────────────────────────────────

def _load_cf_creds():
    secrets_dir = os.environ.get("SECRETS_DIR", ".")
    for base in [Path.home(), Path(secrets_dir)]:
        acct_f  = base / "cf_account_id.txt"
        token_f = base / "cf_api_token.txt"
        if acct_f.exists() and token_f.exists():
            return acct_f.read_text().strip(), token_f.read_text().strip()
    return None, None


# ── Gemini text call (concept prompts) ────────────────────────────────────────

def _gemini_concept(title, features, keys):
    prompt_text = _CONCEPT_PROMPT.format(title=title, features=features or title)
    payload = {
        "contents": [{"parts": [{"text": prompt_text}]}],
        "generationConfig": {
            "temperature":    0.8,
            "maxOutputTokens": 2048,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    for key in keys:
        url = f"{_GEMINI_API_BASE}/models/{_GEMINI_MODEL}:generateContent?key={key}"
        try:
            r = requests.post(url, json=payload, timeout=30)
            if r.status_code == 429:
                continue
            r.raise_for_status()
            # gemini-2.5-flash is a thinking model: parts[0] is the internal
            # thought (thought=True), the actual response is in a later part.
            parts = r.json()["candidates"][0]["content"]["parts"]
            text = next(
                (p["text"] for p in parts if not p.get("thought") and p.get("text")),
                None,
            )
            if not text:
                raise ValueError("No text part in Gemini response")
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE)
            return json.loads(text.strip())
        except Exception as e:
            log(f"  CF hook: Gemini error ({e}) — trying next key")
    return None


# ── Cloudflare Workers AI REST API ───────────────────────────────────────────

def _cf_generate(account_id, api_token, prompt, product_img_bytes=None):
    """Call the Cloudflare Workers AI REST API directly — no Worker deployment needed."""
    if product_img_bytes:
        model = "@cf/runwayml/stable-diffusion-v1-5-img2img"
        body  = {
            "prompt":    prompt,
            "image":     list(product_img_bytes),   # uint8 array
            "strength":  0.65,
            "num_steps": 20,
        }
    else:
        model = "@cf/stabilityai/stable-diffusion-xl-base-1.0"
        body  = {"prompt": prompt, "num_steps": 20}

    url = _CF_API_BASE.format(account_id=account_id, model=model)
    r = requests.post(
        url,
        headers={"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"},
        json=body,
        timeout=90,
    )
    r.raise_for_status()
    if "application/json" in r.headers.get("Content-Type", ""):
        data = r.json()
        if not data.get("success"):
            raise RuntimeError(f"CF API error: {data.get('errors', r.text[:200])}")
    return r.content   # PNG bytes


def _fetch_img_bytes(url):
    """Download the product image for use as img2img reference."""
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        return r.content
    except Exception as e:
        log(f"  CF hook: could not fetch product image ({e}) — falling back to text-only")
        return None


# ── FFmpeg helpers ────────────────────────────────────────────────────────────

def _zoom_clip(ffmpeg, img, out, dur=1.0):
    """1-second Ken Burns zoom-in clip from a single still image (720×1280, 30fps)."""
    frames = int(dur * 30)
    r = subprocess.run([
        ffmpeg, "-y", "-loop", "1", "-i", img,
        "-vf", (
            "scale=720:1280:force_original_aspect_ratio=decrease,"
            "pad=720:1280:(ow-iw)/2:(oh-ih)/2,"
            f"zoompan=z='min(zoom+0.003,1.09)':d={frames}"
            ":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=720x1280,fps=30"
        ),
        "-t", str(dur), "-pix_fmt", "yuv420p", out,
    ] + _FF_LOG, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"Ken Burns clip failed: {r.stderr.decode()[-400:]}")


def _build_hook_clip(ffmpeg, img1, img2, output):
    """Build the 2-second hook clip: 1s zoom on img1 + 1s zoom on img2."""
    td  = str(Path(output).parent)
    c1  = os.path.join(td, "_cfh_c1.mp4")
    c2  = os.path.join(td, "_cfh_c2.mp4")
    lst = os.path.join(td, "_cfh_list.txt")

    _zoom_clip(ffmpeg, img1, c1, 1.0)
    _zoom_clip(ffmpeg, img2, c2, 1.0)

    Path(lst).write_text(
        f"file '{Path(c1).as_posix()}'\nfile '{Path(c2).as_posix()}'\n"
    )
    r = subprocess.run([
        ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", lst,
        "-c", "copy", output,
    ] + _FF_LOG, capture_output=True)
    for f in [c1, c2, lst]:
        try: os.unlink(f)
        except: pass
    if r.returncode != 0:
        raise RuntimeError(f"Hook clip concat failed: {r.stderr.decode()[-400:]}")
    return output


def prepend_hook_to_reel(hook_path, reel_path, ffmpeg, output_path):
    """
    Prepend the video-only hook clip to the carousel reel (which has audio).
    Adds 2 seconds of silence to the hook, then re-encodes both into a single file.
    """
    td         = str(Path(output_path).parent)
    hook_audio = os.path.join(td, "_cfh_ha.mp4")

    # Step 1: add silent audio track to the video-only hook clip
    r = subprocess.run([
        ffmpeg, "-y",
        "-i", hook_path,
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
        "-shortest", hook_audio,
    ] + _FF_LOG, capture_output=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"Hook audio inject failed: {r.stderr.decode()[-300:]}")

    # Step 2: concat hook+reel — re-encode to guarantee A/V sync and codec compat
    r = subprocess.run([
        ffmpeg, "-y",
        "-i", hook_audio,
        "-i", reel_path,
        "-filter_complex", "[0:v][0:a][1:v][1:a]concat=n=2:v=1:a=1[outv][outa]",
        "-map", "[outv]", "-map", "[outa]",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-c:a", "aac", "-b:a", "128k",
        "-pix_fmt", "yuv420p",
        output_path,
    ] + _FF_LOG, capture_output=True, timeout=300)
    try: os.unlink(hook_audio)
    except: pass
    if r.returncode != 0:
        raise RuntimeError(f"Hook prepend concat failed: {r.stderr.decode()[-400:]}")
    return output_path


# ── Public API ────────────────────────────────────────────────────────────────

def generate_hook(product, gemini_keys, ffmpeg, td):
    """
    Generate 2 AI hook images and a 2-second Ken Burns clip.

    Returns (hook_clip_path, thumb_img_path) where:
      hook_clip_path — 2s video-only MP4 to prepend to the carousel reel
      thumb_img_path — image 2 (peak moment) PNG, used as YouTube/Pinterest thumbnail

    Returns (None, None) on any failure — the caller continues without the hook.
    """
    account_id, api_token = _load_cf_creds()
    if not account_id or not api_token:
        log("  CF hook: credentials not found (cf_account_id.txt / cf_api_token.txt) — skipping")
        return None, None

    title    = (product.get("title") or product.get("title_text") or "").strip()
    features = (product.get("vo_text") or "").strip()

    log("  CF hook: generating image prompts via Gemini...")
    concept = _gemini_concept(title, features, gemini_keys)
    if not concept or not concept.get("image_1") or not concept.get("image_2"):
        log("  CF hook: concept generation failed — skipping")
        return None, None

    log(f"  CF hook img1: {concept['image_1'][:90]}")
    log(f"  CF hook img2: {concept['image_2'][:90]}")

    cover_url      = (product.get("cover") or "").strip()
    prod_img_bytes = _fetch_img_bytes(cover_url) if cover_url else None
    mode           = "img2img" if prod_img_bytes else "text-to-image"
    log(f"  CF hook: calling Cloudflare Workers AI REST API ({mode}) for 2 images...")

    try:
        img1_bytes = _cf_generate(account_id, api_token, concept["image_1"], prod_img_bytes)
        img1_path  = os.path.join(td, "cf_hook_1.png")
        Path(img1_path).write_bytes(img1_bytes)
        log(f"  CF hook: image 1 saved ({len(img1_bytes):,} bytes)")

        img2_bytes = _cf_generate(account_id, api_token, concept["image_2"], prod_img_bytes)
        img2_path  = os.path.join(td, "cf_hook_2.png")
        Path(img2_path).write_bytes(img2_bytes)
        log(f"  CF hook: image 2 saved ({len(img2_bytes):,} bytes)")
    except Exception as e:
        log(f"  CF hook: image generation failed: {e} — skipping")
        return None, None

    try:
        log("  CF hook: building Ken Burns clip...")
        hook_clip = os.path.join(td, "cf_hook_clip.mp4")
        _build_hook_clip(ffmpeg, img1_path, img2_path, hook_clip)
        log(f"  CF hook: clip ready ({os.path.getsize(hook_clip):,} bytes)")
        return hook_clip, img2_path
    except Exception as e:
        log(f"  CF hook: FFmpeg clip failed: {e} — skipping")
        return None, None
