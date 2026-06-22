#!/usr/bin/env python3
"""
test_hook_prompt.py — LOCAL Veo prompt iteration tool (NO billing, NO video).

Pulls a random 60%+ deal from the Amazon deals page, gets its title + ASIN,
and generates ONE tightened Veo hook prompt using free-tier Gemini text.
Prints the prompt so you can test it MANUALLY in Google AI Studio.

It does NOT call Veo and does NOT spend any paid credits — it only generates
the text prompt. The goal is to iterate on the prompt-engineering (the META_PROMPT
below) until the output is consistently strong, then port META_PROMPT into
publish_reel_hook.py::generate_veo_hook.

Usage:
  python test_hook_prompt.py             # scrape fresh, random deal, gen prompt
  python test_hook_prompt.py --cached    # reuse last scrape (fast iteration)
  python test_hook_prompt.py --asin B0X  # use a specific ASIN from the cache
  python test_hook_prompt.py --key 0     # which geminikey.txt line to use (default 0)

Edit META_PROMPT, re-run with --cached, test the output in AI Studio, repeat.
"""

import re, sys, json, random, argparse
from pathlib import Path

from test_deals_scraper import build_goldbox_url, fetch_page, parse_deals, DEALS_URL

KEYS_FILE  = Path.home() / "geminikey.txt"
CACHE_FILE = Path("scraper_test_output") / "deals_cache.json"
TEXT_MODEL = "gemini-2.5-flash"


def log(m): print(m, flush=True)


# Restricted products — skip anything explicit/intimate-apparel for family-friendly feeds.
# Keep in sync with publish_reel_hook.py::_EXPLICIT_KEYWORDS.
_EXPLICIT_KEYWORDS = {
    "bra", "bras", "bralette", "bikini", "bikinis", "panty", "panties", "underwear",
    "underwire", "underwired", "wirefree", "wirefree", "brassiere", "lingerie",
    "thong", "thongs", "corset", "negligee", "g-string", "camisole", "shapewear",
    "swimsuit", "swimwear", "bodysuit", "boyshort", "boyshorts",
}

# Over-saturated categories — the deals page is flooded with the security-camera /
# doorbell cluster (Blink, Arlo, Ring, etc.); block it so the picker doesn't keep
# choosing the same type. Keep in sync with publish_reel_hook.py::_SKIP_CATEGORY_KEYWORDS.
_SKIP_CATEGORY_KEYWORDS = {
    "doorbell", "blink", "floodlight", "arlo", "wyze", "eufy", "surveillance", "cctv",
}
_CAM_WORDS = {"camera", "cameras", "cam", "cams"}


def is_safe(deal):
    """False if the title has a restricted/explicit keyword OR an over-saturated
    category keyword (doorbell / security camera / floodlight cam)."""
    words = set(re.findall(r"\b\w+\b", deal.get("title", "").lower()))
    blocked = words & _EXPLICIT_KEYWORDS
    if blocked:
        log(f"  Skipping {deal.get('asin')} — restricted keyword(s): {blocked}")
        return False
    over = words & _SKIP_CATEGORY_KEYWORDS
    # generic security cameras: "security" co-occurring with a camera word
    if not over and "security" in words and (words & _CAM_WORDS):
        over = {"security camera"}
    if over:
        log(f"  Skipping {deal.get('asin')} — over-saturated category: {over}")
        return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
