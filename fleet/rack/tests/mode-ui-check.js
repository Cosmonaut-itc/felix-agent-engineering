// Production JS with synthetic DOM/transport; no sockets or installed controllers.
const vm=require('vm'), assert=require('assert'), fs=require('fs');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
const elements={},storage={},timers=[],requests=[];
const el=()=>({innerHTML:'',textContent:'',addEventListener(){},focus(){},querySelector(){return {onclick:null}}});
const context={console,Intl,Date,Number,Object,String,Math,Map,Set,Uint8Array,JSON,
 crypto:require('crypto').webcrypto,AbortSignal:{timeout(){}},
 document:{getElementById:id=>elements[id] ||= el(),addEventListener(){},querySelector:()=>null,querySelectorAll:()=>[]},
 window:{addEventListener(){}},setInterval(){},clearTimeout(){},
 setTimeout(fn,ms){timers.push({fn,ms});return timers.length},
 sessionStorage:{getItem:k=>storage[k],setItem:(k,v)=>storage[k]=v},fetch:async()=>({ok:false})};
vm.createContext(context);vm.runInContext(input.js,context);context.fixture=input.fixture;context.applied=input.applied;
const run=s=>vm.runInContext(s,context);
run('render(fixture);globalThis.h=S.hosts[0]');
(async()=>{
 assert.match(run('modeControls(h)'),/data-mode="noche"[^>]*aria-pressed="true"[^>]*disabled/);
 assert.match(run('modeControls(h)'),/Automático · cambia a día a las 08:00/);
 assert.equal(run("modeControls({...h,id:'mini'})"),'');
 assert.match(run('modeControls({...h,control:null})'),/Falta actualizar/);
 assert.match(run('modeControls({...h,control:{...h.control,capabilities:{mode:false}}})'),/data-mode="dia"[^>]*disabled/);
 assert.match(run('modeControls({...h,fresh:false})'),/Actualiza la instantánea/);
 run("startMode(h,'noche')");assert.equal(run('PENDING.size'),0);
 context.fetch=async(url,options)=>{requests.push([url,options]);throw Error('lost')};
 run("globalThis.sent=null;const oldSend=sendSlots;sendSlots=(...a)=>(globalThis.sent=oldSend(...a));startMode(h,'dia')");
 assert.match(elements.app.innerHTML,/Aplicando horario de día…/);
 assert.equal(run('h.control.mode'),'noche'); // no optimistic mode or slots
 await run('sent');
 assert.match(elements.app.innerHTML,/Cambio sin confirmar · consultando…/);
 run('globalThis.a=PENDING.get(slotKey(h));globalThis.key=slotKey(h)');
 run("acceptSlots(key,a,{status:'partial',result:{observed:h.control.slots,desired:applied.result.desired}})");
 assert.match(elements.app.innerHTML,/Horario aplicado parcialmente · revisando…/);
 assert.match(elements.app.innerHTML,/observado \/ .*deseado/);
 const ident=run('a.request.action_id');
 context.fetch=async()=>({ok:true,status:200,json:async()=>({...input.applied,action_id:ident})});
 await run('pollAction(key,a)');
 assert.equal(run('PENDING.size'),0);assert.equal(run('h.control.mode'),'dia');
 assert.match(elements.app.innerHTML,/Adelantado · vuelve a noche a las 23:00/);
 assert.match(elements.toast.innerHTML,/Horario de día aplicado/);assert.match(elements.toast.innerHTML,/Deshacer/);
 assert(timers.some(t=>t.ms===8000));
 context.fetch=async()=>{throw Error('undo reply lost')};
 run("startSlots(h,{mode:true},a.request.action_id)");await run('sent');
 assert.equal(run('PENDING.get(slotKey(h)).request.op'),'undo');
 assert.equal(run('PENDING.get(slotKey(h)).mode'),true);
 run("acceptSlots(slotKey(h),PENDING.get(slotKey(h)),{status:'conflict',error:{message:'El horario o los lugares cambiaron; no se puede deshacer este ajuste'}})");
 assert.match(elements.app.innerHTML,/no se puede deshacer este ajuste/);
 run('SLOT_MESSAGES.clear();h.control.foreign_work=1');
 assert.match(run('modeControls(h)'),/data-mode="noche"[^>]*disabled/);
 assert.match(run('modeControls(h)'),/trabajo fuera del reparto/);
 run('h.control.mode="noche";h.control.slots={hasito:2,jale:7,"jale-verify":1,otros:2,pesadas:3,rapidas:3}');
 assert(!/data-mode="dia"[^>]*disabled/.test(run('modeControls(h)')),'foreign work permits a mode that only decreases');
 run('h.control.foreign_work=0;h.control.mode_note="Adelantado · vuelve a día a las 08:00";h.control.mode_detail="Ajustes manuales hasta las 23:00"');
 assert.match(run('modeControls(h)'),/Adelantado · vuelve a día a las 08:00/);
 assert.match(run('modeControls(h)'),/Ajustes manuales hasta las 23:00/);
 run('h.control.mode_note="Automático · cambia a noche a las 23:00";h.control.mode_detail=null;h.control.mode="dia"');
 assert.match(run('modeControls(h)'),/Automático · cambia a noche a las 23:00/);
 // Expired/late success may not change an already newer host state or offer undo.
 run('NOW=applied.result.expires_at;h.control.revision=99;acceptSlots(key,a,applied)');
 assert.equal(run('h.control.revision'),99);assert.doesNotMatch(elements.toast.innerHTML,/Deshacer/);
 assert.match(elements.toast.innerHTML,/venció/);
 // Returning to the automatic mode clears overrides but its undo still expires.
 run('NOW=applied.result.next_event;h.control.revision=1;SLOT_MESSAGES.clear();acceptSlots(key,a,{...applied,result:{...applied.result,expires_at:null}})');
 assert.equal(run('h.control.revision'),1);assert.doesNotMatch(elements.toast.innerHTML,/Deshacer/);
 assert.match(elements.toast.innerHTML,/venció/);
 assert.equal(requests.filter(([url,o])=>url==='/api/accion' && JSON.parse(o.body).action_id===ident).length,1);
 console.log('UI mode OK: automatic/advanced both ways; pending/partial/conflict/unknown/expired; foreign work; old host; Mini; undo; toast 8s; no replay');
})().catch(e=>{console.error(e);process.exitCode=1});
