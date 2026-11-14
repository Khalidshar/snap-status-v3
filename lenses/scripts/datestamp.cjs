// 7-segment film-camera date digits (original artwork), warm orange glow
const {chromium}=require('playwright');const fs=require('fs'),path=require('path');
const out=process.argv[2];fs.mkdirSync(out,{recursive:true});
const SEG={0:'abcdef',1:'bc',2:'abged',3:'abgcd',4:'fgbc',5:'afgcd',6:'afgedc',7:'abc',8:'abcdefg',9:'abcdfg'};
const W=60,H=100,T=11; // digit box
const pos={a:[T,0,W-2*T,T,0],b:[W-T,T,T,H/2-1.5*T,1],c:[W-T,H/2+0.5*T,T,H/2-1.5*T,1],d:[T,H-T,W-2*T,T,0],e:[0,H/2+0.5*T,T,H/2-1.5*T,1],f:[0,T,T,H/2-1.5*T,1],g:[T,H/2-T/2,W-2*T,T,0]};
const col='#FF8A1F';
function digit(s){return Object.entries(pos).map(([k,[x,y,w,h,v]])=>`<div style="position:absolute;left:${x+12}px;top:${y+12}px;width:${w}px;height:${h}px;border-radius:${T/2}px;background:${s.includes(k)?col:'rgba(255,138,31,0.07)'};${s.includes(k)?`box-shadow:0 0 6px ${col},0 0 14px rgba(255,120,20,.6)`:''};transform:skewX(-6deg)"></div>`).join('');}
(async()=>{const b=await chromium.launch({executablePath:process.env.MADAR_CHROMIUM});const p=await b.newPage({viewport:{width:W+24,height:H+24}});
for(const d of Object.keys(SEG)){await p.setContent(`<body style="margin:0;background:transparent"><div style="position:relative;width:${W+24}px;height:${H+24}px">${digit(SEG[d])}</div></body>`);await p.screenshot({path:path.join(out,`d${d}.png`),omitBackground:true});}
await p.setViewportSize({width:40,height:H+24});
await p.setContent(`<body style="margin:0;background:transparent"><div style="position:absolute;left:14px;top:${H+2}px;width:12px;height:12px;border-radius:6px;background:${col};box-shadow:0 0 6px ${col}"></div></body>`);await p.screenshot({path:path.join(out,'dot.png'),omitBackground:true});
await p.setContent(`<body style="margin:0;background:transparent"><div style="position:absolute;left:14px;top:12px;width:10px;height:26px;border-radius:5px;background:${col};box-shadow:0 0 6px ${col};transform:skewX(-12deg)"></div></body>`);await p.screenshot({path:path.join(out,'apos.png'),omitBackground:true});
await b.close();})();
