#!/usr/bin/env python3
"""
deploy_worker.py — push smartlink_worker.js to the Cloudflare `amz` worker.

Replaces the copy-paste-into-the-dashboard step. Uploads the script via the
Cloudflare API, then verifies the live worker actually changed.

CREDENTIALS (both live OUTSIDE the repo so they can never be committed):
  C:\\Users\\ehaba\\cf_deploy_token.txt    API token with Workers Scripts: Edit
  C:\\Users\\ehaba\\cf_amz_account_id.txt  account ID that owns the `amz` worker

Neither is printed by this script; only lengths and outcomes are logged.

USAGE
  python deploy_worker.py --check     # what is live now, no changes
  python deploy_worker.py --dry-run   # validate token/account/script, no upload
  python deploy_worker.py             # deploy, then verify

WHY THE API AND NOT WRANGLER: wrangler deploy builds its config from a
wrangler.toml, and this repo has none. Writing one from scratch risks
overwriting the worker's routes with whatever the new file happens to declare.
The script-upload endpoint changes the code and nothing else.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
import uuid

SCRIPT_NAME  = os.getenv("CF_WORKER_NAME", "amz")
WORKER_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "smartlink_worker.js")
TOKEN_FILE   = os.path.expanduser(r"~\cf_deploy_token.txt")
ACCOUNT_FILE = os.path.expanduser(r"~\cf_amz_account_id.txt")
LIVE_BASE    = os.getenv("WORKER_BASE", "https://amz.ifreshdeals.workers.dev")
API          = "https://api.cloudflare.com/client/v4"


# The Windows console defaults to cp1252, which cannot encode the box-drawing
# and check characters used below.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def log(m):
    print(m, flush=True)


def _read(path, what):
    if not os.path.exists(path):
        log(f"✗ missing {what}: {path}")
        return None
    val = open(path, encoding="utf-8").read().strip()
    if not val:
        log(f"✗ empty {what}: {path}")
        return None
    log(f"✓ {what} loaded ({len(val)} chars)")
    return val


def _api(path, token, method="GET", body=None, headers=None):
    req = urllib.request.Request(API + path, method=method, data=body)
    req.add_header("Authorization", f"Bearer {token}")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}
    except Exception as e:
        return None, {"errors": [{"message": f"{e.__class__.__name__}: {e}"}]}


def _errors(data):
    return "; ".join(e.get("message", "?") for e in (data or {}).get("errors", [])) or "unknown"


def live_state():
    """What the deployed worker does right now — the only ground truth."""
    url = (f"{LIVE_BASE}/a?asin=B07FZ8S74R&tag=t-20&tld=com"
           "&img=https%3A%2F%2Fm.media-amazon.com%2Fimages%2FI%2F61bArmGzBKL._AC_SL1500_.jpg"
           "&t=Deploy+Check")
    req = urllib.request.Request(url, headers={"User-Agent": "facebookexternalhit/1.1"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode("utf-8", "ignore")
    except Exception as e:
        return {"error": f"{e.__class__.__name__}"}
    import re
    grab = lambda p: (re.search(p, body) or [None, "-"])[1]
    return {
        "og_url":       grab(r'property="og:url" content="([^"]*)"'),
        "og_image":     grab(r'property="og:image" content="([^"]*)"'),
        "meta_refresh": 'http-equiv="refresh"' in body,
        "self_url":     (grab(r'property="og:url" content="([^"]*)"') or "").startswith(LIVE_BASE),
        "self_image":   (grab(r'property="og:image" content="([^"]*)"') or "").startswith(LIVE_BASE),
    }


def report_live(label):
    s = live_state()
    log(f"\n── {label} ──")
    if s.get("error"):
        log(f"   could not reach the worker: {s['error']}")
        return s
    log(f"   og:url        {str(s['og_url'])[:62]}")
    log(f"   og:image      {str(s['og_image'])[:62]}")
    log(f"   meta refresh  {'PRESENT (old version)' if s['meta_refresh'] else 'removed'}")
    log(f"   self-hosted   og:url={s['self_url']}  og:image={s['self_image']}")
    return s


def upload(token, account, source):
    """PUT the script as an ES module. Code only — no bindings are declared, so
    nothing else about the worker is touched."""
    boundary = uuid.uuid4().hex
    metadata = json.dumps({
        "main_module": "worker.js",
        "compatibility_date": "2026-01-01",
    })
    parts = []
    for name, filename, ctype, content in (
        ("metadata", None, "application/json", metadata),
        ("worker.js", "worker.js", "application/javascript+module", source),
    ):
        disp = f'form-data; name="{name}"'
        if filename:
            disp += f'; filename="{filename}"'
        parts.append(f"--{boundary}\r\nContent-Disposition: {disp}\r\n"
                     f"Content-Type: {ctype}\r\n\r\n{content}\r\n")
    body = ("".join(parts) + f"--{boundary}--\r\n").encode("utf-8")

    return _api(f"/accounts/{account}/workers/scripts/{SCRIPT_NAME}",
                token, method="PUT", body=body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="show what is live; change nothing")
    ap.add_argument("--dry-run", action="store_true", help="validate everything but do not upload")
    args = ap.parse_args()

    if args.check:
        report_live("LIVE NOW")
        return 0

    log("▶ Deploying smartlink_worker.js")
    token   = _read(TOKEN_FILE, "API token")
    account = _read(ACCOUNT_FILE, "account ID")
    if not token or not account:
        log("\nCreate them first — see the deployment steps.")
        return 1
    if not os.path.exists(WORKER_FILE):
        log(f"✗ worker source not found: {WORKER_FILE}")
        return 1
    source = open(WORKER_FILE, encoding="utf-8").read()
    log(f"✓ worker source {len(source)} bytes")

    st, data = _api("/user/tokens/verify", token)
    if st != 200:
        log(f"✗ token invalid ({st}): {_errors(data)}")
        return 1
    log("✓ token active")

    st, data = _api(f"/accounts/{account}/workers/scripts", token)
    if st != 200:
        log(f"✗ cannot list workers ({st}): {_errors(data)}")
        log("  The token likely lacks Account → Workers Scripts → Edit,")
        log("  or the account ID belongs to a different account.")
        return 1
    names = [s["id"] for s in data.get("result", [])]
    log(f"✓ workers in this account: {names}")
    if SCRIPT_NAME not in names:
        log(f"✗ '{SCRIPT_NAME}' is not in this account — wrong account ID?")
        return 1

    before = report_live("BEFORE")

    if args.dry_run:
        log("\n✓ dry run: everything checks out, nothing uploaded")
        return 0

    log(f"\n▶ uploading to '{SCRIPT_NAME}'…")
    st, data = upload(token, account, source)
    if st not in (200, 201):
        log(f"✗ deploy failed ({st}): {_errors(data)}")
        return 1
    log("✓ uploaded")

    import time
    time.sleep(4)          # let the edge pick it up
    after = report_live("AFTER")

    ok = after.get("self_url") and after.get("self_image") and not after.get("meta_refresh")
    log("")
    if ok:
        log("✅ deployed and verified — Facebook will now read our own image")
    else:
        log("⚠️ uploaded, but the live worker does not look updated yet.")
        log("   Wait a few seconds and re-run with --check before redeploying.")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
