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

THE LIMIT BEHAVES AS A ROLLING ~24h WINDOW, NOT A CALENDAR UTC DAY, whatever
the docs say. Observed: 429 while the current UTC day showed only 38% used and
a run 48 minutes later succeeded; and, separately, still 429 more than 8 hours
into a fresh UTC day that had logged 0 neurons, following a day that went 13%
over. Treat "10,000 per rolling 24h" as the real constraint.

At 1,836 n/image that sustains ~5.4 images per rolling 24h, so 6 publisher runs
a day will intermittently hit the wall. Note the 429 is ACCOUNT-WIDE: when the
grant is gone every metered model refuses, so _MODEL_CHAIN cannot rescue it —
the chain only helps when an individual model is unavailable. Reducing runs, or
generating at a smaller size (tile count dominates the cost), are the levers.

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
Identify the most relatable PERSONA for this product and the specific PAIN POINT it
relieves, then write ONE photorealistic image prompt of that persona inside the
frustrating moment — BEFORE they found the product. This frame opens the video and
must make a scrolling viewer think "that is exactly me" and stop.

STEP 1 — DERIVE THE WORLD FROM THE PRODUCT (do this first, it drives everything):
Decide the room or place where this product is ACTUALLY used, then pick lighting and
a colour palette that belong to that world. Match the product category:
  • Outdoor / garden / patio / sports -> real daylight, open sky, greenery,
      warm golden-hour or bright midday sun, saturated natural colour
  • Beauty / skincare / hair -> clean bright bathroom or vanity, soft flattering
      light, warm creamy tones, gentle highlights
  • Kitchen / food -> warm domestic kitchen, appetising warm light, rich colour
  • Bedroom / sleep / comfort -> cosy bedroom, soft warm lamplight, inviting textures
  • Fitness / wellness -> bright gym or sunlit home space, energetic clean light
  • Office / tech -> tidy desk with natural window light
  • Cleaning / storage / garage -> the real utility space, but still naturally lit
The frustration must come from the PERSON'S BODY LANGUAGE AND THE SITUATION — never
from making the room dark, grim or depressing. Do NOT default to cold, drab, grey,
industrial, warehouse or basement settings. A cheerful, well-lit room containing a
visibly frustrated person is far more relatable, and far more clickable, than a
gloomy one. Never depict despair, illness, injury or distress.

STEP 2 — WRITE THE IMAGE PROMPT:
  • Open with: "Photorealistic vertical photograph,"
  • FACELESS IS MANDATORY — this is the single most important rule. Compose so NO
    face is visible: shot from directly behind, over-the-shoulder past the back of
    the head, hands and forearms only, or cropped at the chin. Never a mirror
    reflection, never a face at any angle, never eyes.
  • Name the specific room from STEP 1 and the exact lighting and colour palette
  • Describe the physical scene precisely: body position, props, what the hands do
  • Do NOT show the product — this is the "before" state
  • One clear subject, uncluttered — it is viewed on a phone screen
  • Close with: "vertical 9:16, cinematic depth of field, sharp foreground, Canon EOS R5"
  • No text, no logos, no captions, no watermarks
  • Max 120 words — dense and specific beats long and vague

STEP 3 — WRITE THE ON-SCREEN HOOK:
  • pov_text: a punchy first-person line in the style of "POV: you ..." naming the
    frustration. MAX 8 WORDS. No product name, no price, no hashtags, no quotes.
  • emojis: exactly 2 or 3 emoji characters that match the pain point and product
    (food, household, activity, weather, reaction faces are all fine). Emoji only.

