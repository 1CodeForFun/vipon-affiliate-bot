/**
 * smartlink_worker.js — the complete `amz` worker. Replaces worker.js entirely.
 *
 * WHAT THIS SERVES
 *   /a?asin=…&tag=…&tld=…&img=…&t=…   the smartlink the GitHub workflows build
 *   /?asin=…&tag=…&tld=…              the older link format, still live in
 *                                     posts published before the /a switch
 *   /                                 small public homepage
 *   /terms, /privacy                  affiliate disclosure + privacy policy
 *
 * REMOVED IN THIS VERSION
 *   The TikTok OAuth app (CLIENT_KEY, CLIENT_SECRET, /auth/start,
 *   /oauth/callback) and the TikTok domain-verification file. That was an
 *   early attempt at posting to TikTok directly; TikTok's restrictions made it
 *   unworkable and publishing goes through Buffer instead, so the code and its
 *   embedded credentials are dead weight. Removing them also means this file
 *   holds no secrets and is safe to keep in the public repo.
 *
 * THE BUG THIS FIXES
 *   The old worker sent crawlers to Amazon:
 *       // Crawlers → 302 to Amazon so the card shows image/title from Amazon
 *       if (isCrawler) return Response.redirect(dp, 302);
 *   Amazon product pages serve NO Open Graph tags. Facebook was inferring a
 *   preview image from Amazon's markup, and that inference broke when Amazon
 *   changed the markup — which is why link posts started showing a grey
 *   placeholder on some products and not others. Crawlers now get real og:
 *   tags built from the img/t params the pipeline supplies.
 *
 * ALSO FIXED
 *   - Pinterest was missing from the crawler list, so it never got a card.
 *   - The Android intent:// URL had markdown link syntax embedded in it
 *     (`intent://[text](url)`), so it never parsed and every Android user fell
 *     through to the 800ms web fallback instead of opening the Amazon app.
 */

const BRAND         = "FreshDeals US";
const CONTACT_EMAIL = "ifreshdeals@gmail.com";
const LAST_UPDATED  = "2025-10-19";

// Cloudinary account used only to pad product photos to Facebook's large-card
// shape. The cloud name appears in every delivery URL and is not a secret.
const CLOUDINARY_CLOUD = "diufrf8l7";

const AMAZON_ANDROID_PKG = "com.amazon.mShop.android.shopping";
const AMAZON_IOS_APP_ID  = "297606951";

const HTML_HEADERS = {
  "content-type": "text/html; charset=utf-8",
  "cache-control": "public, max-age=3600",
};
const NO_STORE = {
  "content-type": "text/html; charset=utf-8",
  "cache-control": "no-store",
};

