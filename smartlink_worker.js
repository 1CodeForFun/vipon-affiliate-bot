/**
 * smartlink_worker.js — replacement for the amz.ifreshdeals.workers.dev handler.
 *
 * WHY THIS EXISTS
 * Facebook link posts were showing a grey placeholder instead of the product
 * image, randomly and with no pattern. Cause, verified against the live pages:
 *
 *   - Amazon product pages serve ZERO Open Graph tags. No og:image, no og:title,
 *     no <link rel="image_src"> — to the Facebook crawler AND to a normal
 *     browser alike.
 *   - The worker answered facebookexternalhit with a bare 302 to Amazon, so
 *     Facebook scraped Amazon, found no metadata, and fell back to guessing an
 *     image from the markup. Sometimes it guesses; sometimes it shows grey.
 *     Hence "random with no pattern".
 *
 * FIX
 * Humans keep getting the instant 302 to Amazon, exactly as before. CRAWLERS get
 * a tiny HTML page carrying real og: tags — including og:image pointing at the
 * product image we already know — so the preview card is filled in
 * deterministically instead of being guessed.
 *
 * The affiliate tag is untouched: the crawler page still redirects (meta refresh
 * + canonical) to the tagged Amazon URL, and humans never see this page.
 *
 * PARAMS (unchanged, plus one optional):
 *   asin  required   ASIN to redirect to
 *   tag   optional   Amazon Associates tag
 *   tld   optional   com | ca   (default com)
 *   img   optional   product image URL, used as og:image
 *   t     optional   product title, used as og:title
 *
 * DEPLOY: wrangler deploy (or paste into the Cloudflare dashboard editor for
 * the existing amz worker). Nothing else in the pipeline changes — vipon26 just
 * needs to append &img=/&t= to the smartlink, which is a one-line change there.
 */

const PAGE_ROOT = `<!doctype html><meta charset="utf-8">
<title>FreshDeals US</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  body{font:16px system-ui,Segoe UI,Roboto,Arial,sans-serif;max-width:860px;margin:40px auto;padding:0 16px}
  a.btn{display:inline-block;margin:8px 8px 0 0;padding:10px 14px;border:1px solid #ccc;border-radius:10px;text-decoration:none}
  code{background:#f6f6f6;padding:2px 6px;border-radius:6px}
</style>
<h1>FreshDeals US</h1>
<p>Deep links & deal videos. As an Amazon Associate we earn from qualifying purchases.</p>
<p><a class="btn" href="https://amz.ifreshdeals.workers.dev/terms">Terms of Service</a>
   <a class="btn" href="https://amz.ifreshdeals.workers.dev/privacy">Privacy Policy</a></p>
<hr>
<h2>Link format</h2>
<p>Build a deep link with query params:<br>
<code>https://amz.ifreshdeals.workers.dev/?asin=ASIN&amp;tag=YOUR_TAG&amp;tld=com</code></p>
<p>Example:<br>
<code>https://amz.ifreshdeals.workers.dev/?asin=B0CJLT8RGJ&amp;tag=freshdeal00cc-20&amp;tld=com</code></p>`;
const PAGE_TERMS = `<!doctype html><meta charset="utf-8">
<title>Terms of Service | FreshDeals US</title>
<style>body{font:16px/1.6 system-ui,Segoe UI,Roboto,Arial,sans-serif;max-width:820px;margin:32px auto;padding:0 16px}
h1{font-size:28px;margin:.2em 0 .6em}h2{font-size:20px;margin:1.2em 0 .4em}small{color:#666}</style>
<h1>Terms of Service</h1><small>Last updated: 2025-10-19</small>
<p>Welcome to FreshDeals US. By using our links and content, you agree to these Terms.</p>
<h2>Service</h2><p>FreshDeals US curates product deals and videos. We do not sell products;
purchases and support are handled by third-party merchants (e.g., Amazon).</p>
<h2>Affiliate Disclosure</h2><p><strong>As an Amazon Associate we earn from qualifying purchases.</strong></p>
<h2>No Guarantee</h2><p>Deals/coupons may change or expire at any time.</p>
<h2>Acceptable Use</h2><p>No scraping, abuse, or illegal activity.</p>
<h2>IP</h2><p>Content is protected; third-party marks belong to their owners.</p>
<h2>Disclaimer & Liability</h2><p>Provided “as is”; limited liability to the extent permitted by law.</p>
<h2>Changes</h2><p>We may update these Terms.</p>
<h2>Contact</h2><p><a href="mailto:ifreshdeals@gmail.com">ifreshdeals@gmail.com</a></p>`;
const PAGE_PRIVACY = `<!doctype html><meta charset="utf-8">
<title>Privacy Policy | FreshDeals US</title>
<style>body{font:16px/1.6 system-ui,Segoe UI,Roboto,Arial,sans-serif;max-width:820px;margin:32px auto;padding:0 16px}
h1{font-size:28px;margin:.2em 0 .6em}h2{font-size:20px;margin:1.2em 0 .4em}small{color:#666}</style>
<h1>Privacy Policy</h1><small>Last updated: 2025-10-19</small>
<p>This policy explains how FreshDeals US handles information when you visit our pages or links.</p>
<h2>What We Collect</h2><ul>
<li>Basic usage logs (via Cloudflare) for security/performance.</li>
<li>Referral parameters (e.g., Amazon tag/coupon) for attribution.</li>
<li>Platform metadata governed by TikTok/Meta/Amazon policies.</li>
</ul>
<h2>What We Don’t Collect</h2><p>No account signup; no sensitive personal data knowingly collected.</p>
<h2>Cookies/Tracking</h2><p>We don’t set our own tracking cookies; third parties may set theirs.</p>
<h2>Use/Sharing</h2><p>Operate deep links, measure engagement, comply with legal/affiliate reporting.
No sale of personal data.</p>
<h2>Retention</h2><p>Edge logs retained by hosting provider for a limited period.</p>
<h2>Your Choices</h2><p>Use browser/platform privacy settings.</p>
<h2>Children</h2><p>General-audience content; not directed to children.</p>
<h2>Changes</h2><p>We may update this policy.</p>
<h2>Contact</h2><p><a href="mailto:ifreshdeals@gmail.com">ifreshdeals@gmail.com</a></p>`;

