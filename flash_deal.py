#!/usr/bin/env python3
"""
flash_deal.py — ONE Amazon lightning deal, scraped and posted to Facebook.

This is a tail section of the FB text-post flow (FBP_ready.py), not a flow of
its own: no new workflow, no new cron. FB text posts already run hourly, so
this runs hourly with them, after the scheduled rows have gone out.

Per run it opens the deals page filtered to the LIGHTNING-DEALS collection plus
a discount band, walks the grid, and takes the FIRST product that clears two
checks in this order:

    1. not already posted today   (same-day repeat guard, _config!A4)
    2. no prohibited keyword      (the same screen the main scrape uses)

...then builds the post and publishes it immediately.

WHY LIGHTNING DEALS AND NOT A PERCENTAGE FILTER ON THE MAIN DEALS PAGE:
asking /gp/goldbox/ for 90%+ returns 46 items and every one is a Kindle eBook at
$0-$2 — physical goods simply do not get discounted that far, so the percentage
filter alone drags in the book catalogue. Measured at 80%+: 462 items, 429 of
them under $5. The lightning-deals collection is physical goods by construction,
which is what makes a discount band usable here at all.

DISCOUNT BAND is 50-70% by default. Higher bands do exist but skew hard to
apparel and sleepwear; 50-70 was the observed sweet spot for a usable mix.

This page is NOT /gp/goldbox/. It is /deals with a bubble-id, and its cards are
laid out differently — which is why amazon_brand_deals._parse_cards returns
description fragments ("Breathable Cute soft Spring Summer Fall Casu") instead
of titles when pointed at it. Hence the separate parser below.
"""

import json
import os
import random
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from bs4 import BeautifulSoup

# Reused rather than reimplemented: same driver, same real-wheel scroll, same
# keyword screen and the same affiliate smartlink the rest of the pipeline uses.
from amazon_brand_deals import _new_driver, _wheel, _CARDS_JS
from vipon26 import _blocked_keyword_hit, _worker_smartlink, AFFILIATE_ID_DEALS

FLASH_MIN_PCT = int(os.getenv("FLASH_MIN_PCT") or "50")
FLASH_MAX_PCT = int(os.getenv("FLASH_MAX_PCT") or "70")
FLASH_SCROLLS = int(os.getenv("FLASH_SCROLLS") or "12")
_CONFIG_TAB   = "_config"
_LOG_CELL     = "A4"          # A2 = PID history, A3 = capped accounts


def log(m):
    print(m, flush=True)


# ── The page ──────────────────────────────────────────────────────────────────

def lightning_url(min_pct=FLASH_MIN_PCT, max_pct=FLASH_MAX_PCT, tld="com"):
    """Lightning-deals collection, narrowed to a discount band.

    Two independent refinements stacked on /deals:
      bubble-id        selects the Lightning deals pill
      discounts-widget the percentOff slider — a JSON object, json-stringified
                       AGAIN so the inner quotes escape, then double URL-encoded
                       (same encoding as _goldbox_url; it is what the live page
                       sends).
    """
    obj = {"state": {"rangeRefinementFilters": {"percentOff": {"min": min_pct,
                                                              "max": max_pct}}},
           "version": 1}
    enc = quote(quote(json.dumps(json.dumps(obj, separators=(",", ":"))), safe=""), safe="")
    return (f"https://www.amazon.{tld}/deals"
            f"?bubble-id=deals-collection-lightning-deals&discounts-widget={enc}")


# ── The cards ─────────────────────────────────────────────────────────────────

_PCT_RE   = re.compile(r"(\d{1,3})\s*%\s*off", re.I)
_MONEY_RE = re.compile(r"\$\s?([\d,]+(?:\.\d{2})?)")
_CLAIM_RE = re.compile(r"(\d{1,3})\s*%\s*claimed", re.I)


def _full_title(card):
    """The complete title, not the visually truncated one.

    Amazon renders the title twice inside <p id="title-ASIN">: an offscreen span
    carrying the full text, and an aria-hidden span cut to two rows and ending in
    an ellipsis. Taking the card's plain text concatenates both plus the badge,
    the price and the review count — which is where the garbled titles came from.
    """
    p = card.find("p", id=lambda v: bool(v) and v.startswith("title-"))
    if p:
        full = p.find("span", class_="a-truncate-full")
        if full and full.get_text(strip=True):
            return full.get_text(" ", strip=True)
        cut = p.find("span", class_="a-truncate-cut")
        if cut and cut.get_text(strip=True):
            return cut.get_text(" ", strip=True).rstrip("…").strip()
        return p.get_text(" ", strip=True)
    a = card.find("a", attrs={"data-testid": "product-card-link"})
    return a.get_text(" ", strip=True) if a else ""


def _countdown(card):
    """'08:55:31' from the badge's live countdown span, or '' if absent.

    The timer is the whole premise of the post, so a card without one is not
    usable — we will not claim an expiry we cannot read.
    """
    t = card.find("span", attrs={"data-component": "badge-countdown-timer"})
    if not t:
        return ""
    txt = t.get_text(strip=True)
    return txt if re.fullmatch(r"\d{1,2}:\d{2}:\d{2}", txt) else ""


