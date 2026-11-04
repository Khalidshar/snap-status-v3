'use strict';
const $=id=>document.getElementById(id), names={snap:'سناب',status:'واتساب'}, states={REVIEW:'بانتظار مراجعتك',APPROVED:'معتمد',REJECTED:'مرفوض',SUPERSEDED:'طُلبت إعادته',QUEUED:'في الطابور',INTENT:'قيد التنفيذ',DONE:'مكتمل',UNKNOWN:'غير معروف',EXPIRED:'انتهت المهلة',PASS:'اجتاز',SUCCEEDED:'نجح',FAIL:'فشل',FAILED:'فشل',RUNNING:'يعمل',STARTED:'بدأ',candidate:'مرشح',approved:'معتمد',excluded:'مستبعد'};
let current=null,token='',pending=null,activeTab='overview'; const blobs=new Map();
const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const state=x=>states[x]||'غير معروف';
const date=x=>x?new Intl.DateTimeFormat('ar-SA-u-ca-gregory',{timeZone:'Asia/Riyadh',weekday:'long',day:'numeric',month:'long',year:'numeric',hour:'numeric',minute:'2-digit',hour12:true}).format(new Date(x*1000)):'لم يُسجّل';
const empty=text=>`<p class="empty">${esc(text)}</p>`;
const id=()=>crypto.randomUUID().replaceAll('-','');
const workspace=app=>current?.apps?.[app]?.workspace||{previews:[],sources:[],actions:[],schedules:[],audit:[]};
const content=()=>current?.commands?.filter(c=>c.content)||[];
function toast(text){$('toast').textContent=text;$('toast').hidden=false;setTimeout(()=>$('toast').hidden=true,6000);}
async function request(path,body,headers={}){const response=await fetch(path,{method:body?'POST':'GET',headers:{...(token?{Authorization:'Bearer '+token}:{}),...(body instanceof Blob?{}:{'Content-Type':'application/json'}),...headers},body:body?(body instanceof Blob?body:JSON.stringify(body)):undefined,cache:'no-store'});if(!response.ok)throw new Error(String(response.status));return response;}
async function action(app,kind,payload){await request('/api/action',{app,action:kind,payload,request_id:id()});toast('حُفظ الطلب في طابور '+names[app]+'؛ تنتظر النتيجة تنفيذ العامل.');await refresh();}
function confirm(text,callback){pending=callback;$('confirm-text').textContent=text;$('confirm').showModal();}
$('cancel').onclick=()=>{$('confirm').close();pending=null;};$('confirm-action').onclick=async()=>{const callback=pending;pending=null;$('confirm').close();try{await callback?.();}catch{toast('تعذّر اعتماد الإجراء. افحص السجل قبل المحاولة مجددًا.');}};
function show(tab){const pane=document.querySelector(`[data-pane="${tab}"]`);if(!pane)return;activeTab=tab;document.querySelectorAll('[data-pane]').forEach(p=>p.hidden=p!==pane);document.querySelectorAll('[data-tab]').forEach(a=>{if(a.dataset.tab===tab)a.setAttribute('aria-current','page');else a.removeAttribute('aria-current');});}
document.querySelector('.tabs').onclick=e=>{const a=e.target.closest('[data-tab]');if(a)show(a.dataset.tab);};show(location.hash.slice(1)||'overview');
function previewCard(app,p){const c=current.commands.find(c=>c.key===p.command);return `<article class="card"><div class="head"><h3>${esc(c?.label||'مقطع مرفوع')} · ${names[app]}</h3><span class="chip">${state(p.state)}</span></div><video class="preview" controls preload="none" data-media="${app}/${p.id}"></video><button data-load="${app}/${p.id}">تحميل المعاينة</button><p>المصادر: ${esc(JSON.parse(p.sources||'[]').join('، ')||'لم تُسجّل')}</p><p class="statusline">${date(p.at)}</p>${p.state==='REVIEW'?`<div class="actions"><button class="primary" data-review="approve" data-app="${app}" data-id="${p.id}" data-sha="${p.sha}">موافقة وإرسال</button><button class="danger" data-review="reject" data-app="${app}" data-id="${p.id}" data-sha="${p.sha}">رفض</button><button data-review="retry" data-app="${app}" data-id="${p.id}" data-sha="${p.sha}">إعادة</button></div>`:''}<p class="mono">${esc(p.sha.slice(0,12))}</p></article>`;}
function render(){if(!current)return;const all=Object.keys(names).flatMap(app=>workspace(app).previews.map(p=>({app,p}))).sort((a,b)=>b.p.at-a.p.at);
$('connection').textContent='متصل باللوحة';$('p-fresh-t').textContent='آخر قراءة: '+date(current.at);if($('p-live'))$('p-live').className='pill '+(Object.values(current.apps).every(a=>a.active===true)?'live ok':'dim');$('notice').textContent=current.workspace_executor_connected?'طلبات اللوحة محفوظة. حالة التنفيذ من سجل العامل.':'نسخة قيد التنفيذ: طلبات اللوحة تُحفظ، ومنفّذات الإنتاج والإرسال والتبديل والتحليل لم تُربط بعد. البوت الجديد يبقى موقوفًا.';
renderOverview();
$('latest').innerHTML=all.slice(0,3).map(({app,p})=>`<div class="card"><h3>${names[app]}</h3><p>${state(p.state)}</p><small>${date(p.at)}</small></div>`).join('')||empty('لا توجد مقاطع مسجّلة بعد');
renderCommands();renderSchedules();renderAgents();renderDesigner();
$('reviews').innerHTML=all.filter(x=>x.p.state==='REVIEW').map(({app,p})=>previewCard(app,p)).join('')||empty('لا توجد معاينات تنتظر المراجعة');
$('archive-list').innerHTML=all.map(({app,p})=>previewCard(app,p)+`<label>التقييم <select data-rate="${p.id}" data-app="${app}"><option value="">اختر</option>${[1,2,3,4,5].map(n=>`<option value="${n}">${n.toLocaleString('ar-SA')}</option>`).join('')}</select></label>`).join('')||empty('الأرشيف ينتظر أول مقطع');
$('source-list').innerHTML=(current.sources||[]).map(s=>`<div class="card"><div class="head"><b>${esc(s.name)}</b><span class="chip">${state(s.state)}</span></div><small>الاتصال: ${s.health==='ok'?'يشتغل':s.health==='403'?'يرفض 403':s.health==='down'?'متعطل':'لم يُفحص'}</small></div>`).join('');
$('source-command').innerHTML=content().filter(c=>c.section!=='faith').map(c=>`<option value="${c.key}">${esc(c.label)}</option>`).join('');
$('candidates').innerHTML=Object.keys(names).flatMap(app=>workspace(app).sources.map(s=>`<div class="card"><h3>${esc(s.name)} · ${names[app]}</h3><p>${state(s.state)}</p>${s.state==='candidate'?`<button data-source="${s.id}" data-app="${app}">اعتماد المصدر</button>`:''}</div>`)).join('')||empty('لا مصادر مضافة من اللوحة');
$('server-cards').innerHTML=['amazon','wsl'].map(target=>{const s=current.servers?.[target];return `<article class="panel"><div class="head"><h2>${target==='amazon'?'خادم أمازون':'جهاز خالد · الخادم المحلي'}</h2><span class="chip">${s&&Date.now()/1000-s.at<90?'قراءة حديثة':'غير متصل'}</span></div><p class="muted">آخر قراءة: ${date(s?.at)}</p><h3 class="section-title">القرص</h3><p class="metric">${s?Math.round(s.disk_used/s.disk_total*100).toLocaleString('ar-SA')+'٪':'—'}</p><progress max="100" value="${s?Math.round(s.disk_used/s.disk_total*100):0}"></progress><h3 class="section-title">الذاكرة المتاحة</h3><p>${s?Math.round((s.memory_available??s.memory?.MemAvailable??0)/1048576).toLocaleString('ar-SA')+' ميجابايت':'غير معروفة'}</p><p class="muted">الخدمات: ${s?.services?Object.entries(s.services).map(([k,v])=>names[k]+' '+(v==='active'?'يعمل':v==='inactive'?'موقوف':'غير معروف')).join(' · '):'راجع حالة مستمعي البوتين في الرئيسية'}</p><div class="actions">${[['stop','إيقاف كامل'],['start','تشغيل'],['old','البوت القديم'],['new','البوت الجديد']].map(([op,label])=>`<button class="${op==='stop'?'danger':''}" data-server="${target}" data-operation="${op}">${label}</button>`).join('')}</div></article>`;}).join('');
const audits=Object.keys(names).flatMap(app=>workspace(app).audit.map(a=>({...a,app}))).sort((a,b)=>b.at-a.at);
$('audit').innerHTML=audits.slice(0,40).map(a=>`<p>${date(a.at)} · ${names[a.app]} · ${state(a.event)}</p>`).join('')||empty('لا إجراءات مسجّلة');
$('analysis-list').innerHTML=Object.keys(names).flatMap(app=>workspace(app).actions.filter(a=>a.action==='statistics').map(a=>`<p>${names[app]} · ${state(a.state)} · ${date(a.at)}</p>`)).join('')||empty('لا تحليل مسجّل بعد');
}
const LOOKS={footage:'خلفية متحركة',mix:'خليط: خلفية وبطاقات شفافة',solid:'خلفية خضراء ثابتة'};
function renderDesigner(){const label=k=>current.commands.find(c=>c.key===k)?.label||k;$('designer-list').innerHTML=(current.designer||[]).map(d=>`<article class="card"><div class="head"><h3>${esc(label(d.key))} · ${names[d.app]}</h3><span class="chip ${d.bars?'bad':'ok'}">${d.inspected?(d.bars?'رُفض: حواف':'اجتاز الفحص'):'لم يُفحص'}</span></div><p><b>${esc(LOOKS[d.style]||'غير معروف')}</b></p><p>الخلفية: ${esc(d.footage||'—')} · التعتيم ${Math.round(d.dim*100).toLocaleString('ar-SA')}٪</p><ul>${d.reasons.map(r=>`<li>${esc(r)}</li>`).join('')}</ul><small>${date(d.at)}</small></article>`).join('')||empty('لا قرارات مسجّلة بعد');}
function renderCommands(){const q=$('command-search').value; $('commands').innerHTML=content().filter(c=>!q||c.label.includes(q)).map(c=>`<article class="card"><h3>${esc(c.label)}</h3><p>معاينة قبل الموافقة</p><button class="primary" data-produce="${c.key}">أنتج الحين</button></article>`).join('');}
function renderSchedules(){const saved=workspace($('schedule-bot').value).schedules;$('schedule-rows').innerHTML=content().map(c=>{const s=saved.find(s=>s.command===c.key);return `<tr data-command="${c.key}"><td>${esc(c.label)}</td><td><input type="time" value="${s?.clock||''}" aria-label="موعد ${esc(c.label)}"></td><td><label><input type="checkbox" ${s?.enabled?'checked':''}> تشغيل</label></td><td><button data-schedule="${c.key}">حفظ</button></td></tr>`;}).join('');}
let D={agents:[]},AG={},CHAIN=[],TOOLS=[],abot='snap',sel='fact_source',playing=false;
const AST={passed:['اجتاز','ok'],failed:['فشل','bad'],running:['يعمل','info'],unknown:['غير معروف','dim']};
const tx=esc,chip=(text,tone)=>'<span class="chip '+tone+'"><span>'+esc(text)+'</span></span>';
const CX=320,CY=320,R1=238,R2=112;
function pos(i,n,r){const a=-Math.PI/2+i*2*Math.PI/n;return[CX+r*Math.cos(a),CY+r*Math.sin(a)]}
function col(st){return st==='passed'?'#6fe3c6':st==='failed'?'#ff8796':st==='running'?'#7cc7ff':'#93a9c3'}
function renderAgents(){
 const roles=current.agent_roles||[];
 const classify=s=>['PASS','SUCCEEDED'].includes(s)?'passed':['FAIL','FAILED'].includes(s)?'failed':['RUNNING','STARTED'].includes(s)?'running':'unknown';
 D={agents:roles.map(a=>{const result={...a};for(const app of Object.keys(names)){const live=current.apps[app].agents.find(r=>r.id===a.id);result[app]={state:classify(live?.state),does:live?.role||'لا يوجد وصف موثق',last:'آخر نشاط: '+date(live?.at)};}return result;})};
 AG=Object.fromEntries(D.agents.map(a=>[a.id,a]));CHAIN=D.agents.filter(a=>a.chain);TOOLS=D.agents.filter(a=>!a.chain);
 $('ag-bot').innerHTML=Object.keys(names).map(app=>'<button type="button" data-b="'+app+'" aria-pressed="'+(app===abot)+'">'+names[app]+'</button>').join('');
 if(!AG[sel])sel=CHAIN[0]?.id;if(!sel)return;drawOrbit();drawDetail();drawMatrix();
}
$('ag-bot').addEventListener('click',e=>{const b=e.target.closest('[data-b]');if(!b)return;abot=b.dataset.b;renderAgents();});

