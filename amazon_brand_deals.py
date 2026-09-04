#!/usr/bin/env python3
"""
amazon_brand_deals.py — Branded 40%+ Amazon deals, for interleaving with Vipon.

Vipon products carry an exclusive discount CODE. These do not: they are ordinary
Amazon deals that are simply on sale for a limited window. So the pitch has to
lean on the two things they do have — the discount PERCENTAGE and the fact that
the deal EXPIRES — rather than on a code the viewer has to enter.

Sourcing: the goldbox deals page with a percentOff>=40 filter, then filtered
down to recognised brands.

SELENIUM IS REQUIRED, not a convenience. Measured 2026-08-09: fetching the same
URL with `requests` returns the UNFILTERED page — the percentOff refinement is
applied by the page's own JavaScript — so every card comes back at 13-36% and
nothing clears a 40% bar. `&page=N` is ignored too; six pages returned the same
30 cards. Under a real browser the filter applies and scrolling loads more.
Per-brand search (`/s?k=<brand>&rh=p_n_deal_type...`) is worse again: plenty of
ASINs but the search page renders prices lazily, so ~1 discount badge appears in
the HTML and every candidate would need its own page fetch to qualify.

Usage:
  python amazon_brand_deals.py                # show what it finds right now
  python amazon_brand_deals.py --min-pct 50 --scrolls 10
"""

import argparse
import json
import re
import sys
import time
from urllib.parse import quote

import requests

# 0 = take the deals page as it comes. The 40% bar was throwing away most of
# a page that is full of perfectly good products; sorting still puts branded
# and deepest-discounted items first, so quality is preserved by ordering
# rather than by exclusion. Set AMAZON_MIN_PCT to reimpose a floor.
MIN_PCT_DEFAULT = 0

_UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
}

# Well-known brands with heavy Amazon sales that discount deeply and often.
# Ordered roughly by how recognisable the name is on a phone screen — a viewer
# has to register the brand instantly for it to add any pull.
BRANDS = [
    # kitchen & small appliances
    "Ninja", "Instant Pot", "Keurig", "COSORI", "NutriBullet", "KitchenAid",
    "Cuisinart", "Hamilton Beach", "Crock-Pot", "Lodge", "OXO", "Nespresso",
    # floor care, air & home
    "Shark", "Bissell", "Dyson", "iRobot", "Roomba", "LEVOIT", "Tineco", "Roborock",
    # audio & electronics
    "Anker", "soundcore", "Bose", "Sony", "JBL", "Beats", "Apple", "Samsung",
    "Logitech", "TP-Link", "Roku", "Amazon Fire", "Kindle", "Echo",
    # personal care
    "Philips", "Braun", "Oral-B", "Waterpik", "Revlon", "Conair", "Remington",
    # apparel & footwear
    "Nike", "adidas", "Under Armour", "Skechers", "Crocs", "New Balance", "PUMA",
    "Levi's", "Hanes", "Columbia", "The North Face", "Reebok", "ASICS",
    # outdoor & drinkware
    "YETI", "Stanley", "Coleman", "Igloo", "Hydro Flask", "Owala", "CamelBak",
    # beauty & skincare
    "Neutrogena", "CeraVe", "L'Oreal", "Olay", "Maybelline", "Cetaphil", "Aveeno",
    # tools & outdoor power
    "DEWALT", "BLACK+DECKER", "BOSCH", "CRAFTSMAN", "RYOBI", "Greenworks",
    # fitness & health
    "Fitbit", "Garmin", "Therabody", "Theragun", "RENPHO", "Bowflex",
    # baby & pet
    "Graco", "Britax", "Chicco", "PetSafe", "Furbo", "FURminator",
]

_PCT_RE = re.compile(r"(\d+)\s*%\s*off|save\s+(\d+)\s*%|up\s+to\s+(\d+)\s*%", re.I)
# "Ends in 4h", "Ends in 2 days", "Deal ends in ..."
_ENDS_RE = re.compile(r"ends?\s+in\s+([0-9]+\s*(?:h|hr|hour|d|day|m|min)[a-z]*)", re.I)


