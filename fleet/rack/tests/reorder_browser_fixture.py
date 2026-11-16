"""Emit a self-contained page to drag the Mini's queue in a real browser; never starts a server.

TMPDIR=/tmp PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 tests/reorder_browser_fixture.py OUTPUT.html
The page answers /api/accion with receipts that the real control.py produced
for the same queue (through the rack's sanitizer); `window.REORDER_NEXT`
chooses applied, unknown or conflict. No collector, controller or network.
"""
import json
from pathlib import Path
import sys
import test_reorder as rack


def build():
    def fixture(paused=False):
        test = rack.RackReorderFixture('run'); test.setUp()
        try:
            if paused:
                with test.engine.connect(write=True) as db:
                    state = test.engine.state(db); state['reasons']['suites'] = ['manual']; test.engine.store(db, state)
                test.p.groups['suites']['status'] = 'Paused'
                test.snapshot()
            return test.page(now=1_800_000_000)
        finally:
            test.doCleanups()

    def receipts(order):
        test = rack.RackReorderFixture('run'); test.setUp()
        try:
            return test.receipts(order)
        finally:
            test.doCleanups()
    return dict(normal=fixture(), paused=fixture(paused=True),
                receipts={json.dumps(o, separators=(',', ':')): receipts(o) for o in ([1, 5, 2, 3, 4], [1, 2, 3, 5, 4])})


SCRIPT = '''
window.REORDER_NEXT = 'applied'; window.REORDER_DELAY = 500; window.POSTS = [];
let lastReceipts = null;
window.fetch = async (url, options) => {
  const reply = (status, data) => ({ok: status < 400, status, json: async () => structuredClone(data)});
  if (url === '/api/rack.json') return reply(200, {...REORDER_FIXTURES.current, now: NOW});
  if (url === '/api/accion') {
    const request = JSON.parse(options.body); POSTS.push(request);
    await new Promise(r => setTimeout(r, REORDER_DELAY));
    // The real receipt, bound to this page's own action id as the rack would bind it.
    const stamp = data => ({...data, action_id: request.action_id, served_at: NOW + 1,
      ...(data.result ? {result: {...data.result, undo_of: data.result.undo_of && request.action_id}} : {})});
    if (REORDER_NEXT === 'unknown') return reply(200, stamp({schema: 'rack-action@1', status: 'unknown', error: null}));
    if (request.op === 'reorder_undo') {
      if (REORDER_NEXT === 'undo_conflict') return reply(409, stamp({schema: 'rack-action@1', status: 'conflict', effects: false,
        error: {code: 'undo_conflict', message: 'La fila cambió; no se puede deshacer'}}));
      return reply(200, stamp(lastReceipts.undone));
    }
    lastReceipts = REORDER_FIXTURES.receipts[JSON.stringify(request.change.order)];
    return reply(200, stamp(lastReceipts.applied));
  }
  if (url.startsWith('/api/accion/')) return reply(200, {schema: 'rack-action@1', action_id: url.split('/').pop(), status: 'unknown', error: null});
  throw Error('Synthetic fixture: network forbidden');
};
window.useReorderScenario = name => {
  PENDING.clear(); CONFIRMED_ORDER.clear(); MOVED.clear(); POSTS.length = 0;
  REORDER_FIXTURES.current = structuredClone(REORDER_FIXTURES[name]);
  NOW = 0; render(structuredClone(REORDER_FIXTURES.current)); document.getElementById('toast').textContent = '';
};
useReorderScenario('normal');
'''


def main():
    output = Path(sys.argv[1]).resolve()
    source = (rack.BASE / 'index.html').read_text()
    marker = 'refresh(); setInterval(refresh,15000);'
    assert source.count(marker) == 1
    output.write_text(source.replace(marker, 'const REORDER_FIXTURES=' + json.dumps(build(), ensure_ascii=False) + ';\n' + SCRIPT))
    print(str(output))


if __name__ == '__main__':
    main()