# THE META-PROMPT — this is what we iterate on.
# It instructs Gemini how to write the actual Veo video prompt.
# Tightened for: FACELESS, MID-ACTION TENSION, PRODUCT-IN-USE, NO SPEECH,
# and a thumbnail-worthy curiosity peak.
# ─────────────────────────────────────────────────────────────────────────────
META_PROMPT = """You are a viral short-form video director writing a Veo video prompt for the OPENING HOOK of a product deal reel. The hook must make someone STOP scrolling within the first second.

PRODUCT: "{title}"
{features}

Write the Veo prompt in the EXACT STRUCTURED FORMAT below. Do NOT collapse it into one flowing sentence — Veo follows labeled, structured prompts far more reliably. Fill every field with vivid, concrete, photorealistic detail specific to THIS product. Open IN MEDIAS RES: the clip must START already mid-action at the peak of a developing situation — never a calm, static or establishing shot. The "Avoid" block is MANDATORY and must appear verbatim with the listed items.

OUTPUT THIS EXACT TEMPLATE (keep the labels):

Scene: <a charged real-life moment where the product is actively in use; describe the setting, the faceless person, and the product clearly>
First frame (0s): <the literal opening frame — already mid-motion at a moment of peak tension, something actively happening or about to snap; NO calm setup, NO establishing wide, NO product sitting still>
Peak moment (~3s): <the single most dramatic, curiosity-driving instant — the frame that must work as a thumbnail; freeze-worthy tension, something happening or about to>
Subject framing: faceless — <choose: shot from directly behind / over-the-shoulder / hands and wrists only / silhouette / cropped at the shoulders>; the product is held or used by the hands
Camera: <specific movement and framing that keeps the face hidden — e.g. tight handheld push-in, low over-shoulder, fast whip-pan into the action>
Lighting & mood: <dramatic, specific lighting that heightens tension>
Opening sound (0-2s): <a sudden, weird, LOUD non-musical sound effect in the first one to two seconds that fits the scene and jolts a scrolling viewer to stop — e.g. a sharp whoosh, glass shatter, metallic clang, deep bass boom, record scratch, snapping/cracking; it must NOT be speech>
Pacing: start HOT on the very first frame, then escalate across 6 seconds and peak around second 3 — no slow build-up
Avoid: human faces, eyes or facial expressions; on-screen text, captions or subtitles; dialogue, speech or talking; calm, static or staged "lifestyle" shots; brand logos or watermarks; the product sitting unused
Style: photorealistic, cinematic, 9:16 vertical; loud striking sound design with a jarring sound in the first second; no spoken words

GOOD EXAMPLE (Outdoor security camera):
Scene: A homeowner at dusk on a quiet suburban porch, hands pressing a small white security camera onto the wall beside the front door as the street darkens behind them.
First frame (0s): hands already jamming the camera against the wall, knuckles tense, the mounting bracket half-twisted mid-turn.
Peak moment (~3s): The camera's motion spotlight SNAPS on, harshly lighting a hooded figure frozen mid-step at the open back gate, caught in the act.
Subject framing: faceless — low over-the-shoulder from directly behind the homeowner; their hands mount and angle the camera.
Camera: tight handheld over-the-shoulder, a quick push-in toward the gate as the light triggers.
Lighting & mood: deep blue dusk, then a harsh white spotlight burst; tense, alarming.
Opening sound (0-2s): a sudden loud electronic ALARM CHIRP and motion-sensor beep bursting out of near silence.
Pacing: start HOT on the very first frame, then escalate across 6 seconds and peak around second 3.
Avoid: human faces, eyes or facial expressions; on-screen text, captions or subtitles; dialogue, speech or talking; calm, static or staged "lifestyle" shots; brand logos or watermarks; the product sitting unused.
Style: photorealistic, cinematic, 9:16 vertical; loud striking sound design with a jarring sound in the first second; no spoken words.

Now produce the structured prompt for the PRODUCT above. Output ONLY the filled template — no preamble, no explanation, no markdown bold."""


BUILD_KEY_INDEX = 1   # geminikey.txt line 2 = Veo/build key — skip it for free text gen


def load_keys():
    if not KEYS_FILE.exists():
        sys.exit(f"Key file not found: {KEYS_FILE}")
    keys = [k.strip() for k in KEYS_FILE.read_text().splitlines() if k.strip()]
    if not keys:
        sys.exit(f"No keys in {KEYS_FILE}")
    return keys