Respond ONLY in valid JSON. No markdown fences. No explanation.
{{
  "persona":    "one sentence describing the target buyer",
  "pain_point": "one sharp sentence — the core frustration this product solves",
  "setting":    "the room/place and the lighting-and-colour mood you chose",
  "image_prompt": "the complete image prompt for the pain-point frame",
  "pov_text":   "POV: ... (max 8 words)",
  "emojis":     "2-3 emoji characters"
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
    last_i   = len(_MODEL_CHAIN) - 1
    for i, (model, extra) in enumerate(_MODEL_CHAIN):
        more = " — trying next model" if i < last_i else ""
        try:
            log(f"  CF hook: generating image via {model}...")
            return _cf_generate_one(account_id, api_token, prompt, model, extra), model
        except QuotaExhausted as e:
            # 429 is ACCOUNT-WIDE, not per-model: once the grant is spent every
            # metered model refuses, so falling through to a cheaper one does not
            # help here. The chain still earns its keep when a single model is
            # unavailable or rejects the request.
            log(f"  CF hook: {model} — neuron grant spent (account-wide){more}")
            last_err = e
        except Exception as e:
            log(f"  CF hook: {model} failed ({e}){more}")
            last_err = e
    raise last_err or RuntimeError("no image models configured")


# ── FFmpeg helpers ────────────────────────────────────────────────────────────

def _find_font(bold=True):
    """Body font for the POV line."""
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
              "C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf"):
        if os.path.exists(p):
            return p
    return None


def _find_emoji_font():
    """Colour-emoji font. ffmpeg's drawtext CANNOT render these in colour (freetype
    rasterises them monochrome), which is why the overlay is drawn with Pillow and
    composited as an image instead."""
    for p in ("/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
              "/usr/share/fonts/truetype/noto/NotoColorEmoji-Regular.ttf",
              "C:/Windows/Fonts/seguiemj.ttf"):
        if os.path.exists(p):
            return p
    return None


def _render_overlay(pov_text, emojis, out_path, w=720, h=1280):
    """Draw the POV line + emojis onto a transparent PNG. Returns path or None.

    Kept deliberately sparse — one line of text and a small emoji row, upper third,
    so it reads instantly without covering the image.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        log("  CF hook: Pillow not installed — skipping text overlay")
        return None

    pov_text = (pov_text or "").strip()
    emojis   = (emojis or "").strip()
    if not pov_text and not emojis:
        return None

    font_path = _find_font()
    if not font_path:
        log("  CF hook: no text font found — skipping overlay")
        return None

    img  = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # ── Wrap the POV line to the canvas width ────────────────────────────────
    size = 58
    font = ImageFont.truetype(font_path, size)
    margin = 56
    words, lines, cur = pov_text.split(), [], ""
    for word in words:
        trial = f"{cur} {word}".strip()
        if draw.textlength(trial, font=font) <= (w - 2 * margin):
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    lines = lines[:3]

    y = int(h * 0.13)
    for line in lines:
        tw = draw.textlength(line, font=font)
        draw.text(((w - tw) / 2, y), line, font=font, fill=(255, 255, 255, 255),
                  stroke_width=6, stroke_fill=(0, 0, 0, 235))
        y += size + 14

    # ── Emoji row, rendered at the font's native strike then scaled ──────────
    if emojis:
        ef = _find_emoji_font()
        if ef:
            target = 96
            for native in (target, 109, 137):     # NotoColorEmoji only has fixed strikes
                try:
                    efont = ImageFont.truetype(ef, native)
                    tmp   = Image.new("RGBA", (native * 6, int(native * 1.6)), (0, 0, 0, 0))
                    ImageDraw.Draw(tmp).text((0, 0), emojis, font=efont, embedded_color=True)
                    bbox = tmp.getbbox()
                    if not bbox:
                        break
                    strip = tmp.crop(bbox)
                    scale = target / strip.height
                    strip = strip.resize((max(1, int(strip.width * scale)), target),
                                         Image.LANCZOS)
                    img.alpha_composite(strip, ((w - strip.width) // 2, y + 10))
                    break
                except OSError:
                    continue                      # wrong strike size — try the next
                except Exception as e:
                    log(f"  CF hook: emoji render failed ({e})")
                    break
        else:
            log("  CF hook: no colour-emoji font found — text only")

    img.save(out_path)
    return out_path


def _impact_audio(ffmpeg, out, dur=_HOOK_SECS):
    """Loud cinematic impact for the hook: a descending boom plus a noise transient,
    both decaying fast. Grabs attention while the hook frame is on screen."""
    boom = "sin(2*PI*(45+140*exp(-7*t))*t)*exp(-2.4*t)"
    r = subprocess.run([
        ffmpeg, "-y",
        "-f", "lavfi", "-i", f"aevalsrc='{boom}':s=44100:c=stereo:d={dur}",
        "-f", "lavfi", "-i", f"anoisesrc=d={dur}:c=white:a=0.9:r=44100",
        "-filter_complex",
        "[1:a]highpass=f=600,volume='exp(-16*t)':eval=frame[hit];"
        "[0:a][hit]amix=inputs=2:duration=first:weights='1 0.45',"
        "volume=2.2,alimiter=limit=0.95[a]",
        "-map", "[a]", "-t", str(dur), "-c:a", "aac", "-b:a", "128k", out,
    ] + _FF_LOG, capture_output=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"impact audio failed: {r.stderr.decode()[-300:]}")
    return out


def _zoom_clip(ffmpeg, img, out, dur=_HOOK_SECS, overlay=None):
    """Ken Burns slow zoom-in from a single still (720x1280, 30fps).

    The text/emoji overlay is composited AFTER the zoom so it stays pin-sharp and
    stationary while the photo moves behind it.
    """
    frames = int(dur * 30)
    kb = ("scale=720:1280:force_original_aspect_ratio=increase,"
          "crop=720:1280,"
          f"zoompan=z='min(zoom+0.0015,1.12)':d={frames}"
          ":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=720x1280,fps=30")

    if overlay and os.path.exists(overlay):
        cmd = [ffmpeg, "-y", "-loop", "1", "-i", img, "-i", overlay,
               "-filter_complex", f"[0:v]{kb}[bg];[bg][1:v]overlay=0:0:format=auto[v]",
               "-map", "[v]"]
    else:
        cmd = [ffmpeg, "-y", "-loop", "1", "-i", img, "-vf", kb]

    r = subprocess.run(cmd + ["-t", str(dur), "-pix_fmt", "yuv420p", out]
                       + _FF_LOG, capture_output=True)
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
    impact     = os.path.join(td, "_cfh_impact.aac")

    # Step 1: give the hook clip its audio. Prefer the loud impact sting; fall back
    # to silence so a filter problem never costs us the hook.
    try:
        _impact_audio(ffmpeg, impact, _HOOK_SECS)
        audio_in = ["-i", impact]
    except Exception as e:
        log(f"  CF hook: impact sound failed ({e}) — using silence")
        audio_in = ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]

    r = subprocess.run([
        ffmpeg, "-y",
        "-i", hook_path, *audio_in,
        "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
        "-shortest", hook_audio,
    ] + _FF_LOG, capture_output=True, timeout=60)
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
    log(f"  CF hook setting:    {(concept.get('setting') or '')[:90]}")
    log(f"  CF hook overlay:    {(concept.get('pov_text') or '')!r} {concept.get('emojis') or ''}")
    log(f"  CF hook prompt:     {concept['image_prompt'][:90]}")

    try:
        img_bytes, model_used = _cf_generate(account_id, api_token, concept["image_prompt"])
        img_path = os.path.join(td, "cf_hook.png")
        Path(img_path).write_bytes(img_bytes)
        log(f"  CF hook: image saved ({len(img_bytes):,} bytes) via {model_used}")
    except Exception as e:
        log(f"  CF hook: all image models failed: {e} — skipping")
        return None, None

    # Text/emoji overlay is non-fatal — a missing font must not cost us the hook.
    overlay = None
    try:
        overlay = _render_overlay(concept.get("pov_text"), concept.get("emojis"),
                                  os.path.join(td, "cf_hook_overlay.png"))
        if overlay:
            log("  CF hook: overlay rendered")
    except Exception as e:
        log(f"  CF hook: overlay failed ({e}) — continuing without it")
        overlay = None

    try:
        log("  CF hook: building Ken Burns clip...")
        hook_clip = os.path.join(td, "cf_hook_clip.mp4")
        _zoom_clip(ffmpeg, img_path, hook_clip, overlay=overlay)
        log(f"  CF hook: clip ready ({os.path.getsize(hook_clip):,} bytes)")
        # Thumbnail keeps the overlay burned in so the POV line shows on the pin.
        thumb = img_path
        if overlay:
            try:
                from PIL import Image
                base = Image.open(img_path).convert("RGBA").resize((720, 1280), Image.LANCZOS)
                base.alpha_composite(Image.open(overlay).convert("RGBA"))
                thumb = os.path.join(td, "cf_hook_thumb.png")
                base.convert("RGB").save(thumb)
            except Exception as e:
                log(f"  CF hook: thumbnail compose failed ({e}) — using bare image")
                thumb = img_path
        return hook_clip, thumb
    except Exception as e:
        log(f"  CF hook: FFmpeg clip failed: {e} — skipping")
        return None, None
