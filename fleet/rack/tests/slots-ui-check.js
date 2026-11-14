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
 assert.match(run("slotControls({...h,id:'desk'},'suites',{slots:3},0)"),/disabled/);
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
