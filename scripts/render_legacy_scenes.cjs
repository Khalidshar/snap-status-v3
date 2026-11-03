// Offline old-factory scene timeline. Chromium renders every animation frame.
const fs=require('node:fs/promises'),path=require('node:path');
const {chromium}=require('playwright');
const digits=s=>String(s||'').replace(/\d/g,d=>'٠١٢٣٤٥٦٧٨٩'[d]);
const esc=s=>String(s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
(async()=>{
 const [input,root,out]=process.argv.slice(2),value=JSON.parse(await fs.readFile(input,'utf8'));
 const p=value.legacy_production;if(!p||p.scenes.length>18||p.duration<(p.app==='snap-bot-v2'?61:32)||p.duration>(p.app==='snap-bot-v2'?90:58))throw Error('plan');
 const repo=path.resolve(__dirname,'..');
 const fonts={body:'assets/fonts/arabeyes/ae_AlMohanad.ttf',naskh:'assets/fonts/notonaskharabic/NotoNaskhArabic-Regular.ttf',quran:'assets/fonts/amiri/AmiriQuran.ttf',emoji:'assets/fonts/notoemoji/NotoEmoji.ttf'};
 let fontcss='';for(const [family,file] of Object.entries(fonts))fontcss+=`@font-face{font-family:${family};src:url(data:font/ttf;base64,${(await fs.readFile((path.isAbsolute(file)?file:path.join(repo,file)))).toString('base64')})}`;
 const images={};for(const s of p.scenes)if(s.image){if(!/^assets\/[a-zA-Z0-9_.-]+$/.test(s.image))throw Error('imagepath');images[s.image]='data:image/jpeg;base64,'+(await fs.readFile(path.join(root,s.image))).toString('base64');}
 const css=`${fontcss}*{box-sizing:border-box}html,body{margin:0;width:1080px;height:1920px;background:transparent;overflow:hidden}body{color:#f6eede;font-family:body,emoji;direction:rtl}.scene{position:absolute;inset:24px 20px 24px;display:flex;flex-direction:column;justify-content:center;align-items:center;gap:12px;text-align:center;opacity:0}.card{width:100%;flex:1;min-height:0;display:flex;flex-direction:column;justify-content:center;gap:18px;padding:20px;border:2px solid #d6b26c66;border-radius:30px;background:#051e16d9}h1,h2,p{flex-shrink:0;margin:0;white-space:pre-wrap;overflow-wrap:anywhere}h1{font-size:94px;line-height:1.4}h2{font-size:80px;line-height:1.5}.body{font-size:72px;line-height:1.65}.withpic .body{font-size:64px}.withpic h2{font-size:80px}.hook .body{font-size:130px;line-height:1.5;color:#d6b26c}.hook .src{font-size:56px;color:#f6eede}.withpic .titlebody{font-size:75px}.quran{font-family:quran;font-size:90px;line-height:1.8}.tafsir{font-family:naskh,emoji;font-size:55px;line-height:1.65;color:#f6eede}.label,.src{font-family:naskh,emoji;font-size:30px;color:#d6b26c;white-space:pre-wrap}.count{font-size:42px;color:#d6b26c}.pic{width:100%;height:820px;object-fit:cover;object-position:center 30%;border-radius:20px;background:#05140f}.imagebox{flex-shrink:0;width:100%;position:relative;overflow:hidden;border-radius:20px}.withpic .body{min-height:1.65em;max-height:none}.credit{max-width:90%;white-space:normal;overflow-wrap:anywhere;font-family:naskh,emoji;font-size:26px;background:#051a12d9;padding:8px 15px;position:absolute;bottom:10px;left:10px;border-radius:8px}.timer{font-size:100px;border:9px solid #d6b26c;border-radius:50%;width:180px;height:180px;line-height:160px;color:#d6b26c;margin:50px auto 0}.role-question .body{font-size:100px}.role-answer h2{font-size:72px}.role-answer .body{font-size:120px;color:#d6b26c}.punch,.answer{font-size:70px;color:#d6b26c;margin-top:24px}.bars{display:flex;align-items:center;justify-content:center;height:160px;gap:8px}.bars i{display:block;width:12px;border-radius:8px;background:#d6b26c}.progress{position:absolute;bottom:30px;left:75px;height:8px;background:#d6b26c;transform-origin:right;width:930px}.cta h1{font-size:120px}.handle{font:64px naskh;color:#d6b26c;direction:ltr}`;
 const sacred=p.effects===false,show=s=>esc(sacred?s:digits(s));
 let scenes='';p.scenes.forEach((s,i)=>{if(s.role==='cta'){scenes+=`<section data-i="${i}" class="scene cta"><div class="card"><h1>تابعني لكل جديد</h1><div class="handle">@rawwag_khalid</div></div></section>`;return;}const picture=s.image?`<div class="imagebox"><img class="pic" src="${images[s.image]}"><span class="credit">مصدر الصورة: ${esc(s.image_credit)}</span></div>`:'';scenes+=`<section data-i="${i}" class="scene role-${s.role||""} ${s.role==="hook"?"hook":""} ${s.image?'withpic':''}"><div class="count">${show([s.bulletin_title,s.number].filter(Boolean).join(' • '))}</div><div class="card">${picture}${s.head?`<h2>${show(s.head)}</h2>`:''}<p class="body ${s.verse?'quran':''} ${s.role==='headline'?'titlebody':''}">${s.verse?esc(s.body):show(s.body)}</p>${s.tafsir?`<p class="tafsir">${esc(s.tafsir)}</p><div class="label">التفسير الميسّر</div>`:''}${s.punch?`<p class="punch">${show(s.punch)}</p>`:''}${s.countdown?'<div class="timer"></div>':''}${s.levels?'<div class="bars">'+Array(41).fill('<i></i>').join('')+'</div>':''}<p class="src">${show(s.source||'')}${s.published_label?'\n'+show(s.published_label):''}</p></div></section>`});
 const doc=`<!doctype html><html lang="ar" dir="rtl"><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; font-src data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'"><style>${css}</style>${scenes}<div class="progress"></div></html>`;
 await fs.mkdir(out,{recursive:true});await fs.writeFile(path.join(out,'page.html'),doc);
 const browser=await chromium.launch({headless:true,...(process.env.MADAR_CHROMIUM?{executablePath:process.env.MADAR_CHROMIUM}:{}),args:['--no-sandbox','--disable-background-networking','--no-proxy-server']});
 const reports=[];
 try{
  const count=Math.ceil(p.duration*15),workers=2;
  await Promise.all(Array.from({length:workers},(_,worker)=>(async()=>{
   const page=await browser.newPage({viewport:{width:1080,height:1920},deviceScaleFactor:1});
   await page.route('**/*',r=>r.request().url().startsWith('data:')?r.continue():r.abort());
   await page.setContent(doc);await page.evaluate(()=>document.fonts.ready);
   const visual=await page.evaluate((input)=>{
    const output=[];let overflow=0,tiny=0;
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
     if(!input.sacred&&!source.verse){
      // One card per item: a long text ends at a full sentence, never mid-sentence on a second card.
      const e=base.cloneNode(true);document.body.append(e);const b=e.querySelector('.body');
      if(b&&!fits(e)){
       const full=b.textContent;let done=false;
       for(const m of [...full.matchAll(/[.!؟?…،؛](?=\s)/gu)].reverse()){b.textContent=full.slice(0,m.index+1).replace(/[،؛]$/,'…');if(fits(e)){done=true;break;}}
       if(!done){const words=full.split(/\s+/);while(words.length>3&&!fits(e)){words.pop();b.textContent=words.join(' ')+'…';}}
      }
      chunks.push(e);
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
     let cursor=source.at;
     for(let k=0;k<chunks.length;k++){
      const chunk=chunks[k];
      const span=source.verse&&versePages
       ?k<versePages?(source.audio_duration+.15)/versePages:(source.seconds-source.audio_duration-.15)/(chunks.length-versePages)
       :source.seconds/chunks.length;
      grow(chunk);
      const copies=Math.max(1,Math.ceil(span/12));
      for(let j=0;j<copies;j++){
       const e=j===0?chunk:chunk.cloneNode(true);if(j>0)document.body.append(e);
       e.dataset.i=String(output.length);e.style.opacity=0;
       output.push({...source,origin:source.at,at:cursor,seconds:span/copies});cursor+=span/copies;
       if(!fits(e))overflow++;
       for(const n of e.querySelectorAll('h2,.body,.tafsir'))if(n.textContent.trim()){
        const minimum=n.classList.contains('quran')?87.5:n.tagName==='H2'||n.classList.contains('titlebody')?75:55;
        if(parseFloat(getComputedStyle(n).fontSize)<minimum)tiny++;
       }
      }
     }
    }
    return {scenes:output,overflow,tiny,empty:[...document.querySelectorAll('.scene')].filter(s=>!s.innerText.trim()).length,handle:document.querySelector('.handle')?.innerText==='@rawwag_khalid'};
   },{scenes:p.scenes,sacred});
   const {scenes:timeline,...report}=visual;
   reports.push(report);if(report.overflow||report.empty||report.tiny)throw Error('overflow');
   for(let i=worker;i<count;i+=workers){const t=i/15;await page.evaluate(({t,scenes,duration})=>{for(const [i,s]of scenes.entries()){const e=document.querySelector(`[data-i="${i}"]`),dt=t-s.at,visible=dt>=0&&dt<s.seconds;const fade=Math.min(1,Math.max(0,(s.seconds-dt)/.3));e.style.opacity=visible?fade:0;e.style.transform=`translateY(${(i===0?0:Math.max(0,1-dt/.45)*45)}px)`;if(s.image){e.querySelector('.pic').style.transform=`scale(${1+Math.max(0,Math.min(1,dt/s.seconds))*.08})`;}if(s.countdown){const n=Math.floor(t-s.origin-(s.countdown_after||0));e.querySelector('.timer').textContent=n>=0&&n<5?String(5-n).replace(/\d/g,x=>'٠١٢٣٤٥٦٧٨٩'[x]):'';}if(s.punch)e.querySelector('.punch').style.opacity=t-s.origin>=(s.reveal_after||0)?1:0;if(s.levels){const v=s.levels[Math.max(0,Math.floor((t-s.origin)*15))]||0;for(const [k,bar]of [...e.querySelectorAll('.bars i')].entries()){const edge=Math.abs(k-20)/21;bar.style.height=(8+v*140*(1-edge*.65)*(.8+.2*Math.sin(k*1.7+t*6)))+'px';}}}document.querySelector('.progress').style.transform=`scaleX(${t/duration})`;},{t,scenes:timeline,duration:p.duration});await page.screenshot({path:path.join(out,String(i).padStart(5,'0')+'.png'),omitBackground:true});}
   await page.close();
  })()));
 }finally{await browser.close();}
 await fs.writeFile(path.join(out,'layout.json'),JSON.stringify({overflow:reports.reduce((n,r)=>n+r.overflow,0),tiny:reports.reduce((n,r)=>n+r.tiny,0),empty:reports.reduce((n,r)=>n+r.empty,0),handle:reports.every(r=>r.handle),frames:Math.ceil(p.duration*15),fps:15}));
})().catch(e=>{process.stderr.write('LEGACY_CHROMIUM_RENDER_FAILED '+String(e.message).replace(/[^A-Za-z0-9 _-]/g,'').slice(0,100)+'\n');process.exitCode=1});