function drawOrbit(lit){
  let g='<defs><radialGradient id="core" cx="50%" cy="50%" r="50%"><stop offset="0" stop-color="#123049"/><stop offset="1" stop-color="#081220"/></radialGradient>'
   +'<marker id="arw" viewBox="0 0 8 8" refX="6" refY="4" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0L8 4L0 8z" fill="#3b5878"/></marker></defs>';
  g+='<circle cx="'+CX+'" cy="'+CY+'" r="'+R1+'" fill="none" stroke="#21354e" stroke-width="1.5"/>';
  g+='<circle cx="'+CX+'" cy="'+CY+'" r="'+R2+'" fill="none" stroke="#21354e" stroke-dasharray="3 5"/>';
  g+='<circle cx="'+CX+'" cy="'+CY+'" r="64" fill="url(#core)" stroke="#21354e"/>';
  // chain arcs, in order
  CHAIN.forEach((a,i)=>{if(i===CHAIN.length-1)return;const a1=-Math.PI/2+(i+.18)*2*Math.PI/12,a2=-Math.PI/2+(i+.82)*2*Math.PI/12;
    const p1=[CX+R1*Math.cos(a1),CY+R1*Math.sin(a1)],p2=[CX+R1*Math.cos(a2),CY+R1*Math.sin(a2)];
    const on=lit!=null&&i<lit;
    g+='<path d="M'+p1[0].toFixed(1)+' '+p1[1].toFixed(1)+' A'+R1+' '+R1+' 0 0 1 '+p2[0].toFixed(1)+' '+p2[1].toFixed(1)+'" fill="none" stroke="'+(on?'#6fe3c6':'#3b5878')+'" stroke-width="'+(on?3:2)+'" marker-end="url(#arw)"/>'});
  CHAIN.forEach((a,i)=>{const [x,y]=pos(i,12,R1),st=a[abot].state,c=col(st),isSel=a.id===sel,isLit=lit!=null&&i===lit;
    const ang=-Math.PI/2+i*2*Math.PI/12,cs=Math.cos(ang),anc=cs>0.35?'start':cs<-0.35?'end':'middle',off=anc==='middle'?40:32;
    const lx=x+off*cs,ly=y+off*Math.sin(ang)+(anc==='middle'?(Math.sin(ang)<0?-2:6):0);
    g+='<g class="node" tabindex="0" role="button" data-id="'+a.id+'" aria-label="'+esc(a.name)+'">'
     +(isSel||isLit?'<circle cx="'+x+'" cy="'+y+'" r="27" fill="none" stroke="'+(isLit?'#ffcd70':c)+'" stroke-opacity=".55" stroke-width="2"/>':'')
     +'<circle class="core" cx="'+x+'" cy="'+y+'" r="20" fill="#0b1626" stroke="'+(isLit?'#ffcd70':c)+'" stroke-width="2.2"/>'
     +'<text x="'+x+'" y="'+(y+4.5)+'" text-anchor="middle" font-size="13" font-weight="600" fill="'+(isLit?'#ffcd70':c)+'" font-family="Readex Pro,sans-serif">'+a.step.toLocaleString('ar-SA')+'</text>'
     +'<text class="lbl" x="'+lx+'" y="'+(ly+4)+'" text-anchor="'+anc+'">'+esc(a.name)+'</text>'
     +'<rect x="'+(x-22)+'" y="'+(y-22)+'" width="44" height="44" fill="transparent"/></g>'});
  TOOLS.forEach((a,i)=>{const [x,y]=pos(i,6,R2),isSel=a.id===sel;
    g+='<g class="node" tabindex="0" role="button" data-id="'+a.id+'" aria-label="'+esc(a.name)+'">'
     +(isSel?'<circle cx="'+x+'" cy="'+y+'" r="22" fill="none" stroke="#b9a6ff" stroke-opacity=".55" stroke-width="2"/>':'')
     +'<rect x="'+(x-15)+'" y="'+(y-15)+'" width="30" height="30" rx="8" fill="#0b1626" stroke="#b9a6ff" stroke-width="1.8"/>'
     +'<text class="code" x="'+x+'" y="'+(y+3.5)+'" text-anchor="middle" data-layout="ref-5">'+esc(a.code)+'</text>'
     +'<text x="'+x+'" y="'+(y+(y<CY?-22:30))+'" text-anchor="middle" font-size="11" fill="#93a9c3">'+esc(a.name)+'</text></g>'});
  const done=CHAIN.filter(a=>a[abot].state==='passed').length;
  g+='<text x="'+CX+'" y="'+(CY-8)+'" text-anchor="middle" font-family="Readex Pro,sans-serif" font-size="30" font-weight="600" fill="#e8f2fd">12 + 6</text>'
   +'<text x="'+CX+'" y="'+(CY+18)+'" text-anchor="middle" font-size="12" fill="#93a9c3">'+(CHAIN.some(a=>a[abot].state!=='unknown')?done+' اجتاز آخر رصد':'لم يُسجّل نشاط')+'</text>';
  $('orbit').innerHTML=g}
