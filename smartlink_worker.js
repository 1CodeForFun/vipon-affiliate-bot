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
