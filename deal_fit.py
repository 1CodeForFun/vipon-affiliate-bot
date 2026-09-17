#!/usr/bin/env python3
"""
deal_fit.py — rank deals by what the audience actually clicks.

STDLIB ONLY, so every flow can import it regardless of its workflow's deps.

WHY THIS EXISTS
---------------
Associates click data for 9-15 Sep, by category (309 clicks in named categories):

    Home 100 · Cell Phones 47 · Kitchen 40 · Furniture 23 · Clothing 21
    Jewelry 19 · Fire Tablets 13 · Health 12 · Business 12 · Components 11
    Beauty 11

    home+kitchen+furniture  53%     phones+tablets+components  23%
    clothing+jewelry+beauty 16%

The five most-clicked products tell the same story, and add price to it:

    13 clicks  $76.99   Fire HD 10 tablet
    12 clicks  $109.99  Dehumidifier            Home & Kitchen
    12 clicks  (n/a)    8x10 washable area rug  Home & Kitchen
    10 clicks  $23.99   Mattress protector      Home & Kitchen
    10 clicks  $99.00   Moissanite necklace     Jewelry

Four of five are home or a tablet, and the prices cluster at $77-110 — not the
$5-20 the deep-discount pools are full of.

Measured against what we were actually feeding it:

                        clicks   6am deals pool   lightning 50-70%
    home/kitchen/furn     53%         47%               14%
    phone/tablet/tech     23%         26%               10%
    clothing/jewelry      16%         27%               73%

The unfiltered deals page is already well aimed. The lightning band is 73%
clothing against an audience that gives clothing 7% of its clicks — because on
Amazon the deeper the discount filter, the more the pool becomes an apparel bin
(measured at 50%, 80% and 90%: it gets worse each time). Discount depth and
category fit are in tension, and the click data says fit wins.

PREFERENCE, NEVER A FILTER. Nothing is dropped. The sheet still needs its rows
filled every day, so a starved pool would be a worse failure than a badly-aimed
one. This only decides what gets picked FIRST. Set DEAL_FIT=0 to disable.
"""

import os
import re

DEAL_FIT_ON = (os.getenv("DEAL_FIT") or "1") not in ("0", "false", "no")

# Sweet spot from the top-clicked list: $24-110. The band is wider than the
# observations on both sides so a good product is never dropped for being a few
# dollars out.
PRICE_MIN = float(os.getenv("DEAL_PRICE_MIN") or "25")
PRICE_MAX = float(os.getenv("DEAL_PRICE_MAX") or "150")

# Ordered: first match wins, so put the specific before the general.
_CATEGORIES = [
    ("Kitchen", r"\b(kitchen|cookware|frying pan|saucepan|knife|knives|blender|air fryer|"
                r"coffee|espresso|mug|utensil|cutting board|bakeware|frother|dish rack|"
                r"food storage|tumbler|thermos)\b"),
    ("Furniture", r"\b(chair|desk|table|sofa|couch|stool|cabinet|dresser|bed frame|"
                  r"mattress|bookcase|ottoman|nightstand|shelving unit)\b"),
    ("Tech", r"\b(iphone|samsung|galaxy|pixel|case for|screen protector|charger|charging|"
             r"cable|earbud|headphone|bluetooth|usb|adapter|tablet|fire hd|fire tv|laptop|"
             r"monitor|keyboard|mouse|smart ?watch|power bank|router|webcam|ssd|speaker)\b"),
    ("Home", r"\b(rug|curtain|comforter|duvet|bedding|sheet set|pillow|blanket|lamp|"
             r"lighting|storage|organizer|organiser|shelf|hamper|vacuum|mop|cleaner|"
             r"cleaning|towel|shower|bath|decor|candle|humidifi|dehumidifi|air purif|"
             r"fan\b|heater|mattress protector|laundry|trash can|doormat)\b"),
    ("Clothing", r"\b(shirt|t-shirt|tops?|blouse|dress|pajama|pyjama|sleepwear|lingerie|"
                 r"nightgown|hoodie|sweatshirt|sweater|jeans|trousers|pants|shorts|skirt|"
                 r"bra\b|socks?|legging|jacket|coat|boots?|shoes?|sandal|sneaker|slipper|"
                 r"swimsuit|bikini)\b"),
    ("Jewelry", r"\b(necklace|earring|bracelet|ring\b|pendant|jewel|moissanite|"
                r"cubic zirconia|anklet|brooch)\b"),
    ("Beauty", r"\b(serum|skincare|moisturiz|moisturis|cream|lotion|shampoo|conditioner|"
               r"hair (?:dry|straight|curl)|makeup|lipstick|mascara|beard|razor|"
               r"nail polish|perfume|cologne)\b"),
]

# Tier 0 gets picked first, tier 2 last. Uncategorised sits in the middle
# deliberately: "Other" is the single biggest bucket in the click data (270 of
# 579), so an unrecognised title is not evidence of a bad product.
_TIER = {"Home": 0, "Kitchen": 0, "Furniture": 0, "Tech": 0,
         "Other": 1,
         "Clothing": 2, "Jewelry": 2, "Beauty": 2}

_MONEY = re.compile(r"[\d][\d,]*(?:\.\d+)?")


def category_of(title):
    t = (title or "").lower()
    for name, rx in _CATEGORIES:
        if re.search(rx, t):
            return name
    return "Other"


def price_of(deal):
    """Float price from either the scraper's '$9' string or a plain number."""
    v = deal.get("price")
    if isinstance(v, (int, float)):
        return float(v)
    m = _MONEY.search(str(v or ""))
    return float(m.group().replace(",", "")) if m else 0.0


def fit_key(deal):
    """Sort key, lowest first: (category tier, price penalty, -discount).

    Price is a tiebreaker inside a tier rather than a tier of its own — a
    dehumidifier at $18 is still a dehumidifier, and the category signal in the
    data is much stronger than the price signal.
    """
    cat   = category_of(deal.get("title"))
    price = price_of(deal)
    in_band = 0 if (price and PRICE_MIN <= price <= PRICE_MAX) else 1
    return (_TIER.get(cat, 1), in_band, -int(deal.get("pct") or 0))


def rank(deals):
    """Best-fit first. Returns a new list; drops nothing."""
    if not DEAL_FIT_ON:
        return list(deals)
    return sorted(deals, key=fit_key)


def summarise(deals, limit=6):
    """'Home 12 · Tech 8 · Other 5 · Clothing 3' — for a one-line log."""
    counts = {}
    for d in deals:
        c = category_of(d.get("title"))
        counts[c] = counts.get(c, 0) + 1
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:limit]
    return " · ".join(f"{k} {v}" for k, v in top) or "none"