$('orbit').addEventListener('click',e=>{const n=e.target.closest('.node');if(n){sel=n.dataset.id;drawOrbit();drawDetail();drawMatrix()}});
$('orbit').addEventListener('keydown',e=>{const n=e.target.closest('.node');if(n&&(e.key==='Enter'||e.key===' ')){e.preventDefault();sel=n.dataset.id;drawOrbit();drawDetail();drawMatrix();const f=$('orbit').querySelector('[data-id="'+sel+'"]');f&&f.focus()}});
function drawDetail(){const a=AG[sel],other=abot==='snap'?'status':'snap';
  const pos=a.chain?'الخطوة '+a.step+' من 12'+(a.after?' · بعد «'+AG[a.after].name+'»':' · أول السلسلة'):'أداة محلية خارج سلسلة المهمة';
  const box=(k,label)=>{const st=AST[a[k].state];return '<div><small>'+label+'</small>'+chip(st[0],st[1])+'<p>'+tx(a[k].does)+'</p><small>'+tx(a[k].last)+'</small></div>'};
  $('detail').innerHTML='<span class="code">'+esc(a.code)+'</span><h2>'+esc(a.name)+'</h2><p class="muted">'+esc(pos)+'</p>'
   +'<dl class="dl"><dt>المعرّف</dt><dd class="mono">'+esc(a.id)+'</dd><dt>المدخل ← المخرج</dt><dd class="mono">'+esc(a.io)+'</dd>'
   +(a.chain?'<dt>العقد</dt><dd>يعتمد على مخرج الخطوة السابقة فقط، وبصمة العقد نفسها في كل الخطوات</dd>':'')+'</dl>'
   +'<div class="both">'+box(abot,abot==='snap'?'في سناب':'في الحالات')+box(other,other==='snap'?'في سناب':'في الحالات')+'</div>'
   +(a.chain?'<div data-layout="ref-6">'+(a.after?'<button type="button" class="copy" data-go="'+a.after+'">→ السابق</button>':'')+(a.step<12?'<button type="button" class="copy" data-go="'+CHAIN[a.step].id+'">التالي ←</button>':'')+'</div>':'')}
