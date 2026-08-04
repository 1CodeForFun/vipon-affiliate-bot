#!/usr/bin/env python3
"""
cf_measure_image_cost.py — Measure the REAL neuron cost of one hook image.

Catalogue rates and per-call averages have both misled us before: 429-rejected
calls are logged at 0 neurons, so total/call_count understates the true figure.
This generates exactly ONE image and diffs the account's own analytics before
and after, which is the only number worth trusting.

Usage:
  python cf_measure_image_cost.py                 # current production size
  python cf_measure_image_cost.py --w 432 --h 768
  python cf_measure_image_cost.py --w 432 --h 768 --steps 20

Needs Account Analytics: Read on the API token (same as cf_neuron_report.py).
"""

import argparse
import base64
import datetime
import json
import sys
import time
from pathlib import Path

import requests

DAILY_FREE = 10_000
_USAGE_Q = """
query($accountTag: String!, $day: Date!) {
  viewer { accounts(filter: {accountTag: $accountTag}) {
    aiInferenceAdaptiveGroups(limit: 100, filter: {date: $day}) {
      count sum { totalNeurons } dimensions { modelId }
    } } }
}
"""


def _creds():
    import os
    for base in (Path.home(), Path(os.environ.get("SECRETS_DIR", ".")), Path("..")):
        a, t = base / "cf_account_id.txt", base / "cf_api_token.txt"
        if a.exists() and t.exists():
            return a.read_text().strip(), t.read_text().strip()
    sys.exit("Cloudflare credentials not found")


def usage_today(acct, token, model):
    day = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    r = requests.post("https://api.cloudflare.com/client/v4/graphql",
                      headers={"Authorization": f"Bearer {token}",
                               "Content-Type": "application/json"},
                      json={"query": _USAGE_Q,
                            "variables": {"accountTag": acct, "day": day}}, timeout=60)
    d = r.json()
    if d.get("errors"):
        sys.exit("Analytics not permitted — add Account Analytics: Read to the token.\n"
                 + json.dumps(d["errors"])[:200])
    groups = d["data"]["viewer"]["accounts"][0]["aiInferenceAdaptiveGroups"]
    total = sum(g["sum"]["totalNeurons"] for g in groups)
    mine  = sum(g["sum"]["totalNeurons"] for g in groups
                if g["dimensions"]["modelId"] == model)
    return total, mine


def main():
    import cf_image_hook as hook

    ap = argparse.ArgumentParser()
    ap.add_argument("--w", type=int, default=hook._IMG_W)
    ap.add_argument("--h", type=int, default=hook._IMG_H)
    ap.add_argument("--steps", type=int, default=None,
                    help="pass num_steps (unverified for Lucid Origin — may be rejected)")
    ap.add_argument("--model", default=hook._IMAGE_MODEL)
    ap.add_argument("--out", default="flux_test_output/COST_test.png")
    args = ap.parse_args()

    acct, token = _creds()
    tiles = (args.w * args.h) / (512 * 512)
    print(f"model  {args.model}")
    print(f"size   {args.w}x{args.h}  = {tiles:.3f} tiles of 512x512")
    if args.steps:
        print(f"steps  {args.steps}")

    before_total, before_model = usage_today(acct, token, args.model)
    print(f"\nbefore: {before_model:,.0f} n on this model "
          f"({before_total:,.0f} n total today)")

    body = {"prompt":
            "Photorealistic vertical photograph, over-the-shoulder view of hands "
            "gripping a steaming mug on a sunlit kitchen counter, warm morning "
            "light, vertical 9:16, cinematic depth of field, Canon EOS R5",
            "width": args.w, "height": args.h}
    if args.steps:
        body["num_steps"] = args.steps

    t0 = time.time()
    r = requests.post(
        f"https://api.cloudflare.com/client/v4/accounts/{acct}/ai/run/{args.model}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=body, timeout=120)
    dt = time.time() - t0

    if r.status_code == 429:
        sys.exit("429 — daily grant spent, cannot measure right now.")
    if r.status_code != 200:
        sys.exit(f"HTTP {r.status_code}: {r.text[:300]}")

    if "application/json" in r.headers.get("Content-Type", ""):
        img = base64.b64decode((r.json().get("result") or {})["image"])
    else:
        img = r.content
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_bytes(img)
    print(f"generated in {dt:.1f}s -> {args.out} ({len(img):,} bytes)")

    # analytics lag a little behind the call
    print("waiting 45s for analytics to settle...")
    time.sleep(45)
    after_total, after_model = usage_today(acct, token, args.model)
    cost = after_model - before_model

    print(f"after : {after_model:,.0f} n on this model "
          f"({after_total:,.0f} n total today)")
    print(f"\nMEASURED COST: {cost:,.0f} neurons for one {args.w}x{args.h} image")
    if cost > 0:
        print(f"  -> {DAILY_FREE / cost:.1f} images/day on the free grant")
        print(f"  -> 8 reels/day would need {8 * cost:,.0f} n "
              f"({'FITS' if 8 * cost <= DAILY_FREE else 'OVER by ' + format(8 * cost - DAILY_FREE, ',.0f')})")
    else:
        print("  (no delta recorded yet — analytics may still be lagging; re-run "
              "cf_neuron_report.py in a few minutes)")


if __name__ == "__main__":
    main()