def log(m):
    print(m, flush=True)


def _goldbox_url(min_pct=MIN_PCT_DEFAULT, max_pct=100, page=1, tld="com"):
    """Goldbox with a working percentOff filter.

    The discounts-widget value is a JSON object, json-stringified AGAIN so the
    inner quotes are escaped, then double URL-encoded. Reverse-engineered from
    the live deals-page slider; reused from publish_reel_hook.
    """
    # No floor asked for -> request the unrefined deals page. Sending a
    # percentOff refinement of min 0 still narrows the grid to items Amazon
    # has tagged with a discount band, which is not the same as "everything".
    if min_pct <= 0 and max_pct >= 100:
        url = f"https://www.amazon.{tld}/gp/goldbox/"
        return url + (f"?page={page}" if page > 1 else "")

    obj = {"state": {"rangeRefinementFilters": {"percentOff": {"min": min_pct,
                                                              "max": max_pct}}},
           "version": 1}
    inner = json.dumps(obj, separators=(",", ":"))
    enc   = quote(quote(json.dumps(inner), safe=""), safe="")
    url   = f"https://www.amazon.{tld}/gp/goldbox/?discounts-widget={enc}"
    return url + (f"&page={page}" if page > 1 else "")


def _despace(s):
    """Collapse letter-spaced card text.

    Some deal cards render every character separated by a space —
    "4 0 % o f f L i m i t e d t i m e d e a l R O U M E A P i l l o w s" —
    which defeats both the discount regex and the title cleanup. Runs of
    single characters are rejoined; genuine words are left alone.
    """
    if not s:
        return s
    toks = s.split(" ")
    if len(toks) < 12:
        return s
    singles = sum(1 for t in toks if len(t) == 1)
    if singles / len(toks) < 0.55:
        return s
    out, run = [], []
    for t in toks:
        if len(t) == 1:
            run.append(t)
        else:
            if run:
                out.append("".join(run)); run = []
            out.append(t)
    if run:
        out.append("".join(run))
    return " ".join(out)


def _extract_pct(text):
    m = _PCT_RE.search(_despace(text) or "")
    return int(next(g for g in m.groups() if g is not None)) if m else None


# Amazon product titles conventionally OPEN with the manufacturer. Anything
# further in is usually a compatibility claim on a third-party accessory —
# "FNTCASE for Galaxy A17" is not a Samsung product, "Anlmz Charging Station for
# iPhone" is not an Apple one — and posting those as branded deals would be
# misleading. Requiring the brand near the start filters them out.
BRAND_HEAD_CHARS = 28


def match_brand(title):
    """Return the brand this title belongs to, or None.

    Word-boundary matched so 'Sony' does not fire inside 'Masonry', longest
    match wins so 'Instant Pot' is not swallowed by a shorter entry, and the
    brand must appear within the opening BRAND_HEAD_CHARS characters.
    """
    head = (title or "")[:BRAND_HEAD_CHARS]
    hits = []
    for b in BRANDS:
        pat = r"(?<![A-Za-z])" + re.escape(b).replace(r"\ ", r"\s+") + r"(?![A-Za-z])"
        if re.search(pat, head, re.I):
            hits.append(b)
    return max(hits, key=len) if hits else None


def _is_usable_title(title: str) -> bool:
    """Is this a real product name, or price-block wreckage?

    The old check was `len(title) < 8`, which "99 Typical:" clears at 11
    characters — so it reached the sheet, the caption and the reel as the
    product name. A genuine product title has at least two real words.
    """
    if not title or len(title) < 8:
        return False
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z'&+-]{2,}", title)]
    if len(words) < 2:
        return False
    # Mostly digits and punctuation is a mangled price, not a name.
    letters = sum(c.isalpha() for c in title)
    return letters >= max(8, len(title) * 0.45)


