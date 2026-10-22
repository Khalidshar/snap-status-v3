import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {AgentRuntime,ReplayRejected} from './agent-runtime.mjs';

const chunks=[];
let size=0;
for await (const chunk of process.stdin) {
  size+=chunk.length;
  if(size>4096)throw Error('Event too large');
  chunks.push(chunk);
}
const event=JSON.parse(Buffer.concat(chunks).toString('utf8'));
const validId=value=>typeof value==='string'&&/^[A-Za-z0-9_.-]{1,120}$/.test(value);
if(!validId(event.job)||!validId(event.run)||!validId(event.agent))throw Error('Invalid identity');
if(!['RUNNING','SUCCEEDED','FAILED','UNKNOWN'].includes(event.status))throw Error('Invalid state');
const allowed=new Set(['agent','job','run','status','duration_ms','error_code']);
if(Object.keys(event).some(key=>!allowed.has(key)))throw Error('Unexpected event field');
if(event.duration_ms!==undefined&&(!Number.isSafeInteger(event.duration_ms)||event.duration_ms<0))throw Error('Invalid duration');
if(event.error_code!==undefined&&event.error_code!=='STAGE_FAILED')throw Error('Invalid error code');
const registry=JSON.parse(await fs.readFile(path.join(path.dirname(fileURLToPath(import.meta.url)),'registry.json'),'utf8'));
if(!registry.agents.some(role=>role.id===event.agent))throw Error('Invalid role');
const runtime=new AgentRuntime({root:process.argv[2],application:process.argv[3],registry});
const detail={};
if(event.duration_ms!==undefined)detail.duration_ms=event.duration_ms;
if(event.error_code!==undefined)detail.error_code=event.error_code;
try {await runtime.emit(event.job,event.agent,event.run,event.status,detail);}
catch(error){if(error instanceof ReplayRejected){process.stderr.write('TELEMETRY_RECONCILIATION_REQUIRED\n');process.exitCode=3;}else throw error;}
