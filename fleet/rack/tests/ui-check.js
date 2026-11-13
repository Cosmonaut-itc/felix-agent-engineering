// Executes production JS against a synthetic DOM, network, clock and storage.
// No browser package or frontend dependency is used.
const vm=require('vm'), assert=require('assert'), fs=require('fs');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
const timers=[],elements={},writes=[],storage={};
const element=()=>({addEventListener(){},innerHTML:'',textContent:'',focus(){}});
const dialog={...element(),returnValue:'no',open:false,paragraph:element(),
  buttons:[{value:'no'},{value:'si'}],querySelector(){return this.paragraph},
  querySelectorAll(){return this.buttons},showModal(){this.open=true},
  close(value){this.returnValue=value;this.open=false;this.onclose()}};
elements.confirm=dialog;
const context={console,Intl,Date,Number,Object,String,Math,Map,Set,Uint8Array,JSON,
 crypto:require('crypto').webcrypto,
 document:{getElementById:id=>elements[id] ||= element(),addEventListener(){},querySelector:()=>null,querySelectorAll:()=>[]},
 window:{addEventListener(){}},setInterval(){},clearTimeout(){},
 setTimeout(fn,ms){timers.push({fn,ms});return timers.length},
 sessionStorage:{getItem(k){return storage[k] || null},setItem(k,v){storage[k]=v;writes.push([k,v])}},
 AbortSignal:{timeout(){}},fetch:async()=>({ok:false})};
vm.createContext(context);vm.runInContext(input.js,context);
context.fixture=input.fixture;
vm.runInContext('render(fixture)',context);
(async()=>{
 const run=source=>vm.runInContext(source,context);
 run("globalThis.found={h:S.hosts[0],t:S.hosts[0].tasks[0]}");
 assert.equal(run('cancelReason(found)'),null);
 assert.match(run('cancelReason({h:{...found.h,id:"desk",actions:{reason:"Razón desde configuración"}},t:found.t})'),/Razón desde configuración/);
 assert.equal(run('cancelReason({h:{...found.h,id:"desk",actions:{reason:null}},t:found.t})'),null);
 assert.match(run('cancelReason({h:found.h,t:{...found.t,control:null}})'),/Lanzador no reconocido/);
 const button={disabled:true},reason=element();
 context.menu={querySelector:s=>s==='[data-cancel]'?button:reason};
 context.fetch=async()=>({json:async()=>({schema:'rack-action@1',capabilities:{queued:true,running:true}})});
 await run('prepareCancel(menu,found)');assert.equal(button.disabled,false);
 run('confirmCancel(found)');assert.equal(dialog.open,true);
 assert.equal(dialog.paragraph.textContent,'¿Cancelar SEN-123 · Contrato en Mac Mini? La sesión que la espera la verá como cancelada.');
 dialog.close('no');assert.equal(run('PENDING.size'),0);
 context.fetch=async()=>({json:async()=>({schema:'rack-action@1',status:'cancelling'})});
 run('globalThis.initialSend=null;const originalSend=sendCancel;sendCancel=(...args)=>(globalThis.initialSend=originalSend(...args))');
 run('confirmCancel(found)');dialog.close('si');await run('initialSend');
 assert.equal(run('PENDING.size'),1);
 assert.match(elements.app.innerHTML,/Cancelando…/);assert.match(elements.app.innerHTML,/aria-busy="true"/);
 assert.match(run('cancelReason(found)'),/mismo ID/);
 run('globalThis.action=[...PENDING.values()][0];globalThis.key=[...PENDING.keys()][0]');
 const id=run('action.request.action_id');assert.match(id,/^[a-f0-9]{32}$/);
 assert.equal(JSON.parse(writes.at(-1)[1])[0][1].request.action_id,id);
 for(const status of ['unknown','cancel_unconfirmed']){
  run(`acceptAction(key,action,{status:'${status}'})`);
  assert.equal(run('PENDING.size'),1);
  assert.equal(run('action.request.action_id'),id);
  assert.match(elements.app.innerHTML,status==='unknown'?/Resultado desconocido/:/Cancelación sin confirmar/);
 }
 for(const [status,error,pattern] of [['already_finished',null,/ya había terminado/],
    ['rejected',{code:'stale_state',message:'La corrida cambió'},/cambió/],
    ['rejected',{code:'unsupported',message:'Esta corrida no confirma la cancelación'},/no confirma/]]){
   context.answer={status,error,result:{exit:0}};
   assert.match(run('actionMessage(answer,action)'),pattern);
 }
 const requests=[];
 context.fetch=async(url,options)=>{requests.push([url,options]);throw Error('network')};
 await run('sendCancel(key,action)');
 assert.equal(run('PENDING.size'),1);assert.equal(run('action.status'),'unknown');
 assert.match(elements.toast.innerHTML,/Error de red/);
 await run('pollAction(key,action)');
 assert.equal(requests.at(-1)[0],'/api/accion/'+id);
 assert.equal(requests.filter(([url])=>url==='/api/accion').length,1);
 const pendingReload={...context};vm.createContext(pendingReload);vm.runInContext(input.js,pendingReload);
 vm.runInContext('render(fixture)',pendingReload);
 assert.equal(vm.runInContext('[...PENDING.values()][0].request.action_id',pendingReload),id);
 assert.match(elements.app.innerHTML,/Resultado desconocido/);
 context.fetch=async(url)=>({ok:true,json:async()=>url==='/api/rack.json'?input.fixture:
   {schema:'rack-action@1',status:'cancelled',action_id:id,result:{exit:130}}});
 await run('pollAction(key,action)');
 assert.equal(run('PENDING.size'),0);assert.match(elements.toast.innerHTML,/SEN-123 cancelada/);
 // Render the exact pre-receipt snapshot, including the same task, after cancellation.
 run('render(fixture)');
 assert.equal(run('S.hosts[0].tasks.some(t=>t.id===77)'),false,'cancelled row reappeared from old snapshot');
 assert.doesNotMatch(elements.app.innerHTML,/data-id="77"/);
 // Exclusion is host/task/ref-specific: neither another host nor a reused ID is hidden.
 run(`globalThis.other=JSON.parse(JSON.stringify(fixture));
      other.hosts.push({...JSON.parse(JSON.stringify(other.hosts[0])),id:'desk'});
      other.hosts[0].snapshot.tasks[0].control.ref='20261112-184713-aaaaaa';render(other)`);
 assert.equal(run('S.hosts.every(h=>h.tasks.some(t=>t.id===77))'),true);
 // A reload retains the receipt exclusion as well as pending-action persistence.
 const reloaded={...context};vm.createContext(reloaded);vm.runInContext(input.js,reloaded);
 vm.runInContext('render(fixture)',reloaded);
 assert.equal(vm.runInContext('S.hosts[0].tasks.some(t=>t.id===77)',reloaded),false);
 const toastTimer=timers.filter(t=>t.ms===8000).at(-1);assert(toastTimer);toastTimer.fn();
 assert.equal(elements.toast.textContent,'');
 console.log('UI OK: confirmación, No, pendiente, toast 8s, desk configurable, capacidades, conflictos, unknown y red con mismo ID, cancelada ausente tras snapshot anterior y recarga');
})().catch(error=>{console.error(error);process.exitCode=1});
