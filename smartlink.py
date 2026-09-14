#!/usr/bin/env python3
"""
smartlink.py — the ONE place affiliate links are built.

STDLIB ONLY, on purpose. This is imported by buffer_publish.py, which runs in a
workflow installing only gspread/oauth2client/requests; pulling it from vipon26
would drag in selenium, undetected-chromedriver and Pillow and break that job.

WHY EVERY LINK MUST GO THROUGH THE WORKER
-----------------------------------------
A bare https://www.amazon.com/dp/ASIN?tag=... opens the Amazon *website* in
whatever browser the tap happened in. It does NOT open the Amazon app:

  - iOS fires Universal Links only for links TAPPED in an app that supports
    them. A URL pasted into Safari's address bar — which is exactly what happens
    when a viewer copies a link out of a YouTube or TikTok description — never
    triggers one, by Apple's design.
  - Once a user has picked "open in browser" for a domain, iOS disables the
    Universal Link for that domain until they go and re-enable it.

The worker serves an interstitial that hands off to the Amazon app by its own
URL scheme (com.amazon.mobile.shopping.web://) with a timed fallback to the web,
so a pasted link reaches the app. It ALSO answers crawlers with real og: tags,
which is what makes Facebook link previews render at all — Amazon product pages
carry no og: tags whatsoever.

So: a raw /dp/ link loses both the app hand-off and the preview card. Anywhere a
link is published, build it here.
"""

import os
import re
import urllib.parse

WORKER_BASE = (os.getenv("WORKER_BASE") or "https://amz.ifreshdeals.workers.dev")


def dp_link(asin: str, tag: str, tld: str = "com") -> str:
    """The plain Amazon URL. Only used as the fallback when no worker is set."""
    if not asin:
        return ""
    return f"https://www.amazon.{tld}/dp/{asin.upper()}?tag={urllib.parse.quote(tag)}"


def smartlink(asin: str, tag: str, tld: str = "com", image: str = "",
              title: str = "", cart: bool = False, badge: str = "",
              pct: int = 0) -> str:
    """Affiliate smartlink through the worker.

    image/title are optional and only feed the link-preview card: passing them
    lets the worker answer crawlers with real og: tags while humans still get
    the instant hand-off. Harmless where a preview is never rendered.

    badge stamps the preview card (see BADGES in smartlink_worker.js); the
    worker only honours known keys, so an unrecognised value degrades to the
    plain card rather than failing.
    """
    if not asin:
        return ""
    asin = asin.upper()
    if not WORKER_BASE:
        return dp_link(asin, tag, tld)

    # Amazon renditions are encoded in the filename: _SL500_ is a 500px box.
    # Facebook needs at least 600x315 for the large preview card and shows a
    # small square thumbnail below that, so ask for the 1500px rendition.
    if image:
        image = re.sub(r"\._(?:AC_)?(?:S[LXY]|UX|UY|CR)[\d,]*_", "._AC_SL1500_", image)

    params = {"asin": asin, "tag": tag, "tld": tld}
    if cart:
        params["cart"] = "1"
    if image:
        params["img"] = image
    if title:
        params["t"] = title[:110]
    if badge:
        params["badge"] = badge
        if pct:
            params["pct"] = str(int(pct))
    return f"{WORKER_BASE}/a?{urllib.parse.urlencode(params)}"
