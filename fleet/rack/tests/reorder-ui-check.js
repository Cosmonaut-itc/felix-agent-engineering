// U3.6: production JS against a synthetic DOM and network; real host receipts come from Python.
const vm=require('vm'),assert=require('assert'),fs=require('fs');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
const elements={},storage={},timers=[],requests=[],waiting=[],writes=[];
function el(tag){
  const node={tag,listeners:{},children:[],_text:'',_html:'',type:'',onclick:null,
    addEventListener(type,fn){this.listeners[type]=fn},focus(){},
    append(...nodes){this.children.push(...nodes)},querySelector(){return null}};
  Object.defineProperty(node,'textContent',{get(){return this._text+this.children.map(c=>c.textContent).join('')},set(v){this._text=String(v);this.children=[];this._html=''}});
  Object.defineProperty(node,'innerHTML',{get(){return this._html},set(v){writes.push(tag);this._html=String(v)}});
  return node;
}
const context={console,Intl,Date,Number,Object,String,Math,Map,Set,Uint8Array,JSON,structuredClone,Promise,
 crypto:require('crypto').webcrypto,AbortSignal:{timeout(){}},performance:{now:()=>0},
 document:{getElementById:id=>elements[id] ||= el(id),createElement:tag=>el(tag),addEventListener(){},querySelector:()=>null,querySelectorAll:()=>[]},
 window:{addEventListener(){}},setInterval(){},clearTimeout(){},matchMedia:()=>({matches:false}),
 setTimeout(fn,ms){timers.push({fn,ms});return timers.length},
 sessionStorage:{getItem:k=>storage[k],setItem:(k,v)=>storage[k]=v},
 fetch:(url,opts)=>{const r={url,body:opts?.body ? JSON.parse(opts.body) : null};requests.push(r);return new Promise(res=>waiting.push({...r,res}));}};
