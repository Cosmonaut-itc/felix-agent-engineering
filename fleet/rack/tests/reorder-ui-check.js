// Execute production handlers and receipt transitions with a deterministic clock/DOM.
const vm=require('vm'),assert=require('assert'),fs=require('fs');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
const elements={},timers=[],listeners={},storage={},requests=[];
class El{
 constructor(){this.html='';this.text='';this.children=[];this.listeners={}}
 get innerHTML(){return this.html+this.children.map(c=>c.innerHTML).join('')}
 set innerHTML(v){this.html=v;this.children=[]}
 get textContent(){return this.text}
 set textContent(v){this.text=String(v);this.html=this.text;this.children=[]}
 appendChild(c){this.children.push(c)}
 addEventListener(t,fn){this.listeners[t]=fn} focus(){this.focused=true}
}
const context={console,Intl,Date,Number,Object,String,Math,Map,Set,Uint8Array,JSON,structuredClone,
 crypto:require('crypto').webcrypto,AbortSignal:{timeout(){}},
 document:{getElementById:id=>elements[id] ||= new El(),createElement:()=>new El(),addEventListener:(t,fn)=>(listeners[t] ||= []).push(fn),removeEventListener(){},querySelector:()=>null,querySelectorAll:()=>[]},
 window:{addEventListener(){},matchMedia:()=>({matches:false})},setInterval(){},clearTimeout(){},
 setTimeout(fn,ms){timers.push({fn,ms});return timers.length},sessionStorage:{getItem:k=>storage[k],setItem:(k,v)=>storage[k]=v},
 fetch:async(url,options)=>{requests.push([url,options]);throw Error('lost')}};
