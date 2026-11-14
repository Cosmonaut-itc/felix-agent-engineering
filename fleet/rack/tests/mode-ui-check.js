// Production JS with synthetic DOM/transport; no sockets or installed controllers.
const vm=require('vm'), assert=require('assert'), fs=require('fs');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
const elements={},storage={},timers=[],requests=[];
const el=()=>({innerHTML:'',textContent:'',button:{onclick:null},addEventListener(){},focus(){},querySelector(){return this.button}});
const context={console,Intl,Date,Number,Object,String,Math,Map,Set,Uint8Array,JSON,
 crypto:require('crypto').webcrypto,AbortSignal:{timeout(){}},
 document:{getElementById:id=>elements[id] ||= el(),addEventListener(){},querySelector:()=>null,querySelectorAll:()=>[]},
 window:{addEventListener(){}},setInterval(){},clearTimeout(){},
 setTimeout(fn,ms){timers.push({fn,ms});return timers.length},
 sessionStorage:{getItem:k=>storage[k],setItem:(k,v)=>storage[k]=v},fetch:async()=>({ok:false})};
vm.createContext(context);vm.runInContext(input.js,context);context.fixture=input.fixture;context.applied=input.applied;
const run=s=>vm.runInContext(s,context);
run('render(fixture);globalThis.h=S.hosts[0]');
if(input.check) (async()=>{
 run('sendSlots=()=>{}');
 const before=run('JSON.stringify(slotExpected(h))');
 if(input.check==='conflict'){
   const error='El horario o los lugares cambiaron; no se puede deshacer este ajuste';
   for(const host of ['desk','mini']){
     context.host=host;
     run("h.id=host;h.control.slots_note=host==='desk'?'Lugares ajustados · hasta las 23:00':'Lugares ajustados · hasta otro cambio manual';SLOT_MESSAGES.set(host,'"+error+"')");
     const html=run('hostControlState(h)');console.log(host,html);
     assert(html.includes(error));assert(html.includes(run('h.control.slots_note')));
   }
 }else if(input.check==='pending'){
   run("startMode(h,'dia')");
   const html=elements.app.innerHTML.replace(/<[^>]*>/g,'');
   console.log('pending',run('modeControls(h)'),run('hostControlState(h)'));
   assert.equal((html.match(/Aplicando horario de día…/g)||[]).length,1);
   assert.doesNotMatch(run('modeControls(h)').replace(/<[^>]*>/g,''),/Hay un ajuste pendiente; consulta su recibo/);
   assert.match(run('modeControls(h)'),/Aplicando horario de día…/);
   assert.match(run('modeControls(h)'),/tabindex="0"[^>]*aria-describedby="mode-desk-note"/);
   assert.match(run('modeControls(h)'),/data-mode="dia"[^>]*disabled/);
 }else if(input.check==='tables'){
   run("h.control.foreign_work=1;h.control.tables.dia={...h.control.tables.dia,hasito:3,jale:3}");
   assert.match(run('modeControls(h)'),/data-mode="dia"[^>]*disabled/);
   assert.match(run('modeControls(h)'),/trabajo fuera del reparto/);
   run('h.control.tables.dia.hasito=1;h.control.tables.dia.jale=5');
   assert.doesNotMatch(run('modeControls(h)'),/data-mode="dia"[^>]*disabled/);
 }else if(input.check==='undo'){
   for(const from of ['dia','noche']){
     context.from=from;
     run("PENDING.clear();h.control.mode=from;globalThis.target=from==='dia'?'noche':'dia';startMode(h,target);globalThis.action=PENDING.get(slotKey(h));globalThis.key=slotKey(h)");
     // Confirm through production acceptSlots, then use the actual toast callback.
     run("acceptSlots(key,action,{status:'applied',revision:h.control.revision+1,result:{...h.control,mode:target,before:h.control.slots,observed:h.control.slots,desired:h.control.slots,expires_at:null,next_event:NOW+3600}})");
     elements.toast.button.onclick();
     console.log('undo',from,run('JSON.stringify(PENDING.get(key))'),run('modeControls(h)'),run('hostControlState(h)'));
     assert.equal(run('PENDING.get(key).request.op'),'undo');
     assert.equal(run('JSON.stringify(PENDING.get(key).request.expected)'),run('JSON.stringify(slotExpected(h))'));
     assert.equal(run('PENDING.get(key).request.change'),undefined);
     const expected='Aplicando horario de '+(from==='dia'?'día':'noche')+'…';
     assert((run('modeControls(h)')+run('hostControlState(h)')).includes(expected));
     // Reload the production script using the persisted pending action, then render fresh host data.
     const fresh={...context};vm.createContext(fresh);vm.runInContext(input.js,fresh);
     fresh.reloaded=run('JSON.parse(JSON.stringify(fixture))');
     vm.runInContext('render(reloaded);globalThis.restored=S.hosts[0]',fresh);
     assert(vm.runInContext('modeControls(restored)+hostControlState(restored)',fresh).includes(expected));
     vm.runInContext("PENDING.get(slotKey(restored)).status='unknown'",fresh);
     assert(vm.runInContext('modeControls(restored)+hostControlState(restored)',fresh).includes('Cambio sin confirmar · consultando…'));
     const reloadCalls=[];
     const id=vm.runInContext('PENDING.get(slotKey(restored)).request.action_id',fresh);
     fresh.fetch=async url=>{reloadCalls.push(url);return {ok:true,status:200,json:async()=>({
       schema:'rack-action@1',action_id:id,status:'unknown'})}};
     await vm.runInContext('pollAction(slotKey(restored),PENDING.get(slotKey(restored)))',fresh);
     assert.deepEqual(reloadCalls,['/api/accion/'+id]);
     console.log('reload unknown: GET only',reloadCalls);
   }
 }
 assert.equal(requests.length,0);
 if(input.check!=='undo') assert.equal(run('JSON.stringify(slotExpected(h))'),before);
 console.log('state before',before,'after',run('JSON.stringify(slotExpected(h))'));
 console.log('UI injerto OK:',input.check,'transport calls',requests.length);
})().catch(e=>{console.error(e);process.exitCode=1});else (async()=>{
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
 elements.toast.button.onclick();await run('sent');
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
