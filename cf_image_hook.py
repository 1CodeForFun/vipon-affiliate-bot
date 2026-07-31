#!/usr/bin/env python3
"""
cf_image_hook.py — Generate ONE AI pain-point hook image via Cloudflare Workers AI.

Calls Cloudflare's AI REST API directly — no Worker deployment required.
Generates a single cinematic pain-point image, animates it as a 2-second Ken Burns
clip prepended to the carousel reel, and reuses the same still as the
YouTube/Pinterest thumbnail.

Model: @cf/leonardo/lucid-origin (Leonardo Lucid Origin)
  Chosen after benchmarking every text-to-image model on the account.
  FLUX.1 Schnell is ~5x cheaper but produces mangled hands and artifacts.
  FLUX.2 [dev] is better still but needs multipart, ~113s/image, and 2,500
  neurons/call — only ~4 images/day, so it cannot cover 6 publisher runs.

BUDGET — Cloudflare grants 10,000 Neurons/day free, shared across ALL Workers
AI usage on the account. Cost per 576x1024 image:
    lucid-origin 1,836 n/image  -> 5.4 images/day
    flux-1-schnell  62 n/image  -> ~160 images/day
    phoenix-1.0  1,560 n/image  (worse quality than Lucid — not used)
    flux-2-dev   2,500 n/image  (multipart + ~113s — not usable at this volume)

READ THE NEURON FIGURES CAREFULLY when re-deriving these from
cf_neuron_report.py: 429-rejected calls are logged with 0 neurons, so
total/call_count badly understates the true cost. Divide by the number of
SUCCESSFUL calls only. (Doing it wrong gave "306/image" and a 6x-too-generous
budget.) The published per-tile rate independently confirms 1,836:
2.25 tiles x 636 n/tile + ~34 steps x 12 n/step ~= 1,839.

At 6 US publisher runs/day this needs 11,016 n against a 10,000 grant, so the
last run of the day falls back to Schnell via _MODEL_CHAIN. That is expected,
not a fault.

Credentials (loaded from home dir or SECRETS_DIR):
  cf_account_id.txt  — Cloudflare Account ID
  cf_api_token.txt   — API token with "Workers AI" permission

Falls back to (None, None) silently so the reel publisher continues without the hook.
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

_CF_API_BASE = "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model}"
_IMG_W, _IMG_H = 576, 1024      # 9:16 vertical — matches the 720x1280 reel
_HOOK_SECS     = 2.0            # length of the prepended hook clip

# Ordered best-quality-first. Cloudflare itself tells us when the daily neuron
# grant is gone (HTTP 429), so rather than tracking a budget we just walk down
# the chain: a cheaper image beats no image. Schnell costs ~5x less than Lucid
# Origin, so it still works long after the premium budget is spent.
_MODEL_CHAIN = [
    ("@cf/leonardo/lucid-origin",            {}),
    ("@cf/black-forest-labs/flux-1-schnell",  {"num_steps": 4}),
]

_IMAGE_MODEL = _MODEL_CHAIN[0][0]   # preferred model, for logging/tests

_CONCEPT_PROMPT = """\
You are a viral short-form video creative director specialising in Amazon affiliate content.

PRODUCT: {title}
KEY FEATURES / VO SCRIPT:
{features}

YOUR TASK:
Identify the single most relatable PERSONA who would buy this product, and the specific
PAIN POINT this product relieves. Then write ONE photorealistic image prompt capturing
that persona INSIDE the frustrating moment — BEFORE they found this product.

This single frame opens the video. It must make a scrolling viewer instantly think
"that is exactly me" and stop.

THE FRAME:
  • Show the persona mid-struggle — the discomfort must be legible at a glance
  • Do NOT show the product; this is the "before" state
  • One clear subject, uncluttered composition — it will be viewed on a phone screen

