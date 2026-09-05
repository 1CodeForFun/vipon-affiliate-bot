/* ===========================================================================
   PATCH for the deployed `amz` worker  —  TWO EDITS, DO NOT REPLACE THE FILE

   The live worker also carries the TikTok OAuth key/secret, /auth/start,
   /oauth/callback, the TikTok verification file, and the Terms/Privacy pages.
   Replacing it wholesale would break the TikTok integration, so this changes
   only the crawler branch.

   THE BUG (in the deployed worker, with its own comment):

       // Crawlers → 302 to Amazon so the card shows image/title from Amazon
       if (isCrawler) return Response.redirect(dp, 302);

   That assumption never held: Amazon product pages serve NO Open Graph tags.
   Facebook was INFERRING a preview image from Amazon's page markup, and that
   inference broke when Amazon changed the markup — which is why link posts
   started showing a grey placeholder on some products but not others, with no
   pattern. Verified independently: our own HTTP image scraper broke at the
   same time, returning 0 images for 5 of 6 sampled ASINs.

   Tested locally against a faithful copy of the deployed worker: all six
   existing routes byte-identical, humans (desktop/iOS/Android/in-app)
   unchanged, and links WITHOUT an img param still 302 exactly as today — so
   already-published links behave as they do now.
   =========================================================================== */


/* ── EDIT 1 ────────────────────────────────────────────────────────────────
   Pinterest is missing from isCrawler, so Pinterest never gets a card of its
   own. Replace the existing `const isCrawler = ...` block with this one.     */

    const isCrawler =
      L.includes("facebookexternalhit") || L.includes("facebot") ||
      L.includes("facebookcatalog") || L.includes("meta-externalagent") ||
      L.includes("twitterbot") || L.includes("slackbot") ||
      L.includes("discordbot") || L.includes("linkedinbot") ||
      L.includes("whatsapp") || L.includes("pinterest") ||
      L.includes("telegrambot") || L.includes("redditbot") ||
      L.includes("embedly") || L.includes("skypeuripreview");


/* ── EDIT 2 ────────────────────────────────────────────────────────────────
   Replace these two lines:

       // Crawlers → 302 to Amazon so the card shows image/title from Amazon
       if (isCrawler) return Response.redirect(dp, 302);

   with the block below. Note the `if (!img) return Response.redirect(dp, 302)`
   guard: links already published carry no img param and keep their current
   behaviour exactly, so this cannot make anything worse than it is today.    */

    if (isCrawler) {
      const img = (q.get("img") || "").trim();
      const ttl = (q.get("t")   || "").trim() || "Today's Amazon deal";

      // No image supplied (every link published before this change) → behave
      // exactly as before rather than serving a card with no picture.
      if (!img) return Response.redirect(dp, 302);

      const esc = (s) => (s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;")
                                  .replace(/>/g, "&gt;").replace(/"/g, "&quot;");

      const card = `<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>${esc(ttl)}</title>
<link rel="canonical" href="${esc(dp)}">
<meta property="og:type" content="product">
<meta property="og:title" content="${esc(ttl)}">
<meta property="og:description" content="Limited-time Amazon deal. Tap to see the current price.">
<meta property="og:url" content="${esc(dp)}">
<meta property="og:image" content="${esc(img)}">
<meta property="og:image:secure_url" content="${esc(img)}">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="${esc(ttl)}">
<meta name="twitter:image" content="${esc(img)}">
<meta http-equiv="refresh" content="0;url=${esc(dp)}">
</head><body><a href="${esc(dp)}">${esc(ttl)}</a></body></html>`;

      return new Response(card, {
        status: 200,
        headers: {
          "content-type": "text/html; charset=utf-8",
          "cache-control": "public, max-age=3600",
        },
      });
    }


/* ── VERIFY AFTER DEPLOY ───────────────────────────────────────────────────

   New behaviour (expect 1):
     curl -A "facebookexternalhit/1.1" \
       "https://amz.ifreshdeals.workers.dev/?asin=B07FZ8S74R&tag=t-20&tld=com&img=https%3A%2F%2Fexample.com%2Fx.jpg&t=Test" \
       | grep -c og:image

   Nothing else changed (each must behave as before):
     curl -sI "https://amz.ifreshdeals.workers.dev/?asin=B07FZ8S74R&tag=t-20&tld=com"   # 302
     curl -s  "https://amz.ifreshdeals.workers.dev/terms"   | head -2
     curl -s  "https://amz.ifreshdeals.workers.dev/privacy" | head -2
     curl -s  "https://amz.ifreshdeals.workers.dev/tiktokEUViA1mjO0sRuVVGBp11d7eJV7fEj3AF.txt"
     curl -sI "https://amz.ifreshdeals.workers.dev/auth/start"                          # 302 tiktok
*/
