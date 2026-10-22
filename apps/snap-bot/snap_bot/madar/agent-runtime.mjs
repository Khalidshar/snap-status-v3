import fs from 'node:fs/promises';
import path from 'node:path';
import {createReadStream} from 'node:fs';
import {createHash, randomUUID} from 'node:crypto';

const apps = new Set(['snap-bot-v2', 'status-bot-v2']);
const hash = value => createHash('sha256').update(JSON.stringify(value)).digest('hex');
const safeId = value => typeof value === 'string' && /^[a-zA-Z0-9_.-]{1,120}$/.test(value);
export class Busy extends Error {}
export class AlreadyExecuted extends Error {}
export class OutcomeUnknown extends Error {}
export class ContractRejected extends Error {}
export class ReplayRejected extends ContractRejected {}

// Adapters must wrap computation/validation, never Telegram/WhatsApp delivery.
// Sending remains owned by each application's existing durable outbox.
export class AgentRuntime {
  constructor({root, application, registry, clock = () => Date.now()}) {
    if (!apps.has(application)) throw new TypeError('Unknown application');
    if (registry.agents.length !== 18 || new Set(registry.agents.map(a=>a.id)).size !== 18)
      throw new TypeError('Exactly 18 unique roles required');
    this.root = path.join(root, application);
    this.application = application;
    this.registry = registry;
    this.clock = clock;
    this.adapters = new Map();
  }
  register(id, {execute, validateInput, validateOutput, repair}) {
    if (!this.registry.agents.some(a=>a.id === id) || this.adapters.has(id))
      throw new TypeError('Unknown or duplicate adapter');
    if (![execute, validateInput, validateOutput].every(fn=>typeof fn === 'function'))
      throw new TypeError('Execution and both contracts required');
    if (repair !== undefined && typeof repair !== 'function') throw new TypeError('Invalid repair');
    this.adapters.set(id, {execute, validateInput, validateOutput, repair});
  }
  async scan({job, agent, collect=false}={}) {
    const result={sequence:0,digest:'0'.repeat(64),lastMatch:null,latest:new Map(),tail:[],events:[]};
    let pending='';
    const consume=line=>{
      if(!line || Buffer.byteLength(line)>8192)throw new OutcomeUnknown('Invalid journal line');
      let row;
      try {row=JSON.parse(line);}catch(cause){throw new OutcomeUnknown('Invalid journal encoding',{cause});}
      const {digest,...entry}=row;
      if(entry.application!==this.application || entry.sequence!==result.sequence+1 || entry.previous!==result.digest || digest!==hash(entry))
        throw new OutcomeUnknown('Journal integrity failed');
      result.sequence=entry.sequence;result.digest=digest;
      if(entry.job===job && entry.agent===agent)result.lastMatch=row;
      if(this.registry.agents.some(role=>role.id===entry.agent))result.latest.set(entry.agent,row);
      result.tail.push(row);if(result.tail.length>100)result.tail.shift();
      if(collect)result.events.push(row);
    };
    try {
      for await(const chunk of createReadStream(path.join(this.root,'events.jsonl'),{encoding:'utf8',highWaterMark:4096})) {
        pending+=chunk;
        let newline;
        while((newline=pending.indexOf('\n'))!==-1){consume(pending.slice(0,newline));pending=pending.slice(newline+1);}
        if(Buffer.byteLength(pending)>8192)throw new OutcomeUnknown('Journal line too large');
      }
    }catch(error){if(error.code==='ENOENT')return result;throw error;}
    if(pending)throw new OutcomeUnknown('Incomplete journal');
    return result;
  }
  async events() {return (await this.scan({collect:true})).events;}
  async lock(name) {
    await fs.mkdir(this.root, {recursive:true, mode:0o700});
    try { return await fs.open(path.join(this.root, name), 'wx', 0o600); }
    catch (e) { if (e.code === 'EEXIST') throw new Busy('Operation already locked'); throw e; }
  }
  async emit(job, agent, run, status, detail = {}, mayWrite = () => true) {
    let lock;
    const deadline = Date.now()+2000;
    while (!lock) {
      try { lock = await this.lock('journal.lock'); }
      catch (error) {
        if (!(error instanceof Busy) || Date.now() >= deadline) throw error;
        await new Promise(resolve=>setTimeout(resolve,5));
      }
    }
    try {
      const journal = await this.scan({job,agent});
      if (!safeId(job) || !safeId(run) || !this.registry.agents.some(a=>a.id===agent))
        throw new ContractRejected('Invalid event identity');
      const last = journal.lastMatch;
      const transitions = {RUNNING:['SUCCEEDED','FAILED','UNKNOWN','RETRYABLE_FAILURE'], RETRYABLE_FAILURE:['REPAIRING','UNKNOWN'], REPAIRING:['SUCCEEDED','FAILED','UNKNOWN','RETRYABLE_FAILURE']};
      if (!last) {
        if (!['RUNNING','UNIMPLEMENTED'].includes(status))throw new ContractRejected('Initial event required');
      } else if (last.run!==run || !(transitions[last.status] ?? []).includes(status)) {
        throw new ReplayRejected('Terminal outcome or replay rejected');
      }
      const entry = {schema_version:1, application:this.application,
        sequence:journal.sequence+1, timestamp:this.clock(), job, agent, run, status,
        ...detail, previous:journal.digest};
      const file = await fs.open(path.join(this.root,'events.jsonl'), 'a', 0o600);
      try {
        // A deadline may expire while waiting for the journal lock or opening
        // the file. Recheck inside the critical section: no late terminal event
        // can replace the UNKNOWN outcome already recorded by the caller.
        if (!mayWrite()) throw new OutcomeUnknown('Late journal outcome');
        await file.writeFile(JSON.stringify({...entry, digest:hash(entry)})+'\n');
        await file.sync();
      }
      finally { await file.close(); }
    } finally { await lock.close(); await fs.unlink(path.join(this.root,'journal.lock')); }
  }
  async run({job, agent, input, timeoutMs = 30000, maxRepairs = 0}) {
    if (!safeId(job) || !this.registry.agents.some(a=>a.id===agent)) throw new TypeError('Invalid run identity');
    if (!Number.isInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 120000 ||
        !Number.isInteger(maxRepairs) || maxRepairs < 0 || maxRepairs > 2)
      throw new TypeError('Invalid execution budget');
    const run = randomUUID();
    const lockName = hash({job,agent})+'.lock';
    const lock = await this.lock(lockName);
    let timedOut = false;
    let timer;
    let successReceipt=null;
    let committingSuccess=false;
    try {
      if ((await this.scan({job,agent})).lastMatch)
        throw new AlreadyExecuted('Use a new job identity; replay requires reconciliation');
      const adapter = this.adapters.get(agent);
      if (!adapter) {
        await this.emit(job,agent,run,'UNIMPLEMENTED');
        throw new ContractRejected('Adapter not implemented');
      }
      const start = this.clock();
      await this.emit(job,agent,run,'RUNNING');
      const signal = new AbortController();
      const timeout = new Promise((resolve,reject)=>{
        timer = setTimeout(()=>{
          if (committingSuccess && successReceipt) {
            let grace;
            const cap=new Promise((_,fail)=>{grace=setTimeout(()=>fail(new OutcomeUnknown('Receipt settlement deadline exceeded')),2000);});
            Promise.race([successReceipt.promise,cap]).then(()=>resolve(successReceipt.output),cause=>{
              timedOut=true;signal.abort();reject(new OutcomeUnknown('Success receipt uncertain',{cause}));
            }).finally(()=>clearTimeout(grace));
            return;
          }
          timedOut = true;
          signal.abort();
          reject(new OutcomeUnknown('Execution deadline exceeded'));
        },timeoutMs);
      });
      const work = async()=>{
        const emitWork = async (status, detail) => {
          try { return await this.emit(job,agent,run,status,detail,()=>!timedOut); }
          catch(cause) { throw new OutcomeUnknown('Stage receipt unavailable',{cause}); }
        };
        let attempt = 0;
        let current = input;
        try {
          if (await adapter.validateInput(current) !== true) throw new ContractRejected('Input rejected');
          if (timedOut) throw new OutcomeUnknown('Late input validation');
        } catch (error) {
          if (timedOut) throw new OutcomeUnknown('Late input rejection');
          await emitWork('FAILED',{error_code:'INPUT_CONTRACT',duration_ms:this.clock()-start});
          throw error;
        }
        while (true) {
          try {
            const output = await adapter.execute(current, {signal:signal.signal, job, application:this.application});
            if (timedOut) throw new OutcomeUnknown('Late outcome');
            if (await adapter.validateOutput(output) !== true) throw new ContractRejected('Output rejected');
            if (timedOut) throw new OutcomeUnknown('Late outcome');
            // A completed callback must never run again because its receipt failed.
            try {
              const promise=this.emit(job,agent,run,'SUCCEEDED',{attempt,duration_ms:this.clock()-start},()=>{
                if(timedOut)return false;
                committingSuccess=true;return true;
              });
              successReceipt={promise,output};
              await promise;
            }
            catch (cause) { throw new OutcomeUnknown('Success receipt unavailable', {cause}); }
            return output;
          } catch (error) {
            if (timedOut || error instanceof OutcomeUnknown) throw error;
            // Error messages, inputs and outputs never enter the telemetry journal.
            await emitWork(adapter.repair && attempt < maxRepairs ? 'RETRYABLE_FAILURE' : 'FAILED',{attempt,error_code:error instanceof ContractRejected?'OUTPUT_CONTRACT':'EXECUTION_ERROR',duration_ms:this.clock()-start});
            if (!adapter.repair || attempt >= maxRepairs) throw error;
            attempt++;
            await emitWork('REPAIRING',{attempt});
            try { current = await adapter.repair(current, {signal:signal.signal,attempt}); }
            catch (repairError) {
              if (!timedOut) await emitWork('FAILED',{attempt,error_code:'REPAIR_ERROR',duration_ms:this.clock()-start});
              throw repairError;
            }
            if (timedOut) throw new OutcomeUnknown('Late repair outcome');
            const accepted = await adapter.validateInput(current);
            if (timedOut) throw new OutcomeUnknown('Late repaired input validation');
            if (accepted !== true) {
              await emitWork('FAILED',{attempt,error_code:'REPAIRED_INPUT_CONTRACT'});
              throw new ContractRejected('Repaired input rejected');
            }
          }
        }
      };
      try { return await Promise.race([work(), timeout]); }
      catch (error) {
        if (timedOut || error instanceof OutcomeUnknown) {
          try { await this.emit(job,agent,run,'UNKNOWN',{error_code:'OUTCOME_UNKNOWN',duration_ms:this.clock()-start}); }
          catch (cause) { throw new OutcomeUnknown('Outcome uncertain; journal unavailable', {cause}); }
        }
        throw error;
      } finally { clearTimeout(timer); }
    } finally { await lock.close(); await fs.unlink(path.join(this.root,lockName)); }
  }
  async snapshot({freshnessMs=60000}={}) {
    const journal = await this.scan();
    const now = this.clock();
    return {schema_version:1, application:this.application, source:'madar-agent-journal',
      observed_at:now, agents:this.registry.agents.map(role=>{
        const latest = journal.latest.get(role.id);
        const stale = !!latest && now-latest.timestamp > freshnessMs;
        return {id:role.id, name:role.name, role:role.role,
          adapter_registered:this.adapters.has(role.id),
          state:stale && ['RUNNING','REPAIRING'].includes(latest?.status) ? latest.status+'_STALE' : latest?.status ?? (this.adapters.has(role.id)?'IDLE':'UNIMPLEMENTED'),
          observed_status:latest?.status ?? null,
          freshness:latest ? (stale?'STALE':'FRESH') : 'UNOBSERVED',
          adapter_status:role.adapter_status ?? 'NOT_IMPLEMENTED',
          acceptance_status:role.acceptance_status ?? 'NOT_VERIFIED',
          last_event_status:latest?.status ?? null,
          stale, last_event_at:latest?.timestamp ?? null, job:latest?.job ?? null};
      }), events:journal.tail};
  }
}
