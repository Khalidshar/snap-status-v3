// A separate, dependency-free worker. No body, credential or signature logging.
const LIMIT = 262144;
const encoder = new TextEncoder();
const response = (status, body) => new Response(body, {
  status, headers: { "content-type": "text/plain; charset=utf-8", "cache-control": "no-store" },
});

async function boundedBody(request) {
  const length = request.headers.get("content-length");
  if (length !== null && (!/^\d+$/.test(length) || Number(length) > LIMIT)) throw new RangeError();
  if (!request.body) return new Uint8Array();
  const reader = request.body.getReader();
  const chunks = [];
  let size = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.length;
    if (size > LIMIT) { await reader.cancel(); throw new RangeError(); }
    chunks.push(value);
  }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
  return bytes;
}

// The Meta verify token is derived from the app secret on both sides, so it is never
// copied by hand: hex(HMAC-SHA256(APP_SECRET, "madar-v3-whatsapp-verify")).
async function verifyToken(secret) {
  const key = await crypto.subtle.importKey("raw", encoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const mac = new Uint8Array(await crypto.subtle.sign("HMAC", key, encoder.encode("madar-v3-whatsapp-verify")));
  return [...mac].map(b => b.toString(16).padStart(2, "0")).join("");
}

function equal(a, b) {
  const left = encoder.encode(a), right = encoder.encode(b);
  if (left.length !== right.length) return false;
  let difference = 0;
  for (let i = 0; i < left.length; i++) difference |= left[i] ^ right[i];
  return difference === 0;
}

export default {
  async fetch(request, env) {
    // Secrets pasted from a terminal may carry a trailing newline or spaces.
    env = { ...env, APP_SECRET: env.APP_SECRET?.trim?.() };
    try {
      const url = new URL(request.url);
      if (!["/", "/webhook"].includes(url.pathname)) return response(404, "NOT_FOUND");
      if (typeof env.APP_SECRET !== "string" || env.APP_SECRET.length < 32 ||
          typeof env.INBOX?.fetch !== "function" ||
          !/^[a-z0-9.-]+$/.test(env.INBOX_HOST || "")) return response(503, "NOT_READY");
      if (request.method === "GET") {
        // Meta may add fields; only the three it documents decide the answer.
        if (url.searchParams.get("hub.mode") !== "subscribe" ||
            !/^\d{1,128}$/.test(url.searchParams.get("hub.challenge") || "") ||
            !equal((url.searchParams.get("hub.verify_token") || "").trim(), await verifyToken(env.APP_SECRET))) {
          return response(403, "VERIFY_DENIED");
        }
        return response(200, url.searchParams.get("hub.challenge"));
      }
      if (request.method !== "POST" || url.search ||
          request.headers.get("content-type")?.split(";", 1)[0].trim() !== "application/json") {
        return response(405, "METHOD_DENIED");
      }
      const signature = /^sha256=([0-9a-f]{64})$/.exec(request.headers.get("x-hub-signature-256") || "");
      if (!signature) return response(403, "SIGNATURE_DENIED");
      const body = await boundedBody(request);
      const key = await crypto.subtle.importKey("raw", encoder.encode(env.APP_SECRET),
        { name: "HMAC", hash: "SHA-256" }, false, ["verify"]);
      const mac = Uint8Array.from(signature[1].match(/../g), x => parseInt(x, 16));
      if (!await crypto.subtle.verify("HMAC", key, mac, body)) return response(403, "SIGNATURE_DENIED");
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 8000);
      try {
        const result = await env.INBOX.fetch(new Request(`http://${env.INBOX_HOST}/whatsapp/webhook`, {
          method: "POST", redirect: "manual", signal: controller.signal,
          headers: { "content-type": "application/json", "x-hub-signature-256": signature[0] }, body,
        }));
        // Do not acknowledge Meta until the existing durable inbox commits.
        return result.status === 200 ? response(200, "OK") : response(503, "INBOX_UNAVAILABLE");
      } finally { clearTimeout(timer); }
    } catch (error) {
      return response(error instanceof RangeError ? 413 : 503, "REQUEST_BLOCKED");
    }
  },
};