def _prices(card):
    """(price, list_price) as floats. Either may be 0.0 when not shown."""
    sec = card.find("div", attrs={"data-testid": "price-section"})
    if not sec:
        return 0.0, 0.0
    vals = [float(m.replace(",", "")) for m in _MONEY_RE.findall(sec.get_text(" ", strip=True))]
    if not vals:
        return 0.0, 0.0
    if len(vals) == 1:
        return vals[0], 0.0
    # Order differs between layouts, so take by VALUE: the list price is the
    # larger of the two by definition.
    return min(vals), max(vals)


def _parse_cards(html):
    """Lightning-deal cards -> list of dicts. Skips anything missing an ASIN,
    a title or a countdown."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for card in soup.find_all("div", attrs={"data-testid": "product-card"}):
        asin = (card.get("data-asin") or "").strip().upper()
        if not asin:
            continue
        title = _full_title(card)
        ends  = _countdown(card)
        if not title or not ends:
            continue
        text = card.get_text(" ", strip=True)
        pct  = _PCT_RE.search(text)
        claim = _CLAIM_RE.search(text)
        price, list_price = _prices(card)
        img = card.find("img")
        out.append({
            "asin":       asin,
            "title":      title,
            "pct":        int(pct.group(1)) if pct else 0,
            "price":      price,
            "list_price": list_price,
            "ends_in":    ends,
            "claimed":    int(claim.group(1)) if claim else 0,
            "image":      (img.get("src") or "") if img else "",
            "lightning":  "lightning-deals" in (card.get("data-csa-c-filtered") or ""),
        })
    return out


def fetch_flash_deals(min_pct=FLASH_MIN_PCT, max_pct=FLASH_MAX_PCT,
                      tld="com", scrolls=FLASH_SCROLLS, headless=True):
    """Every usable lightning deal in the band, deepest discount first.

    Cards are unmounted as they scroll out of view, so each round reads what is
    on screen RIGHT NOW and accumulates — a card missed this round is gone.
    """
    driver, seen, deals = None, set(), []
    try:
        driver = _new_driver(headless)
        driver.get(lightning_url(min_pct, max_pct, tld))
        import time
        time.sleep(8)
        for _ in range(max(1, scrolls)):
            try:
                html = "".join(driver.execute_script(_CARDS_JS) or [])
            except Exception:
                html = driver.page_source
            for d in _parse_cards(html):
                if d["asin"] not in seen:
                    seen.add(d["asin"])
                    deals.append(d)
            _wheel(driver, 900)
            time.sleep(1.2)
    except Exception as e:
        log(f"  ⚡ flash: scrape failed ({e.__class__.__name__}) — skipping this run")
    finally:
        if driver:
            try: driver.quit()
            except Exception: pass
    deals.sort(key=lambda d: -d["pct"])
    log(f"  ⚡ flash: {len(deals)} lightning deal(s) at {min_pct}-{max_pct}% with a live timer")
    return deals


# ── Same-day repeat guard ─────────────────────────────────────────────────────
# Stored beside the existing state in _config: A2 is the rolling PID history and
# A3 the capped accounts, so A4 keeps the flash log in the same place rather
# than introducing a second mechanism. Only TODAY matters — a lightning deal is
# gone by tomorrow, so there is nothing to age out beyond one day.

def _today():
    return datetime.now().strftime("%Y-%m-%d")


def read_posted_today(ss):
    try:
        val = ss.worksheet(_CONFIG_TAB).acell(_LOG_CELL).value
        data = json.loads(val) if val else {}
        if data.get("date") != _today():
            return set()
        return {str(a).upper() for a in data.get("asins", [])}
    except Exception as e:
        # Fail OPEN on a read error: posting a repeat is a far smaller problem
        # than silently posting nothing for the rest of the day.
        log(f"  ⚡ flash: could not read today's log ({e.__class__.__name__}) — treating as empty")
        return set()


def mark_posted(ss, asin):
    try:
        cfg = ss.worksheet(_CONFIG_TAB)
        val = cfg.acell(_LOG_CELL).value
        data = json.loads(val) if val else {}
        if data.get("date") != _today():
            data = {"date": _today(), "asins": []}
        if asin.upper() not in {str(a).upper() for a in data["asins"]}:
            data["asins"].append(asin.upper())
        cfg.update(_LOG_CELL, [[json.dumps(data)]])
        log(f"  ⚡ flash: logged {asin} ({len(data['asins'])} posted today)")
    except Exception as e:
        log(f"  ⚡ flash: WARNING could not log {asin} ({e.__class__.__name__}) — it may repeat")


# ── The post ──────────────────────────────────────────────────────────────────
# Rotated so an hourly post does not read as the same template every hour.
# ⏳ is reserved for the expiry line below, so it does not appear twice in one post.
_OPENERS = [
    "⚡ FLASH DEAL",
    "⚡ LIGHTNING DEAL — going fast",
    "🔥 ON THE CLOCK",
    "⚡ PRICE DROP — hours only",
    "🔥 QUICK ONE",
    "🔥 FLASH PRICE",
]

_CLOSERS = [
    "Lightning deals end when the timer runs out or stock sells through — whichever comes first.",
    "This one disappears when the clock hits zero.",
    "Once the timer's done, the price goes back.",
    "Lightning deals don't come back at this price.",
]


def _pretty_time(ends_in):
    """'08:55:31' -> '8h 55m'. Never rounds up into a claim we cannot support.

    Kept for logging only — see _ends_at for why the POST does not use this.
    """
    try:
        h, m, _s = (int(x) for x in ends_in.split(":"))
    except Exception:
        return ""
    if h and m:
        return f"{h}h {m}m"
    if h:
        return f"{h}h"
    return f"{m}m"


def _ends_at(ends_in, now=None):
    """'08:55:31' -> 'Ends 9:45 PM ET today'.

    A Facebook post is frozen the moment it publishes: the text never updates
    and the preview image is fetched and cached at post time, so there is no
    such thing as a live countdown in a post. "Ends in 8h 55m" is therefore
    accurate for one instant and wrong for the rest of the deal's life — and
    wrong in the direction that makes the page look like it overstates urgency.
    An absolute clock time stays true for as long as the post exists.

    The page is US, so times are rendered in Eastern and labelled. Returns ''
    when the countdown cannot be read, and the caller then omits the line
    rather than guessing.
    """
    try:
        h, m, s = (int(x) for x in ends_in.split(":"))
    except Exception:
        return ""
    now = now or datetime.now(timezone.utc)
    end = now + timedelta(hours=h, minutes=m, seconds=s)
    # Fixed -4 (EDT). The alternative is a tz database the runner may not carry;
    # an hour's drift for part of the year is not worth that dependency on a
    # line whose job is "roughly when does this stop".
    et = end + timedelta(hours=-4)
    when = et.strftime("%-I:%M %p") if os.name != "nt" else et.strftime("%I:%M %p").lstrip("0")
    day = "today" if et.date() == (now + timedelta(hours=-4)).date() else "tomorrow"
    return f"Ends {when} ET {day}"


def build_post_text(deal):
    left = _ends_at(deal["ends_in"])
    lines = [f"{random.choice(_OPENERS)} — {deal['pct']}% off", "", deal["title"]]

    if deal["price"] and deal["list_price"]:
        lines.append(f"${deal['price']:,.2f}  (was ${deal['list_price']:,.2f})")
    elif deal["price"]:
        lines.append(f"${deal['price']:,.2f}")

    if left:
        tail = f"⏳ {left}"
        # Only mention the claimed bar once it means something — "0% claimed"
        # reads as nobody wants it.
        if deal["claimed"] >= 15:
            tail += f" · {deal['claimed']}% already claimed"
        lines.append(tail)

    lines += ["", random.choice(_CLOSERS)]
    return "\n".join(lines)


# ── Entry point ───────────────────────────────────────────────────────────────

def post_flash_deal(ss, page_id, page_token, graph_api_version, publish_fn, tld="com"):
    """Scrape, screen and post ONE lightning deal. Returns the ASIN or ''.

    Best-effort by design: this runs after the scheduled posts, so anything that
    goes wrong here must never take the main flow down with it.
    """
    deals = fetch_flash_deals(tld=tld)
    if not deals:
        return ""

    posted_today = read_posted_today(ss)
    log(f"  ⚡ flash: {len(posted_today)} already posted today")

    chosen = None
    repeats = blocked = 0
    for d in deals:
        # Order is deliberate and matches the spec: repeat guard first, keyword
        # screen second.
        if d["asin"] in posted_today:
            repeats += 1
            continue
        hit = _blocked_keyword_hit(d["title"])
        if hit:
            blocked += 1
            log(f"  ✗ flash: blocked keyword '{hit}' — {d['asin']}")
            continue
        chosen = d
        break

    if not chosen:
        log(f"  ⚡ flash: nothing new to post ({repeats} already posted today, "
            f"{blocked} blocked) — skipping")
        return ""

    link = _worker_smartlink(chosen["asin"], AFFILIATE_ID_DEALS, tld,
                             chosen["image"], chosen["title"],
                             badge="lightning", pct=chosen["pct"])
    message = build_post_text(chosen)
    log(f"  ⚡ flash: posting {chosen['asin']} — {chosen['pct']}% off, "
        f"ends in {_pretty_time(chosen['ends_in'])}")

    post_id, _ = publish_fn(page_id, page_token, graph_api_version, message, link)
    log(f"  ✅ flash deal posted (id={post_id})")
    # Logged only AFTER a successful post, so a failed publish is retried next
    # hour rather than being burned.
    mark_posted(ss, chosen["asin"])
    return chosen["asin"]
