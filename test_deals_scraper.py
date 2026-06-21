#!/usr/bin/env python3
"""
test_deals_scraper.py — local test for Amazon deals page scraping.

Run: python test_deals_scraper.py

Fetches both the goldbox URL and amazon.com/deals, parses deals with
BeautifulSoup, and prints a summary. Also saves page source to disk
so you can inspect what Selenium actually sees.
"""

import os, re, time, json
from pathlib import Path
from urllib.parse import quote


def build_goldbox_url(min_pct=60, max_pct=100):
    """Build the goldbox URL with a working percentOff discount filter.
    Reverse-engineered from the live slider URL: the discounts-widget value is a
    JSON string, json-stringified again (escaped quotes), then double URL-encoded."""
    obj = {"state": {"rangeRefinementFilters": {"percentOff": {"min": min_pct, "max": max_pct}}},
           "version": 1}
    inner = json.dumps(obj, separators=(",", ":"))
    widget_value = json.dumps(inner)                       # wrap in quotes + escape inner quotes
    encoded = quote(quote(widget_value, safe=""), safe="")  # double URL-encode
    return f"https://www.amazon.com/gp/goldbox/?discounts-widget={encoded}"


GOLDBOX_URL = build_goldbox_url(60, 100)
DEALS_URL = "https://www.amazon.com/deals"

OUT_DIR = Path("scraper_test_output")
OUT_DIR.mkdir(exist_ok=True)


def log(m): print(m, flush=True)


def _init_driver():
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    opts = Options()
    for a in (
        "--headless=new", "--no-sandbox", "--disable-dev-shm-usage",
        "--disable-gpu", "--window-size=1280,900",
        "--user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    ):
        opts.add_argument(a)
    return webdriver.Chrome(options=opts)


def fetch_page(url, label):
    """Load URL with Selenium, scroll to trigger lazy loading, collecting a
    page-source SNAPSHOT at each scroll position (the page virtualizes — cards
    that scroll out of view get removed from the DOM, so the final page_source
    alone misses most deals). Returns a list of HTML snapshots."""
    log(f"\n{'='*60}")
    log(f"Fetching: {label}")
    log(f"URL: {url[:80]}...")
    driver = _init_driver()
    snapshots = []
    try:
        driver.get(url)
        time.sleep(8)
        snapshots.append(driver.page_source)
        for px in (1500, 3000, 5000, 7000, 9000, 12000, 15000, 18000):
            driver.execute_script(f"window.scrollTo(0, {px})")
            time.sleep(1.5)
            snapshots.append(driver.page_source)
        log(f"Captured {len(snapshots)} snapshots "
            f"({sum(len(s) for s in snapshots):,} total chars)")

        # Save last snapshot to disk for inspection
        out = OUT_DIR / f"{label.replace(' ', '_').lower()}.html"
        out.write_text(snapshots[-1], encoding="utf-8")
        log(f"Saved last snapshot to: {out}")
        return snapshots
    finally:
        try: driver.quit()
        except: pass


def parse_one_snapshot(html):
    """Parse a single HTML snapshot by iterating product-card divs.
    Each <div data-testid='product-card' data-asin='ASIN'> contains a
    <span class='a-size-mini'>X% off</span> badge and a /dp/ASIN link."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, 'html.parser')

    cards = soup.find_all('div', attrs={'data-testid': 'product-card'})
    if not cards:
        cards = soup.find_all('div', attrs={'data-asin': True})

    out = []
    for c in cards:
        asin = c.get('data-asin', '').strip()
        if not asin or not re.match(r'^[A-Z0-9]{10}$', asin):
            link0 = c.find('a', href=re.compile(r'/dp/([A-Z0-9]{10})'))
            if link0:
                mm = re.search(r'/dp/([A-Z0-9]{10})', link0.get('href', ''))
                asin = mm.group(1) if mm else ''
        if not asin:
            continue

        badge = c.find('span', class_=re.compile(r'\ba-size-mini\b'),
                       string=re.compile(r'\d+\s*%\s*off', re.I))
        if not badge:
            continue
        pct = int(re.match(r'(\d+)', badge.get_text(strip=True)).group(1))

        link = c.find('a', href=re.compile(r'/dp/[A-Z0-9]{10}'))
        href = link.get('href', '') if link else ''
        slug = re.search(r'amazon\.com/([^/]+)/dp/', href) or re.match(r'/([^/]+)/dp/', href)
        title = slug.group(1).replace('-', ' ')[:80] if slug else asin

        out.append({'asin': asin, 'pct': pct, 'title': title})
    return out


def parse_deals(snapshots, label):
    """Merge deals across all snapshots (dedupe by ASIN, keep max discount seen)."""
    merged = {}
    for snap in snapshots:
        for d in parse_one_snapshot(snap):
            if d['asin'] not in merged or d['pct'] > merged[d['asin']]['pct']:
                merged[d['asin']] = d

    deals = sorted(merged.values(), key=lambda x: -x['pct'])
    log(f"\n[{label}] merged {len(deals)} unique deals across {len(snapshots)} snapshots:")
    for d in deals:
        flag = "  <-- 60%+" if d['pct'] >= 60 else ""
        log(f"    {d['pct']:>3}% off  {d['asin']}  {d['title'][:55]}{flag}")
    return deals


def main():
    log("=== Amazon Deals Scraper Test ===")
    log(f"Output dir: {OUT_DIR.resolve()}")

    # Test goldbox URL
    snaps_gb = fetch_page(GOLDBOX_URL, "goldbox")
    deals_gb = parse_deals(snaps_gb, "Goldbox")

    # Test amazon.com/deals
    snaps_d = fetch_page(DEALS_URL, "deals")
    deals_d = parse_deals(snaps_d, "Deals")

    # Simulate the production selection logic: prefer 60%+, else highest available
    def pick(deals, label):
        qualified = [d for d in deals if d['pct'] >= 60]
        if qualified:
            log(f"  {label}: {len(qualified)} deals at 60%+ — would randomly pick one of them")
            return qualified
        elif deals:
            top = max(d['pct'] for d in deals)
            best = [d for d in deals if d['pct'] == top]
            log(f"  {label}: NONE at 60%+ — fallback to highest ({top}%): "
                f"{best[0]['asin']} {best[0]['title'][:40]}")
            return best
        else:
            log(f"  {label}: no deals at all")
            return []

    log(f"\n{'='*60}")
    log("SUMMARY (production selection simulation)")
    pick(deals_gb, "Goldbox")
    pick(deals_d, "/deals")
    log(f"\nGoldbox URL used:\n  {GOLDBOX_URL[:100]}...")
    log(f"\nPage sources saved to {OUT_DIR.resolve()}")


if __name__ == "__main__":
    main()
