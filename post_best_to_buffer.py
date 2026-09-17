#!/usr/bin/env python3
"""
post_best_to_buffer.py — ONE Amazon deal, scraped, built into a video, and
published to TikTok + Pinterest via Buffer.

Self-contained: scrape -> screen -> build -> publish, one product per run, driven
entirely by however often cron fires it. It does NOT read the vipon sheet for
candidates and does NOT reuse a reel built for Facebook/Instagram/YouTube.

WHAT THIS REPLACED, AND WHY
---------------------------
It used to pick the highest-scoring row out of the sheet that already had a
video and cross-post that same file. Two problems with that:

  - the video was built for the FB/IG/YT audience and was simply re-sent, so
    TikTok and Pinterest never got anything of their own;
  - what it picked was whatever happened to have a video. In practice that was
    already Amazon deals (in a 25-row sample: 6 of 12 deal rows had a video,
    0 of 13 Vipon rows did) but only by accident, not by rule.

Now the source is Amazon deals by construction, and the video is generated for
this flow with its own AI hook image.

NEURON BUDGET — READ BEFORE RAISING THE CRON RATE. Each hook image costs ~1,210
neurons against an account-wide grant of 10,000 per rolling 24h, shared with the
US reel publisher's 6 runs a day:

    6 publisher runs   x 1,210  =  7,260
    3 of these         x 1,210  =  3,630
                                  ------
                                  10,890   against a 10,000 grant

So at three runs a day this sits ~9% over and some runs will lose their hook
image. That is not a crash: generate_hook returns (None, None) on a 429 and the
video is built without the hook. The levers, in order of preference, are fewer
publisher runs, a smaller _IMG_W/_IMG_H in cf_image_hook, or accepting the
occasional hookless video. See cf_image_hook for the full figures.
"""

import json
import os
import re
from datetime import datetime, timedelta

import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ── CONFIG ────────────────────────────────────────────────────────────────────
SECRETS_DIR       = os.environ.get("SECRETS_DIR", ".")
GOOGLE_CREDS_FILE = os.path.join(SECRETS_DIR, "vipon_google_creds.json")
GOOGLE_SHEET_NAME = "vipon"

_CONFIG_TAB = "_config"
_LOG_CELL   = "A5"        # A2 pid history, A3 capped accounts, A4 flash deals
# How long a product stays benched after going out on TikTok/Pinterest. Longer
# than the flash-deal guard's single day because a deals-page item persists for
# days, so a one-day window would let the same product come straight back.
_REPEAT_DAYS = int(os.getenv("BUFFER_REPEAT_DAYS") or "7")

# Deals-page discount floor. 0 = the whole page; the deepest discounts come
# first anyway because fetch_brand_deals sorts by percentage.
BUFFER_MIN_PCT = int(os.getenv("BUFFER_MIN_PCT") or "0")
BUFFER_SCROLLS = int(os.getenv("BUFFER_SCROLLS") or "25")


def log(m):
    print(m, flush=True)


