// Production JS on a synthetic DOM: prints the rendered panel and the host notice. No assertions here.
const vm=require('vm'),fs=require('fs');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
const elements={};
const el=()=>({innerHTML:'',textContent:'',addEventListener(){},focus(){},querySelector(){return {onclick:null}}});
const context={console,Intl,Date,Number,Object,String,Math,Map,Set,Uint8Array,JSON,
 crypto:require('crypto').webcrypto,AbortSignal:{timeout(){}},
 document:{getElementById:id=>elements[id] ||= el(),addEventListener(){},querySelector:()=>null,querySelectorAll:()=>[]},
 window:{addEventListener(){}},setInterval(){},clearTimeout(){},setTimeout(){return 0},
 sessionStorage:{getItem(){},setItem(){}},fetch:async()=>({ok:false})};
vm.createContext(context);vm.runInContext(input.js,context);context.fixture=input.fixture;
vm.runInContext('render(fixture)',context);
process.stdout.write(JSON.stringify({html:elements.app.innerHTML,notice:vm.runInContext('hostControlMessage(S.hosts[0])',context)}));
