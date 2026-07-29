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

# Benchmarked cost per 576x1024 image against the 10,000 Neurons/day free tier.
# (steps=None means the model's own default; we don't pass num_steps.)
FLUX_MODELS = {
    "lucid":     ("@cf/leonardo/lucid-origin",                    None),  # ~1730 n  — PRODUCTION
    "phoenix":   ("@cf/leonardo/phoenix-1.0",                     None),  # ~1440 n
    "schnell":   ("@cf/black-forest-labs/flux-1-schnell",            4),  # ~49 n, distortion-prone
    "lightning": ("@cf/bytedance/stable-diffusion-xl-lightning",  None),  # free, lowest quality
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

# The concept prompt lives in cf_image_hook so test and production can't drift.
from cf_image_hook import _CONCEPT_PROMPT, _gemini_concept


def log(m): print(m, flush=True)


# ── Credentials ───────────────────────────────────────────────────────────────

def _load_gemini_keys():
    """Use the production reader so this test never touches the billed key
    (~/geminipro.txt) — see reel_concept_test._read_gemini_keys."""
    from reel_concept_test import _read_gemini_keys
    keys = _read_gemini_keys()
    if not keys:
        sys.exit("No free Gemini keys found (~/geminikeys.txt / ~/geminikey.txt)")
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
    """Delegates to cf_image_hook._gemini_concept so test == production."""
    features = product["vo_text"] or product["title"]
    features = "\n".join(f"  - {l.strip()}" for l in features.splitlines() if l.strip())
    concept  = _gemini_concept(product["title"], features, keys)
    if not concept or not concept.get("image_prompt"):
        sys.exit("All Gemini keys failed for concept generation.")
    return concept


# ── FLUX.1 Schnell image generation ──────────────────────────────────────────

def flux_generate(account_id, api_token, prompt, model_key="lucid", label="image"):
    model_id, num_steps = FLUX_MODELS[model_key]
    url  = CF_API_BASE.format(account_id=account_id, model=model_id)
    body = {"prompt": prompt, "width": FLUX_W, "height": FLUX_H}
    if num_steps:
        body["num_steps"] = num_steps
    log(f"  Cloudflare {model_id} → {label}...")
    r = requests.post(
        url,
        headers={"Authorization": f"Bearer {api_token}",
                 "Content-Type":  "application/json"},
        json=body, timeout=180,
    )
    if r.status_code == 429:
        raise RuntimeError("daily free allocation of 10,000 neurons exhausted "
                           "(resets at UTC midnight)")
    if r.status_code != 200:
        raise RuntimeError(f"CF API {r.status_code}: {r.text[:300]}")
    # Some models return raw PNG bytes, others JSON with a base64 image.
    if "application/json" in r.headers.get("Content-Type", ""):
        data = r.json()
        if not data.get("success"):
            raise RuntimeError(f"CF error: {data.get('errors', r.text[:200])}")
        img = (data.get("result") or {}).get("image")
        if img:
            import base64
            return base64.b64decode(img)
        raise RuntimeError("200 but no image in result")
    return r.content   # raw PNG bytes


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--row",      type=int,   help="Sheet row number (header=1)")
    parser.add_argument("--title",    type=str,   help="Product title (skip sheet)")
    parser.add_argument("--features", type=str,   help="Features/VO text (with --title)")
    parser.add_argument("--price",    type=str,   default="N/A")
    parser.add_argument("--disc",     type=str,   default="0% off")
    parser.add_argument("--model",    type=str,   default="lucid",
                        choices=list(FLUX_MODELS) + ["all"],
                        help="lucid (production) | phoenix | schnell | lightning | all "
                             "— note: 'all' burns a large share of the 10k/day free neurons")
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
    log(f"\n  Image prompt:\n    {concept.get('image_prompt', '')[:200]}")

    # Save concept JSON for reference / iteration
    concept_path = OUTPUT_DIR / f"{slug}_concept.json"
    concept_path.write_text(json.dumps({**concept, "product": product}, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"\n  Concept saved → {concept_path}")

    # ── 3. Image generation ───────────────────────────────────────────────────
    model_keys = list(FLUX_MODELS) if args.model == "all" else [args.model]
    account_id, api_token = _load_cf_creds()
    prompt = concept["image_prompt"]

    for model_key in model_keys:
        try:
            img_bytes = flux_generate(account_id, api_token, prompt, model_key, "pain-point")
            out_path  = OUTPUT_DIR / f"{slug}_{model_key}.png"
            out_path.write_bytes(img_bytes)
            log(f"  ✓ Saved → {out_path}  ({len(img_bytes):,} bytes)")
        except Exception as e:
            log(f"  ✗ {model_key} failed: {e}")

    log(f"\n=== Done — output in ./{OUTPUT_DIR}/ ===")


if __name__ == "__main__":
    main()
