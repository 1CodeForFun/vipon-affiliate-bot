#!/usr/bin/env python3
"""
cf_image_hook.py — Generate 2 AI hook images via Cloudflare Workers AI.

Generates two sequential cinematic images of a persona using the product,
animates them as a 2-second Ken Burns clip, and returns both the clip and
the peak-moment image (used as the YouTube + Pinterest thumbnail).

Credentials (loaded from home dir or SECRETS_DIR):
  cf_worker_url.txt  — full Cloudflare Worker URL, e.g. https://xyz.workers.dev
  cf_worker_key.txt  — the API_KEY set in the Worker's Environment Variables

Falls back to (None, None) silently if credentials are missing or generation fails,
so the reel publisher continues without the hook rather than failing entirely.
"""

import base64
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


# ── Credentials ───────────────────────────────────────────────────────────────

def _load_cf_creds():
    secrets_dir = os.environ.get("SECRETS_DIR", ".")
    for base in [Path.home(), Path(secrets_dir)]:
        url_f = base / "cf_worker_url.txt"
        key_f = base / "cf_worker_key.txt"
        if url_f.exists() and key_f.exists():
            return url_f.read_text().strip(), key_f.read_text().strip()
    return None, None


# ── Gemini text call (concept prompts) ────────────────────────────────────────

def _gemini_concept(title, features, keys):
    prompt_text = _CONCEPT_PROMPT.format(title=title, features=features or title)
    payload = {
        "contents": [{"parts": [{"text": prompt_text}]}],
        "generationConfig": {"temperature": 0.8, "maxOutputTokens": 1024},
    }
    for key in keys:
        url = f"{_GEMINI_API_BASE}/models/{_GEMINI_MODEL}:generateContent?key={key}"
        try:
            r = requests.post(url, json=payload, timeout=30)
            if r.status_code == 429:
                continue
            r.raise_for_status()
            text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE)
            return json.loads(text.strip())
        except Exception as e:
            log(f"  CF hook: Gemini error ({e}) — trying next key")
    return None


# ── Cloudflare Worker call ────────────────────────────────────────────────────

def _cf_generate(worker_url, api_key, prompt, img_b64=None):
    body = {"prompt": prompt}
    if img_b64:
        body["image"] = img_b64
    r = requests.post(
        worker_url,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=body,
        timeout=90,
    )
    r.raise_for_status()
    ct = r.headers.get("Content-Type", "")
    if "application/json" in ct:
        raise RuntimeError(f"CF worker returned error: {r.text[:200]}")
    return r.content   # PNG bytes


def _fetch_img_b64(url):
    """Download product image and base64-encode it for img2img."""
    try:
        r = requests.get(url, timeout=15,
                         headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        return base64.b64encode(r.content).decode()
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
    worker_url, api_key = _load_cf_creds()
    if not worker_url or not api_key:
        log("  CF hook: credentials not found (cf_worker_url.txt / cf_worker_key.txt) — skipping")
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

    cover_url = (product.get("cover") or "").strip()
    img_b64   = _fetch_img_b64(cover_url) if cover_url else None
    mode      = "img2img" if img_b64 else "text-to-image"
    log(f"  CF hook: calling Cloudflare Worker ({mode}) for 2 images...")

    try:
        img1_bytes = _cf_generate(worker_url, api_key, concept["image_1"], img_b64)
        img1_path  = os.path.join(td, "cf_hook_1.png")
        Path(img1_path).write_bytes(img1_bytes)
        log(f"  CF hook: image 1 saved ({len(img1_bytes):,} bytes)")

        img2_bytes = _cf_generate(worker_url, api_key, concept["image_2"], img_b64)
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
