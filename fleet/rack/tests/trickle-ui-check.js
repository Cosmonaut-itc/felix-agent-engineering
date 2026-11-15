// The production script runs with the same synthetic-DOM pattern as ui-check.js.
const vm=require('vm'),assert=require('assert'),fs=require('fs');
const input=JSON.parse(fs.readFileSync(0,'utf8')), elements={};
const element=()=>({addEventListener(){},innerHTML:'',textContent:''});
const context={console,Intl,Date,Number,Object,String,Math,Map,Set,JSON,Uint8Array,
 crypto:require('crypto').webcrypto,document:{getElementById:id=>elements[id] ||= element(),
 addEventListener(){},querySelector:()=>null,querySelectorAll:()=>[]},window:{addEventListener(){}},
 setInterval(){},setTimeout(){},clearTimeout(){},sessionStorage:{getItem(){return null}},
 AbortSignal:{timeout(){}},fetch:async()=>({ok:false})};
vm.createContext(context);vm.runInContext(input.js,context);context.fixture=input.fixture;
vm.runInContext('render(fixture)',context);
let out=elements.app.innerHTML;
assert.match(out,new RegExp(input.expected));
for(const text of input.absent || [])assert.ok(!out.includes(text),text+' unexpectedly present');
if(input.held)assert.equal((out.match(/class="slot held/g)||[]).length,1);
process.stdout.write(out);
