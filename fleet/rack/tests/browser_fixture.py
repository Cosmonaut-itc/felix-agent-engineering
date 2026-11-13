"""Loopback-only synthetic rack for browser QA. Ctrl-C closes it and removes fixtures.

TMPDIR=/tmp /usr/bin/python3 tests/browser_fixture.py --port 0
Never starts the collector or accesses installed controllers, Pueue or Tailscale.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
from http.server import ThreadingHTTPServer
from unittest.mock import Mock
import safety  # same global guard as the complete suite

BASE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('rack_serve', BASE / 'serve.py')
serve = importlib.util.module_from_spec(spec)
spec.loader.exec_module(serve)
REF = '20261112-184713-d976b4'
HOSTS = serve.load_hosts(BASE / 'hosts.json')
ENQUEUED = time.time() - 90


def fixture():
    now=time.time()
    def host(config):
        row=dict(id=77,group='suites',state='Queued',name='contract',ticket='SEN-123',project='Hasito',
                 enqueued=ENQUEUED,start=None,end=None,result=None,scope=None,repo=None,sha=None,branch=None,subject=None,priority=0,
                 control=dict(launcher='remote-run',ref=REF,expected=dict(task_id=77,state='Queued'),reason=None))
        snap=dict(schema='rack-host@1',host=config['id'],taken=now,groups=[dict(name='suites',status='Running',slots=1)],
                  tasks=[row],cpu=dict(cores=10,load=[1,1,1]),mem=dict(total=16*1024**3,available=12*1024**3,swap_total=0,swap_used=0))
        return dict(id=config['id'],name=config['name'],fresh=True,age=0,error=None,snapshot=snap,estimates={},
                    actions=dict(reason=serve.accion.transport_reason(config, 'remote-run')))
    return dict(schema='rack@1',now=now,hosts=[host(h) for h in HOSTS],tickets={})


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=0)
    args=parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='rack-c3-browser-',dir='/tmp') as folder:
        root=Path(folder)
        control=root/'.local/lib/remote-run/run-control.py'
        control.parent.mkdir(parents=True)
        control.write_text('''import json,sys,time
from pathlib import Path
r=json.load(sys.stdin)
p=Path(__file__).with_name(r.get('action_id','capabilities')+'.json')
a=dict(schema='rack-cancel-result@1',ref=r['ref'])
if r['op']=='capabilities':
 a['capabilities']=dict(queued=True,running=True,reason=None)
else:
 a['action_id']=r['action_id']
 if r['op']=='cancel' and not p.exists():p.write_text(str(time.time()))
 a['status']='cancelled' if p.exists() and time.time()-float(p.read_text())>7 else 'cancelling'
 a['result']={'exit':130} if a['status']=='cancelled' else None
 a['error']=None
print(json.dumps(a))
''')
        executor=serve.accion.Executor(home=root)
        actions=serve.accion.Actions(HOSTS,root,fixture,executor=executor)
        server=ThreadingHTTPServer(('127.0.0.1',args.port),serve.Handler)
        server.actions=actions
        server.owner_check=Mock();server.owner_check.allows.return_value=True
        server.rack=Mock();server.rack.read.side_effect=fixture
        server.rack.refresh.side_effect=lambda timeout:fixture()
        print(json.dumps(dict(url='http://127.0.0.1:'+str(server.server_port),pid=os.getpid(),temporary=str(root))),flush=True)
        try:server.serve_forever()
        except KeyboardInterrupt:pass
        finally:server.server_close()


if __name__=='__main__':main()