// ── Static pages, preserved verbatim from the previously deployed worker ──
// Amazon Associates and the social platforms verify the domain against these,
// so replacing the worker without them would break compliance, not just links.
const STATIC = { "/": PAGE_ROOT, "/terms": PAGE_TERMS, "/privacy": PAGE_PRIVACY };

const CRAWLERS = [
  "facebookexternalhit", "facebookcatalog", "meta-externalagent",
  "twitterbot", "pinterest", "linkedinbot", "slackbot", "whatsapp",
  "telegrambot", "discordbot", "embedly", "quora link preview",
  "redditbot", "applebot", "bingbot", "googlebot", "skypeuripreview",
];

function isCrawler(ua) {
  const s = (ua || "").toLowerCase();
  return CRAWLERS.some((c) => s.includes(c));
}

const esc = (s) =>
  (s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;")
           .replace(/>/g, "&gt;").replace(/"/g, "&quot;");

export default {
  async fetch(request) {
    const url  = new URL(request.url);

    const page = STATIC[url.pathname.replace(/\/+$/, "") || "/"];
    if (page !== undefined) {
      return new Response(page, {
        status: 200,
        headers: { "Content-Type": "text/html; charset=utf-8" },
      });
    }

    const asin = (url.searchParams.get("asin") || "").toUpperCase();

    if (!/^[A-Z0-9]{10}$/.test(asin)) {
      return new Response("Bad or missing asin", { status: 400 });
    }

    const tld = (url.searchParams.get("tld") || "com").toLowerCase() === "ca" ? "ca" : "com";
    const tag = url.searchParams.get("tag") || "";
    const img = url.searchParams.get("img") || "";
    const ttl = url.searchParams.get("t")   || "Today's Amazon deal";

    const target = `https://www.amazon.${tld}/dp/${asin}` + (tag ? `?tag=${encodeURIComponent(tag)}` : "");

    // Humans: straight through, unchanged behaviour.
    if (!isCrawler(request.headers.get("User-Agent"))) {
      return Response.redirect(target, 302);
    }

    // Crawlers: a real preview card. og:image is the whole point — without it
    // Facebook renders the grey box.
    const html = `<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>${esc(ttl)}</title>
<link rel="canonical" href="${esc(target)}">
<meta property="og:type" content="product">
<meta property="og:title" content="${esc(ttl)}">
<meta property="og:description" content="Limited-time Amazon deal. Tap to see the current price.">
<meta property="og:url" content="${esc(target)}">
${img ? `<meta property="og:image" content="${esc(img)}">
<meta property="og:image:width" content="1000">
<meta property="og:image:height" content="1000">
<meta name="twitter:image" content="${esc(img)}">` : ""}
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="${esc(ttl)}">
<meta http-equiv="refresh" content="0;url=${esc(target)}">
</head><body><a href="${esc(target)}">${esc(ttl)}</a></body></html>`;

    return new Response(html, {
      status: 200,
      headers: {
        "Content-Type": "text/html; charset=utf-8",
        // Let Facebook cache the card, but not so long that a changed image sticks.
        "Cache-Control": "public, max-age=3600",
      },
    });
  },
};
