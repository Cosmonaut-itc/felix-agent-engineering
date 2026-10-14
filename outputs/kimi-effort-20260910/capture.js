import {appendFileSync} from 'node:fs';
import {createHash} from 'node:crypto';
export const CaptureEffort = async () => {
 const original = globalThis.fetch;
 const file = process.env.GO_EFFORT_METADATA;
 appendFileSync(file,JSON.stringify({event:'plugin_loaded'})+'\n');
 const wrapped = async (...args) => {
  const [input,init]=args;
  const url=typeof input==='string'?input:input instanceof URL?input.href:input?.url;
  if(url!=='https://opencode.ai/zen/go/v1/chat/completions')return original(...args);
  const body=typeof init?.body==='string'?init.body:input instanceof Request?await input.clone().text():null;
  const d=body?JSON.parse(body):{};
  const rec={event:'request_response',url,requested_at:new Date().toISOString(),body_sha256:body?createHash('sha256').update(body).digest('hex'):null,request:Object.fromEntries(['model','reasoning_effort','thinking','stream','max_tokens'].filter(k=>k in d).map(k=>[k,d[k]]))};
  const started=Date.now();const response=await original(...args);
  rec.status=response.status;
  rec.response_header_names=[...response.headers.keys()];
  rec.response_effort_headers=Object.fromEntries([...response.headers].filter(([k])=>/effort/i.test(k)));
  const text=await response.clone().text();
  rec.seconds=(Date.now()-started)/1000;
  const chunks=text.split('\n').filter(l=>l.startsWith('data: ')&&l!=='data: [DONE]').flatMap(l=>{try{return[JSON.parse(l.slice(6))]}catch{return[]}});
  const efforts=[];const scan=(v,p='')=>{if(Array.isArray(v))v.forEach((x,i)=>scan(x,p+'['+i+']'));else if(v&&typeof v==='object')for(const[k,x]of Object.entries(v)){if(/effort/i.test(k))efforts.push({path:p+'.'+k,value:x});scan(x,p+'.'+k)}};
  chunks.forEach(c=>scan(c));
  rec.response={models:[...new Set(chunks.map(c=>c.model).filter(Boolean))],ids:[...new Set(chunks.map(c=>c.id).filter(Boolean))],usage:chunks.map(c=>c.usage).filter(Boolean),effort_fields:efforts,top_level_fields:[...new Set(chunks.flatMap(c=>Object.keys(c)))],reasoning_chars:chunks.flatMap(c=>c.choices??[]).reduce((n,c)=>n+(c.delta?.reasoning_content??'').length,0),finish_reasons:chunks.flatMap(c=>(c.choices??[]).map(c=>c.finish_reason)).filter(Boolean)};
  if(response.status!==200)rec.error_body=text.slice(0,1200);
  appendFileSync(file,JSON.stringify(rec)+'\n');return response;
 };
 Object.assign(wrapped,original);globalThis.fetch=wrapped;
 return {};
};
