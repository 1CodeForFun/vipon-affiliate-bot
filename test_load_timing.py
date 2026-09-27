#!/usr/bin/env python3
"""
test_load_timing.py — TEMPORARY. Measures how long the /deals page actually
takes to finish mounting its FIRST batch of cards, on THIS environment.

This is deliberately NOT a local check. Timing is exactly the kind of thing
that can differ between this machine and the GitHub runner — network path to
Amazon, CPU, whatever else — and we already proved once in this same
investigation that Amazon treats a request from this machine differently to
one from GitHub's runner (that is the whole reason the connection-wall showed
up on CI and never showed up here). So a number measured locally would tell us
how fast the page loads locally, nothing about what GitHub experiences. This
script only means anything run from the Actions workflow.

No scrolling, no clicking — this measures ONLY the initial load, the question
being: does the on-screen card count keep growing after the fixed 8s wait
fetch_brand_deals() currently uses, or has it already plateaued? That decides
whether "wait longer" is even the right lever for the deals that go missing
before any scroll ever happens.

    python test_load_timing.py --tld both
"""

import argparse
import io
import json
import os
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import amazon_brand_deals as A


def log(m):
    print(m, flush=True)


def card_count(driver):
    try:
        return driver.execute_script(
            'return document.querySelectorAll(\'[data-testid="product-card"]\').length;'
        ) or 0
    except Exception:
        return -1


def measure(tld, headless=True, duration=22.0, interval=0.5):
    """Sample the on-screen card count every `interval` seconds for `duration`
    seconds after navigation, doing NOTHING else — no scroll, no click."""
    url = A._deals_url(0, 100, tld=tld)
    log(f"\n{'='*64}\n  amazon.{tld}  |  {'headless' if headless else 'visible'}  |  load-timing only\n"
        f"  url: {url}\n{'='*64}")
    driver = A._new_driver(headless)
    samples = []
    try:
        t0 = time.time()
        driver.get(url)
        # Sample from the moment get() returns — driver.get() itself already
        # blocks until the base document loads, so t=0 here is "navigation
        # complete", not "click sent". That is the same t=0 fetch_brand_deals
        # uses for its own 8s wait.
        end = t0 + duration
        while time.time() < end:
            n = card_count(driver)
            elapsed = round(time.time() - t0, 2)
            samples.append({"t": elapsed, "cards": n})
            log(f"    t={elapsed:>5.1f}s  cards={n}")
            time.sleep(interval)
    except Exception as e:
        log(f"  ERROR: {e.__class__.__name__}: {str(e)[:140]}")
    finally:
        try:
            driver.quit()
        except Exception:
            pass

    if samples:
        final = samples[-1]["cards"]
        # First sample time whose count already equals the final count —
        # i.e. the earliest point nothing more showed up after.
        plateau_t = next((s["t"] for s in samples if s["cards"] == final), None)
        log(f"\n  RESULT amazon.{tld}: plateaued at {final} cards by t={plateau_t}s "
            f"(measured for {duration}s total)")
        return {"tld": tld, "samples": samples, "final_cards": final, "plateau_t": plateau_t}
    return {"tld": tld, "samples": [], "final_cards": -1, "plateau_t": None}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tld", default="both", choices=["com", "ca", "both"])
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--duration", type=float, default=22.0)
    ap.add_argument("--interval", type=float, default=0.5)
    args = ap.parse_args()
    headless = not args.show

    results = []
    for tld in (["com", "ca"] if args.tld == "both" else [args.tld]):
        results.append(measure(tld, headless, args.duration, args.interval))

    log(f"\n{'='*64}\n  SUMMARY ({'headless' if headless else 'visible'})\n{'='*64}")
    for r in results:
        log(f"    amazon.{r['tld']:3} -> final {r['final_cards']:>3} cards, "
            f"plateaued at t={r['plateau_t']}s")

    os.makedirs("debug_artifacts", exist_ok=True)
    with open("debug_artifacts/load_timing.json", "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
