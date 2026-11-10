"""Measure closure ticks and day recovery under 100 unrelated forks/s."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path.cwd() / 'fleet'))
from deskpolicy.store import code_digest, read_json
from deskpolicy.tests.test_policy import PolicyTest


FORKER = '''import json,os,signal,time
stop=False
def end(*args):
    global stop
    stop=True
signal.signal(signal.SIGTERM,end)
os.setsid()
print("ready",flush=True)
started=time.monotonic()
count=0
while not stop:
    child=os.fork()
    if child==0: os._exit(0)
    os.waitpid(child,0)
    count+=1
    time.sleep(max(0,started+count/100-time.monotonic()))
elapsed=time.monotonic()-started
print(json.dumps({"forks":count,"elapsed_s":elapsed,"forks_per_s":count/elapsed,
                  "affinity":sorted(os.sched_getaffinity(0))}),flush=True)
'''


def execute(case, name, lane=1):
    result = case.run_cli(['ejecutar', '--request-id', name, '--run-id', 'run-' + name,
                           '--lane', str(lane), '--', '/bin/true'])
    receipt = json.loads(result['output'].splitlines()[-1])
    return result['rc'], receipt


def closure_ticks():
    outcomes = []
    for index in range(20):
        case = PolicyTest()
        case.setUp()
        try:
            case.h.now = case.h.now.replace(hour=16)
            case.pending('true')
            rc, receipt = execute(case, 'true')
            before = read_json(case.engine.record_path('true'))['claim']['state']
            ticks = 0
            if before != 'closed':
                case.engine.reconcile()
                ticks = 1
            after = read_json(case.engine.record_path('true'))['claim']['state']
            outcomes.append({'rc': rc, 'initial': before, 'final': after,
                             'ticks': ticks if after == 'closed' else None})
        finally:
            case.tearDown()
    print('R5 CHURN20', json.dumps(outcomes), flush=True)
    return all(x['rc'] == 0 and x['ticks'] in (0, 1) for x in outcomes)


def no_writer():
    case = PolicyTest()
    case.setUp()
    outcomes = []
    try:
        case.h.now = case.h.now.replace(hour=16)
        case.publish()
        for index in range(40):
            name = 'day-' + str(index)
            case.pending(name)
            rc, receipt = execute(case, name, 1 + index % 2)
            outcomes.append({'rc': rc, 'state': receipt.get('claim_state'),
                             'reason': receipt.get('reason')})
            if rc != 0:
                case.engine.register(name, 'hasito', 'script', 'test', 1, withdraw=True)
        print('R5 NOWRITER40', json.dumps(outcomes), 'live', case.engine.status()['live'], flush=True)
        return all(x['rc'] == 0 and x['reason'] is None for x in outcomes)
    finally:
        case.tearDown()


print('R5 CHURN module_sha256', code_digest(), 'affinity', sorted(os.sched_getaffinity(0)), flush=True)
assert sorted(os.sched_getaffinity(0)) == [0, 1]
with subprocess.Popen([sys.executable, '-I', '-c', FORKER], stdout=subprocess.PIPE,
                      text=True) as generator:
    assert generator.stdout.readline().strip() == 'ready'
    try:
        closures = closure_ticks()
        admission = no_writer()
    finally:
        generator.terminate()
        report = generator.communicate(timeout=60)[0]
        print('R5 GENERATOR', report.strip(), flush=True)
    assert generator.returncode == 0
    measured = json.loads(report)
    assert 80 <= measured['forks_per_s'] <= 110, 'generator did not sustain about 100 forks/s'
    assert measured['affinity'] == [0, 1]
    assert closures and admission, 'closure or no-writer capacity regression'
