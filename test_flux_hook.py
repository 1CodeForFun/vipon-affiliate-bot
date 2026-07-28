#!/usr/bin/env python3
"""
test_flux_hook.py — Standalone image quality test for FLUX.1 Schnell hook images.

Reads a product from the vipon Google Sheet, uses Gemini to craft a pain-point
persona concept, then generates 2 images via Cloudflare Workers AI (FLUX.1 Schnell).
Images are saved locally for quality review — nothing is posted anywhere.

Usage:
  python test_flux_hook.py                  # top Social Score product
  python test_flux_hook.py --row 10         # specific sheet row (1-based, header=1)
  python test_flux_hook.py --title "..." --features "..."  # manual input

Credentials needed (same as main pipeline):
  ~/cf_account_id.txt   — Cloudflare Account ID
  ~/cf_api_token.txt    — Cloudflare API token (Workers AI permission)
  ~/geminikey.txt or ~/geminikeys.txt  — Gemini API keys
  secrets/vipon_google_creds.json      — Google service account (for --row / default mode)
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

import requests

# ── Config ────────────────────────────────────────────────────────────────────
def _find_secrets_dir():
    """Search for vipon_google_creds.json in common local locations."""
    if "SECRETS_DIR" in os.environ:
        return os.environ["SECRETS_DIR"]
    candidates = [
        "secrets",
        "..",
        os.path.join("..", "vipon-secrets"),
        os.path.join("..", "secrets"),
        str(Path.home()),
    ]
    for d in candidates:
        if os.path.exists(os.path.join(d, "vipon_google_creds.json")):
            return d
    return "secrets"   # fallback — will produce a clear error

SECRETS_DIR     = _find_secrets_dir()
GOOGLE_CREDS    = os.path.join(SECRETS_DIR, "vipon_google_creds.json")
SHEET_NAME      = "vipon"
OUTPUT_DIR      = Path("flux_test_output")

GEMINI_BASE     = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_MODEL    = "gemini-2.5-flash"

CF_API_BASE     = "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model}"
FLUX_W, FLUX_H  = 576, 1024   # 9:16 vertical

FLUX_MODELS = {
    "schnell":   ("@cf/black-forest-labs/flux-1-schnell",            4),
    "lightning": ("@cf/bytedance/stable-diffusion-xl-lightning",     1),   # fastest, lower quality
}

# Sheet columns (1-based, matching vipon_publisher.py)
COL_A = 1   # affiliate link
COL_F = 6   # discount code
COL_G = 7   # discount %
COL_I = 9   # title
COL_J = 10  # price
COL_O = 15  # VO text / features
COL_S = 19  # Social Score
COL_P = 16  # Posted flag

# ── Prompt ────────────────────────────────────────────────────────────────────
_CONCEPT_PROMPT = """\
You are a viral short-form video creative director specialising in Amazon affiliate content.

PRODUCT DETAILS:
  Title:    {title}
  Price:    {price}
  Discount: {pct}% off
  Features / VO script:
{features}

YOUR TASK:
Identify the single most relatable PERSONA who would buy this product and the specific
PAIN POINT this product relieves. Then write TWO photorealistic image prompts for
FLUX.1, a state-of-the-art AI image generator.

IMAGE 1 — PAIN POINT (hook frame — makes the viewer say "that's exactly me"):
  Capture the persona IN their frustrating situation, BEFORE they found this product.
  The viewer must instantly feel the discomfort or struggle.
  Do NOT show the product yet.

IMAGE 2 — RELIEF / TRANSFORMATION (reward frame — makes the viewer want the product):
  The same persona experiencing the peak benefit — the moment of relief, satisfaction,
  power, or delight that this product delivers.
  The product (or its effect) should be clearly present.

FLUX PROMPT RULES (apply to BOTH images):
  • Open with: "Photorealistic vertical photograph,"
  • FACELESS — over-shoulder, hands/wrists only, tight crop below chin, or shot from behind
  • Describe the EXACT physical scene: setting, body position, props, what hands are doing
  • Include specific lighting: golden-hour window light / soft overcast / dramatic rim light
  • Describe colour mood: warm & cozy / cool & clinical / vibrant & energetic
  • Close with: "vertical 9:16, cinematic depth of field, sharp foreground, Canon EOS R5"
  • No text, no logos, no on-screen captions
  • Max 120 words per prompt — FLUX responds best to dense, specific descriptions

