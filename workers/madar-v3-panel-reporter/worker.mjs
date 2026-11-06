// Separate telemetry endpoint. This credential cannot reach any owner UI/action route.
export default {
  async fetch(request, env) {
    const url=new URL(request.url);
    if (url.pathname !== '/api/heartbeat' || url.search || request.method !== 'POST') return new Response('DENIED',{status:404});
    if (typeof env.REPORTER_TOKEN !== 'string' || env.REPORTER_TOKEN.length<32 || !env.PANEL?.fetch) return new Response('NOT_READY',{status:503});
    const supplied=new TextEncoder().encode(request.headers.get('Authorization')||'');
    const expected=new TextEncoder().encode('Bearer '+env.REPORTER_TOKEN);
    if (supplied.length!==expected.length || !crypto.subtle.timingSafeEqual(supplied,expected)) return new Response('DENIED',{status:403});
    try {
      const reader=request.body?.getReader();if(!reader)return new Response('DENIED',{status:400});
      const chunks=[];let size=0;
      for(;;){const {value,done}=await reader.read();if(done)break;size+=value.length;if(size>4096){await reader.cancel();return new Response('TOO_LARGE',{status:413});}chunks.push(value);}
      const body=new Uint8Array(size);let offset=0;for(const chunk of chunks){body.set(chunk,offset);offset+=chunk.length;}
      return await env.PANEL.fetch(new Request('http://127.0.0.1:8876/api/heartbeat',{method:'POST',redirect:'manual',headers:{Authorization:'Bearer '+env.REPORTER_TOKEN,'Content-Type':'application/json','Content-Length':String(size)},body,signal:AbortSignal.timeout(10000)}));
    } catch {return new Response('UNAVAILABLE',{status:503});}
  }
};
