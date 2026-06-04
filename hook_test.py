#!/usr/bin/env python3
"""
hook_test.py — Standalone proof-of-concept for the Veo 2 hook clip pipeline.

Pipeline for ONE product (first unposted row in Sheet1):
  1. Read product data from Google Sheet
  2. Gemini 2.0 Flash → generate absurd-but-product-related Veo prompt
  3. Gemini 2.0 Flash → generate short punchy VO script from FB post text
  4. Veo 2 → generate clip 1 (8 s)  +  extend via last-frame → clip 2 (8 s) ≈ 16 s total
  5. Gemini Native Audio → VO audio (WAV)
  6. FFmpeg: loop hook to VO length, carousel images with discount overlay
  7. Mix: VO over hook+carousel; beats fill any tail silence
  8. Upload final reel to Cloudinary and print URL

Touch NOTHING in the existing codebase — fully standalone.
"""

import base64
import hashlib
import json
import os
import random
import re
import subprocess
import tempfile
import time
from pathlib import Path

import gspread
import requests
from oauth2client.service_account import ServiceAccountCredentials

# ─── CONFIG ──────────────────────────────────────────────────────────────────
SECRETS_DIR = os.environ.get("SECRETS_DIR", ".")

def _p(f): return os.path.join(SECRETS_DIR, f)

GOOGLE_CREDS_FILE = _p("vipon_google_creds.json")
GOOGLE_SHEET_NAME = "vipon"

VIDEO_W, VIDEO_H = 720, 1280
FPS              = 30
IMG_DUR_SEC      = 3       # seconds per carousel image
MAX_IMGS         = 4       # max carousel images
VEO_MODEL        = "veo-2.0-generate-001"
VEO_DURATION     = 8      # seconds per Veo clip (max free tier)
VEO_POLL_INT     = 8      # poll interval (seconds)
VEO_MAX_WAIT     = 300    # max wait per clip (seconds)
CLOUDINARY_FOLDER = "vipon_hooks_test"

_FF_LOG    = ["-loglevel", "error", "-hide_banner"]
_FF_ENCODE = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "28", "-threads", "1"]

# ─── LOGGING ─────────────────────────────────────────────────────────────────
def log(msg): print(msg, flush=True)

# ─── CREDENTIALS ─────────────────────────────────────────────────────────────
def _read_gemini_keys():
    multi = os.path.expanduser("~/geminikeys.txt")
    if os.path.exists(multi):
        return [l.strip() for l in open(multi) if l.strip() and not l.startswith("#")]
    single = os.path.expanduser("~/geminikey.txt")
    if os.path.exists(single):
        k = open(single).read().strip()
        return [k] if k else []
    return []

def _load_cloudinary():
    p = os.path.expanduser("~/cloudinary.json")
    d = json.load(open(p))
    return d["cloud_name"], d["api_key"], d["api_secret"]

def _which_ffmpeg():
    for p in ("/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        if os.path.exists(p): return p
    import shutil; return shutil.which("ffmpeg")

def _find_font():
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
              "/System/Library/Fonts/Helvetica.ttc"):
        if os.path.exists(p): return p
    return None

# ─── GOOGLE SHEET ────────────────────────────────────────────────────────────
def read_first_product():
    """Return the first product row from Sheet1 (any row with a reel URL)."""
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds  = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDS_FILE, scope)
    ws     = gspread.authorize(creds).open(GOOGLE_SHEET_NAME).sheet1
    rows   = ws.get_all_values()
    for row in rows[1:]:
        while len(row) < 18: row.append("")
        aff_link = row[0].strip()
        reel_url = row[13].strip()   # col N
        title    = row[8].strip()    # col I
        code     = row[5].strip()    # col F
        disc     = row[6].strip()    # col G
        price    = row[9].strip()    # col J
        expiry   = row[7].strip()    # col H
        post_txt = row[14].strip()   # col O
        imgs     = [row[11].strip(), row[12].strip()]  # col L, M
        imgs     = [u for u in imgs if u]
        if aff_link and title:
            return {
                "aff_link": aff_link, "reel_url": reel_url, "title": title,
                "code": code, "disc": disc, "price": price, "expiry": expiry,
                "post_txt": post_txt, "imgs": imgs,
            }
    return None