vm.createContext(context);vm.runInContext(input.js,context);context.fixture=input.fixture;
const run=s=>vm.runInContext(s,context);
const app=()=>elements.app.innerHTML, toast=()=>context.document.getElementById('toast');
const posts=()=>requests.filter(r=>r.url==='/api/accion');
const ids=()=>[...app().matchAll(/<ol data-host="mini" data-group="suites"[^>]*>([\s\S]*?)<\/ol>/g)][0][1].match(/data-id="(\d+)"/g).map(x=>Number(x.slice(9,-1)));
const flush=()=>new Promise(r=>setImmediate(r));
async function answer(data,status=200){
  const w=waiting.find(w=>w.url==='/api/accion');waiting.splice(waiting.indexOf(w),1);
  w.res({ok:status<400,status,json:async()=>data});await flush();await flush();
}
const tick=()=>{const due=timers.splice(0);for(const t of due) if(t.ms!==8000) t.fn();};
run('render(fixture);globalThis.h=S.hosts[0]');
(async()=>{
 const name=input.scenario;
 if(name==='off'){
   run('h.actions={reason:"Acciones apagadas"};paint()');
   assert.doesNotMatch(app(),/class="reorder|<li[^>]*tabindex|⋮⋮/);
   // A served queue that is not the one on screen is never draggable.
   run('render(fixture);h=S.hosts[0];h.control.reorder.suites.queue.reverse();paint()');
   assert.doesNotMatch(app(),/class="reorder|<li[^>]*tabindex|⋮⋮/);
   console.log('UI reorder off OK');return;
 }
 if(name==='paused'){
   assert.match(app(),/<h4>Fila · 5<span class="note" role="status">No se puede reordenar mientras el grupo está pausado · pausa manual<\/span><\/h4>/);
   assert.doesNotMatch(app(),/class="reorder|<li[^>]*tabindex/);
   console.log('UI reorder paused OK');return;
 }
 assert.match(app(),/<ol data-host="mini" data-group="suites" class="reorder live" aria-busy="false">/);
 assert.equal((app().match(/tabindex="0" aria-keyshortcuts="Alt\+ArrowUp Alt\+ArrowDown"/g)||[]).length,5);
 assert.deepEqual(ids(),[1,2,3,4,5]);
 if(name==='too-long'){
   run('h.control.reorder.suites.max_writes=1');
   run('startReorder(h,"suites",[1,5,2,3,4],{id:5,from:5,keyboard:false})');
   assert.equal(posts().length,0);assert.equal(toast().textContent,'Fila demasiado larga para reordenar de una vez');
   run('h.control.reorder.suites.max_writes=64;h.control.reorder.suites.queue.pop()');
   run('startReorder(h,"suites",[1,5,2,3,4],{id:5,from:5,keyboard:false})');
   assert.equal(posts().length,0);assert.equal(toast().textContent,'La fila cambió. Actualiza antes de decidir.');
   console.log('UI reorder too-long OK');return;
 }
 const keyboard=name==='keyboard';
 const order=keyboard ? [1,2,3,5,4] : [1,5,2,3,4];
 run(`startReorder(h,"suites",${JSON.stringify(order)},{id:5,from:5,keyboard:${keyboard}})`);
 const sent=posts()[0].body;
 assert.equal(sent.op,'reorder');assert.deepEqual(sent.change.order,order);
 assert.deepEqual(sent.expected,{group:'suites',generation:run('h.control.generation'),queue_etag:run('h.control.reorder.suites.queue_etag'),queue:run('h.control.reorder.suites.queue')});
 // While the request is open: the dropped order, «Reordenando…», the group locked.
 assert.match(app(),/<span class="note" role="status">Reordenando…<\/span>/);
 assert.match(app(),/class="reorder" aria-busy="true"/);
 assert.deepEqual(ids(),order);
 assert.match(app(),new RegExp(`<span class="pos">5→${order.indexOf(5)+1}</span>`));
 run('startReorder(h,"suites",[5,4,3,2,1],{id:5,from:5,keyboard:false})');
 assert.equal(posts().length,1,'a second order waits for the first');
 if(keyboard){console.log('UI reorder keyboard OK');return;}
 if(name==='unknown'){
   await answer({schema:'rack-action@1',action_id:sent.action_id,status:'unknown',error:null,served_at:1001});
   assert.match(app(),/No se pudo confirmar el orden · consultando/);
   assert.equal(toast().textContent,'');assert(timers.some(t=>t.ms===2000));
   console.log('UI reorder unknown OK');return;
 }
 const applied=input.receipts.applied;
 await answer(applied);
 assert.doesNotMatch(app(),/Reordenando…/);
 assert.deepEqual(ids(),[1,5,2,3,4]);
 assert.match(app(),/<li class=" moved" data-id="5"/);
 assert.match(app(),/<span class="pos">5→2<\/span>/);
 assert(timers.some(t=>t.ms===2500)&&timers.some(t=>t.ms===8000));
 assert.equal(toast()._text,run('h.tasks.find(t=>t.id===5).ticket')+' pasó al lugar 2 de la fila de suites');
 assert.equal(writes.filter(t=>t==='toast').length,0,'the toast never takes markup');
 const [button]=toast().children;
 assert.equal(button.textContent,'Deshacer');
 tick();assert.doesNotMatch(app(),/ moved/,'highlight ends after 2.5 s');
 assert.match(app(),/class="reorder live"/);
 if(name==='applied'){console.log('UI reorder applied OK');return;}
 button.onclick();
 const undo=posts().find(r=>r.body.op==='reorder_undo').body;
 assert.deepEqual(undo.expected,{group:'suites',generation:sent.expected.generation,queue_etag:applied.result.after_etag});
 assert.equal(undo.undo_of,applied.action_id);assert.equal(toast().textContent,'');
 assert.match(app(),/Reordenando…/);assert.deepEqual(ids(),[1,2,3,4,5]);
 if(name==='undo'){
   await answer(input.receipts.undone);
   assert.equal(toast().textContent,'Orden anterior restaurado');assert.deepEqual(ids(),[1,2,3,4,5]);
   console.log('UI reorder undo OK');return;
 }
 await answer(input.conflict,409);
 assert.equal(toast().textContent,'La fila cambió; no se puede deshacer');
 assert.deepEqual(ids(),[1,5,2,3,4]);
 console.log('UI reorder undo-conflict OK');
})().catch(e=>{console.error(e);process.exit(1);});
