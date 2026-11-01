import {test} from 'node:test';import assert from 'node:assert/strict';import worker from './worker.mjs';
const access=email=>({access:{aud:'panel',getIdentity:async()=>({email})}});
const env={ACCESS_AUD:'panel',PANEL_ORIGIN_TOKEN:'fixture'.repeat(8),PANEL:{fetch:async req=>new Response(req.headers.get('Authorization')?'ok':'bad')}};
test('deny forged email and absent authenticated context',async()=>{assert.equal((await worker.fetch(new Request('https://panel.workers.dev/',{headers:{'Cf-Access-Authenticated-User-Email':'khalid.sharahili.sa@gmail.com'}}),env,{})).status,403);});
test('deny another identity',async()=>assert.equal((await worker.fetch(new Request('https://panel.workers.dev/'),env,access('foreign@example.com'))).status,403));
test('allow exact owner and pin origin port',async()=>{let target;const local={...env,PANEL:{fetch:async req=>{target=req.url;return new Response('ok')}}};assert.equal((await worker.fetch(new Request('https://panel.workers.dev/'),local,access('khalid.sharahili.sa@gmail.com'))).status,200);assert.equal(target,'http://127.0.0.1:8876/');});
test('deny cross-origin command',async()=>assert.equal((await worker.fetch(new Request('https://panel.workers.dev/api/command',{method:'POST',headers:{Origin:'https://evil.example','Content-Type':'application/json'},body:'{}'}),env,access('khalid.sharahili.sa@gmail.com'))).status,403));
