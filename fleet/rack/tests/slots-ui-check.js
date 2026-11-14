// Production JS; synthetic DOM/network/storage. No browser or server required.
const vm=require('vm'),assert=require('assert'),fs=require('fs');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
const elements={},storage={},timers=[],requests=[],listeners=[];
const el=()=>({innerHTML:'',textContent:'',addEventListener(){},focus(){},querySelector(){return {onclick:null}}});
const context={console,Intl,Date,Number,Object,String,Math,Map,Set,Uint8Array,JSON,
 crypto:require('crypto').webcrypto,AbortSignal:{timeout(){}},
 document:{getElementById:id=>elements[id] ||= el(),addEventListener(type,fn){listeners.push([type,fn])},querySelector:()=>null,querySelectorAll:()=>[]},
 window:{addEventListener(){}},setInterval(){},clearTimeout(){},
 setTimeout(fn,ms){timers.push({fn,ms});return timers.length},
 sessionStorage:{getItem:k=>storage[k],setItem:(k,v)=>storage[k]=v},
 fetch:async()=>({ok:false})};
vm.createContext(context);vm.runInContext(input.js,context);context.fixture=input.fixture;
const run=s=>vm.runInContext(s,context);
run('render(fixture);globalThis.h=S.hosts[0]');
(async()=>{
 if(input.scenario==='disabled'){
   context.fetch=async(url,options)=>{requests.push([url,options]);throw Error('POST answer lost')};
   run("globalThis.sent=null;const originalSendSlots=sendSlots;sendSlots=(...args)=>(globalThis.sent=originalSendSlots(...args));startSlots(h,{group:'suites',delta:-1})");
   await run('sent');
   run('globalThis.a=PENDING.get(slotKey(h));globalThis.key=slotKey(h)');
   const ident=run('a.request.action_id');
   // A whois/Host/Origin denial from serve.py has no receipt or action_id.
   const disabled={...input.disabled_response,action_id:ident};
   for(const denial of [{schema:'rack-action@1',error:{code:'forbidden'}},
                         {...disabled,action_id:'different-00001'},
                         {...disabled,effects:true},
                         {...disabled,error:{code:'forbidden'}}]){
     context.fetch=async()=>({ok:false,status:403,json:async()=>denial});
     await run('pollAction(key,a)');
     assert.equal(run('PENDING.size'),1);assert.equal(run('a.status'),'unknown');
   }
   const reload={...context};vm.createContext(reload);vm.runInContext(input.js,reload);vm.runInContext('render(fixture)',reload);
   assert.equal(vm.runInContext('[...PENDING.values()][0].request.action_id',reload),ident);
   context.fetch=async()=>({ok:false,status:403,json:async()=>disabled});
   await run('pollAction(key,a)');
   assert.equal(run('PENDING.size'),0,'confirmed disabled GET must release host lock');
   assert.match(elements.app.innerHTML,/Los ajustes de lugares están apagados/);
   assert.doesNotMatch(elements.app.innerHTML,/Resultado desconocido/);
   assert.deepEqual(JSON.parse(storage['rack-actions'] || '[]'),[]);
   // Fresh capabilities permit another click, always with a new action_id.
   run('render(fixture);globalThis.h=S.hosts[0]');
   assert(!/data-step="-1"[^>]*disabled/.test(run("slotControls(h,'suites',h.groups.suites,0)")));
   context.fetch=async(url,options)=>{requests.push([url,options]);throw Error('next POST answer lost')};
   run("startSlots(h,{group:'suites',delta:-1})");await run('sent');
   assert.notEqual(run('PENDING.get(slotKey(h)).request.action_id'),ident);
   assert.equal(requests.filter(r=>r[0]==='/api/accion' && JSON.parse(r[1].body).action_id===ident).length,1);
   console.log('UI slots OK: disabled 403 confirmado libera bloqueo; autorización falla cerrada; ID nuevo sin reenviar');
   return;
 }
 if(input.scenario==='desk'){
   const at=t=>new Date(t*1000).toLocaleTimeString('es-MX',{hour:'2-digit',minute:'2-digit'});
   const html=elements.app.innerHTML;
   assert.doesNotMatch(html,/Quitar un lugar a default/,'default del desk sin tareas queda oculto');
   assert.match(html,/· tope 14 de día/);
   const lane=(g,c)=>run(`slotControls({...h${c?',control:'+JSON.stringify(c):''}},${JSON.stringify(g)},{slots:(${c?JSON.stringify(c.slots):'h.control.slots'})[${JSON.stringify(g)}] ?? 1},0)`);
   // Mínimo, máximo y tope lleno de día.
   assert.match(lane('hasito'),/data-step="-1"[^>]*title="Mínimo de 1 lugar."[^>]*disabled/);
   assert.match(lane('jale'),/Tope de 14 lugares. Quita un lugar a otro grupo para añadirlo aquí./);
   const day={...input.fixture.hosts[0].snapshot.control};
   assert.match(lane('jale',{...day,slots:{...day.slots,jale:8,pesadas:1,otros:1}}),/Máximo de 8 lugares/);
   assert.doesNotMatch(lane('otros',{...day,slots:{...day.slots,jale:4}}),/data-step="1"[^>]*disabled/);
   // Tope lleno de noche.
   const night={...day,...input.night};
   assert.match(lane('jale',night),/Tope de 18 lugares/);
   assert.match(run(`capacity({...h,control:${JSON.stringify(night)}})`),/· tope 18 de noche/);
   // `default` con tareas se ve, sin botones activos: fuera del reparto.
   assert.match(run("slotControls(h,'default',{slots:1},1)"),/Fuera del reparto: no cuenta en el tope.[^]*disabled/);
   // Host antiguo.
   assert.match(lane('jale',{status:'unsupported',capabilities:{slots:false},error:{message:'Falta actualizar el control de este host.'},slots:{}}),/disabled[^]*Falta actualizar/);
   // Evento vencido pero aún sin guardar: reconciliación pendiente, sin clics.
   assert.match(lane('otros',{...day,recovery_pending:true}),/Reconciliación pendiente[^]*/);
   assert.match(lane('otros',{...day,recovery_pending:true}),/data-step="1"[^>]*disabled/);
   // Pendiente/unknown: un solo envío, con el modo y el vector vistos.
   context.fetch=async(url,options)=>{requests.push([url,options]);throw Error('network')};
   run("globalThis.sent=null;const originalSendSlots=sendSlots;sendSlots=(...args)=>(globalThis.sent=originalSendSlots(...args));");
   run("startSlots(h,{group:'jale',delta:-1})"); await run('sent');
   const body=JSON.parse(requests.find(r=>r[0]==='/api/accion')[1].body);
   assert.equal(body.host,'desk');assert.equal(body.expected.mode,'dia');assert.deepEqual(body.expected.slots,day.slots);
   assert.equal(run("PENDING.get(slotKey(h)).status"),'unknown');assert.match(elements.app.innerHTML,/Resultado desconocido/);
   assert.match(run("slotControls(h,'otros',h.groups.otros,0)"),/Hay un ajuste pendiente/);
   run("globalThis.a=PENDING.get(slotKey(h));globalThis.key=slotKey(h)");
   // Aplicado con vencimiento: toast con hora y Deshacer; el estado del host la repite.
   const expires=input.expires_at;
   run(`acceptSlots(key,a,{status:'applied',revision:2,result:{observed:${JSON.stringify({...day.slots,jale:4})},desired:${JSON.stringify({...day.slots,jale:4})},expires_at:${expires}}})`);
   assert.equal(run('PENDING.size'),0);
   assert.match(elements.toast.innerHTML,new RegExp('jale: 4 lugares · hasta las '+at(expires)));
   assert.match(elements.toast.innerHTML,/Deshacer/);
   assert.match(elements.app.innerHTML,new RegExp('Ajustes hasta las '+at(expires)+' · después rige la tabla de noche'));
   assert.match(elements.app.innerHTML,/· tope 14 de día/);
   // Deshacer tras el evento: conflicto visible, sin bloqueo.
   run("globalThis.h=S.hosts[0];startSlots(h,null,a.request.action_id)");await run('sent');
   run("globalThis.undo=PENDING.get(slotKey(h));acceptSlots(slotKey(h),undo,{status:'conflict',error:{code:'undo_expired',message:'El ajuste venció con el cambio de horario. No se puede deshacer.'}})");
   assert.match(elements.app.innerHTML,/venció con el cambio de horario/);assert.equal(run('PENDING.size'),0);
   // Un ajuste pendiente que el evento reemplazó: terminal, con la razón.
   run("startSlots(h,{group:'otros',delta:-1})");await run('sent');
   run("globalThis.old=PENDING.get(slotKey(h));acceptSlots(slotKey(h),old,{status:'aborted',error:{code:'expired',message:'Venció con el cambio de horario; rige la tabla del modo nuevo.'}})");
   assert.equal(run('PENDING.size'),0);assert.match(elements.app.innerHTML,/rige la tabla del modo nuevo/);
   // Conflicto por vector vivo.
   run("startSlots(h,{group:'otros',delta:-1})");await run('sent');
   run("globalThis.stale=PENDING.get(slotKey(h));acceptSlots(slotKey(h),stale,{status:'conflict',error:{message:'Los lugares cambiaron. Actualiza antes de decidir.'}})");
   assert.match(elements.app.innerHTML,/Los lugares cambiaron/);
   console.log('UI slots desk OK: mínimo/máximo, tope 14 día y 18 noche, default fuera del reparto, host antiguo, pendiente/unknown, vencimiento con hora, undo vencido, abortado y conflicto');
   return;
 }
 assert.match(elements.app.innerHTML,/default/);
 assert.match(run("slotControls(h,'suites',h.groups.suites,0)"),/<b>3<\/b>/);
 assert.match(run("slotControls(h,'suites',h.groups.suites,0)"),/0\/3 en uso/);
 assert.match(elements.app.innerHTML,/· tope 7/);
 const disabled={disabled:true,dataset:{step:'1',host:'mini',group:'suites'}};
 for(const [type,fn] of listeners) if(type==='click') fn({target:{closest:selector=>selector==='button[data-step]'?disabled:null}});
 assert.equal(run('PENDING.size'),0);

 assert.match(run("slotControls(h,'default',h.groups.default,0)"),/Mínimo de 1 lugar/);
 assert.match(run("slotControls(h,'suites',h.groups.suites,0)"),/Tope de 7 lugares. Quita un lugar a otro grupo para añadirlo aquí./);
 assert.match(run("slotControls({...h,control:{...h.control,slots:{suites:8,default:1,rapidas:1}}},'suites',{slots:8},0)"),/Máximo de 8 lugares/);
 assert.match(run("slotControls({...h,control:null},'suites',{slots:3},0)"),/Falta actualizar/);
 assert.match(run("hostControlState({...h,control:{status:'busy',error:{message:'El control está ocupado. Vuelve a intentarlo.'}}})"),/El control está ocupado/);
 assert.doesNotMatch(run("slotControls({...h,control:{status:'busy',error:{message:'El control está ocupado. Vuelve a intentarlo.'}}},'suites',{slots:3},0)"),/pausa/);
 assert.match(run("slotControls({...h,id:'desk',control:{status:'unsupported',capabilities:{slots:false}}},'suites',{slots:3},0)"),/disabled[^]*Falta actualizar/);
 assert.match(run("slotControls(h,'suites',{slots:2},4)"),/4\/2 en uso · no entran nuevas hasta bajar de 2/);
 context.fetch=async(url,options)=>{requests.push([url,options]);throw Error('network')};
 run("globalThis.sent=null;const originalSendSlots=sendSlots;sendSlots=(...args)=>(globalThis.sent=originalSendSlots(...args));");
 run("startSlots(h,{group:'suites',delta:-1})"); await run('sent');
 assert.equal(run('PENDING.size'),1);assert.equal(run("PENDING.get(slotKey(h)).status"),'unknown');
 assert.equal(run('h.groups.suites.slots'),3);assert.match(elements.app.innerHTML,/Resultado desconocido/);
 run("globalThis.a=PENDING.get(slotKey(h));globalThis.key=slotKey(h)");
 const ident=run('a.request.action_id');
 assert.equal(requests.filter(r=>r[0]==='/api/accion').length,1);
 context.fetch=async(url)=>{requests.push([url]);return {ok:false,status:404,json:async()=>({schema:'rack-action@1',action_id:ident,status:'unknown'})}};
 await run('pollAction(key,a)');
 assert.equal(requests.filter(r=>r[0]==='/api/accion').length,1);
 const reload={...context};vm.createContext(reload);vm.runInContext(input.js,reload);vm.runInContext('render(fixture)',reload);
 assert.equal(vm.runInContext('[...PENDING.values()][0].request.action_id',reload),ident);
 run("acceptAction(key,a,{status:'partial',result:{observed:{suites:3,default:1,rapidas:3},desired:{suites:2,default:1,rapidas:3}}})");
 assert.match(elements.app.innerHTML,/Aplicado parcialmente/);assert.match(elements.app.innerHTML,/3 observado \/ 2 deseado/);
 run("acceptSlots(key,a,{status:'applied',revision:2,result:{observed:{suites:2,default:1,rapidas:3},desired:{suites:2,default:1,rapidas:3}}})");
 assert.equal(run('PENDING.size'),0);assert.equal(run('h.groups.suites.slots'),2);assert.match(elements.toast.innerHTML,/Deshacer/);
 assert(timers.some(t=>t.ms===8000));run('render(fixture)');assert.equal(run('S.hosts[0].groups.suites.slots'),2);
 run("globalThis.h=S.hosts[0];startSlots(h,null,a.request.action_id)");await run('sent');
 assert.equal(run("PENDING.get(slotKey(h)).request.op"),'undo');
 run("globalThis.undo=PENDING.get(slotKey(h));acceptSlots(slotKey(h),undo,{status:'conflict',error:{message:'Los lugares cambiaron. No se puede deshacer.'}})");
 assert.match(elements.app.innerHTML,/Los lugares cambiaron/);assert.equal(run('PENDING.size'),0);
 run("startSlots(h,{group:'suites',delta:-1})"); await run('sent');
 run("globalThis.terminal=PENDING.get(slotKey(h))");
 const terminalId=run('terminal.request.action_id');
 context.fetch=async()=>({ok:false,status:409,json:async()=>({schema:'rack-action@1',action_id:terminalId,status:'rejected',error:{message:'Tope confirmado'}})});
 await run('pollAction(slotKey(h),terminal)');
 assert.equal(run('PENDING.size'),0);
 assert.match(elements.app.innerHTML,/Tope confirmado/);

 console.log('UI slots OK: mínimo/máximo/tope, host antiguo, exceso, pendiente/conflicto/unknown, sin optimismo, parcial visible, reload, GET sin reenvío, undo y toast 8 s');
})().catch(e=>{console.error(e);process.exitCode=1});
