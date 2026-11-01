import assert from "node:assert/strict";
import { test } from "node:test";
import worker from "./worker.mjs";
const appSecret = "fixture-app-secret-".repeat(3);
const verifyToken = "fixture-verify-token-".repeat(3);
const enc = new TextEncoder();
const env = (fn) => ({ APP_SECRET: appSecret, VERIFY_TOKEN: verifyToken, INBOX_HOST: "madar-v3-whatsapp.internal", INBOX: { fetch: fn } });
async function signature(raw) {
  const key = await crypto.subtle.importKey("raw", enc.encode(appSecret), {name:"HMAC", hash:"SHA-256"}, false, ["sign"]);
  return "sha256=" + Buffer.from(await crypto.subtle.sign("HMAC", key, enc.encode(raw))).toString("hex");
}
const post = (raw, sig) => new Request("https://madar-v3-whatsapp.workers.dev/webhook", {method:"POST",headers:{"content-type":"application/json","x-hub-signature-256":sig},body:raw});
test("challenge matches only the private verification token", async () => {
  let calls = 0;
  const config = env(async () => {calls++; return new Response("OK");});
  const query = new URLSearchParams({"hub.mode":"subscribe","hub.verify_token":verifyToken,"hub.challenge":"12345"});
  const result = await worker.fetch(new Request("https://unit.test/?"+query), config);
  assert.equal(result.status,200); assert.equal(await result.text(),"12345"); assert.equal(calls,0);
  query.set("hub.verify_token","wrong");
  assert.equal((await worker.fetch(new Request("https://unit.test/?"+query), config)).status,403);
});
test("bad or missing signature is rejected before forwarding", async () => {
  let calls = 0;
  const config = env(async () => {calls++; return new Response("OK");});
  for (const sig of ["", "sha256="+"0".repeat(64), "sha1=wrong"]) assert.equal((await worker.fetch(post("{}",sig),config)).status,403);
  assert.equal(calls,0);
});
test("valid signed bytes forward unchanged to the dedicated binding only", async () => {
  const raw = '{"object":"whatsapp_business_account","entry":[]}';
  const sig = await signature(raw);
  let calls = 0;
  const config = env(async request => {
    calls++;
    assert.equal(request.url,"http://madar-v3-whatsapp.internal/whatsapp/webhook");
    assert.equal(await request.text(),raw); assert.equal(request.headers.get("x-hub-signature-256"),sig);
    assert.equal(request.headers.get("cookie"),null); return new Response("OK");
  });
  assert.equal((await worker.fetch(post(raw,sig),config)).status,200); assert.equal(calls,1);
  assert.equal((await worker.fetch(post(raw+" ",sig),config)).status,403); assert.equal(calls,1);
});
test("origin failure and absent bindings fail closed", async () => {
  const raw = "{}", sig = await signature(raw);
  for (const status of [302,400,500]) assert.equal((await worker.fetch(post(raw,sig),env(async()=>new Response("",{status})))).status,503);
  assert.equal((await worker.fetch(post(raw,sig),{})).status,503);
});
test("large signed request never reaches the server", async () => {
  const raw = "x".repeat(262145), sig = await signature(raw);
  let calls=0;
  const result=await worker.fetch(post(raw,sig),env(async()=>{calls++;return new Response("OK");}));
  assert.equal(result.status,413);assert.equal(calls,0);
});
