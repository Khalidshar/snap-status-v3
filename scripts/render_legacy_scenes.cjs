// Offline old-factory scene timeline. Chromium renders every animation frame.
const fs=require('node:fs/promises'),path=require('node:path');
const {chromium}=require('playwright');
const digits=s=>String(s||'').replace(/\d/g,d=>'٠١٢٣٤٥٦٧٨٩'[d]);
const esc=s=>String(s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
(async()=>{
 const [input,root,out,designFile]=process.argv.slice(2),value=JSON.parse(await fs.readFile(input,'utf8'));
 // The designer agent's decision (production/designer.py); without one, the approved default.
 const design=designFile?JSON.parse(await fs.readFile(designFile,'utf8')):{style:'solid'};
 const p=value.legacy_production;if(!p||p.scenes.length>18||p.duration<(p.app==='snap-bot-v2'?61:32)||p.duration>(p.app==='snap-bot-v2'?(['verse','dua','friday','kahf','fast','isha'].includes(p.key)?120:90):58))throw Error('plan');
 const repo=path.resolve(__dirname,'..');
 const fonts={body:'assets/fonts/arabeyes/ae_AlMohanad.ttf',naskh:'assets/fonts/notonaskharabic/NotoNaskhArabic-Regular.ttf',quran:'assets/fonts/amiri/AmiriQuran.ttf',hafs:'assets/fonts/kfgqpc/kfgqpc_hafs_v30.ttf',emoji:'assets/fonts/notoemoji/NotoEmoji.ttf'};
 let fontcss='';for(const [family,file] of Object.entries(fonts))fontcss+=`@font-face{font-family:${family};src:url(data:font/ttf;base64,${(await fs.readFile((path.isAbsolute(file)?file:path.join(repo,file)))).toString('base64')})}`;
 const images={};for(const s of p.scenes)if(s.image){if(!/^assets\/[a-zA-Z0-9_.-]+$/.test(s.image))throw Error('imagepath');images[s.image]='data:image/jpeg;base64,'+(await fs.readFile(path.join(root,s.image))).toString('base64');}
 const css=`${fontcss}*{box-sizing:border-box}html,body{margin:0;width:1080px;height:1920px;background:transparent;overflow:hidden}body{color:#f6eede;text-shadow:0 3px 18px #000,0 0 6px #000,0 1px 3px #000;font-family:body,emoji;direction:rtl}.scene{position:absolute;inset:150px 20px 24px;display:flex;flex-direction:column;justify-content:center;align-items:center;gap:12px;text-align:center;opacity:0}.card{width:100%;flex:1;min-height:0;display:flex;flex-direction:column;justify-content:center;gap:18px;padding:20px;border:0;border-radius:30px;background:transparent}h1,h2,p{flex-shrink:0;margin:0;white-space:pre-wrap;overflow-wrap:anywhere}h1{font-size:94px;line-height:1.4}h2{font-size:80px;line-height:1.5}.body{font-size:72px;line-height:1.65}.scene.withpic{inset:0;padding:150px 20px 24px;background:#041a12}.scene.withpic.mix{inset:150px 20px 24px;padding:0;background:transparent}.withpic.mix .card{background:#06140fb8;border:2px solid #d6b26c55}.withpic .body{font-size:64px}.withpic h2{font-size:80px}.hook .body{font-size:130px;line-height:1.5;color:#d6b26c}.hook .src{font-size:56px;color:#f6eede}.withpic .titlebody{font-size:75px}.quran{font-family:quran;font-size:90px;line-height:1.8}.tafsir{font-family:naskh,emoji;font-size:55px;line-height:1.65;color:#f6eede}.label,.src{font-family:naskh,emoji;font-size:30px;color:#d6b26c;white-space:pre-wrap}.count{font-size:42px;color:#d6b26c}.pic{width:100%;height:760px;object-fit:cover;object-position:center 30%;border-radius:20px;background:#05140f}.imagebox{flex-shrink:0;width:100%;position:relative;overflow:hidden;border-radius:20px}.withpic .body{min-height:1.65em;max-height:none}.snapbadge{position:absolute;top:14px;right:14px;direction:ltr;font-family:naskh,emoji;font-size:30px;font-weight:700;color:#D6B26C;background:#062B21e6;border:2px solid #D6B26C;padding:4px 16px;border-radius:22px;text-shadow:none}.credit{max-width:90%;white-space:normal;overflow-wrap:anywhere;font-family:naskh,emoji;font-size:26px;background:#051a12d9;padding:8px 15px;position:absolute;bottom:10px;left:10px;border-radius:8px}.timer{font-size:100px;border:9px solid #d6b26c;border-radius:50%;width:180px;height:180px;line-height:160px;color:#d6b26c;margin:50px auto 0}.role-question .body{font-size:100px}.role-answer h2{font-size:78px}.role-answer .body{font-size:120px;color:#d6b26c}.punch,.answer{font-size:70px;color:#d6b26c;margin-top:24px}.bars{display:flex;align-items:center;justify-content:center;height:160px;gap:8px}.bars i{display:block;width:12px;border-radius:8px;background:#d6b26c}.progress{position:absolute;bottom:30px;left:75px;height:8px;background:#d6b26c;transform-origin:right;width:930px}.cta h1{font-size:112px}.top{position:absolute;top:26px;left:24px;right:24px;display:flex;justify-content:space-between;align-items:center;z-index:5;text-shadow:none;white-space:nowrap}.top .handle{font:700 58px naskh;direction:ltr;color:#fff;background:#062b21e6;border:2px solid #d6b26c;border-radius:40px;padding:8px 30px}.top .handle b{color:#fffc00}.kind{font:700 58px naskh;color:#fff;background:#0b3d2ee6;border:2px solid #d6b26c;border-radius:16px;padding:8px 28px}.kind.live{background:#ff3b30;border-color:#ff3b30}.kind .dot{display:inline-block;width:22px;height:22px;border-radius:50%;background:#fff;margin-left:10px}.color{font-family:'Noto Color Emoji',emoji}.cta .ghost{font-size:210px;line-height:1}.cta .user{font:700 64px naskh;direction:ltr;color:#062b21;background:linear-gradient(90deg,#f3d58a,#d6b26c);box-shadow:0 0 40px #d6b26c88;border-radius:26px;padding:6px 40px 14px;text-shadow:none}.cta .line{font:700 44px naskh;color:#f6eede}.handle{font:64px naskh;color:#d6b26c;direction:ltr}.tilawa .card{background:transparent;border:0;position:relative}.phr{position:absolute;left:30px;right:30px;top:50%;transform:translateY(-50%);margin:0;text-align:center;font-family:hafs;font-size:104px;line-height:1.9;color:#f4ecd8;opacity:0;text-shadow:0 2px 14px rgba(0,0,0,.85),0 0 4px rgba(0,0,0,.6)}.sacredclip .card{background:transparent!important;border:0!important}.card{position:relative}.num{display:inline-block;text-align:center;background:#d6b26c3d;box-shadow:0 0 0 5px #d6b26c3d;border-radius:12px}.trend{position:absolute;top:18px;left:26px;width:0;height:0;border-left:40px solid transparent;border-right:40px solid transparent;opacity:0}.trend.up{border-bottom:68px solid #2ecc71}.trend.down{border-top:68px solid #e74c3c}.key{color:#ffd36b}.hook .key,.role-answer .key{color:inherit}`;
 const sacred=p.effects===false,show=s=>esc(sacred?s:digits(s));
 const kinds={news:'📰 موجز الأخبار',tech:'📱 تقنية',style:'👗 موضة وجمال',riddle:'🧠 لغز اليوم',funny:'😂 مواقف',facts:'💡 هل تعلم',did_you_know:'💡 هل تعلم',verse:'🕊 وقفة تدبّر',adhkar_am:'🌅 أذكار الصباح',adhkar_pm:'🌇 أذكار المساء',hadith:'🕌 حديث اليوم',night:'🌙 قبل ما تنام',history:'📜 حدث اليوم',brief:'☀️ يومك في دقيقة',urgent:'عاجل',saudi:'🌴 السعودية',world:'🌍 العالم',football:'⚽ كرة القدم',markets:'📈 الأسواق',news_noon:'📰 أخبار الظهر',dua:'🤲 دعاء اليوم',friday:'🕌 رسالة الجمعة',kahf:'📖 سورة الكهف',fast:'🌙 تذكير الصيام',isha:'🌙 ليلة طيبة'};
 const kind=kinds[p.key]||'';
 const top=`<div class="top"><div class="handle"><span class="color">👻</span> <b>@rawwag_khalid</b></div>${kind?`<div class="kind ${p.key==='urgent'?'live':''}">${p.key==='urgent'?'<span class="dot"></span>':''}${esc(kind).replace(/^(\S+) /u,'<span class="color">$1</span> ')}</div>`:''}</div>`;
 let scenes='';p.scenes.forEach((s,i)=>{if(s.role==='cta'){scenes+=`<section data-i="${i}" class="scene cta"><div class="card"><div class="ghost color">👻</div><h1>تابعني لكل جديد</h1><div class="user handle">@rawwag_khalid</div><p class="line">${p.key==="news"?"لا تفوتك الأخبار أول بأول":"كل يوم شي جديد يستاهل"}</p></div></section>`;return;}if(s.phrases){const w=s.body.split(' '),mark=w[w.length-1].startsWith('۝')?w.pop():'';scenes+=`<section data-i="${i}" class="scene tilawa"><div class="card">${s.phrases.map((f,k)=>`<p class="phr" data-k="${k}">${esc(w.slice(f.words[0],f.words[1]+1).join(' ')+(k===s.phrases.length-1&&mark?' '+mark:''))}</p>`).join('')}</div></section>`;return;}const picture=s.image?`<div class="imagebox"><img class="pic" src="${images[s.image]}"><span class="credit">مصدر الصورة: ${esc(s.image_credit)}</span></div>`:'';scenes+=`<section data-i="${i}" class="scene role-${s.role||""} ${s.role==="hook"?"hook":""} ${s.image?"withpic":""} ${(s.design||(design.style==="mix"?"mix":""))==="mix"?"mix":""}"><div class="count">${show([s.bulletin_title,s.number].filter(Boolean).join(' • '))}</div><div class="card">${picture}${s.head?`<h2>${show(s.head)}</h2>`:''}<p class="body ${s.verse?'quran':''} ${s.role==='headline'?'titlebody':''}">${s.verse?esc(s.body):show(s.body)}</p>${s.tafsir?`<p class="tafsir">${esc(s.tafsir)}</p><div class="label">التفسير الميسّر</div>`:''}${s.punch?`<p class="punch">${show(s.punch)}</p>`:''}${s.countdown?'<div class="timer"></div>':''}${s.levels?'<div class="bars">'+Array(41).fill('<i></i>').join('')+'</div>':''}<p class="src">${show(s.source||'')}${s.published_label?'\n'+show(s.published_label):''}</p></div></section>`});
 const doc=`<!doctype html><html lang="ar" dir="rtl"><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; font-src data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'"><style>${css}</style><body class="${sacred?'sacredclip':''}">${scenes}${top}<div class="progress"></div></body></html>`;
 await fs.mkdir(out,{recursive:true});await fs.writeFile(path.join(out,'page.html'),doc);
 const browser=await chromium.launch({headless:true,...(process.env.MADAR_CHROMIUM?{executablePath:process.env.MADAR_CHROMIUM}:{}),args:['--no-sandbox','--disable-background-networking','--no-proxy-server']});
 const reports=[];
 try{
  const count=Math.ceil(p.duration*15),workers=Math.max(1,Math.min(8,parseInt(process.env.MADAR_RENDER_WORKERS||"2",10)||2));
  await Promise.all(Array.from({length:workers},(_,worker)=>(async()=>{
   const page=await browser.newPage({viewport:{width:1080,height:1920},deviceScaleFactor:1});
   await page.route('**/*',r=>r.request().url().startsWith('data:')?r.continue():r.abort());
   await page.setContent(doc);await page.evaluate(()=>document.fonts.ready);
   const visual=await page.evaluate((input)=>{
    const output=[],bad=[],cut=[];let overflow=0,tiny=0;
    const fitsAll=e=>{const c=e.querySelector('.card');return c.scrollHeight<=c.clientHeight+2&&e.scrollHeight<=e.clientHeight+2&&c.scrollWidth<=c.clientWidth+2&&e.scrollWidth<=e.clientWidth+2;};
    const grow=e=>{if(e.classList.contains('hook'))return;const ns=[...e.querySelectorAll('h2,.body,.tafsir,.punch')].filter(n=>n.textContent.trim());if(!ns.length)return;const base=ns.map(n=>parseFloat(getComputedStyle(n).fontSize));let k=1;for(let s=1.05;s<=1.6;s+=.05){ns.forEach((n,i)=>n.style.fontSize=base[i]*s+'px');if(ns.some((n,i)=>base[i]*s>(n.classList.contains('quran')?115:110))||!fitsAll(e))break;k=s;}ns.forEach((n,i)=>n.style.fontSize=base[i]*k+'px');};
    const originals=[...document.querySelectorAll(".scene")];
    for(const [index,source] of input.scenes.entries()){
     const original=originals[index];
     const base=original.cloneNode(true);const chunks=[];let versePages=0;
     const fields=['h2','.body','.tafsir','.punch'];
     const parts=fields.map(selector=>({selector,text:base.querySelector(selector)?.textContent||''}));
     const make=()=>{const e=base.cloneNode(true);for(const f of parts){const n=e.querySelector(f.selector);if(n)n.textContent='';}document.body.append(e);return e;};
     const fits=e=>{const c=e.querySelector('.card');const b=e.querySelector('.body');return c.scrollHeight<=c.clientHeight+2&&e.scrollHeight<=e.clientHeight+2&&c.scrollWidth<=c.clientWidth+2&&e.scrollWidth<=e.clientWidth+2;};
     const plan=input.cards[index]||{};
     if(plan.size){const n=base.querySelector('.body');if(n)n.style.fontSize=plan.size+'px';}
     if(source.phrases){const e=base.cloneNode(true);document.body.append(e);chunks.push(e);}else if(!input.sacred&&!source.verse){
      // One card per item: a long text ends at a full sentence, never mid-sentence on a second card.
      const e=base.cloneNode(true);document.body.append(e);const b=e.querySelector('.body');
      // Designer (8 Nov 2026): a long text may take two pages split at a sentence end, not a cut.
      let split=false;
      if(b&&!fits(e)&&plan.pages===2){
       const full=b.textContent,ends=[...full.matchAll(/[.!؟?…؛](?=\s)/gu)].map(m=>m.index+1);
       const order=ends.sort((x,y)=>Math.abs(x-full.length/2)-Math.abs(y-full.length/2));
       for(const at of order){
        const e2=base.cloneNode(true);document.body.append(e2);
        b.textContent=full.slice(0,at).trim();e2.querySelector('.body').textContent=full.slice(at).trim();
        if(fits(e)&&fits(e2)){chunks.push(e,e2);split=true;break;}
        e2.remove();
       }
       if(!split)b.textContent=full;
      }
      if(!split){
      if(b&&!fits(e)){
       const full=b.textContent;let done=false;
       for(const m of [...full.matchAll(/[.!؟?…،؛](?=\s)/gu)].reverse()){b.textContent=full.slice(0,m.index+1).replace(/[،؛]$/,'…');if(fits(e)){done=true;break;}}
       if(!done){const words=full.split(/\s+/);while(words.length>3&&!fits(e)){words.pop();b.textContent=words.join(' ')+'…';}}
       if(b.textContent!==full)cut.push(index);
      }
      chunks.push(e);
      }
     }else{
     let current=make();
     for(const field of parts){
      if(source.verse&&field.selector==='.tafsir'&&field.text){chunks.push(current);versePages=chunks.length;current=make();}
      for(const word of field.text.match(/\S+\s*/gu)||[]){
       let node=current.querySelector(field.selector);const previous=node.textContent;node.textContent+=word;
       if(!fits(current)){
        node.textContent=previous;
        if(!fields.some(f=>current.querySelector(f)?.textContent.trim()))throw Error('unpageable');
        chunks.push(current);current=make();node=current.querySelector(field.selector);node.textContent=word;
        if(!fits(current))throw Error('unpageable');
       }
      }
     }
     chunks.push(current);}
     original.remove();
     let cursor=source.at,offset=0;
     for(let k=0;k<chunks.length;k++){
      const chunk=chunks[k];
      const span=source.verse&&versePages
       ?k<versePages?(source.audio_duration+.15)/versePages:(source.seconds-source.audio_duration-.15)/(chunks.length-versePages)
       :source.seconds/chunks.length;
      grow(chunk);
      // Owner decision 8 Nov 2026: word timing under speech, explanatory motion in general clips.
      // Display only: the text stays exactly as laid out above; never on Quran or tafsir.
      const b=chunk.querySelector('.body');
      const speech=source.voice_kind==='speech'&&source.audio_duration>0,motion=!input.sacred&&!source.verse&&source.role!=='cta';
      if(b&&!source.verse&&(speech||motion)){
       const text=b.textContent;b.textContent='';let wi=0;
       for(const w of text.split(/(\s+)/u)){
        if(!w)continue;
        if(/^\s+$/u.test(w)){b.append(w);offset+=w.length;continue;}
        const sp=document.createElement('span');sp.className='w';sp.textContent=w;sp.dataset.a=offset;sp.dataset.z=offset+w.length;sp.dataset.n=wi++;
        const bare=w.replace(/[^\p{L}\p{N}]/gu,'');
        if(motion&&(plan.keys||[]).some(k=>k.split(/\s+/u).some(part=>part.length>2&&(bare===part||bare.endsWith(part)&&bare.length-part.length<=3))))sp.classList.add('key');
        const m=motion&&w.match(/^(\D*?)([0-9٠-٩]+(?:[.,٫٬][0-9٠-٩]+)*)(.*)$/u);
        if(m){
         const raw=m[2].replace(/[٠-٩]/gu,d=>'٠١٢٣٤٥٦٧٨٩'.indexOf(d)).replace(/٫/gu,'.').replace(/[,٬]/gu,'');
         const v=parseFloat(raw),year=/^\d{4}$/.test(raw)&&v>=1300&&v<=2100;
         sp.classList.add('num');sp.dataset.o=w;
         if(isFinite(v)&&v>0&&!year){sp.dataset.v=v;sp.dataset.d=(raw.split('.')[1]||'').length;sp.dataset.pre=m[1];sp.dataset.post=m[3];sp.dataset.sep=m[2].includes('٫')?'٫':'.';sp.dataset.ar=/[٠-٩]/u.test(m[2])?'1':'';}
        }
        b.append(sp);offset+=w.length;
       }
       b.dataset.words=wi;if(getComputedStyle(b).color==='rgb(214, 178, 108)')b.dataset.gold='1';
       for(const n of b.querySelectorAll('.num'))n.style.minWidth=n.getBoundingClientRect().width+'px';
       if(motion&&input.key==='markets'){
        const up=/ارتفع|ارتفاع|صعد|صعود|مكاسب|يرتفع|قفز/u.test(text),down=/انخفض|انخفاض|تراجع|هبط|هبوط|خسائر|يتراجع/u.test(text);
        if(up!==down){const tr=document.createElement('div');tr.className='trend '+(up?'up':'down');chunk.querySelector('.card').append(tr);}
       }
      }
      const cardAt=cursor;
      const copies=Math.max(1,Math.ceil(span/12));
      for(let j=0;j<copies;j++){
       const e=j===0?chunk:chunk.cloneNode(true);if(j>0)document.body.append(e);
       e.dataset.i=String(output.length);e.style.opacity=0;
       output.push({...source,origin:source.at,at:cursor,seconds:span/copies,card:cardAt});cursor+=span/copies;
       if(!fits(e)){overflow++;bad.push(index);}
       for(const n of e.querySelectorAll('h2,.body,.tafsir'))if(n.textContent.trim()){
        const minimum=n.classList.contains('quran')?87.5:n.tagName==='H2'||n.classList.contains('titlebody')?75:55;
        if(parseFloat(getComputedStyle(n).fontSize)<minimum)tiny++;
       }
      }
     }
    }
    return {scenes:output,bad,cut,overflow,tiny,empty:[...document.querySelectorAll('.scene')].filter(s=>!s.innerText.trim()).length,handle:document.querySelector('.cta .handle')?.innerText==='@rawwag_khalid'&&document.querySelector('.top .handle b')?.innerText==='@rawwag_khalid'};
   },{scenes:p.scenes,sacred,key:p.key,cards:(design.type&&design.type.cards)||[]});
   const {scenes:timeline,...report}=visual;
   reports.push(report);if(report.overflow||report.empty||report.tiny)throw Error("overflow "+(report.bad||[]).join(" ")+" tiny "+report.tiny+" empty "+report.empty);
   for(let i=worker;i<count;i+=workers){const t=i/15;await page.evaluate(({t,scenes,duration})=>{for(const [i,s]of scenes.entries()){const e=document.querySelector(`[data-i="${i}"]`),dt=t-s.at,visible=dt>=0&&dt<s.seconds;const fade=Math.min(1,Math.max(0,(s.seconds-dt)/.3));e.style.opacity=visible?fade:0;e.style.transform=`translateY(${(i===0||(e.classList.contains("withpic")&&!e.classList.contains("mix"))?0:Math.max(0,1-dt/.45)*45)}px)`;if(s.image){e.querySelector('.pic').style.transform=`scale(${1+Math.max(0,Math.min(1,dt/s.seconds))*.08})`;}if(s.phrases&&visible){const local=t-s.origin-.15,cycle=s.audio_duration+.6,k=Math.floor(local/cycle),lt=local-k*cycle;for(const ph of e.querySelectorAll('.phr')){const f=s.phrases[+ph.dataset.k],on=local>=0&&k<=(s.replays||0)&&lt>=f.start&&lt<f.end;ph.style.opacity=on?Math.min(1,(lt-f.start+.05)/.25,(f.end-lt)/.2):0;}}const b=visible&&e.querySelector('.body');if(b&&b.dataset.words){const ws=b.querySelectorAll('.w'),dc=t-s.card;if(s.voice_kind==='speech'&&s.audio_duration>0){const total=(s.body||'').length||1,c=(t-s.origin-.15)/s.audio_duration*total;for(const w of ws){const now=c>=+w.dataset.a&&c<+w.dataset.z,said=c>=+w.dataset.z||c>total;w.style.opacity=now||said?1:.55;w.style.color=now?(b.dataset.gold?'#ffffff':'#ffd36b'):'';}}else{const step=Math.min(.06,Math.max(.02,Math.min(1.2,s.seconds*.25)/Math.max(1,+b.dataset.words)));for(const w of ws){const d=+w.dataset.n*step;w.style.opacity=Math.max(0,Math.min(1,(dc-d)/.25));if(w.dataset.v){const q=Math.max(0,Math.min(1,(dc-d-.1)/.8)),ease=1-Math.pow(1-q,3);if(q>=1)w.textContent=w.dataset.o;else{let val=(+w.dataset.v*ease).toFixed(+w.dataset.d).replace('.',w.dataset.sep);if(w.dataset.ar)val=val.replace(/\d/g,x=>'٠١٢٣٤٥٦٧٨٩'[x]);w.textContent=w.dataset.pre+val+w.dataset.post;}w.style.transform=`scale(${1+.18*Math.sin(Math.PI*q)})`;}}}const tr=e.querySelector('.trend');if(tr){tr.style.opacity=Math.max(0,Math.min(1,dc/.4));tr.style.transform=`translateY(${(tr.classList.contains('up')?-1:1)*12*Math.abs(Math.sin(t*2.2))}px)`;}}if(s.countdown){const n=Math.floor(t-s.origin-(s.countdown_after||0));e.querySelector('.timer').textContent=n>=0&&n<5?String(5-n).replace(/\d/g,x=>'٠١٢٣٤٥٦٧٨٩'[x]):'';}if(s.punch)e.querySelector('.punch').style.opacity=t-s.origin>=(s.reveal_after||0)?1:0;if(s.levels){const v=s.levels[Math.max(0,Math.floor((t-s.origin)*15))]||0;for(const [k,bar]of [...e.querySelectorAll('.bars i')].entries()){const edge=Math.abs(k-20)/21;bar.style.height=(8+v*140*(1-edge*.65)*(.8+.2*Math.sin(k*1.7+t*6)))+'px';}}}document.querySelector('.progress').style.transform=`scaleX(${t/duration})`;const k=Math.abs(Math.sin(t*Math.PI/1.2));for(const g of document.querySelectorAll('.ghost'))g.style.transform=`translateY(${-24*k}px) rotate(${-6*k}deg)`;for(const u of document.querySelectorAll('.cta .user'))u.style.transform=`scale(${1+.06*k})`;const dot=document.querySelector('.kind .dot');if(dot)dot.style.opacity=Math.floor(t*2)%2?.15:1;},{t,scenes:timeline,duration:p.duration});await page.screenshot({path:path.join(out,String(i).padStart(5,'0')+'.png'),omitBackground:true});}
   await page.close();
  })()));
 }finally{await browser.close();}
 await fs.writeFile(path.join(out,'layout.json'),JSON.stringify({overflow:reports.reduce((n,r)=>n+r.overflow,0),tiny:reports.reduce((n,r)=>n+r.tiny,0),empty:reports.reduce((n,r)=>n+r.empty,0),handle:reports.every(r=>r.handle),cut:(reports[0]&&reports[0].cut)||[],frames:Math.ceil(p.duration*15),fps:15}));
})().catch(e=>{process.stderr.write('LEGACY_CHROMIUM_RENDER_FAILED '+String(e.message).replace(/[^A-Za-z0-9 _-]/g,'').slice(0,100)+'\n');process.exitCode=1});
