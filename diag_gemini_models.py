#!/usr/bin/env python3
"""
diag_gemini_models.py — List all Gemini models available for your keys,
then test which ones accept image generation requests.

Usage:
  python diag_gemini_models.py           # tries all keys, picks first that works
  python diag_gemini_models.py --key 2   # use only key 2 (billing key)
"""

import sys, json, argparse, requests
from pathlib import Path

KEYS_FILE = Path.home() / "geminikey.txt"
BASE = "https://generativelanguage.googleapis.com/v1beta"

TEST_PROMPT = "A dramatic photo of a dark hallway with light under a door at night. Cinematic, 9:16 vertical."

def load_keys():
    keys = [k.strip() for k in KEYS_FILE.read_text().splitlines() if k.strip()]
    print(f"Loaded {len(keys)} keys")
    return keys

def list_models(key: str) -> list:
    resp = requests.get(f"{BASE}/models?key={key}&pageSize=200", timeout=30)
    if resp.status_code != 200:
        print(f"  /models → {resp.status_code}: {resp.text[:300]}")
        return []
    return resp.json().get("models", [])

def test_image_gen(model_name: str, key: str) -> bool:
    """Try to generate an image. Returns True if we got image bytes back."""
    short = model_name.replace("models/", "")
    url = f"{BASE}/{model_name}:generateContent?key={key}"

    # Try with both TEXT+IMAGE and IMAGE-only modalities
    for modalities in [["TEXT", "IMAGE"], ["IMAGE"]]:
        payload = {
            "contents": [{"parts": [{"text": TEST_PROMPT}]}],
            "generationConfig": {"responseModalities": modalities},
        }
        resp = requests.post(url, json=payload, timeout=60)
        code = resp.status_code
        if code == 404:
            print(f"    {short} ({modalities}) → 404 (model not found/available)")
            break  # 404 means model doesn't exist for this key — no point trying other modalities
        elif code == 400:
            body = resp.json()
            err = body.get("error", {}).get("message", "")
            print(f"    {short} ({modalities}) → 400: {err[:120]}")
            continue  # 400 might be request format — try other modalities
        elif code == 429:
            print(f"    {short} ({modalities}) → 429 (rate limit)")
            return False
        elif code != 200:
            print(f"    {short} ({modalities}) → {code}: {resp.text[:200]}")
            continue
        else:
            # 200 — check if there's an image in the response
            parts = resp.json().get("candidates", [{}])[0].get("content", {}).get("parts", [])
            for part in parts:
                if part.get("inlineData", {}).get("data"):
                    print(f"    {short} ({modalities}) → ✓ IMAGE RECEIVED")
                    return True
            # 200 but no image — check text for clues
            for part in parts:
                if part.get("text"):
                    print(f"    {short} ({modalities}) → 200 but text-only: {part['text'][:120]}")
            continue
    return False

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--key", type=int, default=None, help="1-based key index to test")
    args = parser.parse_args()

    keys = load_keys()
    if args.key:
        idx = args.key - 1
        if idx < 0 or idx >= len(keys):
            sys.exit(f"--key {args.key} out of range")
        test_keys = [(args.key, keys[idx])]
    else:
        test_keys = list(enumerate(keys, 1))

    for kidx, key in test_keys:
        print(f"\n{'═'*60}")
        print(f"KEY {kidx}")
        print(f"{'═'*60}")

        # 1. List all available models
        print("\n── All available models ──")
        models = list_models(key)
        if not models:
            print("  (could not fetch model list)")
            continue

        # Filter for anything image-generation related
        img_models = [m for m in models if any(
            kw in m.get("name", "").lower() or
            kw in m.get("displayName", "").lower()
            for kw in ["image", "imagen", "vision", "flash-exp", "flash-preview"]
        )]

        print(f"  Total models available: {len(models)}")
        print(f"  Image-related models ({len(img_models)}):")
        for m in img_models:
            name = m.get("name", "?")
            display = m.get("displayName", "")
            methods = m.get("supportedGenerationMethods", [])
            print(f"    {name}  [{display}]  methods={methods}")

        if not img_models:
            print("  (none — showing ALL model names for reference):")
            for m in models:
                print(f"    {m.get('name','')}  {m.get('displayName','')}")

        # 2. Test each image-related model
        print("\n── Image generation tests ──")
        working = []
        for m in img_models:
            name = m.get("name", "")
            methods = m.get("supportedGenerationMethods", [])
            if "generateContent" not in methods:
                print(f"  Skipping {name} (no generateContent support, methods={methods})")
                continue
            print(f"  Testing {name}...")
            if test_image_gen(name, key):
                working.append(name)

        if working:
            print(f"\n  ✓ WORKING image-gen models for key {kidx}:")
            for w in working:
                print(f"    {w}")
        else:
            print(f"\n  ✗ No working image-gen models for key {kidx}")

if __name__ == "__main__":
    main()
