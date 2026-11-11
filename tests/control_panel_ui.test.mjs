import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {test} from 'node:test';
import assert from 'node:assert/strict';
const root=new URL('../',import.meta.url);
const source=readFileSync(new URL('scripts/control_panel_ui/app.js',root),'utf8');
const html=readFileSync(new URL('scripts/control_panel_ui/index.html',root),'utf8');
const roles=JSON.parse(readFileSync(new URL('config/status/panel-agent-roles.json',root),'utf8'));
function boot(){const events=new Map();const nodes=new Map();const node=id=>{if(!nodes.has(id))nodes.set(id,{id,innerHTML:'',textContent:'',hidden:true,value:'',dataset:{},elements:{file:{}},classList:{add(){},remove(){}},addEventListener(){},setAttribute(){},focus(){},querySelectorAll(){return []},querySelector(){return {innerHTML:'',value:''}},append(){}});return nodes.get(id)};
for(const id of [...html.matchAll(/id="([^"]+)"/g)].map(m=>m[1]))node(id);
node('schedule-bot').value='snap';node('production-bot').value='snap';
const panes=[...html.matchAll(/data-pane="([^"]+)"/g)].map(m=>({dataset:{pane:m[1]},hidden:true}));
const ctx=vm.createContext({document:{getElementById:id=>{if(!nodes.has(id))throw new Error('Missing element '+id);return node(id)},querySelector:selector=>selector==='.tabs'?node('tabs'):panes.find(p=>selector.includes('"'+p.dataset.pane+'"')),querySelectorAll:selector=>selector==='[data-pane]'?panes:[],addEventListener(){},activeElement:node('say')},console,Intl,Date,JSON,Math,Number,String,Object,Array,Set,Map,RegExp,crypto:{randomUUID:()=> 'a'.repeat(32)},location:{hash:''},fetch:async()=>({ok:false,status:503}),setInterval(){},setTimeout(){},addEventListener:(key,callback)=>events.set(key,callback),Blob,URL,FormData,matchMedia:()=>({matches:true})});
vm.runInContext(source,ctx);return {ctx,node,panes,events};}
const app=()=>({active:null,halted:false,jobs:null,clips:[],agents:roles.map(a=>({id:a.id,name:a.name,role:'دور موثق',at:null,state:'UNKNOWN'})),workspace:{previews:[],actions:[],sources:[],schedules:[],audit:[]}});
test('all eight panes remain reachable and Ctrl+K opens the palette without side effects',()=>{const {ctx,node,panes,events}=boot();events.get('keydown')({ctrlKey:true,key:'k',preventDefault(){}});vm.runInContext("show('agents');",ctx);assert.equal(node('pal').hidden,false);assert.equal(panes.find(p=>p.dataset.pane==='agents').hidden,false);assert.equal(panes.filter(p=>!p.hidden).length,1);assert.match(node('pal-list').innerHTML,/الرئيسية/);});
test('reference geometry uses chain roles and tool roles rather than inventory position',()=>{const {ctx,node}=boot();ctx.fixture={agent_roles:roles,commands:[],apps:{snap:app(),status:app()},servers:{amazon:null,wsl:null}};vm.runInContext('current=fixture;render();',ctx);assert.equal((node('orbit').innerHTML.match(/class="node"/g)||[]).length,18);assert.match(node('orbit').innerHTML,/١٢/);assert.match(node('detail').innerHTML,/المصادر/);assert.equal((node('health').innerHTML.match(/class="hc /g)||[]).length,4);assert.equal((node('bots').innerHTML.match(/class="panel bot"/g)||[]).length,2);assert.match(node('bots').innerHTML,/18\/18/);assert.match(node('bots').innerHTML,/غير معروف/);assert.doesNotMatch(node('orbit').innerHTML,/موصولة · لم تُشغَّل/);});
test('source data is escaped before the reference details are rendered',()=>{const {ctx,node}=boot();const fixtureRoles=structuredClone(roles);fixtureRoles[0].name='<img src=x onerror=alert(1)>';
ctx.fixture={agent_roles:fixtureRoles,apps:{snap:app(),status:app()}};vm.runInContext('current=fixture;renderAgents();',ctx);assert.doesNotMatch(node('detail').innerHTML,/<img/);assert.match(node('detail').innerHTML,/&lt;img/);});
