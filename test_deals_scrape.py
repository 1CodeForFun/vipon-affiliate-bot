#!/usr/bin/env python3
"""
test_deals_scrape.py — TEMPORARY diagnostic. Delete once the deals shortfall
is fixed; nothing in the pipeline imports it.

WHY THIS EXISTS
The scrape log shows Amazon returning 6 deal cards to the GitHub runner where
the same code gets 337 on a home connection:

    deals: +6 (total 6)  ->  no more to load (after 6 rounds)
    Amazon com: 6 deal(s), 2 usable, 0 blocked, 0 near-duplicate
    US Amazon rows written: 2/24

A card count cannot distinguish a throttled response from a captcha, a
bot-interstitial or a layout change — they all look like "6 deals". This opens
the same page with the same driver and records what Amazon actually served, so
the cause is visible rather than inferred.

It runs the deals fetch ONLY: no Google Sheet, no Vipon login, no video build,
so a run takes ~2 minutes instead of the full pipeline's hour.

    python test_deals_scrape.py                # both marketplaces
    python test_deals_scrape.py --tld com      # one
    python test_deals_scrape.py --show         # visible window, local use

Output lands in debug_artifacts/ and is uploaded by .github/workflows/test_deals.yml.
"""

import argparse
import io
import json
import os
import re
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import amazon_brand_deals as A

OUT = os.path.join(os.getcwd(), "debug_artifacts")

# Phrases Amazon serves instead of the grid when it does not trust the client.
BLOCK_MARKERS = [
    "enter the characters you see below",
    "sorry, we just need to make sure you're not a robot",
    "type the characters you see in this image",
    "to discuss automated access to amazon data",
    "we're sorry, an error has occurred",
    "robot check",
    "captcha",
]


def log(m):
    print(m, flush=True)


def snap(driver, name):
    png = os.path.join(OUT, f"{name}.png")
    htm = os.path.join(OUT, f"{name}.html")
    try:
        driver.save_screenshot(png)
    except Exception as e:
        log(f"    (screenshot failed: {e.__class__.__name__})")
    try:
        with open(htm, "w", encoding="utf-8") as fh:
            fh.write(driver.page_source)
    except Exception as e:
        log(f"    (html save failed: {e.__class__.__name__})")
    return png, htm


def probe(tld, scrolls, headless):
    """One baseline pass. Records, at each stage, what the page actually is."""
    url = A._goldbox_url(0, 100, tld=tld)
    log(f"\n{'='*66}\nMARKETPLACE: amazon.{tld}\n  url: {url}\n{'='*66}")

    report = {"tld": tld, "url": url}
    driver = None
    try:
        driver = A._new_driver(headless)
    except Exception as e:
        log(f"  FATAL: cannot start Chrome — {e.__class__.__name__}: {str(e)[:90]}")
        report["error"] = f"chrome: {e.__class__.__name__}"
        return report

    try:
        t0 = time.time()
        driver.get(url)
        time.sleep(8)
        report["load_seconds"] = round(time.time() - t0, 1)
        report["landed_url"] = driver.current_url
        report["page_title"] = driver.title
        html = driver.page_source
        report["html_bytes"] = len(html)

        low = html.lower()
        hits = [m for m in BLOCK_MARKERS if m in low]
        report["block_markers"] = hits

        log(f"  landed on : {driver.current_url[:96]}")
        log(f"  page title: {driver.title!r}")
        log(f"  html size : {len(html):,} bytes   (a real grid is ~1.5M)")
        log(f"  bot-block markers: {hits or 'none'}")

        # How many product cards exist in the DOM at all, before any parsing
        # rules are applied — separates "Amazon sent nothing" from "our parser
        # rejected what it sent".
        raw_cards = len(re.findall(r'data-testid="product-card"', html))
        parsed = A._parse_cards(html, 0)
        log(f"  cards in DOM on load: {raw_cards}   parsed by our rules: {len(parsed)}")
        report["cards_on_load"] = raw_cards
        report["parsed_on_load"] = len(parsed)

        snap(driver, f"deals_{tld}_01_onload")

        # Now scroll exactly the way production does and watch whether the grid
        # grows. If it never grows, lazy-loading is not firing for this client.
        seen, growth = set(d["asin"] for d in parsed), []
        for i in range(scrolls):
            driver.execute_script("window.scrollBy(0, window.innerHeight*2.5);")
            time.sleep(2.0)
            h = driver.execute_script("return document.body.scrollHeight;") or 0
            page = driver.page_source
            now = A._parse_cards(page, 0)
            new = [d for d in now if d["asin"] not in seen]
            for d in new:
                seen.add(d["asin"])
            growth.append({"round": i + 1, "height": h, "total": len(seen), "new": len(new)})
            log(f"    round {i+1:>2}: height={h:>7}  +{len(new):>3} new  total={len(seen)}")
            if i == scrolls - 1 or (len(new) == 0 and i >= 3):
                break

        report["growth"] = growth
        report["final_total"] = len(seen)
        snap(driver, f"deals_{tld}_02_after_scroll")

        # Does the load-more button exist for this client?
        try:
            from selenium.webdriver.common.by import By
            btns = driver.find_elements(
                By.CSS_SELECTOR, '[data-testid="load-more-view-more-button"]')
            report["load_more_present"] = len(btns)
            log(f"  'View more deals' buttons found: {len(btns)}")
        except Exception:
            report["load_more_present"] = "error"

        log(f"\n  RESULT: {len(seen)} deals for amazon.{tld}")

    except Exception as e:
        log(f"  ERROR during probe: {e.__class__.__name__}: {str(e)[:120]}")
        report["error"] = f"{e.__class__.__name__}: {str(e)[:120]}"
        try:
            snap(driver, f"deals_{tld}_99_error")
        except Exception:
            pass
    finally:
        try:
            driver.quit()
        except Exception:
            pass
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tld", default="both", choices=["com", "ca", "both"])
    ap.add_argument("--scrolls", type=int, default=8)
    ap.add_argument("--show", action="store_true", help="visible window (local only)")
    args = ap.parse_args()

    os.makedirs(OUT, exist_ok=True)
    log(f"Baseline deals probe — output to {OUT}")
    log(f"runner: {'GitHub Actions' if os.getenv('GITHUB_ACTIONS') else 'local'}")

    tlds = ["com", "ca"] if args.tld == "both" else [args.tld]
    reports = [probe(t, args.scrolls, headless=not args.show) for t in tlds]

    with open(os.path.join(OUT, "deals_probe.json"), "w", encoding="utf-8") as fh:
        json.dump(reports, fh, indent=2)

    log(f"\n{'='*66}\nSUMMARY\n{'='*66}")
    for r in reports:
        log(f"  amazon.{r['tld']:3} : {r.get('final_total', 0):>4} deals | "
            f"html {r.get('html_bytes', 0):>9,}b | "
            f"title {str(r.get('page_title'))[:34]!r} | "
            f"blocks={r.get('block_markers') or 'none'}")
    log(f"\n  files: {sorted(os.listdir(OUT))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
