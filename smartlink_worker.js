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

  const upstream = await fetch(target.toString(), {
    headers: {
      // Amazon is friendlier to a browser-shaped request than to a bare fetch.
      "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
                    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
      "Accept": "image/avif,image/webp,image/jpeg,image/png,*/*;q=0.8",
      "Referer": "https://www.amazon.com/",
    },
    cf: { cacheEverything: true, cacheTtl: 604800 },   // 7 days at the edge
  });

  if (!upstream.ok) {
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

    const dp = `https://www.amazon.${tld}/dp/${asin}?tag=${encodeURIComponent(tag)}`;

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

    // In-app on iOS: App Links let Facebook/Instagram offer "Open in Amazon".
    if (isInApp && isIOS) {
      const html = `<!doctype html><html><head>
<meta charset="utf-8"><title>Opening in Amazon…</title>
<meta property="al:ios:url" content="${esc(dp)}">
<meta property="al:ios:app_store_id" content="${AMAZON_IOS_APP_ID}">
<meta property="al:ios:app_name" content="Amazon Shopping">
<meta property="al:web:url" content="${esc(dp)}">
<meta property="al:web:should_fallback" content="true">
<meta name="apple-itunes-app" content="app-id=${AMAZON_IOS_APP_ID}">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>${PAGE_CSS}</style>
</head><body>
<p>Opening in Amazon…</p>
<p>If nothing happens, Facebook should show <b>Open in Amazon</b> at the top.</p>
<p><a class="btn" href="${esc(dp)}">Continue on the web</a></p>
</body></html>`;
      return new Response(html, { headers: NO_STORE });
    }

    if (isIOS) return Response.redirect(dp, 302);

    if (isAndroid) {
      // Correct intent:// syntax. The previous version had markdown link
      // syntax baked into this string, so it never parsed and every Android
      // visitor silently fell through to the web fallback.
      const androidIntent =
        `intent://www.amazon.${tld}/dp/${asin}?tag=${encodeURIComponent(tag)}` +
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
