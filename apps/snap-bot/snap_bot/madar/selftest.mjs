import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import {AgentRuntime,AlreadyExecuted,OutcomeUnknown,ContractRejected,Busy} from './agent-runtime.mjs';

export async function selftest(root, registry) {
  const folder=await fs.mkdtemp(path.join(root,'madar-tests-'));
  const snap=new AgentRuntime({root:folder,application:'snap-bot-v2',registry});
  const status=new AgentRuntime({root:folder,application:'status-bot-v2',registry});
  const contract={validateInput:x=>typeof x==='number',validateOutput:x=>typeof x==='number'};
  let calls=0;
  snap.register('writer',{...contract,execute:x=>{calls++;return x*2;}});
  status.register('writer',{...contract,execute:x=>x+1});
  assert.equal(await snap.run({job:'a',agent:'writer',input:2}),4);
  assert.equal(await status.run({job:'a',agent:'writer',input:2}),3);
  await assert.rejects(snap.run({job:'a',agent:'writer',input:2}),AlreadyExecuted);
  assert.equal(calls,1);
  await assert.rejects(snap.run({job:'bad',agent:'writer',input:'token-secret-value'}),ContractRejected);
  assert.equal(calls,1);
  let attempts=0;
  snap.register('reviewer',{...contract,execute:()=>{attempts++;throw Error('PRIVATE SECRET');},repair:x=>x});
  await assert.rejects(snap.run({job:'repair-budget',agent:'reviewer',input:1,maxRepairs:2}));
  assert.equal(attempts,3);
  let repaired=0;
  snap.register('fixer',{...contract,execute:x=>{if(x===0)throw Error('reject');return x;},repair:x=>{repaired++;return x+1;}});
  assert.equal(await snap.run({job:'repair-ok',agent:'fixer',input:0,maxRepairs:1}),1);
  assert.equal(repaired,1);
  snap.register('quality',{...contract,execute:()=>new Promise(resolve=>setTimeout(()=>resolve(2),30))});
  await assert.rejects(snap.run({job:'timeout',agent:'quality',input:1,timeoutMs:5}),OutcomeUnknown);
  await new Promise(resolve=>setTimeout(resolve,45));
  assert.equal((await snap.events()).filter(e=>e.job==='timeout').at(-1).status,'UNKNOWN');
  await assert.rejects(snap.run({job:'timeout',agent:'quality',input:1}),AlreadyExecuted);
  snap.register('final',{...contract,execute:x=>x});
  const originalEmit=snap.emit.bind(snap);
  snap.emit=async(...args)=>{
    if(args[0]==='late-journal'&&args[3]==='UNKNOWN')await new Promise(resolve=>setTimeout(resolve,50));
    if(args[0]==='late-journal'&&args[3]==='SUCCEEDED')
      await new Promise(resolve=>setTimeout(resolve,30));
    return originalEmit(...args);
  };
  await assert.rejects(snap.run({job:'late-journal',agent:'final',input:1,timeoutMs:5}),OutcomeUnknown);
  await new Promise(resolve=>setTimeout(resolve,60));
  assert.equal((await snap.events()).filter(e=>e.job==='late-journal').at(-1).status,'UNKNOWN');
  assert.ok(!(await snap.events()).some(e=>e.job==='late-journal'&&e.status==='SUCCEEDED'));
  snap.emit=originalEmit;
  snap.register('manager',{...contract,execute:async x=>{
    const held=await snap.lock('journal.lock');
    setTimeout(async()=>{await held.close();await fs.unlink(path.join(snap.root,'journal.lock'));},40);
    return x;
  }});
  await assert.rejects(snap.run({job:'external-journal-lock',agent:'manager',input:1,timeoutMs:5}),OutcomeUnknown);
  await new Promise(resolve=>setTimeout(resolve,60));
  assert.ok(!(await snap.events()).some(e=>e.job==='external-journal-lock'&&e.status==='SUCCEEDED'));
  assert.equal((await snap.events()).filter(e=>e.job==='external-journal-lock').at(-1).status,'UNKNOWN');
  let receiptCalls=0;
  snap.register('analyst',{...contract,execute:x=>{receiptCalls++;return x;},repair:x=>x});
  snap.emit=async(...args)=>{
    if(args[0]==='receipt-failure'&&args[3]==='SUCCEEDED')throw Error('synthetic EIO');
    if(args[0]==='unknown-failure'&&args[3]==='UNKNOWN')throw Error('synthetic EIO');
    return originalEmit(...args);
  };
  await assert.rejects(snap.run({job:'receipt-failure',agent:'analyst',input:1,maxRepairs:1}),OutcomeUnknown);
  assert.equal(receiptCalls,1);
  assert.deepEqual((await snap.events()).filter(e=>e.job==='receipt-failure').map(e=>e.status),['RUNNING','UNKNOWN']);
  await assert.rejects(snap.run({job:'unknown-failure',agent:'quality',input:1,timeoutMs:5}),OutcomeUnknown);
  await new Promise(resolve=>setTimeout(resolve,45));
  snap.emit=originalEmit;
  snap.register('judge',{...contract,execute:()=>{throw Error('synthetic execution failure');}});
  snap.emit=async(...args)=>{if(args[0]==='failed-receipt'&&args[3]==='FAILED')throw Error('synthetic EIO');return originalEmit(...args);};
  await assert.rejects(snap.run({job:'failed-receipt',agent:'judge',input:1}),OutcomeUnknown);
  assert.deepEqual((await snap.events()).filter(e=>e.job==='failed-receipt').map(e=>e.status),['RUNNING','UNKNOWN']);
  snap.emit=originalEmit;
  // Delay the actual FileHandle.sync, not the emit wrapper.
  const realOpen=fs.open;
  let syncCount=0;
  fs.open=async(...args)=>{
    const handle=await realOpen(...args);
    if(String(args[0]).endsWith('events.jsonl')&&args[1]==='a'){
      const sync=handle.sync.bind(handle);
      handle.sync=async()=>{if(++syncCount===2)await new Promise(resolve=>setTimeout(resolve,150));return sync();};
    }
    return handle;
  };
  try {assert.equal(await snap.run({job:'slow-success-sync',agent:'final',input:9,timeoutMs:60}),9);}
  finally {fs.open=realOpen;}
  assert.equal((await snap.events()).filter(e=>e.job==='slow-success-sync').at(-1).status,'SUCCEEDED');
  const staleSnapshot=await new AgentRuntime({root:folder,application:'snap-bot-v2',registry,clock:()=>Date.now()+86400000}).snapshot();
  assert.equal(staleSnapshot.agents.find(a=>a.id==='quality').state,'RUNNING_STALE');
  assert.equal(staleSnapshot.agents.find(a=>a.id==='quality').observed_status,'RUNNING');
  await assert.rejects(snap.run({job:'missing',agent:'voice',input:1}),ContractRejected);
  assert.equal((await snap.snapshot()).agents.find(a=>a.id==='voice').state,'UNIMPLEMENTED');
  assert.equal((await status.events()).length,2);
  const raw=await fs.readFile(path.join(snap.root,'events.jsonl'),'utf8');
  assert.ok(!raw.includes('PRIVATE SECRET')&&!raw.includes('token-secret-value'));
  const old=new AgentRuntime({root:folder,application:'snap-bot-v2',registry,clock:()=>Date.now()+120000});
  assert.equal((await old.snapshot()).agents.find(a=>a.id==='writer').state,'FAILED');
  assert.equal((await old.snapshot()).agents.find(a=>a.id==='writer').freshness,'STALE');
  assert.equal((await old.snapshot()).agents.find(a=>a.id==='reviewer').state,'FAILED');
  assert.equal((await old.snapshot()).agents.find(a=>a.id==='reviewer').acceptance_status,'NOT_VERIFIED');
  assert.equal((await old.snapshot()).agents.length,18);
  const concurrent=snap.run({job:'locked',agent:'quality',input:1});
  await new Promise(resolve=>setTimeout(resolve,5));
  await assert.rejects(snap.run({job:'locked',agent:'quality',input:1}),Busy);
  await concurrent;
  await assert.rejects(snap.emit('orphan','writer','run','SUCCEEDED'),ContractRejected);
  await assert.rejects(snap.emit('timeout','quality',(await snap.events()).find(e=>e.job==='timeout').run,'SUCCEEDED'),ContractRejected);
  await assert.rejects(snap.emit('timeout','quality','new-run','RUNNING'),ContractRejected);
  await assert.rejects(snap.emit('123456789:synthetic-token','writer','run','RUNNING'),ContractRejected);
  // Modification and truncation fail closed, rather than inventing healthy agents.
  await fs.writeFile(path.join(snap.root,'events.jsonl'),raw.replace('SUCCEEDED','CORRUPTED'));
  await assert.rejects(snap.snapshot(),OutcomeUnknown);
  await fs.writeFile(path.join(snap.root,'events.jsonl'),raw.slice(0,-2));
  await assert.rejects(snap.snapshot(),OutcomeUnknown);
  return {passed:true,checks:['application isolation','duplicate execution blocked','input contract','bounded repair','repair verified','timeout unknown','late result suppressed','late journal outcome suppressed','deadline while journal lock held','unknown replay blocked','unimplemented honest','secret-free journal','stale terminal status preserved','success receipt failure never retries','unknown journal failure preserves uncertainty','acceptance status explicit','18 roles per application','concurrency lock','orphan outcome rejected','terminal outcome immutable','direct replay rejected','colon identity rejected','tamper rejected','partial-line truncation rejected','slow sync success agrees with caller','failed receipt uncertainty','stale running explicit'],test_directory:folder};
}