$('detail').addEventListener('click',e=>{const b=e.target.closest('[data-go]');if(b){sel=b.dataset.go;drawOrbit();drawDetail();drawMatrix()}});
function drawMatrix(){$('matrix').querySelector('tbody').innerHTML=D.agents.map(a=>'<tr data-id="'+a.id+'"'+(a.id===sel?' class="sel"':'')+'><td class="num">'+(a.step||'—')+'</td><td><b data-layout="ref-7">'+esc(a.name)+'</b> <span class="mono muted">'+esc(a.code)+'</span></td><td class="muted">'+(a.after?esc(AG[a.after].name):a.chain?'البداية':'—')+'</td><td>'+chip(AST[a.snap.state][0],AST[a.snap.state][1])+'</td><td>'+chip(AST[a.status.state][0],AST[a.status.state][1])+'</td></tr>').join('')}
$('matrix').addEventListener('click',e=>{const r=e.target.closest('tr[data-id]');if(r){sel=r.dataset.id;drawOrbit();drawDetail();drawMatrix();$('orbit').scrollIntoView({behavior:'smooth',block:'center'})}});
$('play').addEventListener('click',()=>{if(playing)return;playing=true;$('play').disabled=true;let i=0;
  const reduce=matchMedia('(prefers-reduced-motion: reduce)').matches;
  const step=()=>{if(i>=12){playing=false;$('play').disabled=false;drawOrbit();return}sel=CHAIN[i].id;drawOrbit(i);drawDetail();drawMatrix();i++;setTimeout(step,reduce?60:650)};step()});

