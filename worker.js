export default {
  async fetch(request, env) {
    const API_KEY = env.API_KEY;
    const url     = new URL(request.url);
    const auth    = request.headers.get("Authorization");

    if (auth !== `Bearer ${API_KEY}`) {
      return json({ error: "Unauthorized" }, 401);
    }
    if (request.method !== "POST" || url.pathname !== "/") {
      return json({ error: "Not allowed" }, 405);
    }

    try {
      const { prompt, image } = await request.json();
      if (!prompt) return json({ error: "Prompt is required" }, 400);

      let result;
      if (image) {
        // img2img: product reference image supplied as a base64 string.
        // strength 0.65 = keep composition from reference, change style/context.
        const bytes = Uint8Array.from(atob(image), c => c.charCodeAt(0));
        result = await env.AI.run(
          "@cf/runwayml/stable-diffusion-v1-5-img2img",
          { prompt, image: [...bytes], strength: 0.65, num_steps: 20 }
        );
      } else {
        // Text-to-image: Stable Diffusion XL.
        result = await env.AI.run(
          "@cf/stabilityai/stable-diffusion-xl-base-1.0",
          { prompt, num_steps: 20 }
        );
      }

      return new Response(result, {
        headers: { "Content-Type": "image/png" },
      });
    } catch (err) {
      return json({ error: "Image generation failed", details: err.message }, 500);
    }
  },
};

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
