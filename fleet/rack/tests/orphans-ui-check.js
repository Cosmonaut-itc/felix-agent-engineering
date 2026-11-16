// Production JS with a synthetic DOM that records every innerHTML write on built nodes.
const vm=require('vm'),fs=require('fs');
const input=JSON.parse(fs.readFileSync(0,'utf8')),elements={},writes=[];
let boxes=[];
class El{
  constructor(tag){this.tagName=tag.toUpperCase();this.children=[];this.className='';this.title='';this.dataset={};}
  append(...parts){for(const p of parts) this.children.push(p instanceof El ? p : {text:String(p)});}
  get textContent(){return this.children.map(c=>c instanceof El ? c.textContent : c.text).join('');}
  set textContent(v){this.children=[{text:String(v)}];}
  get innerHTML(){return this.html ?? '';}
  set innerHTML(v){this.html=String(v);if(this.tagName!=='APP') writes.push(String(v));}
  addEventListener(){}
}
const tree=e=>({tag:e.tagName,cls:e.className,text:e.textContent,title:e.title,open:e.dataset.open,type:e.type,
  children:e.children.filter(c=>c instanceof El).map(tree)});
const context={console,Intl,Date,Number,Object,String,Math,Map,Set,Uint8Array,JSON,
 crypto:require('crypto').webcrypto,AbortSignal:{timeout(){}},
 document:{getElementById:id=>elements[id] ||= new El(id==='app' ? 'app' : 'div'),createElement:tag=>new El(tag),
  addEventListener(){},querySelector:()=>null,
  querySelectorAll:sel=>sel!=='[data-orphans]' ? [] : (boxes=[...elements.app.innerHTML.matchAll(/data-orphans="([^"]*)"/g)]
    .map(m=>{const b=new El('section');b.dataset.orphans=m[1];return b;}))},
 window:{addEventListener(){}},setInterval(){},setTimeout(){},clearTimeout(){},
 sessionStorage:{getItem(){return null},setItem(){}},fetch:async()=>({ok:false})};
vm.createContext(context);vm.runInContext(input.js,context);context.fixture=input.fixture;
vm.runInContext('render(fixture)',context);
if(input.open) vm.runInContext("OPEN[JSON.stringify(['orphans','desk'])]=true;paint()",context);
process.stdout.write(JSON.stringify({html:elements.app.innerHTML,boxes:boxes.map(tree),writes}));