PROMPT RULES:
  • Open with: "Photorealistic vertical photograph,"
  • FACELESS — over-shoulder, hands/wrists only, tight crop below the chin, or from behind
  • Describe the EXACT physical scene: setting, body position, props, what the hands do
  • Specific lighting: golden-hour window light / harsh overhead fluorescent / dim lamplight
  • Colour mood that reinforces the frustration: cold & drab / cluttered & chaotic / harsh
  • Close with: "vertical 9:16, cinematic depth of field, sharp foreground, Canon EOS R5"
  • No text, no logos, no captions, no watermarks
  • Max 120 words — dense and specific beats long and vague

Respond ONLY in valid JSON. No markdown fences. No explanation.
{{
  "persona":    "one sentence describing the target buyer",
  "pain_point": "one sharp sentence — the core frustration this product solves",
  "image_prompt": "the complete image prompt for the pain-point frame"
}}
"""


def log(m):
    print(m, flush=True)


# ── Credentials ───────────────────────────────────────────────────────────────

def _load_cf_creds():
    secrets_dir = os.environ.get("SECRETS_DIR", ".")
    for base in [Path.home(), Path(secrets_dir)]:
        acct_f  = base / "cf_account_id.txt"
        token_f = base / "cf_api_token.txt"
        if acct_f.exists() and token_f.exists():
            return acct_f.read_text().strip(), token_f.read_text().strip()
    return None, None


# ── Gemini text call (concept prompt) ─────────────────────────────────────────

def _gemini_concept(title, features, keys):
    prompt_text = _CONCEPT_PROMPT.format(title=title, features=features or title)
    payload = {
        "contents": [{"parts": [{"text": prompt_text}]}],
        "generationConfig": {
            "temperature":     0.85,
            "maxOutputTokens": 2048,
            # Thinking tokens count against maxOutputTokens and truncate the JSON.
            "thinkingConfig":  {"thinkingBudget": 0},
        },
    }
    for key in keys:
        url = f"{_GEMINI_API_BASE}/models/{_GEMINI_MODEL}:generateContent?key={key}"
        try:
            r = requests.post(url, json=payload, timeout=30)
            if r.status_code == 429:
                continue
            r.raise_for_status()
            parts = r.json()["candidates"][0]["content"]["parts"]
            text  = next(
                (p["text"] for p in parts if not p.get("thought") and p.get("text")),
                None,
            )
            if not text:
                raise ValueError("no text part in Gemini response")
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE)
            return json.loads(text.strip())
        except Exception as e:
            log(f"  CF hook: Gemini error ({e}) — trying next key")
    return None


# ── Cloudflare Workers AI REST API ───────────────────────────────────────────

class QuotaExhausted(RuntimeError):
    """Cloudflare returned 429 — the daily free neuron grant is spent."""


def _cf_generate_one(account_id, api_token, prompt, model, extra=None):
    """Generate one image with a specific model. Returns PNG bytes. Raises on failure."""
    url  = _CF_API_BASE.format(account_id=account_id, model=model)
    body = {"prompt": prompt, "width": _IMG_W, "height": _IMG_H, **(extra or {})}
    r = requests.post(
        url,
        headers={"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"},
        json=body,
        timeout=120,
    )
    if r.status_code == 429:
        raise QuotaExhausted("daily free neuron allocation exhausted")
    r.raise_for_status()

    # Lucid Origin returns JSON with a base64 image; some models return raw bytes.
    if "application/json" in r.headers.get("Content-Type", ""):
        data = r.json()
        if not data.get("success"):
            raise RuntimeError(f"CF API error: {data.get('errors', r.text[:200])}")
        img_b64 = (data.get("result") or {}).get("image")
        if not img_b64:
            raise RuntimeError("CF returned 200 but no image in result")
        return base64.b64decode(img_b64)
    return r.content


def _cf_generate(account_id, api_token, prompt):
    """Walk the model chain best-first, degrading on quota exhaustion.

    Returns (png_bytes, model_used). Raises only if every model fails.
    """
    last_err = None
    for model, extra in _MODEL_CHAIN:
        try:
            log(f"  CF hook: generating image via {model}...")
            return _cf_generate_one(account_id, api_token, prompt, model, extra), model
        except QuotaExhausted as e:
            log(f"  CF hook: {model} — daily neuron grant spent, trying cheaper model")
            last_err = e
        except Exception as e:
            log(f"  CF hook: {model} failed ({e}) — trying next model")
            last_err = e
    raise last_err or RuntimeError("no image models configured")


# ── FFmpeg helpers ────────────────────────────────────────────────────────────

def _zoom_clip(ffmpeg, img, out, dur=_HOOK_SECS):
    """Ken Burns slow zoom-in from a single still (720x1280, 30fps)."""
    frames = int(dur * 30)
    r = subprocess.run([
        ffmpeg, "-y", "-loop", "1", "-i", img,
        "-vf", (
            "scale=720:1280:force_original_aspect_ratio=increase,"
            "crop=720:1280,"
            f"zoompan=z='min(zoom+0.0015,1.12)':d={frames}"
            ":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=720x1280,fps=30"
        ),
        "-t", str(dur), "-pix_fmt", "yuv420p", out,
    ] + _FF_LOG, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"Ken Burns clip failed: {r.stderr.decode()[-400:]}")
    return out


def prepend_hook_to_reel(hook_path, reel_path, ffmpeg, output_path):
    """
    Prepend the video-only hook clip to the carousel reel (which has audio).
    Adds silence to the hook, then re-encodes both into a single file.
    """
    td         = str(Path(output_path).parent)
    hook_audio = os.path.join(td, "_cfh_ha.mp4")

    # Step 1: add a silent audio track to the video-only hook clip
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
    except Exception: pass
    if r.returncode != 0:
        raise RuntimeError(f"Hook prepend concat failed: {r.stderr.decode()[-400:]}")
    return output_path


# ── Public API ────────────────────────────────────────────────────────────────

def generate_hook(product, gemini_keys, ffmpeg, td):
    """
    Generate ONE AI pain-point image and a 2-second Ken Burns clip from it.

    Returns (hook_clip_path, image_path) where:
      hook_clip_path — 2s video-only MP4 to prepend to the carousel reel
      image_path     — the same still, used as the YouTube/Pinterest thumbnail

    Returns (None, None) on any failure — the caller continues without the hook.
    """
    account_id, api_token = _load_cf_creds()
    if not account_id or not api_token:
        log("  CF hook: credentials not found (cf_account_id.txt / cf_api_token.txt) — skipping")
        return None, None

    title    = (product.get("title") or product.get("title_text") or "").strip()
    features = (product.get("vo_text") or "").strip()

    log("  CF hook: generating pain-point concept via Gemini...")
    concept = _gemini_concept(title, features, gemini_keys)
    if not concept or not concept.get("image_prompt"):
        log("  CF hook: concept generation failed — skipping")
        return None, None

    log(f"  CF hook pain point: {(concept.get('pain_point') or '')[:90]}")
    log(f"  CF hook prompt:     {concept['image_prompt'][:90]}")

    try:
        img_bytes, model_used = _cf_generate(account_id, api_token, concept["image_prompt"])
        img_path = os.path.join(td, "cf_hook.png")
        Path(img_path).write_bytes(img_bytes)
        log(f"  CF hook: image saved ({len(img_bytes):,} bytes) via {model_used}")
    except Exception as e:
        log(f"  CF hook: all image models failed: {e} — skipping")
        return None, None

    try:
        log("  CF hook: building Ken Burns clip...")
        hook_clip = os.path.join(td, "cf_hook_clip.mp4")
        _zoom_clip(ffmpeg, img_path, hook_clip)
        log(f"  CF hook: clip ready ({os.path.getsize(hook_clip):,} bytes)")
        return hook_clip, img_path
    except Exception as e:
        log(f"  CF hook: FFmpeg clip failed: {e} — skipping")
        return None, None