vm.createContext(context);vm.runInContext(input.js,context);context.fixture=input.fixture;
const run=s=>vm.runInContext(s,context);
run('render(structuredClone(fixture));globalThis.h=S.hosts[0]');
(async()=>{
 assert.match(elements.app.innerHTML,/data-reorder="true"/);
 run('globalThis.promise=null;const send=sendOrder;sendOrder=(...args)=>(promise=send(...args));startOrder(h,"suites",[1,5,2,3,4],5)');
 assert.match(elements.app.innerHTML,/Reordenando…/);
 assert(elements.app.innerHTML.indexOf('data-id="5"')<elements.app.innerHTML.indexOf('data-id="2"'),'pending gesture retains its spatial order');
 await run('promise');
 assert.match(elements.app.innerHTML,/No se pudo confirmar el orden · consultando/);
 assert.equal(elements.toast?.innerHTML || '','','unknown has no success toast');
 assert.equal(requests.filter(r=>r[1]?.method==='POST').length,1);
 run('globalThis.a=PENDING.get(slotKey(h));globalThis.key=slotKey(h)');
 const a=JSON.parse(run('JSON.stringify(a)'));
 const expected=a.request.expected;
 context.answer={status:'applied',revision:2,result:{group:'suites',before:expected.queue,
  desired:[1,5,2,3,4].map((id,i)=>({...expected.queue.find(t=>t.id===id),priority:5-i})),
  observed:[1,5,2,3,4].map((id,i)=>({...expected.queue.find(t=>t.id===id),priority:5-i})),queue_etag:'c'.repeat(64)}};
 const priorTimerCount=timers.length;
 run('acceptOrder(key,a,answer)');
 assert.match(elements.toast.innerHTML,/SEN-6405 en lugar 2 · suites/);
 assert.match(elements.app.innerHTML,/>5→2<\/span>/);
 assert.equal(elements.toast.children[0].textContent,'Deshacer');
 assert(timers.slice(priorTimerCount).some(t=>t.ms===2500));assert(timers.slice(priorTimerCount).some(t=>t.ms===8000));
 assert.match(elements.app.innerHTML,/order-done/);assert.equal(run('PENDING.size'),0);
 elements.toast.children[0].onclick();await run('promise');
 assert.equal(run('PENDING.get(key).request.op'),'undo');
 assert.equal(run('PENDING.get(key).request.expected.queue_etag'),'c'.repeat(64));
 run('acceptOrder(key,PENDING.get(key),{status:"conflict",error:{code:"undo_conflict",message:"irrelevant"}})');
 assert.equal(elements.toast.textContent,'La fila cambió; no se puede deshacer');
 assert.equal(run('PENDING.size'),0);
 // Keyboard handler preserves the identity to focus and shares the pipeline.
 run('render(structuredClone(fixture));globalThis.h=S.hosts[0]');
 assert.equal(run('h.control.revision'),2,'stale snapshot cannot replace confirmed order');
 assert.deepEqual(JSON.parse(run('JSON.stringify(queued(h).map(t=>t.id))')),[1,5,2,3,4]);
 run('HOST_CONFIRMED.clear();ORDER_RECEIPTS.clear();render(structuredClone(fixture));globalThis.h=S.hosts[0];globalThis.captured=null;startOrder=(...a)=>(captured=a)');
 const ol={dataset:{host:'mini',group:'suites'}},li={dataset:{id:'5'},parentElement:ol};
 for(const fn of listeners.keydown) fn({target:{closest:()=>li},altKey:true,key:'ArrowUp',preventDefault(){}});
 assert(run('captured!==null'),'keyboard produces a move');
 assert.deepEqual(JSON.parse(run('JSON.stringify(captured[2])')),[1,2,3,5,4]);assert.equal(run('captured[3]'),5);
 // Escape / pointercancel must use cancel=true, never POST.
 run('globalThis.cancelled=[];DRAG={};finishDrag=(cancel)=>{cancelled.push(cancel);DRAG=null}');
 for(const fn of listeners.keydown) fn({target:{closest:()=>null},key:'Escape',preventDefault(){}});
 assert.equal(run('cancelled[0]'),true);
 // Touch outside the handle preserves vertical scroll and never captures.
 run('DRAG=null');
 for(const fn of listeners.pointerdown) fn({target:{closest:s=>s==='.fila li[data-reorder="true"]'?li:null},button:0,pointerType:'touch',pointerId:4,clientY:100,preventDefault(){throw Error('scroll captured')}});
 assert.equal(run('DRAG'),null,'touch on row body must scroll');
 li.getBoundingClientRect=()=>({top:100});li.focus=()=>{};ol.children=[li];
 for(const fn of listeners.pointerdown) fn({target:{closest:s=>s==='button'?null:li},button:0,pointerId:3,clientY:100,preventDefault(){}});
 for(const fn of listeners.pointercancel || []) fn({pointerId:99});
 assert(run('DRAG!==null'),'another pointer cannot cancel the drag');
 for(const fn of listeners.pointercancel || []) fn({pointerId:3});
 assert.equal(run('cancelled.at(-1)'),true,'pointercancel restores without submitting');
 assert.equal(requests.filter(r=>r[1]?.method==='POST').length,2,'cancelling gestures never POST');

 run('h.control.reorder_groups.suites={...h.control.reorder_groups.suites,enabled:false,reason:"No se puede reordenar mientras el grupo está pausado · pressure"};h.groups.suites.status="Paused"');
 assert.match(run('view()'),/No se puede reordenar mientras el grupo está pausado · pressure/);
 assert.match(run('view()'),/data-reorder="false"/);
 assert(/\.fila li\.order-done\{[^}]*box-shadow:inset 3px 0 var\(--accent\)/.test(input.css),'confirmed state has its lateral marker');
 assert(input.js.includes("pointercancel"));assert(input.js.includes("setPointerCapture"));
 assert(input.js.includes('toast.textContent=text'));assert(!input.js.includes('toast.innerHTML'));
 console.log('UI reorder OK: pending, unknown, no replay, 5→2 receipt, exact undo, conflict, Alt, Escape, paused, confirmation timers, textContent');
})().catch(e=>{console.error(e);process.exitCode=1});
