#!/usr/bin/env python3
"""
test_wheel_scroll.py — TEMPORARY. Answers two questions before we change the
scraper:

  1. Do REAL mouse-wheel events work in a HEADLESS browser (what GitHub runs)?
  2. Does the same approach work on amazon.ca?

Background: in the Claude browser on this same machine, same minute --
  scripted scrolling (window.scrollBy)  ->   4 deals, page never grew
  real wheel scrolling                  -> 205 deals, page grew 3.3k -> 21k px
So Amazon's page ignores scripted scrolling. This checks whether Selenium can
send the real thing, headless.

Also proves out the collection strategy: the grid keeps only ~20 cards in the
page at once and recycles them, so we harvest after every small scroll and
accumulate, instead of reading once per big jump.

    python test_wheel_scroll.py --tld com
    python test_wheel_scroll.py --tld com --show     # visible, for comparison
"""

import argparse
import io
import os
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import amazon_brand_deals as A


def log(m):
    print(m, flush=True)


def harvest(driver, seen):
    """Collect every deal ASIN currently in the page.

    Two sources: the grid slot (data-testid holds the ASIN even before the card
    paints) and the painted card itself.
    """
    try:
        asins = driver.execute_script("""
            const out = [];
            document.querySelectorAll('[data-test-index]').forEach(s => {
                const a = s.getAttribute('data-testid');
                if (/^B0[A-Z0-9]{8}$/.test(a||'')) out.push(a);
            });
            document.querySelectorAll('[data-testid="product-card"][data-asin]')
                .forEach(c => out.push(c.getAttribute('data-asin')));
            return out;
        """) or []
    except Exception:
        return
    for a in asins:
        seen.add(a)


def run(tld, headless, mode, max_rounds=60):
    """mode: 'wheel' (real input) or 'script' (what the scraper does today)."""
    url = A._goldbox_url(0, 100, tld=tld)
    log(f"\n{'='*64}\n  amazon.{tld}  |  {'headless' if headless else 'visible'}  |  {mode} scrolling\n{'='*64}")
    driver = A._new_driver(headless)
    seen, marks = set(), []
    try:
        driver.get(url)
        time.sleep(9)
        harvest(driver, seen)
        h0 = driver.execute_script("return document.body.scrollHeight")
        log(f"  on load: {len(seen)} deals, page height {h0}")

        from selenium.webdriver.common.action_chains import ActionChains
        stagnant = 0
        for i in range(max_rounds):
            before = len(seen)
            if mode == "wheel":
                # Real wheel events. Selenium 4 sends these through CDP as
                # genuine input, which is what the page listens for.
                ActionChains(driver).scroll_by_amount(0, 900).perform()
            else:
                driver.execute_script("window.scrollBy(0, window.innerHeight*2.5);")
            time.sleep(0.9)
            harvest(driver, seen)

            if i % 8 == 0:
                h = driver.execute_script("return document.body.scrollHeight")
                marks.append((i, len(seen), h))
                log(f"    round {i:>3}: {len(seen):>4} deals, height {h}")

            if len(seen) == before:
                stagnant += 1
                if stagnant in (6, 14, 22):
                    # At the bottom: try the load-more button.
                    clicked = driver.execute_script("""
                        const b = [...document.querySelectorAll('button,a,div[role="button"],span')]
                          .find(x => /view more deals/i.test((x.innerText||'').trim()) && x.offsetParent !== null);
                        if (b) { b.scrollIntoView({block:'center'}); b.click(); return true; }
                        return false;
                    """)
                    if clicked:
                        log(f"    round {i}: clicked 'View more deals'")
                        time.sleep(4)
                        harvest(driver, seen)
                        stagnant = 0
                if stagnant > 24:
                    log(f"    stopping at round {i} — no new deals")
                    break
            else:
                stagnant = 0

        h = driver.execute_script("return document.body.scrollHeight")
        log(f"\n  RESULT  amazon.{tld} {mode}: {len(seen)} unique deals, final height {h}")
        return len(seen)
    except Exception as e:
        log(f"  ERROR: {e.__class__.__name__}: {str(e)[:140]}")
        return -1
    finally:
        try:
            driver.quit()
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tld", default="both", choices=["com", "ca", "both"])
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--rounds", type=int, default=60)
    args = ap.parse_args()
    headless = not args.show

    results = {}
    for tld in (["com", "ca"] if args.tld == "both" else [args.tld]):
        results[(tld, "script")] = run(tld, headless, "script", args.rounds)
        results[(tld, "wheel")] = run(tld, headless, "wheel", args.rounds)

    log(f"\n{'='*64}\n  SUMMARY ({'headless' if headless else 'visible'})\n{'='*64}")
    for (tld, mode), n in results.items():
        log(f"    amazon.{tld:3} {mode:7} -> {n:>4} deals")
    return 0


if __name__ == "__main__":
    sys.exit(main())
