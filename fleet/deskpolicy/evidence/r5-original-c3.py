import sys
from pathlib import Path
sys.path.insert(0,str(Path.cwd()/'fleet'))
import json, os, time, unittest
from unittest import mock
import multiprocessing as mp
from deskpolicy.tests.test_policy import PolicyTest, cli_actor
from deskpolicy.store import read_json, process, session_members, code_digest
print("NEW affinity",sorted(os.sched_getaffinity(0)),"module_sha256",code_digest(),flush=True)


def until(check, label):
    deadline=time.monotonic()+15
    while time.monotonic()<deadline:
        if check(): return
        time.sleep(.01)
    raise AssertionError(label)


def live(pid):
    try: return not process(pid)['zombie']
    except (FileNotFoundError,ProcessLookupError): return False


case=PolicyTest()
case.setUp()
try:
    case.pending('fork-race')
    case.publish()
    base=case.h.directory
    ready,go,born=base/'stage.pid',base/'go',base/'born.pid'
    program='''import os,pathlib,subprocess,time
pathlib.Path({ready!r}).write_text(str(os.getpid()))
while not pathlib.Path({go!r}).exists(): time.sleep(.005)
p=subprocess.Popen(['/bin/sleep','60'],close_fds=True)
pathlib.Path({born!r}).write_text(str(p.pid))
os._exit(0)
'''.format(ready=str(ready),go=str(go),born=str(born))
    arguments=['ejecutar','--request-id','fork-race','--run-id','run-fork-race','--lane','1','--',sys.executable,'-I','-c',program]
    ctx=mp.get_context('fork')
    parent,child=ctx.Pipe()
    worker=ctx.Process(target=cli_actor,args=(case.h,arguments,child))
    worker.start(); child.close(); case.children.append((worker,parent))
    until(ready.exists,'stage start')
    stage_pid=int(ready.read_text())
    case.extra_pids.append((stage_pid,process(stage_pid)['start']))
    worker.kill(); worker.join(5)
    claim=case.engine.status()['claims'][0]
    print('REPRO fork-race before',json.dumps({'claim_state':claim['state'],'session_members':session_members(claim['process']['sid']),'cpus':claim['cpus']}),flush=True)
    original_iterdir=Path.iterdir
    fired=False
    def pause_after_proc_snapshot(path):
        global fired
        if path==Path('/proc') and not fired:
            fired=True
            entries=list(original_iterdir(path))
            go.touch()
            until(born.exists,'descendant born')
            new_pid=int(born.read_text())
            case.extra_pids.append((new_pid,process(new_pid)['start']))
            until(lambda:not live(stage_pid),'stage exit')
            print('REPRO fork-race schedule',json.dumps({'stage_in_snapshot':Path('/proc')/str(stage_pid) in entries,'new_child_in_snapshot':Path('/proc')/str(new_pid) in entries,'stage_alive':live(stage_pid),'new_child_alive':live(new_pid)}),flush=True)
            return iter(entries)
        return original_iterdir(path)
    with mock.patch.object(Path,'iterdir',pause_after_proc_snapshot):
        result=case.engine.reconcile()
    new_pid=int(born.read_text())
    durable=read_json(case.engine.record_path('fork-race'))['claim']
    print('REPRO fork-race reconcile',json.dumps(result),flush=True)
    print('REPRO fork-race after',json.dumps({'claim_state':durable['state'],'close_cause':durable['close_cause'],'child_alive':live(new_pid),'child_sid':process(new_pid)['sid'],'claim_sid':durable['process']['sid'],'session_members':session_members(durable['process']['sid']),'live':case.engine.status()['live']}),flush=True)
    case.h.advance(120)
    case.pending('replacement')
    case.publish()
    receipt,_,_=case.launch('replacement')
    print('REPRO fork-race replacement',json.dumps({k:receipt[k] for k in ('state','lane','cpus','execute')}),'original_child_alive',live(new_pid),flush=True)
finally:
    case.tearDown()

assert result["closed"] == []
assert durable["state"] == "uncertain"
assert receipt["state"] == "denied"
print("R5 ORIGINAL C3 PASS", flush=True)
