#!/usr/bin/env python3
"""
cf_neuron_report.py — Show actual Cloudflare Workers AI neuron consumption.

Cloudflare grants 10,000 Neurons/day free, shared across ALL Workers AI usage
on the account. Model catalogue prices are published per-tile/per-step, but the
only way to know what a call REALLY cost is to read the account's own analytics.

Usage:
  python cf_neuron_report.py            # last 7 days, per model
  python cf_neuron_report.py --days 30

Requires the API token to have **Account Analytics: Read** in addition to
Workers AI. Without it the GraphQL API returns "not authorized for that
account" and this script explains how to fix it.
"""

import argparse
import datetime
import json
import sys
from pathlib import Path

import requests

DAILY_FREE_NEURONS = 10_000

_QUERY = """
query($accountTag: String!, $start: Date!, $end: Date!) {
  viewer {
    accounts(filter: {accountTag: $accountTag}) {
      aiInferenceAdaptiveGroups(
        limit: 1000,
        filter: {date_geq: $start, date_leq: $end}
        orderBy: [date_DESC]
      ) {
        count
        sum { totalNeurons }
        dimensions { modelId date }
      }
    }
  }
}
"""


def _creds():
    import os
    secrets_dir = os.environ.get("SECRETS_DIR", ".")
    for base in [Path.home(), Path(secrets_dir), Path("..")]:
        a, t = base / "cf_account_id.txt", base / "cf_api_token.txt"
        if a.exists() and t.exists():
            return a.read_text().strip(), t.read_text().strip()
    sys.exit("Cloudflare credentials not found (cf_account_id.txt / cf_api_token.txt)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()

    acct, token = _creds()
    today = datetime.datetime.now(datetime.timezone.utc).date()
    start = (today - datetime.timedelta(days=args.days - 1)).isoformat()

    r = requests.post(
        "https://api.cloudflare.com/client/v4/graphql",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"query": _QUERY,
              "variables": {"accountTag": acct, "start": start, "end": today.isoformat()}},
        timeout=60,
    )
    data = r.json()

    if data.get("errors"):
        msg = json.dumps(data["errors"])
        if "not authorized" in msg:
            print("Analytics not permitted for this API token.\n")
            print("To enable ongoing visibility into neuron spend:")
            print("  1. dash.cloudflare.com -> My Profile -> API Tokens")
            print("  2. Edit the Workers AI token (or create a new one)")
            print("  3. Add permission:  Account  ->  Account Analytics  ->  Read")
            print("  4. Save, then update cf_api_token.txt in the vipon-secrets repo")
            sys.exit(1)
        sys.exit(f"GraphQL error: {msg[:400]}")

    try:
        groups = data["data"]["viewer"]["accounts"][0]["aiInferenceAdaptiveGroups"]
    except (KeyError, IndexError, TypeError):
        sys.exit(f"Unexpected response: {json.dumps(data)[:400]}")

    if not groups:
        print(f"No Workers AI usage recorded in the last {args.days} day(s).")
        return

    # ── Per-day totals, newest first ──────────────────────────────────────────
    by_day = {}
    for g in groups:
        d = g["dimensions"]["date"]
        by_day.setdefault(d, []).append(g)

    print(f"Free allocation: {DAILY_FREE_NEURONS:,} neurons/day "
          f"(shared across ALL Workers AI usage)\n")

    for day in sorted(by_day, reverse=True):
        rows  = sorted(by_day[day], key=lambda x: -x["sum"]["totalNeurons"])
        total = sum(x["sum"]["totalNeurons"] for x in rows)
        pct   = total / DAILY_FREE_NEURONS * 100
        flag  = "  <-- OVER" if total >= DAILY_FREE_NEURONS else ""
        print(f"{day}   {total:>9,.0f} neurons  ({pct:5.1f}% of daily free){flag}")
        for g in rows:
            n, c = g["sum"]["totalNeurons"], g["count"]
            print(f"    {g['dimensions']['modelId'][:52]:52} "
                  f"{c:>4} call(s) {n:>9,.0f} n  ({n/max(c,1):>8,.0f}/call)")
        print()


if __name__ == "__main__":
    main()
