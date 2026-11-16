// U3.6 in a real Chromium: drag, Alt+arrows, Escape, pointercancel, toast, undo, unknown and paused.
// NODE_PATH=<node_modules con playwright> node tests/reorder-browser-check.js PAGE.html OUT_DIR
// PAGE.html comes from reorder_browser_fixture.py; it has no network.
const {chromium}=require('playwright');
const assert=require('assert'),path=require('path');
const [page_path,out]=process.argv.slice(2);
const url='file://'+path.resolve(page_path);
const row=id=>`ol[data-group="suites"] li[data-id="${id}"]`;

async function open(browser,width,scheme,reduced='no-preference'){
  const page=await browser.newPage({viewport:{width,height:width<500?1100:860},colorScheme:scheme,reducedMotion:reduced});
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto(url);page.errors=errors;
  return page;
}
const ids=page=>page.$$eval('ol[data-group="suites"] li',xs=>xs.map(x=>Number(x.dataset.id)));
const shot=(page,name)=>page.locator('ol[data-group="suites"]').evaluate(e=>e.closest('.lane').scrollIntoView({block:'center'}))
  .then(()=>page.screenshot({path:path.join(out,name+'.png')}));
const noScroll=page=>page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth);

async function drag(page,from,to,{shotMid,cancel}={}){
  const a=await page.locator(row(from)).boundingBox(), b=await page.locator(row(to)).boundingBox();
  const touchless=page.viewportSize().width>=500;
  // Mouse lifts from anywhere on the row; the phone layout is checked with the handle.
  const handle=await page.locator(row(from)+' .h').count() ? await page.locator(row(from)+' .h').boundingBox() : a;
  const x=touchless ? a.x+a.width/2 : handle.x+3;
  await page.mouse.move(x,a.y+a.height/2);await page.mouse.down();
  const target=b.y+b.height*0.3;
  for(let k=1;k<=12;k++){await page.mouse.move(x,a.y+a.height/2+(target-a.y-a.height/2)*k/12);await page.waitForTimeout(16);}
  await page.waitForTimeout(200);
  if(shotMid) await shot(page,shotMid);
  if(cancel==='escape'){await page.keyboard.press('Escape');await page.mouse.up();return;}
  if(cancel==='pointercancel'){await page.evaluate(()=>document.dispatchEvent(new PointerEvent('pointercancel')));await page.mouse.up();return;}
  await page.mouse.up();
}

(async()=>{
  // The Chrome already on the machine; this check never downloads a browser.
  const browser=await chromium.launch({channel:'chrome',headless:true});
  const results=[];
  try{
    for(const width of [1280,390]) for(const scheme of ['light','dark']){
      const tag=`${width}-${scheme}`, page=await open(browser,width,scheme);
      assert.deepEqual(await ids(page),[1,2,3,4,5]);
      assert(await noScroll(page),'no horizontal scroll '+tag);
      await shot(page,`inicial-${tag}`);
      // Escape and pointercancel restore without a request.
      await drag(page,5,2,{cancel:'escape'});
      assert.deepEqual(await ids(page),[1,2,3,4,5]);
      await drag(page,5,2,{cancel:'pointercancel'});
      assert.deepEqual(await ids(page),[1,2,3,4,5]);
      assert.equal(await page.evaluate(()=>POSTS.length),0);
      // Drag 5 → 2: the lifted row follows the pointer, then «Reordenando…» and the group is locked.
      await drag(page,5,2,{shotMid:`arrastre-${tag}`});
      await page.waitForSelector('ol[data-group="suites"][aria-busy="true"]');
      assert.match(await page.textContent('.fila h4'),/Reordenando…/);
      assert.deepEqual(await page.evaluate(()=>POSTS[0].change.order),[1,5,2,3,4]);
      await shot(page,`reordenando-${tag}`);
      await page.waitForSelector('#toast button');
      assert.deepEqual(await ids(page),[1,5,2,3,4]);
      assert.match(await page.textContent('#toast'),/pasó al lugar 2 de la fila de suites/);
      assert.equal(await page.textContent(row(5)+' .pos'),'5→2');
      assert(await page.$(row(5)+'.moved'));
      await shot(page,`aplicado-${tag}`);
      // Undo from the toast restores the receipt's exact priorities.
      await page.click('#toast button');
      await page.waitForFunction(()=>document.getElementById('toast').textContent==='Orden anterior restaurado');
      assert.deepEqual(await ids(page),[1,2,3,4,5]);
      const posts=await page.evaluate(()=>POSTS);
      assert.equal(posts[1].op,'reorder_undo');assert.equal(posts[1].undo_of,posts[0].action_id);
      // Alt+↑ keeps the keyboard focus on the moved row through the request and the reply.
      await page.focus(row(5));await page.keyboard.press('Alt+ArrowUp');
      assert.equal(await page.evaluate(()=>document.activeElement.dataset.id),'5');
      await page.waitForSelector('#toast button');
      assert.deepEqual(await ids(page),[1,2,3,5,4]);
      assert.equal(await page.evaluate(()=>document.activeElement.dataset.id),'5');
      // Undo conflict.
      await page.evaluate(()=>{REORDER_NEXT='undo_conflict'});
      await page.click('#toast button');
      await page.waitForFunction(()=>document.getElementById('toast').textContent==='La fila cambió; no se puede deshacer');
      await shot(page,`deshacer-conflicto-${tag}`);
      // Unknown: no success, a visible consulting note.
      await page.evaluate(()=>{useReorderScenario('normal');REORDER_NEXT='unknown'});
      await drag(page,5,2);
      await page.waitForFunction(()=>document.querySelector('.fila h4').textContent.includes('No se pudo confirmar el orden · consultando'));
      assert.equal(await page.textContent('#toast'),'');
      await shot(page,`desconocido-${tag}`);
      // Paused with its owner; nothing draggable.
      await page.evaluate(()=>{useReorderScenario('paused');REORDER_NEXT='applied'});
      assert.match(await page.textContent('.fila h4'),/No se puede reordenar mientras el grupo está pausado · pausa manual/);
      assert.equal(await page.$$eval('ol[data-group="suites"] li[tabindex]',x=>x.length),0);
      await shot(page,`pausado-${tag}`);
      await drag(page,5,2);
      assert.equal(await page.evaluate(()=>POSTS.length),0);
      assert.deepEqual(page.errors,[]);
      results.push(tag);await page.close();
    }
    // Reduced motion: no sliding neighbours and a static highlight.
    const page=await open(browser,1280,'light','reduce');
    await drag(page,5,2);
    await page.waitForSelector('#toast button');
    assert.equal(await page.$eval(row(5),e=>getComputedStyle(e).animationName),'none');
    assert.equal(await page.$eval(row(1),e=>e.style.transition),'');
    results.push('reduced-motion');await page.close();
  }finally{await browser.close();}
  console.log('UI reorder browser OK: '+results.join(', '));
})().catch(e=>{console.error(e);process.exit(1);});
