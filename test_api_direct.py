#!/usr/bin/env python3
"""
test_api_direct.py — TEMPORARY. Tests the raw JSON API found underneath the
/deals page, from the GitHub runner specifically.

Found via the browser pane (a residential session): the deals grid is actually
backed by a plain JSON endpoint, not scroll-triggered DOM pagination —

    GET https://www.amazon.{tld}/d2b/api/v1/products/search
        ?pageSize=N&startIndex=M&calculateRefinements=false
        &rankingContext={"pageTypeId":"deals","rankGroup":"ESPEON_RANKING"}
        &filters={...excludedTags...}

Called directly from that same browser tab (fetch(), riding on its cookies):
six calls of pageSize=50, incrementing startIndex by nextIndex each time, 300
unique products in 3.2s, zero scrolling, zero clicking. Every field already
structured (asin, title, price.basisPrice/priceToPay, dealBadge text) — no DOM
parsing at all.

TWO THINGS THAT CANNOT BE ASSUMED FROM THAT TEST, and are exactly what this
script exists to check — both are the kind of environment-dependent question
that has to be answered from the actual runner, not locally (this whole
investigation already proved .com treats this environment differently at
least once):

  1. Does .com serve this endpoint to the runner at all, or is IT also walled
     the way the rendered page's pagination was?
  2. Does it need real browser session/cookies (established by first loading
     the page), or does a bare HTTP request work with nothing at all — which
     would mean Selenium/Chrome are not even needed for this step?

Two paths tested, in order:
  A. Bare `requests`, no browser, no cookies at all.
  B. Selenium: load /deals first (real session/cookies), then run the exact
     same fetch() walk from inside the page via execute_script — the closest
     reproduction of what worked in the pane.

    python test_api_direct.py --tld both
"""

import argparse
import io
import json
import os
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests

import amazon_brand_deals as A

RANKING_CONTEXT = {"pageTypeId": "deals", "rankGroup": "ESPEON_RANKING"}
FILTERS = {
    "includedDepartments": [], "excludedDepartments": [],
    "includedTags": [], "excludedTags": ["restrictedasin", "noprime", "GS_DEAL",
                                         "StudentDeal", "restrictedcontent"],
    "promotionTypes": [], "accessTypes": [], "brandIds": [], "unifiedIds": [],
}


def log(m):
    print(m, flush=True)


def api_url(tld, start_index, page_size):
    return (f"https://www.amazon.{tld}/d2b/api/v1/products/search"
            f"?pageSize={page_size}&startIndex={start_index}&calculateRefinements=false"
            f"&rankingContext={json.dumps(RANKING_CONTEXT)}"
            f"&filters={json.dumps(FILTERS)}"
            f"&pinnedPromotionsLayoutGroup=TDP26Devices")


# ── Path A: bare requests, no browser at all ─────────────────────────────────

def try_bare_requests(tld, pages=4, page_size=50):
    log(f"\n{'='*64}\n  PATH A — bare requests, no browser, no cookies — amazon.{tld}\n{'='*64}")
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
        "Accept": "application/json",
    }
    seen, cursor = set(), 0
    for i in range(pages):
        url = api_url(tld, cursor, page_size)
        try:
            r = requests.get(url, headers=headers, timeout=20)
        except Exception as e:
            log(f"    page {i}: request error {e.__class__.__name__}: {str(e)[:100]}")
            break
        if r.status_code != 200:
            log(f"    page {i}: HTTP {r.status_code} — {r.text[:150]}")
            break
        try:
            j = r.json()
        except Exception:
            log(f"    page {i}: 200 but not JSON — {r.text[:150]}")
            break
        got = j.get("products") or []
        for p in got:
            if p.get("asin"):
                seen.add(p["asin"])
        nxt = j.get("nextIndex")
        log(f"    page {i}: HTTP 200, {len(got)} products, {len(seen)} unique so far, nextIndex={nxt}")
        if nxt is None or nxt == cursor:
            break
        cursor = nxt
    log(f"  RESULT PATH A amazon.{tld}: {len(seen)} unique products via bare requests")
    return len(seen)


# ── Path B: Selenium session, fetch() from inside the loaded page ───────────

def try_via_browser_session(tld, headless=True, pages=6, page_size=50):
    log(f"\n{'='*64}\n  PATH B — Selenium session + in-page fetch() — amazon.{tld}\n{'='*64}")
    driver = A._new_driver(headless)
    seen = 0
    try:
        driver.get(f"https://www.amazon.{tld}/deals")
        time.sleep(4)
        script = f"""
const done = arguments[0];
const rankingContext = {json.dumps(RANKING_CONTEXT)};
const filters = {json.dumps(FILTERS)};
function buildUrl(startIndex, pageSize) {{
  return `https://www.amazon.{tld}/d2b/api/v1/products/search?pageSize=${{pageSize}}&startIndex=${{startIndex}}&calculateRefinements=false&rankingContext=${{encodeURIComponent(JSON.stringify(rankingContext))}}&filters=${{encodeURIComponent(JSON.stringify(filters))}}&pinnedPromotionsLayoutGroup=TDP26Devices`;
}}
(async () => {{
  const seen = new Set();
  const pageLog = [];
  let cursor = 0;
  for (let i = 0; i < {pages}; i++) {{
    try {{
      const r = await fetch(buildUrl(cursor, {page_size}), {{headers: {{accept: 'application/json'}}}});
      if (!r.ok) {{ pageLog.push({{i, status: r.status, note: 'FAILED'}}); break; }}
      const j = await r.json();
      (j.products || []).forEach(p => {{ if (p.asin) seen.add(p.asin); }});
      pageLog.push({{i, status: r.status, got: (j.products||[]).length, uniqueSoFar: seen.size, nextIndex: j.nextIndex}});
      if (j.nextIndex == null || j.nextIndex === cursor) break;
      cursor = j.nextIndex;
    }} catch (e) {{ pageLog.push({{i, error: String(e)}}); break; }}
  }}
  done({{totalUnique: seen.size, pageLog}});
}})();
"""
        result = driver.execute_async_script(script)
        for p in result.get("pageLog", []):
            log(f"    {p}")
        seen = result.get("totalUnique", 0)
        log(f"  RESULT PATH B amazon.{tld}: {seen} unique products via in-page fetch()")
    except Exception as e:
        log(f"  ERROR: {e.__class__.__name__}: {str(e)[:200]}")
    finally:
        try:
            driver.quit()
        except Exception:
            pass
    return seen


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tld", default="both", choices=["com", "ca", "both"])
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()
    headless = not args.show

    results = {}
    for tld in (["com", "ca"] if args.tld == "both" else [args.tld]):
        results[(tld, "A_bare")] = try_bare_requests(tld)
        results[(tld, "B_browser_session")] = try_via_browser_session(tld, headless)

    log(f"\n{'='*64}\n  SUMMARY\n{'='*64}")
    for (tld, path), n in results.items():
        log(f"    amazon.{tld:3} {path:20} -> {n:>4} unique products")
    return 0


if __name__ == "__main__":
    sys.exit(main())
