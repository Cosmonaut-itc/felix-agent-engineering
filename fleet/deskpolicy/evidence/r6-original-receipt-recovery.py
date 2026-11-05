"""Real child/uncertain close, transient receipt write failures, writer then replay."""
import json, os, sys, time, unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0,str(Path.cwd()/'fleet'))
from deskpolicy import cli
from deskpolicy.store import read_json, code_digest
from deskpolicy.tests.test_policy import PolicyTest

case=PolicyTest(); case.setUp()
try:
    case.pending('receipt-recovery'); case.publish()
    run=case.h.directory/'runs/run-receipt-recovery'; run.mkdir(parents=True,mode=0o700)
    pidfile=case.h.directory/'child.pid'
    program='import subprocess,pathlib,sys; p=subprocess.Popen(["/bin/sleep","1.5"],close_fds=True); pathlib.Path(sys.argv[1]).write_text(str(p.pid)); raise SystemExit(3)'
    args=['ejecutar','--request-id','receipt-recovery','--run-id','run-receipt-recovery','--lane','1','--recibo','--',sys.executable,'-I','-c',program,str(pidfile)]
    real_atomic=cli.atomic_json
    writes=0
    def transient(path,value,**kw):
        global writes
        if path.name=='pao-admission.json':
            writes+=1
            if writes>1: raise OSError('receipt-volume-unavailable')
        return real_atomic(path,value,**kw)
    print('RECOVERY module_sha256',code_digest(),'affinity',sorted(os.sched_getaffinity(0)),flush=True)
    with mock.patch.object(cli,'atomic_json',side_effect=transient):
        result=case.run_cli(args)
    claim=read_json(case.engine.record_path('receipt-recovery'))['claim']
    before=read_json(run/'pao-admission.json')
    print('RECOVERY before',json.dumps({'wrapper_rc':result['rc'],'claim_state':claim['state'],'claim_code':claim['script_exit_code'],'receipt_state':before['claim_state'],'receipt_code':before['script_exit_code']}),flush=True)
    assert result['rc']==3 and claim['state']=='uncertain' and claim['script_exit_code']==3
    os.waitpid(int(pidfile.read_text()),0)
    deadline=time.monotonic()+60
    while True:
        closed=case.engine.reconcile()['closed']
        if 'receipt-recovery' in closed: break
        assert time.monotonic()<deadline
        time.sleep(.02)
    reconciled=read_json(run/'pao-admission.json')
    replay=case.run_cli(args)
    view=json.loads(replay['output'])
    final=read_json(run/'pao-admission.json')
    print('RECOVERY after',json.dumps({'closed':closed,'claim_code':read_json(case.engine.record_path('receipt-recovery'))['claim']['script_exit_code'],'reconciled_receipt_code':reconciled['script_exit_code'],'replay_rc':replay['rc'],'replay_execute':view['execute'],'replay_code':view['script_exit_code'],'final_receipt_state':final['claim_state'],'final_receipt_code':final['script_exit_code'],'close_cause':final['close_cause']}),flush=True)
    assert final['script_exit_code']==3,'reconciled closed receipt never recovers its known script exit code'
finally:
    case.tearDown()