const esc = (s) =>
  (s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;")
           .replace(/>/g, "&gt;").replace(/"/g, "&quot;");

// Social crawlers. Pinterest and meta-externalagent were absent from the old
// list, so those platforms fell through to the Amazon redirect and got no card.
const CRAWLERS = [
  "facebookexternalhit", "facebot", "facebookcatalog", "meta-externalagent",
  "twitterbot", "linkedinbot", "slackbot", "whatsapp", "pinterest",
  "telegrambot", "discordbot", "embedly", "redditbot", "skypeuripreview",
  "quora link preview", "applebot", "bingbot", "googlebot",
];

const PAGE_CSS =
  "body{font:16px/1.6 system-ui,Segoe UI,Roboto,Arial,sans-serif;" +
  "max-width:820px;margin:32px auto;padding:0 16px}" +
  "h1{font-size:28px;margin:.2em 0 .6em}h2{font-size:20px;margin:1.2em 0 .4em}" +
  "small{color:#666}a.btn{display:inline-block;margin:8px 8px 0 0;padding:10px 14px;" +
  "border:1px solid #ccc;border-radius:10px;text-decoration:none}";

function homeHTML(base) {
  return `<!doctype html><meta charset="utf-8">
<title>${BRAND}</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>${PAGE_CSS}</style>
<h1>${BRAND}</h1>
<p>Deep links &amp; deal videos. As an Amazon Associate we earn from qualifying purchases.</p>
<p><a class="btn" href="${base}terms">Terms of Service</a>
   <a class="btn" href="${base}privacy">Privacy Policy</a></p>`;
}

const TERMS_HTML = `<!doctype html><meta charset="utf-8">
<title>Terms of Service | ${BRAND}</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>${PAGE_CSS}</style>
<h1>Terms of Service</h1><small>Last updated: ${LAST_UPDATED}</small>
<p>Welcome to ${BRAND}. By using our links and content, you agree to these Terms.</p>
<h2>Service</h2><p>${BRAND} curates product deals and videos. We do not sell products;
purchases and support are handled by third-party merchants (e.g., Amazon).</p>
<h2>Affiliate Disclosure</h2><p><strong>As an Amazon Associate we earn from qualifying purchases.</strong></p>
<h2>No Guarantee</h2><p>Deals/coupons may change or expire at any time.</p>
<h2>Acceptable Use</h2><p>No scraping, abuse, or illegal activity.</p>
<h2>IP</h2><p>Content is protected; third-party marks belong to their owners.</p>
<h2>Disclaimer &amp; Liability</h2><p>Provided &ldquo;as is&rdquo;; limited liability to the extent permitted by law.</p>
<h2>Changes</h2><p>We may update these Terms.</p>
<h2>Contact</h2><p><a href="mailto:${CONTACT_EMAIL}">${CONTACT_EMAIL}</a></p>`;

const PRIVACY_HTML = `<!doctype html><meta charset="utf-8">
<title>Privacy Policy | ${BRAND}</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>${PAGE_CSS}</style>
<h1>Privacy Policy</h1><small>Last updated: ${LAST_UPDATED}</small>
<p>This policy explains how ${BRAND} handles information when you visit our pages or links.</p>
<h2>What We Collect</h2><ul>
<li>Basic usage logs (via Cloudflare) for security/performance.</li>
<li>Referral parameters (e.g., Amazon tag/coupon) for attribution.</li>
<li>Platform metadata governed by TikTok/Meta/Amazon policies.</li>
</ul>
<h2>What We Don&rsquo;t Collect</h2><p>No account signup; no sensitive personal data knowingly collected.</p>
<h2>Cookies/Tracking</h2><p>We don&rsquo;t set our own tracking cookies; third parties may set theirs.</p>
<h2>Use/Sharing</h2><p>Operate deep links, measure engagement, comply with legal/affiliate reporting.
No sale of personal data.</p>
<h2>Retention</h2><p>Edge logs retained by hosting provider for a limited period.</p>
<h2>Your Choices</h2><p>Use browser/platform privacy settings.</p>
<h2>Children</h2><p>General-audience content; not directed to children.</p>
<h2>Changes</h2><p>We may update this policy.</p>
<h2>Contact</h2><p><a href="mailto:${CONTACT_EMAIL}">${CONTACT_EMAIL}</a></p>`;

// Image hosts we are willing to proxy. This is an allowlist, not a filter:
// without it /img would be an open proxy able to fetch anything, including
// internal addresses.
const IMAGE_HOSTS = [
  "m.media-amazon.com",
  "images-na.ssl-images-amazon.com",
  "images-eu.ssl-images-amazon.com",
  "images-fe.ssl-images-amazon.com",
];

/**
 * /img?u=<encoded Amazon image URL> — fetch the image and re-serve it from
 * this worker.
 *
 * WHY: the Sharing Debugger showed Facebook failing with
 *   "Error while downloading https://m.media-amazon.com/images/I/...jpg
 *    with HTTP response code: 429"
 * Amazon rate-limits Facebook's crawler. Handing Facebook an Amazon CDN URL
 * therefore works sometimes and 429s other times, which is exactly the
 * "some products show, some don't" pattern. Serving the bytes ourselves
 * removes Amazon from Facebook's path entirely: Cloudflare fetches the image
 * once, caches it at the edge, and every crawler is then served from cache.
 */
async function proxyImage(u) {
  const raw = u.searchParams.get("u") || "";
  let target;
  try {
    target = new URL(raw);
  } catch {
    return new Response("Bad image url", { status: 400 });
  }
  if (target.protocol !== "https:" || !IMAGE_HOSTS.includes(target.hostname)) {
    return new Response("Image host not allowed", { status: 403 });
  }

  // Amazon product photos are TALL (the Hanes hoodie is 1026x1500, ratio 0.68).
  // Facebook only renders the wide banner card for images near 1.91:1 and at
  // least 600x315; a portrait image gets the compact layout instead — small
  // thumbnail, text taking the rest. Cloudinary pads the photo onto a
  // 1200x630 white canvas, which is exactly the large-card shape. Verified:
  // 1200x630, ratio 1.90, 25KB.
  const padded = `https://res.cloudinary.com/${CLOUDINARY_CLOUD}/image/fetch/` +
                 `c_pad,w_1200,h_630,b_white,f_auto,q_auto/` +
                 encodeURIComponent(target.toString());

  const fetchHeaders = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
                  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "image/avif,image/webp,image/jpeg,image/png,*/*;q=0.8",
    // Amazon is friendlier to a browser-shaped request than to a bare fetch.
    "Referer": "https://www.amazon.com/",
  };

  // Padded version first; the raw Amazon image is the fallback so a Cloudinary
  // outage degrades to the previous behaviour rather than to no image at all.
  let upstream = null;
  try {
    upstream = await fetch(padded, { cf: { cacheEverything: true, cacheTtl: 604800 } });
    if (!upstream.ok) upstream = null;
  } catch { upstream = null; }

  if (!upstream) {
    try {
      upstream = await fetch(target.toString(), {
        headers: fetchHeaders,
        cf: { cacheEverything: true, cacheTtl: 604800 },
      });
    } catch { upstream = null; }
  }

  if (!upstream || !upstream.ok) {
    // Do not hand Facebook an error page: fall back to the original URL so the
    // behaviour is no worse than pointing og:image straight at Amazon.
    return Response.redirect(target.toString(), 302);
  }

  const headers = new Headers();
  headers.set("content-type", upstream.headers.get("content-type") || "image/jpeg");
  headers.set("cache-control", "public, max-age=604800, immutable");
  return new Response(upstream.body, { status: 200, headers });
}

/** Preview card for social crawlers, built from the params the pipeline sends.
 *
 * `self` MUST be this worker's own URL, not the Amazon link. og:url is how
 * Facebook decides which page the card actually represents: pointing it at
 * Amazon made Facebook treat the Amazon page as the canonical object and
 * re-scrape THAT, where there is no og:image — so the card came back with a
 * title (from Amazon's <title>) and a blank picture, labelled AMAZON.COM.
 * canonical and the meta refresh did the same thing. All three now stay on
 * this page so the image we supply is the one Facebook uses.
 *
 * Humans never see this page; they get the 302 further down.
 */
function crawlerCard(self, dp, img, title) {
  const ttl = title || "Today's Amazon deal";
  return `<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>${esc(ttl)}</title>
<link rel="canonical" href="${esc(self)}">
<meta property="og:type" content="product">
<meta property="og:site_name" content="${BRAND}">
<meta property="og:title" content="${esc(ttl)}">
<meta property="og:description" content="Limited-time Amazon deal. Tap to see the current price.">
<meta property="og:url" content="${esc(self)}">
<meta property="og:image" content="${esc(img)}">
<meta property="og:image:secure_url" content="${esc(img)}">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:image:type" content="image/jpeg">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="${esc(ttl)}">
<meta name="twitter:image" content="${esc(img)}">
</head><body><a href="${esc(dp)}">${esc(ttl)}</a></body></html>`;
}

export default {
  async fetch(request) {
    const u = new URL(request.url);
    const q = u.searchParams;
    const path = u.pathname.replace(/\/+$/, "") || "/";

    if (path === "/img") return proxyImage(u);

    if (path === "/terms")   return new Response(TERMS_HTML,   { headers: HTML_HEADERS });
    if (path === "/privacy") return new Response(PRIVACY_HTML, { headers: HTML_HEADERS });

    // Homepage only when there is no deep link to serve. A bare "/" with an
    // asin is the older link format and must still redirect.
    if (path === "/" && !q.get("asin")) {
      return new Response(homeHTML(`${u.origin}/`), { headers: HTML_HEADERS });
    }

    const asin = (q.get("asin") || "").toUpperCase();
    const tag  = (q.get("tag")  || "").trim();
    const tld  = (q.get("tld")  || "com").toLowerCase() === "ca" ? "ca" : "com";

    if (!/^[A-Z0-9]{10}$/.test(asin) || !tag) {
      return new Response("Missing or invalid asin/tag", { status: 400 });
    }

    // cart=1 sends the shopper to Amazon's add-to-cart confirmation instead of
    // the product page. Set by the pipeline for AMAZON DEALS only, where the
    // discount is already in the price. Vipon coded products deliberately do
    // NOT use it: their price only drops once the code is entered at checkout,
    // so the cart would show the full list price right at the moment of
    // commitment (verified: $24.99 in cart for a product advertised at $12.49).
    //
    // Absent the parameter this is byte-identical to before, so every link
    // already published is unaffected.
    const wantCart = (q.get("cart") || "") === "1";
    const dp = wantCart
      ? `https://www.amazon.${tld}/gp/aws/cart/add.html` +
        `?AssociateTag=${encodeURIComponent(tag)}&ASIN.1=${asin}&Quantity.1=1`
      : `https://www.amazon.${tld}/dp/${asin}?tag=${encodeURIComponent(tag)}`;

    const ua = request.headers.get("user-agent") || "";
    const L  = ua.toLowerCase();

    // Crawlers: serve real og: tags when the link carries an image. Without one
    // — every link published before the pipeline started sending img — fall
    // back to the old redirect so nothing regresses.
    if (CRAWLERS.some((c) => L.includes(c))) {
      const img = (q.get("img") || "").trim();
      if (!img) return Response.redirect(dp, 302);
      // Serve the image through our own /img so Facebook never has to fetch
      // from Amazon, which 429s its crawler.
      const proxied = `${u.origin}/img?u=${encodeURIComponent(img)}`;
      return new Response(
        crawlerCard(u.toString(), dp, proxied, (q.get("t") || "").trim()), {
          status: 200,
          headers: HTML_HEADERS,
        });
    }

    const isAndroid = L.includes("android");
    const isIOS     = /\b(iPhone|iPad|iPod)\b/i.test(ua);
    const isInApp   = L.includes("fban") || L.includes("fbav") ||
                      L.includes("fbios") || L.includes("instagram");

    // ALL iOS visitors get this page — not just the Facebook/Instagram in-app
    // browser, which is what it used to be limited to.
    //
    // WHY: iOS does not fire Universal Links for a URL pasted into Safari's
    // address bar. That is Apple's behaviour, so a viewer who copies the link
    // out of a video description and pastes it always landed on the website —
    // signed out, no saved payment or address — however the redirect was
    // written. Tapping a link INSIDE a page does fire Universal Links, so
    // serving a page with a tappable link is the only way to reach the app
    // from a pasted URL.
    //
    // The redirect is still instant for everyone else; only iOS pays the extra
    // hop, and only because a 302 could never open the app for them.
    if (isIOS) {
      const html = `<!doctype html><html><head>
<meta charset="utf-8"><title>Opening in Amazon…</title>
<meta property="al:ios:url" content="${esc(dp)}">
<meta property="al:ios:app_store_id" content="${AMAZON_IOS_APP_ID}">
<meta property="al:ios:app_name" content="Amazon Shopping">
<meta property="al:web:url" content="${esc(dp)}">
<meta property="al:web:should_fallback" content="true">
<meta name="apple-itunes-app" content="app-id=${AMAZON_IOS_APP_ID}">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>${PAGE_CSS}
.wrap{text-align:center;margin-top:18vh}
a.go{display:inline-block;background:#FFD814;border:1px solid #FCD200;border-radius:999px;
padding:16px 30px;font-size:19px;font-weight:600;color:#0F1111;text-decoration:none}
p.sub{color:#565959;font-size:14px;margin-top:14px}</style>
</head><body>
<div class="wrap">
<p>Your deal is ready.</p>
<p><a class="go" id="go" href="${esc(dp)}">Open in Amazon</a></p>
<p class="sub">Opens the Amazon app if you have it installed.</p>
</div>
<script>
  // A tap is what triggers the Universal Link, so try a synthetic one first and
  // leave the button for anyone it does not work for.
  setTimeout(function(){ try { document.getElementById('go').click(); } catch(e){} }, 350);
</script>
</body></html>`;
      return new Response(html, { headers: NO_STORE });
    }

    if (isAndroid) {
      // Correct intent:// syntax. The previous version had markdown link
      // syntax baked into this string, so it never parsed and every Android
      // visitor silently fell through to the web fallback.
      const androidIntent =
        `intent://${dp.replace(/^https:\/\//, "")}` +
        `#Intent;scheme=https;package=${AMAZON_ANDROID_PKG};` +
        `S.browser_fallback_url=${encodeURIComponent(dp)};end`;

      const html = `<!doctype html><html><head>
<meta charset="utf-8"><title>Opening in Amazon…</title>
<meta property="al:android:url" content="${esc(dp)}">
<meta property="al:android:package" content="${AMAZON_ANDROID_PKG}">
<meta property="al:android:app_name" content="Amazon Shopping">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>${PAGE_CSS}</style>
</head><body>
<p>Opening in Amazon… If nothing happens, <a href="${esc(dp)}">tap here</a>.</p>
<script>
  (function(){
    var dp=${JSON.stringify(dp)};
    var intent=${JSON.stringify(androidIntent)};
    var t=setTimeout(function(){location.href=dp;},800);
    try{ location.href=intent; }catch(e){ location.href=dp; }
    setTimeout(function(){ clearTimeout(t); },1200);
  })();
</script>
</body></html>`;
      return new Response(html, { headers: NO_STORE });
    }

    return Response.redirect(dp, 302);
  },
};