async function refresh(){try{current=await (await request('/api/snapshot')).json();$('login').hidden=true;render();}catch(e){$('connection').textContent='الاتصال غير متاح';$('notice').textContent='تعذّر تحديث البيانات. المعروض آخر قراءة ناجحة.';if(e.message==='401')$('login').hidden=false;}}
$('refresh').onclick=refresh;$('login-form').onsubmit=e=>{e.preventDefault();token=$('token').value;$('token').value='';refresh();};
$('command-search').oninput=renderCommands;$('schedule-bot').onchange=renderSchedules;
$('simulate').onsubmit=e=>{e.preventDefault();const value=$('say').value.trim(),c=current?.commands.find(c=>c.label.includes(value)||String(c.number)===value);const b=document.createElement('div');b.className='bubble';b.textContent=c?c.label+' — ينتج المقطع من زر «أنتج الحين» ويحتاج موافقتك بعد المعاينة.':'اكتب اسم أمر أو رقمه من قائمة الإنتاج.';$('chat').append(b);$('say').value='';};
$('source-form').onsubmit=async e=>{e.preventDefault();const f=new FormData(e.target);try{await action(f.get('app'),'source_add',{name:f.get('name'),url:f.get('url'),command:f.get('command')});e.target.reset();}catch{toast('تعذّر إضافة المصدر. استخدم رابطًا عامًا آمنًا دون بيانات دخول.');}};
async function upload(form,kind){const f=new FormData(form),file=f.get('file');if(!file?.size||file.size>50_000_000){toast('اختر ملفًا لا يتجاوز ٥٠ ميجابايت');return;}try{await request('/api/upload',file,{'Content-Type':file.type,'X-Panel-App':f.get('app'),'X-Panel-Kind':kind});toast('حُفظ الملف وأُضيف طلبه إلى الطابور.');await refresh();}catch{toast('تعذّر الرفع. افحص الحجم والنوع والاتصال.');}}
$('upload-form').onsubmit=e=>{e.preventDefault();upload(e.target,'upload');};$('stats-form').onsubmit=e=>{e.preventDefault();upload(e.target,'statistics');};
let localURL;$('upload-form').elements.file.onchange=e=>{if(localURL)URL.revokeObjectURL(localURL);const file=e.target.files[0];if(file){localURL=URL.createObjectURL(file);$('local-video').src=localURL;$('local-video').hidden=false;}};
document.addEventListener('click',async e=>{const b=e.target.closest('button');if(!b)return;try{if(b.dataset.produce)await action($('production-bot').value,'produce',{command:b.dataset.produce});if(b.dataset.review)confirm('تأكيد '+(b.dataset.review==='approve'?'الموافقة وإرسال هذا المقطع المحدد':b.dataset.review==='reject'?'رفض المقطع':'إعادة إنتاج المقطع')+'؟',()=>action(b.dataset.app,b.dataset.review,{preview:b.dataset.id,sha:b.dataset.sha}));if(b.dataset.source)confirm('اعتماد هذا المصدر للبوت المختار؟',()=>action(b.dataset.app,'source_approve',{source:b.dataset.source}));if(b.dataset.schedule){const row=b.closest('tr');confirm('حفظ موعد الأمر وحالته المطلوبة؟',()=>action($('schedule-bot').value,'schedule',{command:b.dataset.schedule,clock:row.querySelector('[type=time]').value,enabled:row.querySelector('[type=checkbox]').checked}));}if(b.dataset.server)confirm('تأكيد طلب '+b.textContent+' على '+(b.dataset.server==='wsl'?'جهاز خالد':'أمازون')+' للبوتين؟',async()=>{for(const app of Object.keys(names))await action(app,'server',{target:b.dataset.server,operation:b.dataset.operation,confirmed:true});});if(b.dataset.load){const key=b.dataset.load;if(!blobs.has(key)){const response=await request('/api/media/'+key);blobs.set(key,URL.createObjectURL(await response.blob()));}b.parentElement.querySelector('video').src=blobs.get(key);b.hidden=true;}}catch{toast('تعذّر تنفيذ الطلب. راجع سجل الإجراءات.');}});
document.addEventListener('change',async e=>{if(e.target.dataset.rate&&e.target.value){try{await action(e.target.dataset.app,'rate',{preview:e.target.dataset.rate,rating:Number(e.target.value)});}catch{toast('تعذّر حفظ التقييم');}}});
function clock(){$('clock').textContent=date(Date.now()/1000);}clock();setInterval(()=>{clock();refresh();},30000);refresh();

