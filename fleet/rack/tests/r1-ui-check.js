(async()=>{
 const run=source=>vm.runInContext(source,context);
 run(`globalThis.found={h:S.hosts[0],t:S.hosts[0].tasks[0]};
      globalThis.action={request:{...actionRequest(found,'cancel'),action_id:'action-0001'},task:{...found.t},status:'cancelling'};
      globalThis.key=actionKey(found.h,found.t);PENDING.set(key,action);render(fixture)`);
 if(input.scenario==='a1'){
  run("acceptAction(key,action,{status:'cancelled',result:{exit:130}});render(fixture)");
  assert.equal(run('S.hosts[0].tasks.length'),0);
  for(const field of ['enqueued','start']){
   context.next=JSON.parse(JSON.stringify(input.fixture));context.next.hosts[0].snapshot.tasks[0][field]=1059;
   run('render(next)');assert.equal(run('S.hosts[0].tasks.length'),1,'new '+field+' attempt hidden');
   const reload={...context};vm.createContext(reload);vm.runInContext(input.js,reload);
   vm.runInContext('render(next)',reload);assert.equal(vm.runInContext('S.hosts[0].tasks.length',reload),1);
   vm.runInContext('render(fixture)',reload);assert.equal(vm.runInContext('S.hosts[0].tasks.length',reload),0);
  }
  console.log('displayed tasks after restart: 1; old attempt: 0; reload: preserved');
 }else if(input.scenario==='b1'){
  for(const [state,status] of [['Queued','cancelling'],['Stashed','unknown'],['Done','cancel_unconfirmed'],['Done','cancelling']]){
   context.next=JSON.parse(JSON.stringify(input.fixture));context.next.hosts[0].snapshot.tasks[0].state=state;
   context.pendingStatus=status;run('action.status=pendingStatus;render(next)');
   assert.match(elements.app.innerHTML,/data-id="77"/);
   assert.match(elements.app.innerHTML,/Cancelando…|Resultado desconocido|Cancelación sin confirmar/);
   assert.equal(run('PENDING.size'),1);
   console.log(state+' + '+status+': row visible: true; pending text visible: true');
  }
 }else if(input.scenario==='b5'){
  const sent=[];
  context.fetch=async(url,options)=>{
   if(url==='/api/rack.json') return {ok:true,json:async()=>input.fixture};
   if(options?.method==='POST'){
    sent.push(JSON.parse(options.body));if(sent.length===1) throw Error('initial POST never reached server');
    return {ok:true,json:async()=>({schema:'rack-action@1',action_id:'action-0001',status:'cancelling'})};
   }
   return {ok:false,status:404,json:async()=>({schema:'rack-action@1',action_id:'action-0001',status:'unknown'})};
  };
  await run('sendCancel(key,action)');await run('pollAction(key,action)');
  assert.equal(sent.length,2,'missing receipt must resend once');assert.deepEqual(sent[0],sent[1]);
  const reload={...context};vm.createContext(reload);vm.runInContext(input.js,reload);
  assert.equal(vm.runInContext('[...PENDING.values()][0].resent',reload),true,'retry flag must survive reload');
  await run('pollAction(key,action)');const notices=timers.filter(t=>t.ms===8000).length;
  await run('pollAction(key,action)');await run('pollAction(key,action)');
  assert.equal(sent.length,2);assert.equal(timers.filter(t=>t.ms===8000).length,notices,'repeating notice loop');
  context.fetch=async(url)=>({ok:true,json:async()=>url==='/api/rack.json'?input.fixture:
    {schema:'rack-action@1',action_id:'action-0001',status:'cancelled',result:{exit:130}}});
  await run('pollAction(key,action)');assert.equal(run('PENDING.size'),0);assert.match(elements.toast.innerHTML,/cancelada/);
  console.log('404 x3: one resend; identical body/action_id; no repeated notices; final cancelled; PENDING.size=0');
 }
})().catch(error=>{console.error(error);process.exitCode=1});
