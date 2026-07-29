#!/usr/bin/env python3
"""
creators_api.py — Amazon Creators API client (replaces the retired PA-API v5).

PA-API v5 was deprecated 2026-04-30 and retired 2026-05-15, so the old
paapi5_python_sdk path is permanently dead. This is the supported replacement.

Differences from PA-API v5:
  - OAuth2 client_credentials, NOT AWS SigV4
  - Single global host `creatorsapi.amazon`; region comes from the x-marketplace header
  - Path and every field are lowerCamelCase (`getItems`, `itemIds`, `images.primary.large`)

Credentials come from FDP-credentials.csv, downloaded from Associates Central ->
Tools -> Creators API. Columns: Application, Application Id, Credential Id,
Secret, Version. The partner tag is the part of "Application Id" before the dot
(e.g. "freshdeal00cc-20.fdp" -> "freshdeal00cc-20").

ELIGIBILITY: the Creators API requires 10 qualified Associates referral sales in
the trailing 30 days — stricter than PA-API's 3-per-180-days. Until that is met,
every call returns 403 AssociateNotEligible. That is NOT a code fault: callers
fall back to the Selenium page scrape, which supplies the same gallery images.
"""

import base64
import csv
import os
import time
from pathlib import Path

import requests

_CRED_FILE   = "FDP-credentials.csv"
_API_HOST    = "https://creatorsapi.amazon"
_TOKEN_URL_V3 = "https://api.amazon.com/auth/o2/token"
_TOKEN_URL_V2 = "https://creatorsapi.auth.us-east-1.amazoncognito.com/oauth2/token"

# Partner tag per marketplace (the CSV only carries the US one).
_TAG_BY_TLD = {"com": "freshdeal00cc-20", "ca": "fdcanada00-20"}

_token_cache   = {"token": None, "expires_at": 0.0, "version": ""}
_INELIGIBLE    = False    # set once per process so we stop retrying a known 403


def log(m):
    print(m, flush=True)


# ── Credentials ───────────────────────────────────────────────────────────────

def _load_creds():
    """Return (credential_id, secret, version, default_tag) or (None,)*4."""
    secrets_dir = os.environ.get("SECRETS_DIR", ".")
    for base in (Path.home(), Path(secrets_dir), Path(__file__).resolve().parent):
        p = base / _CRED_FILE
        if not p.exists():
            continue
        try:
            row = next(csv.DictReader(open(p, encoding="utf-8-sig")))
        except Exception:
            continue
        cid = (row.get("Credential Id") or "").strip()
        sec = (row.get("Secret") or "").strip()
        ver = (row.get("Version") or "3.1").strip()
        appid = (row.get("Application Id") or "").strip()
        tag = appid.split(".")[0] if appid else ""
        if cid and sec:
            return cid, sec, ver, tag
    return None, None, None, None


# ── OAuth ─────────────────────────────────────────────────────────────────────

def _get_token():
    """Cached bearer token. Returns (token, version) or (None, None)."""
    if _token_cache["token"] and time.time() < _token_cache["expires_at"]:
        return _token_cache["token"], _token_cache["version"]

    cid, sec, ver, _ = _load_creds()
    if not cid:
        return None, None

    major = int(ver.split(".")[0]) if ver and ver[0].isdigit() else 3
    if major >= 3:
        url, scope = _TOKEN_URL_V3, "creatorsapi::default"
    else:
        url, scope = _TOKEN_URL_V2, "creatorsapi/default"

    basic = base64.b64encode(f"{cid}:{sec}".encode()).decode()
    try:
        r = requests.post(
            url,
            headers={"Authorization": f"Basic {basic}",
                     "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "client_credentials", "scope": scope},
            timeout=30,
        )
        if r.status_code != 200:
            log(f"  Creators API token failed ({r.status_code}): {r.text[:160]}")
            return None, None
        j = r.json()
        _token_cache["token"]      = j["access_token"]
        # refresh a minute early
        _token_cache["expires_at"] = time.time() + int(j.get("expires_in", 3600)) - 60
        _token_cache["version"]    = ver
        return _token_cache["token"], ver
    except Exception as e:
        log(f"  Creators API token error: {e}")
        return None, None


# ── getItems ──────────────────────────────────────────────────────────────────

def _get_items(asin, tld, resources):
    """POST /catalog/v1/getItems. Returns the item dict, or None."""
    global _INELIGIBLE
    if _INELIGIBLE:
        return None

    token, ver = _get_token()
    if not token:
        return None

    _, _, _, csv_tag = _load_creds()
    tag = _TAG_BY_TLD.get(tld) or csv_tag
    marketplace = f"www.amazon.{tld}"
    major = int(ver.split(".")[0]) if ver and ver[0].isdigit() else 3
    auth = f"Bearer {token}" if major >= 3 else f"Bearer {token}, Version {ver}"

    body = {
        "itemIds":     [asin],
        "itemIdType":  "ASIN",
        "partnerTag":  tag,
        "partnerType": "Associates",
        "marketplace": marketplace,
        "resources":   resources,
    }
    try:
        r = requests.post(
            f"{_API_HOST}/catalog/v1/getItems",
            headers={"Authorization": auth, "x-marketplace": marketplace,
                     "Content-Type": "application/json"},
            json=body, timeout=45,
        )
    except Exception as e:
        log(f"  Creators API request error: {e}")
        return None

    if r.status_code == 403:
        # Expected until the account clears 10 qualified sales in 30 days.
        # Latch it so the rest of the run goes straight to the Selenium fallback.
        _INELIGIBLE = True
        log("  Creators API: account not yet eligible (needs 10 qualified sales "
            "in 30 days) — using Selenium images")
        return None
    if r.status_code != 200:
        log(f"  Creators API {r.status_code}: {r.text[:200]}")
        return None

    try:
        items = (r.json().get("itemsResult") or {}).get("items") or []
        return items[0] if items else None
    except Exception as e:
        log(f"  Creators API parse error: {e}")
        return None


# ── Public API (mirrors the old PA-API helpers) ───────────────────────────────

def get_images(asin, tld="com", max_imgs=6):
    """Gallery image URLs for an ASIN. Returns [] on any failure."""
    item = _get_items(asin, tld, ["images.primary.large", "images.variants.large"])
    if not item:
        return []
    images = item.get("images") or {}
    imgs = []

    primary = ((images.get("primary") or {}).get("large") or {}).get("url")
    if primary:
        imgs.append(primary)

    for v in (images.get("variants") or []):
        u = ((v or {}).get("large") or {}).get("url")
        if u and u not in imgs:
            imgs.append(u)
        if len(imgs) >= max_imgs:
            break

    if imgs:
        log(f"  Creators API images: {len(imgs)}")
    return imgs[:max_imgs]


def get_product_info(asin, tld="com"):
    """Clean title + feature bullets. Returns {} on any failure."""
    item = _get_items(asin, tld, ["itemInfo.title", "itemInfo.features"])
    if not item:
        return {}
    info = item.get("itemInfo") or {}
    return {
        "title":    ((info.get("title") or {}).get("displayValue") or "").strip(),
        "features": ((info.get("features") or {}).get("displayValues") or []),
    }


if __name__ == "__main__":
    import sys
    asin = sys.argv[1] if len(sys.argv) > 1 else "B0GS91XQCK"
    cid, _, ver, tag = _load_creds()
    if not cid:
        raise SystemExit(f"No {_CRED_FILE} found (home dir, SECRETS_DIR, or repo dir)")
    log(f"credentials v{ver}, partnerTag={tag}")
    log(f"images  -> {get_images(asin)}")
    log(f"info    -> {get_product_info(asin)}")
