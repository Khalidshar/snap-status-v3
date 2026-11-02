// Offline bitmap renderer for product text layers; never connects to a remote site.
const fs=require('node:fs/promises');const path=require('node:path');const {chromium}=require('playwright');
(async()=>{
 const [planPath,outDir]=process.argv.slice(2);const plan=JSON.parse(await fs.readFile(planPath,'utf8'));
 if(!Array.isArray(plan.scenes)||plan.scenes.length>15)throw Error('Invalid scene plan');
 const browser=await chromium.launch({headless:true,...(process.env.MADAR_CHROMIUM?{executablePath:process.env.MADAR_CHROMIUM}:{}),args:['--disable-background-networking']});
 try{const page=await browser.newPage({viewport:{width:1080,height:1920},deviceScaleFactor:1});
 await page.route('**/*',route=>route.request().url().startsWith('data:')?route.continue():route.abort());
 for(let i=0;i<plan.scenes.length;i++){
  const s=plan.scenes[i];const font=await fs.readFile(s.font);const encoded=font.toString('base64');
  await page.setContent(`<html lang="ar" dir="rtl"><style>@font-face{font-family:layer;src:url(data:font/ttf;base64,${encoded})}html,body{margin:0;width:1080px;height:1920px;background:transparent}main{box-sizing:border-box;position:absolute;inset:300px 110px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:45px;color:#F6EEDE;text-align:center;font-family:layer}small{font-size:35px;color:#D6B26C}p{margin:0;white-space:pre-wrap;line-height:1.9;font-size:64px;max-width:860px;overflow-wrap:anywhere}</style><main><small></small><p></p></main></html>`);
  await page.evaluate(({text,label})=>{document.querySelector('p').textContent=text;document.querySelector('small').textContent=label},{text:s.text,label:s.label});await page.evaluate(()=>document.fonts.ready);
  await page.evaluate(()=>{const p=document.querySelector('p');let size=64;while((p.scrollHeight>1140||p.scrollWidth>860)&&size>28){p.style.fontSize=(size-=2)+'px'}if(p.scrollHeight>1140)throw Error('Text overflow')});
  await page.screenshot({path:path.join(outDir,`scene-${i}.png`),omitBackground:true});
 }
 }finally{await browser.close()}
})().catch(()=>{process.stderr.write('SCENE_LAYER_FAILED\n');process.exitCode=1});