def _parse_cards(html, min_pct, require_brand=False):
    """Parse deal cards -> [{asin, title, pct, ends_in, brand}]. Branded only."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        log("  beautifulsoup4 not installed — cannot parse deals")
        return []

    soup = BeautifulSoup(html, "html.parser")
    cards = (soup.find_all("div", attrs={"data-testid": "product-card"})
             or soup.find_all("div", attrs={"data-csa-c-item-type": True})
             or soup.find_all("div", attrs={"data-asin": True}))

    out, seen = [], set()
    for c in cards:
        asin = (c.get("data-asin") or "").strip()
        if not re.fullmatch(r"[A-Z0-9]{10}", asin):
            a = c.find("a", href=re.compile(r"/dp/([A-Z0-9]{10})"))
            m = re.search(r"/dp/([A-Z0-9]{10})", a.get("href", "")) if a else None
            asin = m.group(1) if m else ""
        if not asin or asin in seen:
            continue

        text = c.get_text(" ", strip=True)
        pct  = _extract_pct(text)
        if pct is None or pct < min_pct:
            continue

        # Title extraction. The card exposes no aria-label or title attribute, and
        # `product-card-link` is an empty overlay anchor — its text is "". The
        # real text is on the CARD, and runs:
        #   "46% off  Limited time deal  Deal Price: $37.89 ... $69.99  <TITLE>"
        # so the product name comes LAST, after the price block. Subtract the
        # price/swatch sub-element TEXT (removing those nodes from the DOM does
        # not work — the title is nested within them) and strip badge wording and
        # stray prices from what remains.
        title = _despace(re.sub(r"\s+", " ", text))
        for tid in ("price-section", "color-swatch"):
            sub = c.find(attrs={"data-testid": tid})
            if sub:
                title = title.replace(_despace(re.sub(r"\s+", " ",
                                     sub.get_text(" ", strip=True))), " ")
        # Price LABELS have to go before the money regex runs, otherwise a card
        # using Amazon's "Typical price: $34.99" layout leaves "99 Typical:"
        # behind as the product name — which then reaches the sheet, the caption
        # and the reel. Seen live on B0G2HMN9X1.
        title = re.sub(r"typical\s+price\s*:?|typical\s*:|list\s+price\s*:?|list\s*:|"
                       r"deal\s+price\s*:?|was\s*:|now\s*:|price\s*:|with\s+coupon|"
                       r"save\s+\$\s?[\d,]+(?:\s?\.\s?\d+)?|save\s+extra",
                       " ", title, flags=re.I)
        title = re.sub(r"\d+\s*%\s*off|save\s+\d+\s*%|limited time deal|"
                       r"deal of the day|best seller|prime exclusive|ends? in\b[^|]*|"
                       r"\$\s?[\d,]+(?:\s?\.\s?\d+)?",
                       " ", title, flags=re.I)
        # Orphaned cents left by a partially-stripped price ("$34." -> "99").
        title = re.sub(r"^\s*\d{1,2}\b(?!\s*(?:pcs?|pack|pk|ct|count|oz|ml|l|g|kg|"
                       r"lb|in|ft|cm|mm|w|v|k|x)\b)", " ", title, flags=re.I)
        title = re.sub(r"\s+", " ", title).strip(" -|·,:")
        if not _is_usable_title(title):
            continue
        # Reject anything still letter-spaced after _despace — a handful of cards
        # use a layout it cannot recover, and a mangled title would end up in the
        # reel and the caption. There is ample supply, so drop them.
        toks = title.split()
        if len(toks) > 6 and sum(1 for t in toks if len(t) == 1) / len(toks) > 0.3:
            continue
        # Brand is a PREFERENCE, not a gate. The deals page simply does not carry
        # enough recognised-brand stock at 40%+ to fill half a 48-product sheet —
        # a live run returned 14 branded items against a need for 24. Unbranded
        # 40%+ deals are still perfectly good posts, so keep everything and let
        # the caller sort branded first.
        brand = match_brand(title)
        if require_brand and not brand:
            continue

        # Price. The price node reads "Deal Price: $37.89 $ 37 . 89 List: $69.99",
        # so the first money value is what you pay and the last is the list price.
        price = list_price = ""
        psec = c.find(attrs={"data-testid": "price-section"})
        money = []
        if psec:
            money = re.findall(r"\$\s?([\d,]+(?:\.\d{2})?)",
                               _despace(re.sub(r"\s+", " ", psec.get_text(" ", strip=True))))
        if not money:
            # Not every card exposes a price-section: the "Typical price" layout
            # puts the money straight on the card, which left the sheet with a
            # blank price and a caption that could not mention one.
            money = re.findall(r"\$\s?([\d,]+(?:\s?\.\s?\d{2})?)",
                               _despace(re.sub(r"\s+", " ", text)))
            money = [m.replace(" ", "") for m in money]
        if money:
            # Decide by VALUE, not position. "Deal Price: $37.89 List: $69.99"
            # puts what you pay first; "Typical price: $34.99 $28.65" puts it
            # last. What you pay is always the lower of the two.
            vals = []
            for m in money:
                try:
                    vals.append(float(m.replace(",", "")))
                except ValueError:
                    pass
            if vals:
                lo, hi = min(vals), max(vals)
                fmt = lambda x: f"${x:,.2f}".rstrip("0").rstrip(".") if x % 1 else f"${x:,.0f}"
                price = fmt(lo)
                if hi > lo:
                    list_price = fmt(hi)

        # Product image straight off the card. Re-fetching it from the product
        # page was failing constantly on the CI runner ("HTTP image fallback: 0"),
        # and a deal with no image is dropped entirely — which is why only a
        # handful of Amazon rows were reaching the sheet. The card already has a
        # usable image; the size suffix is stripped for a larger render.
        image = ""
        img = c.find("img")
        if img:
            image = (img.get("src") or img.get("data-src") or "").split("?")[0]
            image = re.sub(r"\._AC_[^.]*(?=\.[a-z]{3,4}$)", "._AC_SL1500_", image)

        ends = _ENDS_RE.search(_despace(text))
        seen.add(asin)
        out.append({"asin": asin, "title": title[:200], "pct": pct,
                    "brand": brand or "", "price": price, "list_price": list_price,
                    "image": image,
                    "ends_in": ends.group(1) if ends else ""})
    return out


def _click_load_more(driver) -> bool:
    """Click the grid's "View more deals" button. True if it was clicked.

    Confirmed in the live DOM:
      <div data-testid="load-more-footer">
        <button data-testid="load-more-view-more-button">View more deals</button>
    Without this the scraper reaches the end of the first grid and just re-reads
    the same cards, which capped the pool no matter how many scrolls were set.
    """
    from selenium.webdriver.common.by import By
    sels = [
        (By.CSS_SELECTOR, '[data-testid="load-more-view-more-button"]'),
        (By.CSS_SELECTOR, '[data-testid="load-more-footer"] button'),
        (By.XPATH, "//button[contains(., 'View more deals')]"),
        (By.XPATH, "//*[self::button or self::a][contains(., 'See more deals')]"),
    ]
    for how, what in sels:
        try:
            for el in driver.find_elements(how, what):
                if not el.is_displayed():
                    continue
                driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center'});", el)
                time.sleep(0.4)
                driver.execute_script("arguments[0].click();", el)
                return True
        except Exception:
            continue
    return False


def _new_driver(headless=True):
    """Chrome for the deals page. undetected_chromedriver where available (same
    as the Vipon scrape), plain Selenium otherwise."""
    binary = next((b for b in ("/usr/bin/chromium-browser", "/usr/bin/chromium",
                               "/usr/bin/google-chrome") if __import__("os").path.exists(b)), "")
    args = ["--no-sandbox", "--disable-dev-shm-usage", "--window-size=1400,2000",
            "--disable-blink-features=AutomationControlled", f"--lang=en-US"]
    try:
        import undetected_chromedriver as uc
        o = uc.ChromeOptions()
        for a in args:
            o.add_argument(a)
        if headless:
            o.add_argument("--headless=new")
        if binary:
            o.binary_location = binary
        return uc.Chrome(options=o)
    except Exception:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        o = Options()
        for a in args:
            o.add_argument(a)
        if headless:
            o.add_argument("--headless=new")
        if binary:
            o.binary_location = binary
        return webdriver.Chrome(options=o)


def fetch_brand_deals(min_pct=MIN_PCT_DEFAULT, scrolls=14, want=0,
                      headless=True, require_brand=False, tld="com"):
    """Branded deals at >= min_pct, highest discount first. [] on failure.

    Selenium is required, not a nicety. Fetching the same URL with requests
    returns the UNFILTERED page — the percentOff refinement is applied by the
    page's own JavaScript — so every card comes back at 13-36% and nothing
    clears a 40% bar. `&page=N` is ignored too: all six pages returned the same
    30 cards. Under a real browser the filter applies and scrolling loads more.
    """
    driver = None
    try:
        driver = _new_driver(headless)
    except Exception as e:
        log(f"  brand deals: cannot start Chrome ({str(e)[:70]})")
        return []

    found, seen = [], set()
    try:
        driver.get(_goldbox_url(min_pct, 100, tld=tld))
        time.sleep(6)                      # let the refinement apply
        stalls, last_h = 0, 0
        for i in range(max(1, scrolls)):
            got = [d for d in _parse_cards(driver.page_source, min_pct, require_brand)
                   if d["asin"] not in seen]
            for d in got:
                seen.add(d["asin"])
            found += got
            if got:
                log(f"  deals: +{len(got)} (total {len(found)})")
            if want and len(found) >= want:
                break

            # Walk DOWN the page rather than teleporting to the bottom. The grid
            # lazy-loads off intersection observers as cards pass through the
            # viewport; scrollTo(bottom) skips them, so the page stops growing
            # and the run looks exhausted after two rounds when it is not.
            driver.execute_script(
                "window.scrollBy(0, window.innerHeight*2.5);")
            time.sleep(2.0)

            h = driver.execute_script("return document.body.scrollHeight;") or 0
            grew = h > last_h
            last_h = h

            # Only when the page has stopped growing do we go looking for the
            # "View more deals" button that ends the grid.
            clicked = False
            if not grew:
                driver.execute_script(
                    "window.scrollTo(0, document.body.scrollHeight);")
                time.sleep(1.2)
                clicked = _click_load_more(driver)
                if clicked:
                    time.sleep(3.0)

            # A stall is no new cards AND no page growth AND no button. CI
            # runners are slow enough that a single quiet round is routine, so
            # only give up after several consecutive ones.
            if got or grew or clicked:
                stalls = 0
            else:
                stalls += 1
                if stalls >= 4:
                    log(f"  deals: no more to load (after {i+1} rounds)")
                    break
    except Exception as e:
        log(f"  brand deals: scrape error ({str(e)[:70]})")
    finally:
        try:
            driver.quit()
        except Exception:
            pass

    # Branded first, then deepest discount — the caller takes from the top.
    found.sort(key=lambda d: (0 if d["brand"] else 1, -d["pct"]))
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-pct", type=int, default=MIN_PCT_DEFAULT)
    ap.add_argument("--scrolls", type=int, default=8)
    ap.add_argument("--show-browser", action="store_true")
    ap.add_argument("--branded-only", action="store_true")
    args = ap.parse_args()

    log(f"searching goldbox for {args.min_pct}%+ deals from {len(BRANDS)} known brands...")
    deals = fetch_brand_deals(args.min_pct, args.scrolls,
                              headless=not args.show_browser,
                              require_brand=args.branded_only)
    log(f"\n{len(deals)} branded deal(s):\n")
    for d in deals:
        ends = f"  ends in {d['ends_in']}" if d["ends_in"] else ""
        log(f"  {d['pct']:>3}%  {d['brand']:<14} {d['asin']}  {d['title'][:70]}{ends}")


if __name__ == "__main__":
    main()