def _open_ss():
    scope = ["https://spreadsheets.google.com/feeds",
             "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDS_FILE, scope)
    return gspread.authorize(creds).open(GOOGLE_SHEET_NAME)


# ── Repeat guard ──────────────────────────────────────────────────────────────

def _today():
    return datetime.now().strftime("%Y-%m-%d")


def read_recent(ss):
    """ASINs sent to TikTok/Pinterest within the last _REPEAT_DAYS days."""
    try:
        val = ss.worksheet(_CONFIG_TAB).acell(_LOG_CELL).value
        hist = json.loads(val) if val else []
        cutoff = (datetime.now() - timedelta(days=_REPEAT_DAYS)).strftime("%Y-%m-%d")
        out = set()
        for e in hist:
            if e.get("date", "") >= cutoff:
                out.update(str(a).upper() for a in e.get("asins", []))
        return out
    except Exception as e:
        # Fail OPEN: a repeat is a smaller problem than posting nothing at all.
        log(f"  could not read buffer history ({e.__class__.__name__}) — treating as empty")
        return set()


def mark_sent(ss, asin):
    try:
        cfg = ss.worksheet(_CONFIG_TAB)
        val = cfg.acell(_LOG_CELL).value
        hist = json.loads(val) if val else []
        today = next((e for e in hist if e.get("date") == _today()), None)
        if today is None:
            today = {"date": _today(), "asins": []}
            hist.insert(0, today)
        if asin.upper() not in {str(a).upper() for a in today["asins"]}:
            today["asins"].append(asin.upper())
        hist = hist[:_REPEAT_DAYS + 1]
        cfg.update(_LOG_CELL, [[json.dumps(hist)]])
        log(f"  logged {asin} ({len(today['asins'])} sent today)")
    except Exception as e:
        log(f"  WARNING could not log {asin} ({e.__class__.__name__}) — it may repeat")


# ── Pick ──────────────────────────────────────────────────────────────────────

def pick_deal(ss):
    """First deal that clears the repeat guard and the keyword screen."""
    from amazon_brand_deals import fetch_brand_deals
    from vipon26 import _blocked_keyword_hit

    recent = read_recent(ss)
    log(f"  {len(recent)} product(s) benched from the last {_REPEAT_DAYS} days")

    from deal_fit import rank, summarise
    deals = fetch_brand_deals(BUFFER_MIN_PCT, scrolls=BUFFER_SCROLLS,
                              want=0, tld="com", exclude_pids=recent)
    # Category fit first — see deal_fit for the click data. Nothing is dropped,
    # so a thin page still fills the run.
    deals = rank(deals)
    log(f"  {len(deals)} deal(s) on the page — {summarise(deals)}")

    repeats = blocked = 0
    for d in deals:
        if d["asin"].upper() in recent:
            repeats += 1
            continue
        hit = _blocked_keyword_hit(d.get("title", ""))
        if hit:
            blocked += 1
            log(f"  ✗ blocked keyword '{hit}' — {d['asin']}")
            continue
        return d
    log(f"  nothing usable ({repeats} benched, {blocked} blocked)")
    return None


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log("=== post_best_to_buffer.py — build one Amazon deal for TikTok + Pinterest ===")

    ss = _open_ss()
    d  = pick_deal(ss)
    if not d:
        log("No deal to publish this run.")
        return

    from vipon26 import _amazon_deal_to_product
    from vipon_publisher import build_and_upload, _read_gemini_keys, _which_ffmpeg, _find_font

    prod = _amazon_deal_to_product(d, "com")
    if not prod:
        log(f"Could not resolve images for {d['asin']} — skipping this run.")
        return

    # _amazon_deal_to_product returns the SHEET shape (pid/discount/image/link).
    # build_concept_video wants the BUILD shape (asin/disc/cover/aff_link). Same
    # product, two vocabularies — translated here rather than changing either,
    # since both shapes have other callers.
    # Per-platform tags, not the shared deals tag _amazon_deal_to_product hands
    # back. This flow publishes to exactly two places, so its clicks should be
    # attributable to those two places — which is the whole reason the
    # per-platform IDs exist. post_to_buffer builds the links it actually posts
    # the same way; these are here so the build sees the same URLs.
    from smartlink import smartlink
    from buffer_publish import TIKTOK_TAG, PINTEREST_TAG
    asin    = prod["pid"]
    tk_link = smartlink(asin, TIKTOK_TAG,    "com")
    pn_link = smartlink(asin, PINTEREST_TAG, "com")

    product = {
        "title":   prod["title"],
        "asin":    asin,
        "price":   prod["price"],
        "code":    "",                      # an open deal has nothing to enter
        "disc":    prod["discount"],
        "expiry":  prod["expiry"],
        "cover":   prod["image"],
        "aff_link":  tk_link,
        "reel_link": tk_link,
        "ig_link":   pn_link,
        "yt_link":   tk_link,
        "vo_text":   "",                    # no sheet copy — generated at build
    }

    log(f"  building: {product['title'][:65]} | {product['disc']} off | {product['asin']}")

    keys   = _read_gemini_keys()
    ffmpeg = _which_ffmpeg()
    font   = _find_font()
    video_url, thumb_url, thumb_path = build_and_upload(
        product, keys, ffmpeg, font, tld="com", with_hook=True
    )
    if thumb_path:
        try: os.unlink(thumb_path)
        except Exception: pass
    if not video_url:
        log("Video build failed — nothing posted this run.")
        return
    log(f"  video: {video_url[:90]}")

    # post_to_buffer builds its own per-platform worker links from the ASIN, so
    # it needs the ASIN and the discount, not the links themselves.
    pct = int(re.search(r"(\d+)", product["disc"] or "0").group(1))
    deal = {
        "title_text": product["title"],
        "title":      product["title"],
        "asin":       product["asin"],
        "pct":        pct,
        "code":       "",
        "price_text": product["price"],
    }

    from buffer_publish import post_to_buffer
    cover = thumb_url or product["cover"]
    post_to_buffer(video_url, deal, "", thumbnail_url=cover, image_url=cover)

    # Logged only after the publish attempt, so a build that never reached
    # Buffer does not bench the product for a week.
    mark_sent(ss, product["asin"])
    log("=== done ===")


if __name__ == "__main__":
    main()