# ─── GEMINI TEXT ─────────────────────────────────────────────────────────────
def gemini_text(prompt: str, keys: list, max_tokens: int = 400) -> str:
    for key in keys:
        try:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"gemini-2.0-flash:generateContent?key={key}",
                json={"contents": [{"parts": [{"text": prompt}]}],
                      "generationConfig": {"temperature": 0.95, "maxOutputTokens": max_tokens}},
                timeout=30,
            )
            if r.ok:
                return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        except Exception as e:
            log(f"  Gemini text error ({key[:8]}…): {e}")
    return ""

# ─── GEMINI NATIVE AUDIO TTS ─────────────────────────────────────────────────
def gemini_tts(script: str, keys: list, voice: str = "Charon") -> bytes | None:
    """Generate speech via Gemini 2.0 Flash native audio. Returns raw WAV bytes."""
    for key in keys:
        try:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"gemini-2.0-flash-exp:generateContent?key={key}",
                json={
                    "contents": [{"parts": [{"text": script}]}],
                    "generationConfig": {
                        "responseModalities": ["AUDIO"],
                        "speechConfig": {
                            "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}
                        },
                    },
                },
                timeout=60,
            )
            if r.ok:
                part = r.json()["candidates"][0]["content"]["parts"][0]
                data = part.get("inlineData", {})
                if data.get("data"):
                    log(f"  ✓ TTS via Gemini Native Audio ({voice})")
                    return base64.b64decode(data["data"])
            else:
                log(f"  TTS attempt failed: {r.status_code} {r.text[:120]}")
        except Exception as e:
            log(f"  Gemini TTS error: {e}")
    return None

