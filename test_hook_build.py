#!/usr/bin/env python3
"""
test_hook_build.py — LOCAL full-reel builder for testing (NO Veo spend, NO upload/posting).

Takes a manually-generated Veo hook clip + a product, fetches the Amazon image
carousel (Selenium works on your residential IP — PA API not needed locally),
generates the VO, and stitches everything exactly like production build_hook_reel:

    hook (its own audio @100%)  ->  Ken Burns gallery (+ price slide if available)  ->  VO

Output: hook_test_output/final_<asin>_<hook>.mp4 — upload manually to test response.

Usage:
  python test_hook_build.py --hook "hook_test_output\\Hands_assembling_Zinus_box_202606211127.mp4" --asin B01M7M9NXX
  python test_hook_build.py --hook "hook_test_output\\Hand_scribbling_on_legal_pad_202606211127.mp4" --asin B0CF2TSN5T
  python test_hook_build.py --hook "clip.mp4" --asin B0XXXX --title "Custom Product Title"
"""

import argparse, os, json, random, shutil, subprocess
from pathlib import Path

from reel_concept_test import (
    capture_page, _which_ffmpeg, _find_font,
    VIDEO_W, VIDEO_H, FPS, _FF_LOG,
)
from publish_reel_hook import generate_hook_vo, build_hook_reel, _is_safe
from test_hook_prompt import load_keys   # reads ~/geminikey.txt line-by-line (valid free keys)

CACHE = Path("scraper_test_output/deals_cache.json")
OUT   = Path("hook_test_output")


def log(m): print(m, flush=True)


def find_deal(asin, title):
    """Resolve the product: explicit title wins; else look up ASIN in cache; else random safe."""
    if title:
        return {"asin": asin or "UNKNOWN", "title": title, "pct": 60}
    deals = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else []
    if asin:
        for d in deals:
            if d.get("asin") == asin:
                return d
        return {"asin": asin, "title": asin, "pct": 60}   # not in cache — use ASIN, title fetched from page
    safe = [d for d in deals if _is_safe(d)]
    if not safe:
        raise SystemExit("No cached safe deals — pass --asin (and optionally --title).")
    return random.choice(safe)


def normalize_hook(src, ffmpeg):
    """Re-encode the AI-Studio hook to match the carousel (720x1280 @ FPS, yuv420p, SAR 1)
    so the concat in build_hook_reel is clean. Keeps the hook's own audio (the weird sound)."""
    dst = str(OUT / "hook_norm.mp4")
    subprocess.run([ffmpeg, "-y"] + _FF_LOG + [
        "-i", src,
        "-vf", f"scale={VIDEO_W}:{VIDEO_H},fps={FPS},setsar=1,format=yuv420p",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20",
        "-c:a", "aac", "-b:a", "128k", dst],
        check=True, timeout=120)
    log(f"  Normalized hook -> {VIDEO_W}x{VIDEO_H}@{FPS}")
    return dst


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hook", required=True, help="path to your manually-generated Veo hook mp4")
    ap.add_argument("--asin", help="product ASIN")
    ap.add_argument("--title", help="override product title")
    args = ap.parse_args()

    if not os.path.exists(args.hook):
        raise SystemExit(f"Hook file not found: {args.hook}")

    OUT.mkdir(exist_ok=True)
    td     = str(OUT.resolve())   # ABSOLUTE — concat list.txt resolves file paths relative to itself
    ffmpeg = _which_ffmpeg()
    # Font: ffmpeg's drawtext filter chokes on a Windows path (the "C:" colon and
    # backslashes are filtergraph syntax). Copy a system font to a colon-free RELATIVE
    # path (resolved against ffmpeg's cwd = repo root) so the % OFF label renders locally.
    font = _find_font()
    if not font:
        src = next((f for f in (r"C:\Windows\Fonts\arialbd.ttf",
                                r"C:\Windows\Fonts\arial.ttf") if os.path.exists(f)), "")
        if src:
            font = "localfont.ttf"
            if not os.path.exists(font):
                shutil.copy(src, font)
    keys = load_keys()   # 9 valid free keys from geminikey.txt (NOT _read_gemini_keys, which blobs the file locally)
    log(f"ffmpeg={ffmpeg} | font={'yes' if font else 'no'} | gemini keys={len(keys)}")

    deal = find_deal(args.asin, args.title)
    log(f"\nPRODUCT: {deal['title']}  | ASIN {deal['asin']}  ({deal.get('pct','?')}% off)")

    # 1. Carousel images (+ screenshot/price_box) — Selenium works on residential IP
    log("\n[1] Capturing product images...")
    imgs, shot, pw, ph, price_box, _, title_box, _ = capture_page(deal["asin"], td, ffmpeg)
    if not imgs:
        raise SystemExit("No images captured — cannot build carousel.")
    page_data = {"images": imgs, "screenshot": shot, "img_w": pw, "img_h": ph,
                 "price_box": price_box, "title_box": title_box}
    log(f"  images={len(imgs)} | price_box={'y' if price_box else 'n'}")

    # 2. VO (script + Gemini TTS). Uses keys[2:] internally (skips key #0/#1) with rotation.
    log("\n[2] Generating VO (script + TTS)...")
    script, wav = generate_hook_vo(deal, keys)
    if not wav:
        raise SystemExit("VO TTS failed (likely quota) — try again later.")
    log(f"  VO ok ({len(wav):,} bytes)")
    log(f"  Script: {script}")

    # 3. Normalize the hook, then stitch exactly like production
    log("\n[3] Normalizing hook + stitching (hook audio @100% -> carousel + VO)...")
    norm_hook = normalize_hook(args.hook, ffmpeg)
    final = build_hook_reel(deal, page_data, norm_hook, script, wav, ffmpeg, font, td)
    if not final:
        raise SystemExit("Build failed.")

    # Keep a uniquely-named copy so repeated tests don't overwrite final.mp4
    stamp = Path(args.hook).stem[:24]
    dst = OUT / f"final_{deal['asin']}_{stamp}.mp4"
    shutil.copy(final, dst)
    log(f"\n[OK] DONE -> {dst}")
    log("Upload this manually to test the response.")


if __name__ == "__main__":
    main()
