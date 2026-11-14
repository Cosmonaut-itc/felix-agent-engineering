// Production JS; synthetic DOM. Retained groups and interrupted tasks after a desk restart.
const vm=require('vm'),assert=require('assert'),fs=require('fs');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
const elements={};
const el=()=>({innerHTML:'',textContent:'',addEventListener(){},focus(){},querySelector(){return {onclick:null}}});
const context={console,Intl,Date,Number,Object,String,Math,Map,Set,Uint8Array,JSON,
 crypto:require('crypto').webcrypto,AbortSignal:{timeout(){}},
 document:{getElementById:id=>elements[id] ||= el(),addEventListener(){},querySelector:()=>null,querySelectorAll:()=>[]},
 window:{addEventListener(){}},setInterval(){},clearTimeout(){},setTimeout(){return 1},
 sessionStorage:{getItem:()=>undefined,setItem(){}},fetch:async()=>({ok:false})};
vm.createContext(context);vm.runInContext(input.js,context);
const render=fixture=>{context.fixture=fixture;vm.runInContext('render(fixture)',context);return elements.app.innerHTML;};
const notes=html=>[...html.matchAll(/<div class="lane-notes">([\s\S]*?)<\/div><\/div>/g)].map(m=>m[1]);

const restarted=render(input.restarted);
assert.match(restarted,/Retenido al reiniciar: una tarea en fila ya tenía registro · resolver con control\.py reanudar pesadas/);
assert.match(restarted,/Cerrado al reiniciar · espera a Docker/);  // jale only waits for Docker
assert.match(restarted,/SEN-1 · Contract completa · Interrumpida al reiniciar Pueue; relanzar manualmente/);
assert.doesNotMatch(restarted,/#4 · /);  // a Killed row without the host mark is an ordinary cancel
assert.doesNotMatch(restarted,/Pausa tras reinicio/);
for(const block of notes(restarted)) assert.doesNotMatch(block,/<button/);  // resolved by SSH, never a button
assert.doesNotMatch(restarted,/>default</);  // empty default stays hidden (#131)
assert.equal(notes(restarted).length,2);

const normal=render(input.normal);
assert.doesNotMatch(normal,/lane-note/);
assert.doesNotMatch(normal,/Retenido|Interrumpida/);
assert.doesNotMatch(normal,/>default</);

const cut=render(input.cut_default);
assert.match(cut,/>default</);  // shows while it holds a task cut by the restart
assert.match(cut,/Retenido al reiniciar: grupo fuera del reparto · resolver con control\.py reanudar default/);
assert.match(cut,/#7 · Contract completa · Interrumpida al reiniciar Pueue; relanzar manualmente/);
console.log('UI startup OK');
