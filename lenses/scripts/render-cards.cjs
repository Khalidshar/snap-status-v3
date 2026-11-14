// Renders transparent PNG cards for the two Snap lenses in the «روّق مع خالد» look.
const fs=require('fs'),path=require('path');const {chromium}=require('playwright');
const [contentFile,fontFile,out]=process.argv.slice(2);
if (!contentFile || !fontFile || !out) {
 console.error('Usage: node lenses/tools/render.cjs <content.json> <ReadexPro.woff> <output-directory>');
 process.exit(2);
}
const escapeHTML = value => String(value).replace(/[&<>"']/g, ch =>
 ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
const source=JSON.parse(fs.readFileSync(contentFile,'utf8'));
const c={truefalse:source.truefalse.map(x=>({...x,q:escapeHTML(x.q),fact:escapeHTML(x.fact),src:escapeHTML(x.src)})),
 meter:source.meter.map(escapeHTML)};
if (source.truefalse.some(x=>!['صح','خطأ'].includes(x.a))) {
 throw new Error('Invalid answer; expected صح or خطأ');
}
const font=fs.readFileSync(fontFile).toString('base64');
const css=`@font-face{font-family:R;src:url(data:font/woff;base64,${font})}*{margin:0;box-sizing:border-box}html,body{background:transparent}
.card{font-family:R;direction:rtl;color:#fff;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;
background:linear-gradient(160deg,#0B3D2Ef2,#062A20f2);border:6px solid #C9A227;border-radius:36px;box-shadow:0 8px 24px #0006}
.tag{font-size:26px;font-weight:600;color:#062A20;background:#C9A227;border-radius:999px;padding:4px 22px;margin-bottom:14px}
.q{font-size:44px;font-weight:700;line-height:1.35}.sub{font-size:30px;color:#FFF4D6;margin-top:4px;font-weight:600;line-height:1.45}
.big{font-size:84px;font-weight:700;line-height:1.35;margin-bottom:10px}.src{font-size:22px;color:#cfe3d8;margin-top:10px}`;
const pages=[];
c.truefalse.forEach((x,i)=>{const n=String(i+1).padStart(2,'0');
 pages.push([`truefalse/q${n}.png`,720,380,`<div class="card" style="width:720px;height:380px;padding:30px 50px"><div class="tag">صح أم خطأ؟ ← ميّل راسك</div><div class="q">${x.q}</div></div>`]);
 pages.push([`truefalse/a${n}.png`,720,380,`<div class="card" style="width:720px;height:380px;padding:30px 50px;border-color:${x.a==='صح'?'#C9A227':'#C0392B'}"><div class="big" style="color:${x.a==='صح'?'#C9A227':'#E74C3C'}">${x.a==='صح'?'✔ صح':'✘ خطأ'}</div><div class="sub">${x.fact}</div><div class="src">المصدر: ${x.src}</div></div>`]);});
pages.push(['truefalse/hint-right.png',300,300,`<div class="card" style="width:300px;height:300px"><div class="big" style="font-size:90px;color:#C9A227">صح</div><div class="sub">ميّل يمين</div></div>`]);
pages.push(['truefalse/hint-left.png',300,300,`<div class="card" style="width:300px;height:300px;border-color:#C0392B"><div class="big" style="font-size:90px;color:#E74C3C">خطأ</div><div class="sub">ميّل يسار</div></div>`]);
c.meter.forEach((t,i)=>{const n=String(i+1).padStart(2,'0');
 pages.push([`meter/title${n}.png`,600,600,`<div class="card" style="width:600px;height:600px;padding:40px"><div class="tag">عداد الثقافة السعودية</div><div class="q" style="font-size:66px">${t}</div></div>`]);});
pages.push(['meter/spinner.png',600,600,`<div class="card" style="width:600px;height:600px"><div class="tag">عداد الثقافة السعودية</div><div class="big" style="font-size:150px;color:#C9A227">؟</div><div class="sub">اضغط على الشاشة</div></div>`]);
pages.push(['brand/icon.png',320,320,`<div class="card" style="width:320px;height:320px;border-radius:75px"><div class="q" style="font-size:60px;color:#C9A227">روّق</div><div class="sub" style="font-size:25px">مع خالد</div></div>`]);
(async()=>{const b=await chromium.launch({executablePath:process.env.MADAR_CHROMIUM});
try { const p=await b.newPage({deviceScaleFactor:1});
await p.route('**/*', route => route.abort());
for(const [f,w,h,html] of pages){await p.setViewportSize({width:w,height:h});await p.setContent(`<style>${css}</style>${html}`);await p.evaluate(async()=>{
 await document.fonts.load('700 52px R');
 await document.fonts.ready;
 if (![...document.fonts].some(font=>font.family==='R' && font.status==='loaded')) {
  throw new Error('Readex Pro failed to load; refusing fallback font');
 }
 const card=document.querySelector('.card');
 if (card.scrollWidth > card.clientWidth || card.scrollHeight > card.clientHeight) {
  throw new Error('Card text overflows');
 }
});
 fs.mkdirSync(path.join(out,path.dirname(f)),{recursive:true});await p.screenshot({path:path.join(out,f),omitBackground:true,clip:{x:0,y:0,width:w,height:h}});}
} finally { await b.close(); }
console.log('rendered',pages.length);})().catch(e=>{console.error(e);process.exit(1)});