Respond ONLY in valid JSON — no markdown, no explanation:
{{
  "persona": "2-sentence description of the target buyer",
  "pain_point": "one sharp sentence — the core frustration this product solves",
  "image_1_prompt": "complete FLUX prompt for the pain-point frame",
  "image_2_prompt": "complete FLUX prompt for the relief/transformation frame"
}}
"""


def log(m): print(m, flush=True)


# ── Credentials ───────────────────────────────────────────────────────────────

def _load_gemini_keys():
    keys = []
    for fname in ["geminipro.txt", "geminikeys.txt", "geminikey.txt"]:
        p = Path.home() / fname
        if p.exists():
            text = p.read_text().strip()
            for line in text.splitlines():
                line = line.strip()
                if line and not line.startswith("#") and line not in keys:
                    keys.append(line)
    if not keys:
        sys.exit("No Gemini keys found (~/geminikey.txt / ~/geminikeys.txt)")
    return keys


def _load_cf_creds():
    for base in [Path.home(), Path(SECRETS_DIR)]:
        a = base / "cf_account_id.txt"
        t = base / "cf_api_token.txt"
        if a.exists() and t.exists():
            return a.read_text().strip(), t.read_text().strip()
    sys.exit("Cloudflare credentials not found (cf_account_id.txt / cf_api_token.txt)")


# ── Google Sheet ──────────────────────────────────────────────────────────────

def _open_sheet():
    try:
        import gspread
        from oauth2client.service_account import ServiceAccountCredentials
    except ImportError:
        sys.exit("pip install gspread oauth2client")
    scope  = ["https://spreadsheets.google.com/feeds",
               "https://www.googleapis.com/auth/drive"]
    creds  = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDS, scope)
    client = gspread.authorize(creds)
    return client.open(SHEET_NAME).sheet1


def _asin(link):
    m = re.search(r"asin=([A-Za-z0-9]{10})", link, re.I) or \
        re.search(r"\b(B0[A-Z0-9]{8})\b", link, re.I)
    return m.group(1).upper() if m else ""


def load_product_from_sheet(row_num=None):
    ws   = _open_sheet()
    rows = ws.get_all_values()

    if row_num is not None:
        idx = row_num - 1   # convert to 0-based
        if idx < 1 or idx >= len(rows):
            sys.exit(f"Row {row_num} out of range (sheet has {len(rows)} rows)")
        row = rows[idx]
        while len(row) < COL_S: row.append("")
        return _row_to_product(row, row_num)

    # Default: highest Social Score that is already posted (col P = "Yes")
    best = None
    for i, row in enumerate(rows[1:], start=2):
        while len(row) < COL_S: row.append("")
        if row[COL_P - 1].strip().lower() != "yes": continue
        link = row[COL_A - 1].strip()
        if not link or not _asin(link): continue
        try:    score = float(row[COL_S - 1] or 0)
        except: score = 0.0
        if best is None or score > best[0]:
            best = (score, i, row)

    if not best:
        # Fallback: any row with a title
        for i, row in enumerate(rows[1:], start=2):
            while len(row) < COL_S: row.append("")
            if row[COL_I - 1].strip():
                best = (0.0, i, row)
                break

    if not best:
        sys.exit("No suitable products found in the sheet.")

    score, i, row = best
    log(f"  Sheet row {i} | score={score:.0f}")
    return _row_to_product(row, i)


def _row_to_product(row, row_num):
    while len(row) < COL_S: row.append("")
    return {
        "row":      row_num,
        "title":    row[COL_I - 1].strip() or "Product",
        "price":    row[COL_J - 1].strip() or "N/A",
        "disc":     row[COL_G - 1].strip() or "0",
        "code":     row[COL_F - 1].strip(),
        "vo_text":  row[COL_O - 1].strip(),
        "asin":     _asin(row[COL_A - 1].strip()),
    }


# ── Gemini concept generation ─────────────────────────────────────────────────

def generate_concept(product, keys):
    pct      = re.search(r"(\d+)", product["disc"] or "0")
    pct      = pct.group(1) if pct else "0"
    features = product["vo_text"] or product["title"]
    features = "\n".join(f"  - {line.strip()}" for line in features.splitlines() if line.strip())

    prompt_text = _CONCEPT_PROMPT.format(
        title    = product["title"],
        price    = product["price"],
        pct      = pct,
        features = features,
    )
    payload = {
        "contents":        [{"parts": [{"text": prompt_text}]}],
        "generationConfig": {
            "temperature":    0.85,
            "maxOutputTokens": 2048,
            "thinkingConfig": {"thinkingBudget": 0},  # disable thinking — not needed for JSON output
        },
    }
    for i, key in enumerate(keys):
        url = f"{GEMINI_BASE}/models/{GEMINI_MODEL}:generateContent?key={key}"
        log(f"  Gemini concept (key {i+1}/{len(keys)})...")
        try:
            r = requests.post(url, json=payload, timeout=60)
            if r.status_code == 429:
                log("  → 429, trying next key")
                continue
            r.raise_for_status()
            # gemini-2.5-flash is a thinking model: skip thought parts
            parts = r.json()["candidates"][0]["content"]["parts"]
            text  = next(
                (p["text"] for p in parts if not p.get("thought") and p.get("text")),
                None
            )
            if not text:
                raise ValueError("No text part in response")
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE)
            concept = json.loads(text.strip())
            return concept
        except Exception as e:
            log(f"  → error: {e}")
    sys.exit("All Gemini keys failed for concept generation.")


# ── FLUX.1 Schnell image generation ──────────────────────────────────────────

def flux_generate(account_id, api_token, prompt, model_key="schnell", label="image"):
    model_id, num_steps = FLUX_MODELS[model_key]
    url  = CF_API_BASE.format(account_id=account_id, model=model_id)
    body = {
        "prompt":    prompt,
        "num_steps": num_steps,
        "width":     FLUX_W,
        "height":    FLUX_H,
    }
    log(f"  Cloudflare FLUX.1 {model_key.capitalize()} ({num_steps} steps) → {label}...")
    r = requests.post(
        url,
        headers={"Authorization": f"Bearer {api_token}",
                 "Content-Type":  "application/json"},
        json=body, timeout=120,
    )
    if r.status_code != 200:
        raise RuntimeError(f"CF API {r.status_code}: {r.text[:300]}")
    # FLUX returns raw PNG bytes; Workers AI may wrap in JSON on error
    if "application/json" in r.headers.get("Content-Type", ""):
        data = r.json()
        if not data.get("success"):
            raise RuntimeError(f"CF error: {data.get('errors', r.text[:200])}")
        # Some models return base64 in JSON
        img = data.get("result", {}).get("image")
        if img:
            import base64
            return base64.b64decode(img)
    return r.content   # raw PNG bytes


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--row",      type=int,   help="Sheet row number (header=1)")
    parser.add_argument("--title",    type=str,   help="Product title (skip sheet)")
    parser.add_argument("--features", type=str,   help="Features/VO text (with --title)")
    parser.add_argument("--price",    type=str,   default="N/A")
    parser.add_argument("--disc",     type=str,   default="0% off")
    parser.add_argument("--model",    type=str,   default="schnell",
                        choices=["schnell", "lightning", "both"],
                        help="schnell (FLUX.1 Schnell, best quality on CF) | lightning (SDXL Lightning, faster/lower quality) | both")
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(exist_ok=True)

    # ── 1. Product ────────────────────────────────────────────────────────────
    if args.title:
        product = {
            "row":     0,
            "title":   args.title,
            "price":   args.price,
            "disc":    args.disc,
            "code":    "",
            "vo_text": args.features or args.title,
            "asin":    "MANUAL",
        }
        log(f"Manual product: {args.title[:70]}")
    else:
        log("Reading product from Google Sheet...")
        product = load_product_from_sheet(args.row)
        log(f"  Title:   {product['title'][:70]}")
        log(f"  Price:   {product['price']}  |  Discount: {product['disc']}")

    slug = re.sub(r"[^a-z0-9]+", "_", product["title"][:40].lower()).strip("_")

    # ── 2. Concept (Gemini) ───────────────────────────────────────────────────
    log("\nGenerating pain-point concept via Gemini...")
    keys    = _load_gemini_keys()
    concept = generate_concept(product, keys)

    log(f"\n  Persona:    {concept.get('persona', '')[:100]}")
    log(f"  Pain point: {concept.get('pain_point', '')[:100]}")
    log(f"\n  Prompt 1 (pain):\n    {concept.get('image_1_prompt', '')[:150]}")
    log(f"\n  Prompt 2 (relief):\n    {concept.get('image_2_prompt', '')[:150]}")

    # Save concept JSON for reference / iteration
    concept_path = OUTPUT_DIR / f"{slug}_concept.json"
    concept_path.write_text(json.dumps({**concept, "product": product}, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"\n  Concept saved → {concept_path}")

    # ── 3. FLUX image generation ──────────────────────────────────────────────
    model_keys = ["schnell", "dev"] if args.model == "both" else [args.model]
    account_id, api_token = _load_cf_creds()

    for model_key in model_keys:
        log(f"\nGenerating images via Cloudflare FLUX.1 {model_key.capitalize()}...")
        for i, (key, label) in enumerate([
            ("image_1_prompt", "pain-point"),
            ("image_2_prompt", "relief"),
        ], 1):
            prompt = concept.get(key, "")
            if not prompt:
                log(f"  Skipping image {i} — no prompt")
                continue
            try:
                img_bytes = flux_generate(account_id, api_token, prompt, model_key, label)
                out_path  = OUTPUT_DIR / f"{slug}_{model_key}_img{i}_{label.replace('-','_')}.png"
                out_path.write_bytes(img_bytes)
                log(f"  ✓ Saved → {out_path}  ({len(img_bytes):,} bytes)")
            except Exception as e:
                log(f"  ✗ Image {i} failed: {e}")

    log(f"\n=== Done — output in ./{OUTPUT_DIR}/ ===")


if __name__ == "__main__":
    main()
