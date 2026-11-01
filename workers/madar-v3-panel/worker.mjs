const EMAIL = 'khalid.sharahili.sa@gmail.com';
const paths = new Set(['/', '/app.js', '/style.css', '/api/snapshot', '/api/command']);
export default {
  async fetch(request, env, ctx) {
    try {
      // Runtime-authenticated identity, never a user-supplied email header.
      if (!ctx?.access || !env.ACCESS_AUD || ctx.access.aud !== env.ACCESS_AUD) return new Response('DENIED', {status:403});
      const identity = await ctx.access.getIdentity();
      if (identity?.email !== EMAIL) return new Response('DENIED', {status:403});
      const url = new URL(request.url);
      if (!paths.has(url.pathname) || url.search || !['GET','POST'].includes(request.method)) return new Response('DENIED', {status:404});
      if (!env.PANEL?.fetch || typeof env.PANEL_ORIGIN_TOKEN !== 'string' || env.PANEL_ORIGIN_TOKEN.length < 32) return new Response('NOT_READY', {status:503});
      let body;
      if (request.method === 'POST') {
        if (url.pathname !== '/api/command' || request.headers.get('Origin') !== url.origin || request.headers.get('Content-Type')?.split(';')[0] !== 'application/json') return new Response('DENIED', {status:403});
        const reader = request.body?.getReader();
        if (!reader) return new Response('DENIED', {status:400});
        const chunks=[]; let size=0;
        for (;;) {const {done,value}=await reader.read();if(done)break;size+=value.byteLength;if(size>512){await reader.cancel();return new Response('TOO_LARGE',{status:413});}chunks.push(value);}
        body = new Uint8Array(size);let offset=0;for(const c of chunks){body.set(c,offset);offset+=c.length;}
      }
      const upstream = await env.PANEL.fetch(new Request('http://127.0.0.1:8876'+url.pathname, {
        method:request.method,redirect:'manual',headers:{Authorization:'Bearer '+env.PANEL_ORIGIN_TOKEN,
          'Content-Type':'application/json',Origin:'http://127.0.0.1:8876'},body,
        signal:AbortSignal.timeout(10000)
      }));
      const headers=new Headers(upstream.headers);headers.set('Cache-Control','no-store');headers.delete('set-cookie');
      return new Response(upstream.body,{status:upstream.status,headers});
    } catch {return new Response('UNAVAILABLE',{status:503});}
  }
};
