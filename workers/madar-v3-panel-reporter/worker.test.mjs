import {test} from 'node:test';import assert from 'node:assert/strict';import worker from './worker.mjs';
const env={REPORTER_TOKEN:'fixture-'.repeat(8),PANEL:{fetch:async()=>new Response('{}',{status:202})}};
test('reporter has no control or UI routes',async()=>{for(const route of ['/','/api/action','/api/command','/api/snapshot'])assert.equal((await worker.fetch(new Request('https://reporter.workers.dev'+route),env)).status,404);});
test('reject missing reporter credential',async()=>assert.equal((await worker.fetch(new Request('https://reporter.workers.dev/api/heartbeat',{method:'POST',body:'{}'}),env)).status,403));
