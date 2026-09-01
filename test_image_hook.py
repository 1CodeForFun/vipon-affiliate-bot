#!/usr/bin/env python3
"""
test_image_hook.py — LOCAL test, FREE image hook (fallback for when Veo credits run out).

Python half (all free): random 60%+ Amazon deal -> product title + bullets -> Gemini (free
tier text) writes PAIN POINT + PERSONA + ONE "peak action" frozen the instant BEFORE the big
bang + the IMAGE PROMPT. The IMAGE itself is generated for free via Puter.js
(test_hook_puter.html, gemini-2.5-flash-image) — because the direct Gemini image API is
free-tier limit:0 (paid only), while Puter's user-pays model serves the same model free.

  python test_image_hook.py            # fresh random 60%+ deal
  python test_image_hook.py --cached   # reuse last deals cache
  python test_image_hook.py --asin B0XXXXXX
"""

import re, sys, json, time, random, argparse
from pathlib import Path

# Windows consoles default to cp1252 and crash on the ↪/⚠️ chars our helpers log.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from test_hook_prompt import get_deals, is_safe, load_keys, gemini_text

OUTPUT_DIR = Path("hook_test_output")


def log(m): print(m, flush=True)


# ── Product page: title + bullets via STANDARD Selenium (works on the local IP; the
#    undetected_chromedriver path was version-broken) ──────────────────────────────
def scrape_product(asin):
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from bs4 import BeautifulSoup
    from reel_concept_test import _chrome_bits, _handle_continue_shopping

    opts = Options()
    for a in ("--headless=new", "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
              "--window-size=1280,1000",
              "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"):
        opts.add_argument(a)
    binary, drv = _chrome_bits()
    if binary:
        opts.binary_location = binary
    driver = webdriver.Chrome(service=(Service(executable_path=drv) if drv else Service()), options=opts)
    try:
        url = f"https://www.amazon.com/dp/{asin}"
        driver.get(url); time.sleep(4)
        _handle_continue_shopping(driver, url, settle=4)
        soup = BeautifulSoup(driver.page_source, "html.parser")
        t = soup.select_one("#productTitle")
        title = t.get_text(strip=True) if t else ""
        bullets = []
        for sel in ["#feature-bullets li span.a-list-item", "#productFactsDesktop_feature_div li"]:
            for li in soup.select(sel):
                tx = li.get_text(" ", strip=True)
                if len(tx) > 25 and not tx.lower().startswith("make sure"):
                    bullets.append(tx)
                if len(bullets) >= 5:
                    break
            if bullets:
                break
        return title, bullets[:5]
    finally:
        try:
            driver.quit()
        except Exception:
            pass


CONCEPT_PROMPT = """You are a viral short-form creative director designing the OPENING HOOK \
still image for a faceless product reel. The single frame must make a scroller freeze in the \
first instant.

PRODUCT: "{title}"
TOP BENEFITS:
{bullets}

Work through these steps, then output ONLY JSON:

1. PAIN POINT — the single most universal, relatable frustration this product solves.
2. PERSONA — the specific person who feels it most. FACELESS: only hands / wrists / body from \
behind or cropped at the shoulders — never a face.
3. PEAK ACTION — dramatize that pain point as an escalated, exaggerated PHYSICAL moment, frozen \
at the single instant RIGHT BEFORE the "big bang": the spill / crash / collapse / overflow / snap \
is about to happen but has NOT yet — maximum suspended tension, in medias res. Do NOT show the \
product (the hook is the PROBLEM, not the solution).
4. IMAGE PROMPT — one hyper-detailed photographic still description capturing exactly that frozen \
pre-disaster instant: the faceless subject and exact pose, what is mid-motion / about to give way, \
the framing, lighting, mood. End it with: "photorealistic, cinematic, dramatic high-contrast \
lighting, ultra-sharp focus, 9:16 vertical portrait, no text".

Output ONLY valid JSON (no markdown, no code fences):
{{"pain_point": "...", "persona": "...", "peak_action": "...", "image_prompt": "..."}}"""


def gemini_concept(title, bullets, keys):
    bl = "\n".join(f"- {b}" for b in bullets) if bullets else "(no bullets found — infer from the title)"
    raw = gemini_text(CONCEPT_PROMPT.format(title=title, bullets=bl), keys, max_tokens=900)
    if not raw:
        sys.exit("Gemini concept failed — all free keys exhausted (20/day per key).")
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.S)
        if m:
            return json.loads(m.group(0))
        log("  ⚠️ unparseable concept JSON:\n" + raw)
        sys.exit("Concept JSON parse failed.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cached", action="store_true", help="reuse last deals cache")
    ap.add_argument("--asin", help="specific ASIN from the cache")
    args = ap.parse_args()
    OUTPUT_DIR.mkdir(exist_ok=True)

    deals = get_deals(use_cache=args.cached or bool(args.asin))
    if not deals:
        sys.exit("No deals found.")
    if args.asin:
        match = [d for d in deals if d["asin"] == args.asin]
        if not match:
            sys.exit(f"ASIN {args.asin} not in cache.")
        deal = match[0]
    else:
        safe = [d for d in deals if is_safe(d)]
        if not safe:
            sys.exit("No safe deals after the filter.")
        deal = random.choice(safe)

    asin, title0, pct = deal["asin"], deal.get("title", ""), deal.get("pct")
    log("\n" + "=" * 70)
    log(f"PRODUCT : {title0}")
    log(f"ASIN    : {asin}  ({pct}% off)")
    log("=" * 70)

    log("\nScraping product page (title + bullets)...")
    try:
        title, bullets = scrape_product(asin)
    except Exception as e:
        log(f"  product scrape failed ({e}) — using deal title only")
        title, bullets = title0, []
    title = title or title0
    log(f"Title  : {title[:95]}")
    if bullets:
        log("Bullets:")
        for b in bullets:
            log(f"  - {b[:100]}")
    else:
        log("Bullets: (none found)")

    keys = load_keys()
    log("\nGenerating concept (free Gemini text)...")
    c = gemini_concept(title, bullets, keys)
    log(f"\n  PAIN POINT : {c.get('pain_point')}")
    log(f"  PERSONA    : {c.get('persona')}")
    log(f"  PEAK ACTION: {c.get('peak_action')}")
    log(f"\n  IMAGE PROMPT:\n  {c.get('image_prompt')}")

    out_json = OUTPUT_DIR / f"image_hook_{asin}.json"
    out_json.write_text(json.dumps(
        {"asin": asin, "title": title, "pct": pct, "bullets": bullets, **c},
        indent=2, ensure_ascii=False))

    log("\n" + "=" * 70)
    log(f"✓ Concept ready (free) → {out_json}")
    log("  NEXT (free image): open test_hook_puter.html in your browser, paste the product +")
    log("  bullets above, Generate → Puter renders gemini-2.5-flash-image for free.")
    log("=" * 70)


if __name__ == "__main__":
    main()