function ring(done,total,tone){const r=34,C=2*Math.PI*r,f=total?done/total:0;
return '<svg class="ring" viewBox="0 0 84 84" aria-hidden="true"><circle cx="42" cy="42" r="'+r+'" fill="none" stroke="#21354e" stroke-width="7"/>'+'<circle cx="42" cy="42" r="'+r+'" fill="none" stroke="var(--'+tone+')" stroke-width="7" stroke-linecap="round" stroke-dasharray="'+(C*f).toFixed(1)+' '+C.toFixed(1)+'" transform="rotate(-90 42 42)"/>'+'<text x="42" y="40" text-anchor="middle" font-family="Readex Pro,sans-serif" font-size="20" font-weight="600" fill="#e8f2fd">'+done+'</text><text x="42" y="56" text-anchor="middle" font-size="10" fill="#93a9c3" font-family="Readex Pro,sans-serif">من '+total+' مسجّل</text></svg>';}
function renderOverview(){
 const freshness=key=>current.servers?.[key]&&Date.now()/1000-current.servers[key].at<90;
 const health=[['خادم أمازون',freshness('amazon')?'قراءة حديثة':'غير متاح',current.servers?.amazon?'آخر قراءة: '+date(current.servers.amazon.at):'بانتظار اتصال الخادم',freshness('amazon')?'ok':'dim'],['جهاز خالد',freshness('wsl')?'قراءة حديثة':'غير متاح',current.servers?.wsl?'آخر قراءة: '+date(current.servers.wsl.at):'بانتظار عميل الحالة',freshness('wsl')?'ok':'dim'],...Object.keys(names).map(app=>{const a=current.apps[app];return ['بوت '+names[app],a.halted?'إيقاف كامل':a.active===true?'يعمل':a.active===false?'موقوف':'غير معروف',a.jobs?'حالات المهام من قاعدة البوت':'لا توجد قراءة لقاعدة المهام',a.active===true?'ok':'dim'];})];
 $('greet').textContent='أهلًا يا خالد';$('verdict').textContent='الخادمان والبوتان، أمامك.';$('verdict-sub').textContent='كل مقطع ينتظر موافقتك. حالة التشغيل تظهر من السجلات، والبيانات الغائبة غير متاحة.';
 $('health').innerHTML=health.map(h=>'<div class="hc '+h[3]+'"><small>'+esc(h[0])+'</small><b>'+esc(h[1])+'</b><span>'+esc(h[2])+'</span></div>').join('');
 $('bots').innerHTML=Object.keys(names).map(app=>{const a=current.apps[app],w=workspace(app),registered=a.agents.length,total=(current.agent_roles||[]).length,passed=a.agents.filter(x=>['PASS','SUCCEEDED'].includes(x.state)).length,last=w.previews[0];
 const stages=[['الإنتاج',a.latest_job_state?state(a.latest_job_state):'لم يُسجّل'],['الفحوص',a.agents.some(x=>x.state!=='UNKNOWN')?passed.toLocaleString('ar-SA')+' اجتاز آخر رصد':'غير معروف'],['موافقتك',last?state(last.state):'لا معاينة مسجّلة'],['الإرسال',a.clips.some(c=>c.state==='ACK')?'تأكيد مسجّل':'لا تأكيد مسجّل']];
 const facts=[['الوكلاء المسجّلون',registered+'/'+total,'info'],['اجتاز آخر رصد',a.agents.some(x=>x.state!=='UNKNOWN')?passed+'/'+total:'غير معروف','dim'],['طلبات اللوحة',w.actions.length?w.actions.length.toLocaleString('ar-SA'):'لم يُسجّل','dim'],[app==='status'?'نافذة واتساب':'المستمع',app==='status'?(a.window==='open'?'مفتوحة':a.window==='closed'?'مغلقة':'غير معروفة'):a.active===true?'يعمل':a.active===false?'موقوف':'غير معروف','dim']];
 return '<article class="panel bot"><div class="bot-top">'+ring(registered,total,'signal')+'<div class="t"><span class="eyebrow">'+(app==='snap'?'تيليجرام':'واتساب')+'</span><h2>بوت '+names[app]+' الأساسي</h2><span>'+esc(a.halted?'الإرسال موقوف بالكامل':a.active===true?'المستمع يعمل':a.active===false?'المستمع موقوف':'حالة المستمع غير معروفة')+'</span><span class="muted">التسجيل لا يثبت نجاح التشغيل</span></div></div><div class="facts">'+facts.map(f=>'<div class="fact"><small>'+esc(f[0])+'</small><b class="'+f[2]+'">'+esc(f[1])+'</b></div>').join('')+'</div><div><div class="head" data-layout="ref-8"><h3>مراحل المقطع · آخر دليل متاح</h3><span class="mono muted">'+esc(last?.sha?.slice(0,8)||'')+'</span></div><ol class="trail">'+stages.map(s=>'<li><i></i><b>'+esc(s[0])+'</b><span>'+esc(s[1])+'</span></li>').join('')+'</ol></div></article>';
 }).join('');
 const decisions=Object.keys(names).flatMap(app=>workspace(app).previews.filter(p=>p.state==='REVIEW').map(p=>({app,p})));
 $('decisions').innerHTML=decisions.map(({app,p})=>'<div class="dec"><h3>مراجعة مقطع '+names[app]+'</h3>'+chip('قرارك','warn')+'<p>المعاينة محفوظة ببصمتها. افتح الإنتاج لمراجعة المقطع.</p><button data-jump="production">راجع المقطع</button></div>').join('')||'<div class="dec"><h3>لا قرارات مسجّلة تنتظرك</h3>'+chip('بانتظار دليل','dim')+'<p>تظهر المعاينات التي تنتظر موافقتك هنا.</p></div>';
 $('road').innerHTML=Object.keys(names).map(app=>{const w=workspace(app),steps=[['طلبات محفوظة',w.actions.length>0],['معاينات مسجّلة',w.previews.length>0],['قرار خالد',w.previews.some(p=>p.state==='APPROVED')],['تأكيد إرسال',current.apps[app].clips.some(p=>p.state==='ACK')]];return '<div class="track"><h3>'+names[app]+'<span class="num muted" data-layout="ref-9">حسب السجل المتاح</span></h3><ol>'+steps.map(s=>'<li class="'+(s[1]?'ok':'todo')+'">'+(s[1]?'✓ ':'')+s[0]+'</li>').join('')+'</ol></div>';}).join('');
 $('alerts').innerHTML=health.filter(h=>h[3]==='dim').map(h=>'<div class="al warn"><b>'+esc(h[0])+'</b><small>'+esc(h[2])+'</small></div>').join('')||'<p class="muted">لا تنبيهات اتصال في آخر قراءة.</p>';
}
const panelTabs=[['overview','الرئيسية'],['production','الإنتاج'],['upload','رفع مقطع'],['sources','المصادر'],['schedule','الجدول'],['servers','الخادمين'],['agents','الوكلاء'],['archive','الأرشيف والأداء']];
let paletteItems=[],paletteIndex=0,paletteFocus=null;
function openPalette(){paletteFocus=document.activeElement;$('pal').hidden=false;$('pal-q').value='';renderPalette();$('pal-q').focus();}
function closePalette(){$('pal').hidden=true;paletteFocus?.focus();}
function renderPalette(){const q=$('pal-q').value.normalize('NFKC').trim();const items=[...panelTabs.map(([key,title])=>({title,kind:'تبويب',go:()=>show(key)})),...(current?.commands||[]).map(c=>({title:c.number+' · '+c.label,kind:'أمر',go:()=>{show('production');$('command-search').value=c.label;renderCommands();}})),...(current?.agent_roles||[]).map(a=>({title:a.name,kind:a.chain?'وكيل · '+a.step:'أداة إشراف',go:()=>{show('agents');sel=a.id;renderAgents();}}))];paletteItems=items.filter(i=>!q||i.title.normalize('NFKC').includes(q)).slice(0,40);paletteIndex=0;$('pal-list').innerHTML=paletteItems.map((x,i)=>'<li role="option" data-palette="'+i+'" aria-selected="'+(i===0)+'"><span>'+esc(x.title)+'</span><small>'+esc(x.kind)+'</small></li>').join('')||'<li>لا نتائج</li>';}
function markPalette(){document.querySelectorAll('[data-palette]').forEach(el=>el.setAttribute('aria-selected',Number(el.dataset.palette)===paletteIndex));}
$('open-pal').onclick=openPalette;$('pal-q').oninput=renderPalette;
$('pal').onclick=e=>{if(e.target===$('pal'))closePalette();const item=e.target.closest('[data-palette]');if(item){const chosen=paletteItems[Number(item.dataset.palette)];closePalette();chosen.go();}};
$('pal-q').onkeydown=e=>{if(e.key==='ArrowDown'||e.key==='ArrowUp'){e.preventDefault();paletteIndex=Math.max(0,Math.min(paletteItems.length-1,paletteIndex+(e.key==='ArrowDown'?1:-1)));markPalette();}else if(e.key==='Enter'&&paletteItems[paletteIndex]){e.preventDefault();const chosen=paletteItems[paletteIndex];closePalette();chosen.go();}else if(e.key==='Tab'){e.preventDefault();}};
addEventListener('keydown',e=>{if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='k'){e.preventDefault();$('pal').hidden?openPalette():closePalette();}else if(e.key==='Escape'&&!$('pal').hidden)closePalette();});
document.addEventListener('click',e=>{const jump=e.target.closest('[data-jump]');if(jump)show(jump.dataset.jump);});