# ─── VEO 2 ───────────────────────────────────────────────────────────────────
def veo_generate(prompt: str, key: str, start_image_b64: str = None) -> bytes | None:
    base = "https://generativelanguage.googleapis.com/v1beta"
    payload = {
        "prompt": {"text": prompt},
        "generationConfig": {"durationSeconds": VEO_DURATION, "aspectRatio": "9:16"},
    }
    if start_image_b64:
        payload["prompt"]["image"] = {"imageBytes": start_image_b64, "mimeType": "image/jpeg"}

    resp = requests.post(
        f"{base}/models/{VEO_MODEL}:generateVideo?key={key}",
        json=payload, timeout=60,
    )
    if not resp.ok:
        log(f"  Veo submit failed ({resp.status_code}): {resp.text[:200]}")
        return None

    op_name = resp.json().get("name", "")
    if not op_name:
        log("  Veo: no operation name in response")
        return None

    log(f"  Veo job started ({op_name[:40]}…), polling…")
    for _ in range(VEO_MAX_WAIT // VEO_POLL_INT):
        time.sleep(VEO_POLL_INT)
        poll = requests.get(f"{base}/{op_name}?key={key}", timeout=30)
        if not poll.ok: break
        data = poll.json()
        if not data.get("done"): continue
        samples = (data.get("response", {})
                   .get("generateVideoResponse", {})
                   .get("generatedSamples", []))
        if not samples:
            log("  Veo: done but no samples"); return None
        uri = samples[0].get("video", {}).get("uri", "")
        if not uri: return None
        log(f"  Veo clip ready, downloading…")
        for dl in (f"{uri}?key={key}&alt=media", uri):
            try:
                r = requests.get(dl, timeout=120)
                if r.ok and len(r.content) > 10_000:
                    log(f"  ✓ Veo clip: {len(r.content):,} bytes")
                    return r.content
            except Exception: pass
        return None
    log(f"  Veo: timed out"); return None


def extract_last_frame(video_path: str, td: str, ffmpeg_bin: str) -> str | None:
    frame = os.path.join(td, "last_frame.jpg")
    try:
        subprocess.run([ffmpeg_bin, "-y"] + _FF_LOG +
                       ["-sseof", "-1", "-i", video_path, "-vframes", "1", "-q:v", "2", frame],
                       check=True)
        return frame if os.path.exists(frame) else None
    except Exception as e:
        log(f"  last-frame extract failed: {e}"); return None

# ─── FFMPEG HELPERS ───────────────────────────────────────────────────────────
def reencode(src: str, dst: str, ffmpeg_bin: str) -> bool:
    """Re-encode any video to the standard segment format."""
    try:
        subprocess.run([ffmpeg_bin, "-y"] + _FF_LOG + [
            "-i", src,
            "-vf", (f"scale={VIDEO_W}:{VIDEO_H}:force_original_aspect_ratio=decrease,"
                    f"pad={VIDEO_W}:{VIDEO_H}:(ow-iw)/2:(oh-ih)/2,setsar=1"),
            "-r", str(FPS), "-pix_fmt", "yuv420p",
        ] + _FF_ENCODE + ["-an", dst], check=True)
        return True
    except Exception as e:
        log(f"  reencode failed: {e}"); return False


def probe_duration(path: str, ffmpeg_bin: str) -> float:
    try:
        r = subprocess.run([ffmpeg_bin, "-i", path], stderr=subprocess.PIPE, stdout=subprocess.PIPE)
        m = re.search(r"Duration: (\d+):(\d+):(\d+\.?\d*)", r.stderr.decode())
        if m: return int(m.group(1))*3600 + int(m.group(2))*60 + float(m.group(3))
    except Exception: pass
    return 10.0


def build_carousel_seg(img_url: str, seg_path: str, td: str, ffmpeg_bin: str,
                        disc: str, code: str, idx: int, font: str) -> bool:
    """Download one product image and build a video segment with discount overlay."""
    img_path = os.path.join(td, f"cimg_{idx}.jpg")
    try:
        r = requests.get(img_url, timeout=30); r.raise_for_status()
        with open(img_path, "wb") as f: f.write(r.content)
    except Exception as e:
        log(f"  image {idx} download failed: {e}"); return False

    vf = (f"scale={VIDEO_W}:{VIDEO_H}:force_original_aspect_ratio=decrease,"
          f"pad={VIDEO_W}:{VIDEO_H}:(ow-iw)/2:(oh-ih)/2,setsar=1")

    if font:
        def _dt(txt, y, size):
            safe = txt.replace("'", "")
            return (f"drawtext=fontfile='{font}':text='{safe}':x=(w-text_w)/2:y={y}:"
                    f"fontsize={size}:fontcolor=white:box=1:boxcolor=black@0.6:"
                    f"boxborderw=10:shadowcolor=black@0.7:shadowx=2:shadowy=2")
        filters = [vf]
        if disc: filters.append(_dt(disc, "h*0.74", 56))
        if code: filters.append(_dt(f"Code: {code}", "h*0.81", 38))
        vf = ",".join(filters)

    try:
        subprocess.run([ffmpeg_bin, "-y"] + _FF_LOG + [
            "-loop", "1", "-t", str(IMG_DUR_SEC), "-i", img_path,
            "-vf", vf, "-r", str(FPS), "-pix_fmt", "yuv420p",
        ] + _FF_ENCODE + ["-an", seg_path], check=True)
        return True
    except Exception as e:
        log(f"  carousel seg {idx} failed: {e}"); return False


def gen_beat_audio(duration: float, td: str, ffmpeg_bin: str, bpm: int = 100) -> str | None:
    """Generate a simple synthesized beat track using FFmpeg lavfi."""
    beat_path = os.path.join(td, "beat.aac")
    # Four-on-the-floor kick using sine bursts at kick frequency (60 Hz)
    beat_period = 60.0 / bpm
    kick_filter = "+".join(
        f"sine=f=60:d=0.08:delay={i * beat_period:.3f}"
        for i in range(int(duration / beat_period) + 1)
    )
    # Hi-hat (hi freq burst every half beat)
    hat_period = beat_period / 2
    hat_filter = "+".join(
        f"sine=f=8000:d=0.02:delay={i * hat_period:.3f}"
        for i in range(int(duration / hat_period) + 1)
    )
    lavfi = (f"amix=inputs=2:duration=shortest,"
             f"volume=0.25[beat];"
             f"[beat]afade=t=out:st={max(0, duration-1):.1f}:d=1")
    try:
        subprocess.run([ffmpeg_bin, "-y"] + _FF_LOG + [
            "-f", "lavfi", "-i", f"aevalsrc='{kick_filter}':s=44100:c=mono",
            "-f", "lavfi", "-i", f"aevalsrc='{hat_filter}':s=44100:c=mono",
            "-filter_complex", lavfi,
            "-t", str(duration), "-c:a", "aac", "-b:a", "96k", beat_path,
        ], check=True)
        return beat_path
    except Exception as e:
        log(f"  beat generation failed: {e}")
        # Fallback: silence
        silent = os.path.join(td, "silent.aac")
        try:
            subprocess.run([ffmpeg_bin, "-y"] + _FF_LOG + [
                "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=mono",
                "-t", str(duration), "-c:a", "aac", "-b:a", "96k", silent,
            ], check=True)
            return silent
        except: return None

# ─── CLOUDINARY UPLOAD ────────────────────────────────────────────────────────
def cloudinary_upload(video_path: str, public_id: str) -> str:
    cloud_name, api_key, api_secret = _load_cloudinary()
    ts  = int(time.time())
    sig = hashlib.sha1(
        f"public_id={public_id}&timestamp={ts}{api_secret}".encode()
    ).hexdigest()
    with open(video_path, "rb") as f:
        r = requests.post(
            f"https://api.cloudinary.com/v1_1/{cloud_name}/video/upload",
            data={"public_id": public_id, "timestamp": ts,
                  "api_key": api_key, "signature": sig},
            files={"file": f},
            timeout=300,
        )
    if r.ok: return r.json().get("secure_url", "")
    log(f"  Cloudinary upload failed: {r.text[:200]}")
    return ""

# ─── MAIN ────────────────────────────────────────────────────────────────────
def main():
    log("=== hook_test.py starting ===")
    keys     = _read_gemini_keys()
    ffmpeg   = _which_ffmpeg()
    font     = _find_font()

    if not keys:  raise RuntimeError("No Gemini keys found")
    if not ffmpeg: raise RuntimeError("ffmpeg not found")
    log(f"Gemini keys: {len(keys)}  |  ffmpeg: {ffmpeg}  |  font: {font}")

    # 1) Read product
    log("\n[1] Reading product from Sheet1…")
    product = read_first_product()
    if not product:
        raise RuntimeError("No product found in Sheet1")
    log(f"  Product: {product['title']}")
    log(f"  Discount: {product['disc']} | Code: {product['code']} | Price: {product['price']}")
    log(f"  Expiry: {product['expiry']} | Images: {len(product['imgs'])}")

    with tempfile.TemporaryDirectory(prefix="hook_test_") as td:

        # 2) Generate Veo hook prompt via Gemini
        log("\n[2] Generating Veo hook prompt…")
        hook_prompt_req = (
            f"Product: {product['title']}\n\n"
            "Write a Veo 2 video prompt for an 8-second social media hook clip.\n"
            "Rules:\n"
            "- The visual must be ABSURD and unexpected — something people never see in daily life\n"
            "- It must have a SUBTLE thematic connection to the product category (not literal)\n"
            "- Do NOT show the product itself\n"
            "- Do NOT include any text or labels in the scene\n"
            "- Must make someone stop mid-scroll wondering what they're seeing\n"
            "- Cinematic quality, vivid colors, dynamic camera motion\n"
            "- 9:16 vertical format\n"
            "Return only the video prompt, 2-3 sentences."
        )
        veo_prompt = gemini_text(hook_prompt_req, keys, max_tokens=200)
        if not veo_prompt:
            veo_prompt = ("A giant glove made of golden light floats down from the sky onto "
                          "a surprised cat sitting on a throne. Slow-motion sparkles, vivid colors, "
                          "cinematic vertical framing.")
        log(f"  Hook prompt: {veo_prompt}")

        # 3) Generate VO script
        log("\n[3] Generating VO script…")
        # Parse expiry into absolute date
        expiry_date = product['expiry']
        dm = re.search(r"(\d{1,2})[/\-](\d{1,2})[/\-](\d{2,4})", expiry_date)
        if dm:
            try:
                from datetime import datetime
                a, b, c = int(dm.group(1)), int(dm.group(2)), int(dm.group(3))
                year = 2000 + c if c < 100 else c
                dt = datetime(year, a, b)
                expiry_date = f"{dt.strftime('%B')} {dt.day}"
            except: pass
        else:
            m = re.search(r"(\d+)\s*day", expiry_date, re.I)
            if m:
                from datetime import datetime, timedelta
                dt = datetime.now() + timedelta(days=int(m.group(1)))
                expiry_date = f"{dt.strftime('%B')} {dt.day}"

        vo_req = (
            f"Product: {product['title']}\n"
            f"Discount: {product['disc']} off\n"
            f"Code: {product['code'] or 'no code needed'}\n"
            f"Price: {product['price']}\n"
            f"Deal ends: {expiry_date}\n\n"
            "Write a SHORT punchy humorous voiceover script (max 40 words) for a social media reel.\n"
            "- Start with something unexpected/funny, NOT 'Are you looking for'\n"
            "- Mention the discount and deal-end date naturally\n"
            "- If there's a code, say 'use code X at checkout'\n"
            "- End with a clear call to action\n"
            "Return only the script text, ready to be read aloud."
        )
        vo_script = gemini_text(vo_req, keys, max_tokens=100)
        if not vo_script:
            vo_script = (f"Wait — {product['disc']} off {product['title'].split()[0]}? "
                         f"This deal ends {expiry_date}. "
                         f"{'Use code ' + product['code'] + ' at checkout. ' if product['code'] else ''}"
                         "Link in bio — move fast!")
        log(f"  VO script: {vo_script}")

        # 4) Generate Veo clip 1
        log("\n[4] Generating Veo hook clip 1…")
        clip1_bytes = veo_generate(veo_prompt, keys[0])
        clip1_raw = os.path.join(td, "clip1_raw.mp4")
        clip1_seg = os.path.join(td, "clip1.mp4")

        if clip1_bytes:
            with open(clip1_raw, "wb") as f: f.write(clip1_bytes)

            # 4b) Extend: extract last frame → clip 2
            log("\n[4b] Extending hook via last-frame continuation…")
            last_frame = extract_last_frame(clip1_raw, td, ffmpeg)
            clip2_bytes = None
            if last_frame:
                with open(last_frame, "rb") as f:
                    frame_b64 = base64.b64encode(f.read()).decode()
                cont_prompt = (veo_prompt.split(".")[0] +
                               ". Continue the scene smoothly, same visual style, "
                               "slight camera drift, same vivid cinematic look.")
                log("  Generating clip 2 (continuation)…")
                clip2_bytes = veo_generate(cont_prompt, keys[0], start_image_b64=frame_b64)

            # Concat clip1 + clip2 if available, then reencode to segment format
            if clip2_bytes:
                clip2_raw = os.path.join(td, "clip2_raw.mp4")
                with open(clip2_raw, "wb") as f: f.write(clip2_bytes)
                list_file = os.path.join(td, "hook_list.txt")
                hook_concat = os.path.join(td, "hook_concat.mp4")
                with open(list_file, "w") as f:
                    f.write(f"file '{clip1_raw}'\nfile '{clip2_raw}'\n")
                try:
                    subprocess.run([ffmpeg, "-y"] + _FF_LOG +
                                   ["-f", "concat", "-safe", "0", "-i", list_file,
                                    "-c", "copy", hook_concat], check=True)
                    reencode(hook_concat, clip1_seg, ffmpeg)
                    log("  ✓ Hook: ~16s (clip1 + clip2)")
                except Exception as e:
                    log(f"  concat failed, using clip1 only: {e}")
                    reencode(clip1_raw, clip1_seg, ffmpeg)
            else:
                reencode(clip1_raw, clip1_seg, ffmpeg)
                log("  Hook: 8s (clip2 failed, clip1 only)")
        else:
            log("  ⚠️ Veo failed — using black placeholder for hook")
            subprocess.run([ffmpeg, "-y"] + _FF_LOG + [
                "-f", "lavfi", "-i", f"color=c=black:s={VIDEO_W}x{VIDEO_H}:r={FPS}",
                "-t", "8",  "-pix_fmt", "yuv420p",
            ] + _FF_ENCODE + ["-an", clip1_seg], check=True)

        hook_dur = probe_duration(clip1_seg, ffmpeg)
        log(f"  Hook segment duration: {hook_dur:.1f}s")

        # 5) Generate VO audio
        log("\n[5] Generating VO audio…")
        vo_wav  = os.path.join(td, "vo.wav")
        vo_aac  = os.path.join(td, "vo.aac")
        vo_bytes = gemini_tts(vo_script, keys)
        have_vo  = False
        vo_dur   = 0.0

        if vo_bytes:
            with open(vo_wav, "wb") as f: f.write(vo_bytes)
            try:
                subprocess.run([ffmpeg, "-y"] + _FF_LOG +
                               ["-i", vo_wav, "-c:a", "aac", "-b:a", "128k", vo_aac],
                               check=True)
                vo_dur  = probe_duration(vo_aac, ffmpeg)
                have_vo = True
                log(f"  ✓ VO audio: {vo_dur:.1f}s")
            except Exception as e:
                log(f"  VO conversion failed: {e}")
        else:
            log("  ⚠️ Gemini Native Audio failed — reel will have beats only")

        # 6) Build carousel segments
        log("\n[6] Building carousel segments…")
        carousel_segs = []
        for i, img_url in enumerate(product["imgs"][:MAX_IMGS], start=1):
            seg = os.path.join(td, f"cseg_{i}.mp4")
            if build_carousel_seg(img_url, seg, td, ffmpeg, product["disc"], product["code"], i, font):
                carousel_segs.append(seg)
                log(f"  ✓ Carousel image {i}")

        carousel_dur = len(carousel_segs) * IMG_DUR_SEC

        # 7) Determine total structure
        total_vo_remaining = max(0, vo_dur - hook_dur)

        # If VO extends beyond hook, loop hook to VO length OR let VO continue over carousel
        if have_vo and vo_dur > hook_dur and carousel_segs:
            # VO covers hook + some carousel → no separate music needed for covered portion
            log(f"  VO ({vo_dur:.1f}s) covers hook ({hook_dur:.1f}s) + {total_vo_remaining:.1f}s into carousel")

        tail_silence = max(0, hook_dur + carousel_dur - vo_dur)
        log(f"  Total structure: {hook_dur:.1f}s hook + {carousel_dur}s carousel | VO: {vo_dur:.1f}s | tail silence: {tail_silence:.1f}s")

        # 8) Concat video
        log("\n[7] Concatenating video segments…")
        all_video_segs = [clip1_seg] + carousel_segs
        list_all = os.path.join(td, "all.txt")
        concat_mp4 = os.path.join(td, "concat.mp4")
        with open(list_all, "w") as f:
            for s in all_video_segs:
                f.write(f"file '{Path(s).as_posix()}'\n")
        subprocess.run([ffmpeg, "-y"] + _FF_LOG +
                       ["-f", "concat", "-safe", "0", "-i", list_all,
                        "-c", "copy", concat_mp4], check=True)
        total_dur = probe_duration(concat_mp4, ffmpeg)
        log(f"  Video concat: {total_dur:.1f}s total")

        # 9) Mix audio: VO first, beats fill tail silence
        log("\n[8] Mixing audio…")
        out_mp4 = os.path.join(td, "final.mp4")

        if have_vo and tail_silence < 1.0:
            # VO covers full video — just mix VO with video
            subprocess.run([ffmpeg, "-y"] + _FF_LOG + [
                "-i", concat_mp4, "-i", vo_aac,
                "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-shortest", out_mp4,
            ], check=True)
            log("  Audio: VO only (covers full video)")

        elif have_vo and tail_silence >= 1.0:
            # VO + beats for tail
            beat_path = gen_beat_audio(tail_silence, td, ffmpeg)
            if beat_path:
                mixed_audio = os.path.join(td, "mixed.aac")
                subprocess.run([ffmpeg, "-y"] + _FF_LOG + [
                    "-i", vo_aac, "-i", beat_path,
                    "-filter_complex",
                    f"[0]adelay=0|0[a0];[1]adelay={(vo_dur*1000):.0f}|{(vo_dur*1000):.0f}[a1];"
                    f"[a0][a1]amix=inputs=2:duration=longest[out]",
                    "-map", "[out]", "-c:a", "aac", "-b:a", "128k", mixed_audio,
                ], check=True)
                subprocess.run([ffmpeg, "-y"] + _FF_LOG + [
                    "-i", concat_mp4, "-i", mixed_audio,
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-shortest", out_mp4,
                ], check=True)
                log(f"  Audio: VO ({vo_dur:.1f}s) + beats tail ({tail_silence:.1f}s)")
            else:
                subprocess.run([ffmpeg, "-y"] + _FF_LOG + [
                    "-i", concat_mp4, "-i", vo_aac,
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-shortest", out_mp4,
                ], check=True)
        else:
            # No VO — beats for full video
            beat_path = gen_beat_audio(total_dur, td, ffmpeg)
            if beat_path:
                subprocess.run([ffmpeg, "-y"] + _FF_LOG + [
                    "-i", concat_mp4, "-i", beat_path,
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-shortest", out_mp4,
                ], check=True)
                log("  Audio: beats only (no VO)")
            else:
                subprocess.run([ffmpeg, "-y"] + _FF_LOG + [
                    "-i", concat_mp4,
                    "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                    "-c:v", "copy", "-c:a", "aac", "-shortest", out_mp4,
                ], check=True)

        log(f"  ✓ Final video: {probe_duration(out_mp4, ffmpeg):.1f}s  "
            f"({os.path.getsize(out_mp4):,} bytes)")

        # 10) Upload
        log("\n[9] Uploading to Cloudinary…")
        pid     = re.sub(r"[^a-zA-Z0-9]", "_", product["title"][:30])
        pub_id  = f"{CLOUDINARY_FOLDER}/hook_{pid}_{int(time.time())}"
        url     = cloudinary_upload(out_mp4, pub_id)
        if url:
            log(f"\n✅ SUCCESS — Final reel URL:\n{url}")
        else:
            log("\n⚠️ Cloudinary upload failed — check credentials")

    log("=== hook_test.py done ===")


if __name__ == "__main__":
    main()
