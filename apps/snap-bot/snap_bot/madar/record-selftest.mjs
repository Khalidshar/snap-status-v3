import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import fs from 'node:fs/promises';
import path from 'node:path';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
export async function recordSelftest(root) {
  const folder=await fs.mkdtemp(path.join(root,'madar-record-cli-'));
  const cli=fileURLToPath(new URL('./record.mjs',import.meta.url));
  const send=event=>spawnSync(process.execPath,['--max-old-space-size=32',cli,folder,'snap-bot-v2'],{input:JSON.stringify(event),encoding:'utf8',timeout:10000});
  const event={job:'cli-job',agent:'writer',run:'cli-run',status:'RUNNING'};
  assert.equal(send({...event,status:'SUCCEEDED'}).status,1);
  assert.equal(send(event).status,0);
  assert.equal(send({...event,status:'UNKNOWN'}).status,0);
  assert.equal(send({...event,status:'SUCCEEDED'}).status,3);
  assert.equal(send({...event,run:'second-run'}).status,3);
  assert.equal(send({...event,job:'123456789:synthetic'}).status,1);
  const raw=await fs.readFile(path.join(folder,'snap-bot-v2/events.jsonl'),'utf8');
  assert.deepEqual(raw.trim().split('\n').map(line=>JSON.parse(line).status),['RUNNING','UNKNOWN']);
  const large=path.join(folder,'large');
  await fs.mkdir(path.join(large,'snap-bot-v2'),{recursive:true});
  const journal=await fs.open(path.join(large,'snap-bot-v2/events.jsonl'),'w');
  let previous='0'.repeat(64);
  try {
    for(let first=0;first<100000;first+=500){
      let batch='';
      for(let i=first;i<first+500;i++){
        const entry={schema_version:1,application:'snap-bot-v2',sequence:i+1,timestamp:1,job:'historical-'+i,agent:'writer',run:'history-'+i,status:'RUNNING',previous};
        const digest=createHash('sha256').update(JSON.stringify(entry)).digest('hex');
        batch+=JSON.stringify({...entry,digest})+'\n';previous=digest;
      }
      await journal.writeFile(batch);
    }
    await journal.sync();
  }finally{await journal.close();}
  const largeResult=spawnSync(process.execPath,['--max-old-space-size=32',cli,large,'snap-bot-v2'],{input:JSON.stringify({...event,job:'after-100000'}),encoding:'utf8',timeout:30000});
  assert.equal(largeResult.status,0,largeResult.stderr);
  await fs.rm(folder,{recursive:true,force:true});
  return {passed:true,checks:['CLI rejects orphan success','CLI accepts start and unknown','CLI rejects late success','CLI rejects new-run replay','CLI rejects colon identity','CLI appends after 100000 events under 32MB']};
}
