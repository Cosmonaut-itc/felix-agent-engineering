"""Pause a real admitted control in select; send the sync ACK after its deadline."""
import contextlib, json, multiprocessing as mp, os, signal, sys, time, unittest
from pathlib import Path
sys.path.insert(0, str(Path.cwd()/'fleet'))
from deskpolicy.tests.test_policy import PolicyTest, cli_actor
from deskpolicy.store import held, read_json, code_digest

CTX=mp.get_context('fork')
print('LATE module_sha256', code_digest(), 'affinity', sorted(os.sched_getaffinity(0)), flush=True)

def run(delay_ack):
    case=PolicyTest(); case.setUp()
    reader,writer=os.pipe()
    worker=None
    try:
        case.pending('late',weight=1); case.publish()
        rd=case.h.directory/'runs/run-late'; rd.mkdir(parents=True,mode=0o700)
        marker=case.h.directory/'executed'
        args=['ejecutar','--request-id','late','--run-id','run-late','--lane','1','--recibo',
              '--sync-ready-fd',str(reader),'--sync-timeout-s','3','--','/usr/bin/touch',str(marker)]
        parent,child=CTX.Pipe()
        worker=CTX.Process(target=cli_actor,args=(case.h,args,child)); worker.start(); child.close()
        case.children.append((worker,parent))
        expires=time.monotonic()+20
        while True:
            if (rd/'pao-admission.json').exists():
                # A blocking pipe wait proves execution has reached the production select.
                wchan=Path(f'/proc/{worker.pid}/wchan').read_text().strip()
                if wchan in ('poll_schedule_timeout.constprop.0','poll_schedule_timeout','do_select','__pollwait') or 'poll_schedule_timeout' in wchan:
                    break
            if time.monotonic()>=expires: raise RuntimeError('control did not reach select: '+wchan)
            time.sleep(.005)
        admitted_at=time.monotonic()
        print('LATE before',delay_ack,'wait',wchan,'marker',marker.exists(),'life',held(case.engine.life_path('hasito',1)),flush=True)
        if delay_ack:
            os.kill(worker.pid,signal.SIGSTOP)
            _,status=os.waitpid(worker.pid,os.WUNTRACED)
            assert os.WIFSTOPPED(status)
            time.sleep(3.3)
        os.write(writer,b'1')
        ack_age=time.monotonic()-admitted_at
        if delay_ack: os.kill(worker.pid,signal.SIGCONT)
        assert parent.poll(60),'control did not finish'
        result=parent.recv(); worker.join(60)
        receipt=read_json(rd/'pao-admission.json')
        print('LATE result',json.dumps({'delayed':delay_ack,'ack_after_observed_admission_s':round(ack_age,3),'wrapper_rc':result['rc'],'script_executed':marker.exists(),'claim_state':receipt['claim_state'],'close_cause':receipt['close_cause'],'script_exit_code':receipt['script_exit_code'],'weight':case.engine.status()['weight'],'life_held':held(case.engine.life_path('hasito',1))}),flush=True)
        return result['rc'],receipt,marker.exists()
    finally:
        if worker is not None:
            with contextlib.suppress(ProcessLookupError): os.kill(worker.pid,signal.SIGCONT)
        os.close(reader); os.close(writer); case.tearDown()

normal=run(False)
late=run(True)
assert normal[0]==0 and normal[2]
assert late[0]==75 and late[1]['close_cause']=='sync-timeout' and not late[2], 'late sync ACK executed after its deadline'