def gemini_text(prompt, keys, max_tokens=700, start=0):
    """Free-tier Gemini text generation with KEY ROTATION on quota (429) errors.
    Tries each key in turn until one works. Skips the build/Veo key (BUILD_KEY_INDEX)
    so we never touch paid credits. Disables 'thinking' (it burns the output budget)."""
    from google import genai
    from google.genai import types as gtypes

    order = [i for i in range(len(keys)) if i != BUILD_KEY_INDEX]
    if not order:
        order = list(range(len(keys)))
    if start in order:                       # rotate so we begin at `start`
        s = order.index(start)
        order = order[s:] + order[:s]

    for idx in order:
        try:
            client = genai.Client(api_key=keys[idx])
            cfg_kwargs = dict(max_output_tokens=max_tokens, temperature=1.0)
            try:
                cfg_kwargs["thinking_config"] = gtypes.ThinkingConfig(thinking_budget=0)
            except Exception:
                pass
            resp = client.models.generate_content(
                model=TEXT_MODEL, contents=prompt,
                config=gtypes.GenerateContentConfig(**cfg_kwargs),
            )
            text = (resp.text or "").strip()
            for bad, good in {"’": "'", "‘": "'", "“": '"', "”": '"',
                              "—": "-", "–": "-"}.items():
                text = text.replace(bad, good)
            if text:
                log(f"  (generated with key #{idx})")
                return text
            log(f"  key #{idx}: empty response — trying next")
        except Exception as e:
            msg = str(e)
            if any(t in msg for t in ("429", "RESOURCE_EXHAUSTED")) or "quota" in msg.lower():
                log(f"  key #{idx}: quota exhausted — rotating to next key")
            else:
                log(f"  key #{idx}: error ({msg[:70]}) — rotating")
            continue
    log("  All keys exhausted/failed.")
    return None


def get_deals(use_cache):
    """Return list of deal dicts. Scrape fresh, or load from cache for fast iteration."""
    if use_cache and CACHE_FILE.exists():
        deals = json.loads(CACHE_FILE.read_text())
        log(f"Loaded {len(deals)} deals from cache ({CACHE_FILE})")
        return deals

    log("Scraping goldbox (60%+ filter)...")
    snaps = fetch_page(build_goldbox_url(60, 100), "goldbox")
    deals = parse_deals(snaps, "Goldbox")
    deals = [d for d in deals if d["pct"] >= 60]
    if not deals:
        log("Goldbox empty — falling back to /deals...")
        snaps = fetch_page(DEALS_URL, "deals")
        deals = parse_deals(snaps, "Deals")

    CACHE_FILE.parent.mkdir(exist_ok=True)
    CACHE_FILE.write_text(json.dumps(deals, indent=2))
    log(f"Cached {len(deals)} deals to {CACHE_FILE}")
    return deals


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cached", action="store_true", help="reuse last scrape (fast)")
    ap.add_argument("--asin", help="use a specific ASIN from the cache")
    ap.add_argument("--key", type=int, default=0, help="geminikey.txt line index (default 0)")
    args = ap.parse_args()

    deals = get_deals(use_cache=args.cached or bool(args.asin))
    if not deals:
        sys.exit("No deals found.")

    if args.asin:
        match = [d for d in deals if d["asin"] == args.asin]
        deal = match[0] if match else sys.exit(f"ASIN {args.asin} not in cache.")
        if not is_safe(deal):
            sys.exit(f"ASIN {args.asin} is a restricted product.")
    else:
        safe = [d for d in deals if is_safe(d)]
        if not safe:
            sys.exit("No safe deals after restricted-product filter.")
        deal = random.choice(safe)

    title = deal.get("title", "this product")
    features = ""  # title + ASIN only, per spec; left here if we want bullets later

    log("\n" + "=" * 70)
    log(f"PRODUCT : {title}")
    log(f"ASIN    : {deal['asin']}  ({deal['pct']}% off)")
    log("=" * 70)

    keys = load_keys()
    prompt = gemini_text(META_PROMPT.format(title=title, features=features), keys, start=args.key)

    if not prompt:
        sys.exit("Prompt generation failed — all keys exhausted (free tier is 20/day per key).")

    log("\n--- VEO PROMPT (copy into Google AI Studio, 9:16, 6s) ---\n")
    log(prompt)
    log("\n" + "=" * 70)
    log("Edit META_PROMPT in this file and re-run with --cached to iterate fast.")


if __name__ == "__main__":
    main()
