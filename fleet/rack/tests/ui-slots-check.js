// Lugares por grupo: JS de producción contra DOM, red, reloj y almacenamiento sintéticos.
const vm=require('vm'), assert=require('assert'), fs=require('fs');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
const out=[];
function harness(storage={}){
 const timers=[],elements={},listeners={};
 const undo={onclick:null};
 const element=id=>({id,innerHTML:'',textContent:'',focus(){},
  addEventListener(type,fn){(listeners[id+':'+type] ||= []).push(fn)},
  querySelector(){return undo}});
 const context={console,Intl,Date,Number,Object,String,Math,Map,Set,Uint8Array,JSON,Error,
  crypto:require('crypto').webcrypto,
  document:{getElementById:id=>elements[id] ||= element(id),addEventListener(){},querySelector:()=>null,querySelectorAll:()=>[]},
  window:{addEventListener(){}},setInterval(){},clearTimeout(){},
  setTimeout(fn,ms){timers.push({fn,ms});return timers.length},
  sessionStorage:{getItem(k){return storage[k] || null},setItem(k,v){storage[k]=v}},
  AbortSignal:{timeout(){}},fetch:async()=>({ok:false,json:async()=>({})})};
 vm.createContext(context);vm.runInContext(input.js,context);
 const run=source=>vm.runInContext(source,context);
 const app=()=>elements.app.innerHTML, toast=()=>elements.toast.innerHTML;
 // Clic real por el delegado de #app, con el botón que pinta view().
 const click=(host,group,step)=>{
  const html=app();
  const re=new RegExp(`<button type="button" data-step="${step}" data-host="${host}" data-group="${group}" aria-disabled="(true|false)"`);
  assert.match(html,re,'falta el botón '+group+' '+step);
  const button={dataset:{step:String(step),host,group}};
  for(const fn of listeners['app:click']) fn({target:{closest:s=>s==='button[data-step]'?button:null}});
 };
 const disabled=(host,group,step)=>{
  const m=app().match(new RegExp(`data-step="${step}" data-host="${host}" data-group="${group}" aria-disabled="(true|false)"(?: aria-label="[^"]*")?(?: title="([^"]*)")?`));
  assert.ok(m,'sin botón');return m[1]==='true' ? (m[2] || '') : null;
 };
 return {context,run,app,toast,click,disabled,timers,storage,undo,elements};
}
const clone=v=>JSON.parse(JSON.stringify(v));
function fixture(control,changes={}){
 const data=clone(input.fixture);const h=data.hosts[0];
 h.snapshot.control=control===undefined?clone(input.control):control;
 if(control && control.desired){
  h.snapshot.groups=Object.entries(control.desired).map(([name,slots])=>({name,status:'Running',slots}));
 }
 Object.assign(h,changes);return data;
}
const ctl=(desired,extra={})=>({...clone(input.control),desired,observed:{...desired},...extra});
(async()=>{
 // 1. Tope lleno: + deshabilitado con su explicación; − habilitado; el default se ve.
 let t=harness();t.context.fixture=fixture(ctl({suites:3,default:1,rapidas:3}));t.run('render(fixture)');
 assert.match(t.app(),/7 en total · tope 7/);
 assert.match(t.app(),/data-group="default"/,'default con cuota debe verse');
 const full=t.disabled('mini','suites',1);
 assert.equal(full,'Tope de 7 lugares. Quita un lugar a otro grupo para añadirlo aquí.');
 assert.equal(t.disabled('mini','suites',-1),null);
 let sent=[];t.context.fetch=async(url,o)=>{sent.push([url,o]);return {json:async()=>({})}};
 t.click('mini','suites',1);
 assert.equal(sent.length,0,'un + deshabilitado no envía nada');
 assert.match(t.toast(),/Tope de 7 lugares\. Quita un lugar a otro grupo/);
 out.push('tope lleno: + aria-disabled con «'+full+'»; clic sin red; toast con la razón');

 // 2. Mínimo: − deshabilitado en 1.
 t=harness();t.context.fixture=fixture(ctl({suites:1,default:1,rapidas:3}));t.run('render(fixture)');
 assert.equal(t.disabled('mini','suites',-1),'Cada grupo necesita al menos 1 lugar.');
 assert.equal(t.disabled('mini','suites',1),null);
 out.push('mínimo: − deshabilitado en 1 con su razón; + habilitado (5 de 7)');

 // 3. Máximo: + deshabilitado en 8 aunque el tope deje espacio.
 t=harness();t.context.fixture=fixture(ctl({suites:8,default:1,rapidas:3},{cap:20}));t.run('render(fixture)');
 assert.equal(t.disabled('mini','suites',1),'Un grupo admite como máximo 8 lugares.');
 out.push('máximo: + deshabilitado en 8 con tope 20');

 // 4. Clic en − : cuerpo exacto, pendiente, aplicado, Deshacer y refresco.
 t=harness();t.context.fixture=fixture(ctl({suites:3,default:1,rapidas:3}));t.run('render(fixture)');
 sent=[];let answer={status:'applying'};
 t.context.fetch=async(url,o)=>{sent.push([url,o]);
  if(url==='/api/refresh'||url==='/api/rack.json') return {ok:true,json:async()=>t.context.fixture};
  const body=JSON.parse(o.body);return {json:async()=>({schema:'rack-action@1',action_id:body.action_id,op:body.op,host:'mini',...answer})}};
 t.click('mini','suites',-1);await new Promise(r=>setImmediate(r));
 const first=JSON.parse(sent[0][1].body);
 assert.equal(sent[0][0],'/api/accion');assert.equal(sent[0][1].headers['X-Rack-Action'],'1');
 assert.deepEqual(Object.keys(first).sort(),['action_id','change','dry_run','expected','host','op','schema']);
 assert.deepEqual(first.change,{group:'suites',delta:-1});assert.equal(first.dry_run,false);
 assert.deepEqual(first.expected,{revision:4,policy_hash:'a'.repeat(64),slots:{suites:3,default:1,rapidas:3}});
 assert.match(t.app(),/Aplicando…/);
 assert.equal(t.disabled('mini','rapidas',-1),'Hay un cambio de lugares en curso en este host; espera su resultado.');
 assert.match(t.storage['rack-slots'],new RegExp(first.action_id));
 // Recarga con el pendiente: mismo ID, consulta sin reenviar.
 const reload=harness(t.storage);reload.context.fixture=t.context.fixture;reload.run('render(fixture)');
 assert.equal(reload.run('[...SLOTS.values()][0].request.action_id'),first.action_id);
 assert.match(reload.app(),/Aplicando…/);
 t.run('globalThis.a=SLOTS.get("mini")');
 t.context.data={schema:'rack-action@1',action_id:first.action_id,op:'slots',status:'applied',revision:5,
  result:{before:{suites:3,default:1,rapidas:3},desired:{suites:2,default:1,rapidas:3},observed:{suites:2,default:1,rapidas:3}}};
 sent=[];t.run('acceptSlots("mini",a,data)');await new Promise(r=>setImmediate(r));
 assert.equal(t.run('SLOTS.size'),0);
 assert.match(t.toast(),/suites pasa de 3 a 2 lugares\./);assert.match(t.toast(),/Deshacer/);
 assert.match(t.app(),/<b>2<\/b>/,'la cifra aplicada no vuelve a la vieja');
 assert.match(t.app(),/6 en total · tope 7/);
 assert.equal(t.disabled('mini','rapidas',1),'Actualizando la instantánea con el cambio aplicado…');
 assert.equal(sent[0][0],'/api/refresh');
 out.push('clic −: cuerpo cerrado con expected visto; «Aplicando…»; recarga conserva el ID; aplicado muestra 2, Deshacer y pide refresco');
 // La instantánea alcanza la revisión: el override se retira y + vuelve a habilitarse.
 t.context.fixture=fixture(ctl({suites:2,default:1,rapidas:3},{revision:5}));t.run('render(fixture)');
 assert.equal(t.run('CONFIRMED.size'),0);assert.equal(t.disabled('mini','rapidas',1),null);
 // Deshacer: undo_of, revisión y vector nuevos.
 sent=[];answer={status:'applied',revision:6,result:{before:{suites:2,default:1,rapidas:3},desired:{suites:3,default:1,rapidas:3},observed:{suites:3,default:1,rapidas:3},undo_of:first.action_id}};
 t.undo.onclick();await new Promise(r=>setImmediate(r));
 const back=JSON.parse(sent[0][1].body);
 assert.equal(back.op,'undo');assert.equal(back.undo_of,first.action_id);assert.notEqual(back.action_id,first.action_id);
 assert.deepEqual(back.expected,{revision:5,policy_hash:'a'.repeat(64),slots:{suites:2,default:1,rapidas:3}});
 assert.equal('change' in back,false);
 assert.match(t.toast(),/Deshecho: suites pasa de 2 a 3 lugares\./);assert.doesNotMatch(t.toast(),/<button/);
 out.push('Deshacer: op undo con undo_of, expected de la revisión 5; el toast del deshacer no ofrece otro');

 // 5. Conflicto: mensaje del host, sin pendiente.
 t=harness();t.context.fixture=fixture(ctl({suites:2,default:1,rapidas:3}));t.run('render(fixture)');
 t.context.fetch=async(url,o)=>{if(url!=='/api/accion') return {ok:true,json:async()=>t.context.fixture};
  const b=JSON.parse(o.body);return {json:async()=>({schema:'rack-action@1',action_id:b.action_id,op:'slots',status:'conflict',
   error:{code:'stale_state',message:'Los lugares cambiaron desde que los viste. Actualiza antes de decidir.'}})}};
 t.click('mini','suites',1);await new Promise(r=>setImmediate(r));
 assert.equal(t.run('SLOTS.size'),0);assert.match(t.toast(),/cambiaron desde que los viste/);
 out.push('conflicto: toast con el mensaje del host; sin pendiente');

 // 6. Error de red → unknown; 404 reenvía el mismo cuerpo una vez; el segundo 404 no reenvía.
 t=harness();t.context.fixture=fixture(ctl({suites:2,default:1,rapidas:3}));t.run('render(fixture)');
 sent=[];t.context.fetch=async(url,o)=>{sent.push([url,o]);throw Error('red')};
 t.click('mini','suites',1);await new Promise(r=>setImmediate(r));
 assert.equal(t.run('SLOTS.get("mini").status'),'unknown');assert.match(t.app(),/Resultado desconocido · consultando…/);
 assert.match(t.toast(),/Error de red: el cambio pudo aplicarse/);
 const id=JSON.parse(sent[0][1].body).action_id;
 sent=[];t.context.fetch=async(url,o)=>{sent.push([url,o]);
  return url.startsWith('/api/accion/')?{status:404,json:async()=>({schema:'rack-action@1',action_id:id,status:'unknown'})}:{json:async()=>({schema:'rack-action@1',action_id:id,status:'unknown'})}};
 t.run('globalThis.a=SLOTS.get("mini")');
 await t.run('pollSlots("mini",a)');
 assert.deepEqual(sent.map(([u])=>u),['/api/accion/'+id,'/api/accion']);
 assert.equal(JSON.parse(sent[1][1].body).action_id,id);
 sent=[];await t.run('pollSlots("mini",a)');
 assert.deepEqual(sent.map(([u])=>u),['/api/accion/'+id],'un segundo 404 no reenvía');
 assert.equal(t.run('SLOTS.get("mini").request.action_id'),id);
 out.push('unknown: 404 reenvía el mismo cuerpo una vez; el segundo 404 sólo consulta; el ID se conserva');

 // 7. Parcial: estado visible y se sigue consultando.
 t.run('acceptSlots("mini",a,{schema:"rack-action@1",action_id:a.request.action_id,status:"partial"})');
 assert.match(t.app(),/Aplicado parcialmente · intentando completar/);assert.match(t.toast(),/Aplicado parcialmente/);
 out.push('parcial: «Aplicado parcialmente · intentando completar» y consulta');

 // 8. Pendiente que viene del host (otra pestaña).
 t=harness();t.context.fixture=fixture(ctl({suites:2,default:1,rapidas:3},{pending:{action_id:'b'.repeat(32),phase:'applying'}}));t.run('render(fixture)');
 assert.equal(t.disabled('mini','suites',1),'Hay un cambio de lugares en curso en este host; espera su resultado.');
 // Deriva o estado ilegible: la razón del host.
 t=harness();t.context.fixture=fixture(ctl({suites:2,default:1,rapidas:3},{slots:false,reason:'Pueue no tiene las cuotas guardadas; la guardia las está reponiendo.'}));t.run('render(fixture)');
 assert.equal(t.disabled('mini','suites',-1),'Pueue no tiene las cuotas guardadas; la guardia las está reponiendo.');
 out.push('pendiente del host y deriva: deshabilitado con su razón');

 // 9. Host antiguo sin la capacidad, y el desk.
 t=harness();t.context.fixture=fixture(null);t.run('render(fixture)');
 assert.equal(t.disabled('mini','suites',1),'Falta actualizar el control de este host.');
 assert.doesNotMatch(t.app(),/tope/);
 t=harness();const desk=fixture(null);desk.hosts[0].id='desk';desk.hosts[0].name='Felix DESKTOP';t.context.fixture=desk;t.run('render(fixture)');
 assert.equal(t.disabled('desk','suites',1),'Este host todavía no admite cambiar lugares desde el panel.');
 assert.match(t.app(),/class="dots"/,'Cancelar… sigue en el menú de la corrida');
 out.push('host antiguo y desk: steppers deshabilitados con su razón; menú de la corrida intacto');

 // 10. Instantánea vieja.
 t=harness();t.context.fixture=fixture(ctl({suites:2,default:1,rapidas:3}),{fresh:false,age:600});t.run('render(fixture)');
 assert.equal(t.disabled('mini','suites',1),'Actualiza la instantánea antes de cambiar lugares.');
 // 11. Más corridas que lugares tras bajar: aviso en el carril.
 const crowded=fixture(ctl({suites:1,default:1,rapidas:3}));crowded.hosts[0].snapshot.tasks=[0,1].map(i=>({...input.fixture.hosts[0].snapshot.tasks[0],id:90+i,state:'Running',start:900,group:'suites',control:null}));
 t=harness();t.context.fixture=crowded;t.run('render(fixture)');
 assert.match(t.app(),/2\/1 en uso/);assert.match(t.app(),/no entran nuevas hasta bajar de 1/);
 out.push('instantánea vieja deshabilita; 2/1 en uso avisa que no entran nuevas');
 console.log(out.join('\n'));console.log('UI SLOTS OK');
})().catch(e=>{console.error(e);process.exit(1)});
