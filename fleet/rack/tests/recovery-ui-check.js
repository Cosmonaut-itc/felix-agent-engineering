// The pending fixture and terminal receipt come from the real recovery engine.
context.pending=input.pending;context.answer=input.answer;
run('newActionId=()=>"recovery-ui-00001";sendOrder=()=>{};startOrder(h,"suites",[1,5,2,3,4],5);globalThis.a=PENDING.get(slotKey(h));globalThis.key=slotKey(h);acceptOrder(key,a,{status:"unknown"})');
run('render(structuredClone(pending));acceptOrder(key,a,answer);render(structuredClone(pending))');
assert.equal(run('PENDING.size'),0);
assert.equal(run('S.hosts[0].groups.suites.status'),'Running','cached pending group cannot replace terminal recovery');
assert.equal(run('S.hosts[0].control.reorder_groups.suites.owner'),null,'cached owner is already terminal');
assert.doesNotMatch(run('orderInfo(S.hosts[0],"suites").reason || ""'),/En pausa recuperando/);
// The cache can return repeatedly; another group's real pause must still appear.
run('pending.hosts[0].snapshot.groups.find(g=>g.name==="rapidas").status="Paused";pending.hosts[0].snapshot.control.pause_reasons.rapidas=["manual"];render(structuredClone(pending))');
assert.equal(run('S.hosts[0].groups.rapidas.status'),'Paused');
assert.deepEqual(JSON.parse(run('JSON.stringify(S.hosts[0].control.pause_reasons.rapidas)')),['manual']);
assert.equal(run('S.hosts[0].groups.suites.status'),'Running');
// A real later observation may have the SAME revision: manual/pressure pauses
// do not require a new user action. Its owner is not this terminal action.
for(const owner of [null,'another-recovery-001']){
 context.newOwner=owner;
 run('{globalThis.later=structuredClone(pending);const c=later.hosts[0].snapshot.control;c.pause_reasons.suites=[newOwner?"reorder":"manual"];c.reorder_groups.suites={...c.reorder_groups.suites,owner:newOwner,enabled:false,reason:newOwner?"En pausa recuperando… · "+newOwner:"Pausa manual nueva"};render(later)}');
 assert.equal(run('S.hosts[0].groups.suites.status'),'Paused','new same-revision pause remains visible');
 assert.equal(run('S.hosts[0].control.reorder_groups.suites.owner'),owner);
 assert.match(run('orderInfo(S.hosts[0],"suites").reason'),owner?/another-recovery-001/:/Pausa manual nueva/);
}
run('later.hosts[0].snapshot.control.revision++;render(later)');
assert.equal(run('S.hosts[0].groups.suites.status'),'Paused','higher revision remains authoritative');
console.log('Recovery snapshot OK: cached action phase ignored; real same-revision pauses retained');
