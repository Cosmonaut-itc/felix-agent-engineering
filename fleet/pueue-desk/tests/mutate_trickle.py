"""SEN-6417 directed mutants: each must fail an assertion, nominal first."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

SOURCE=Path(__file__).resolve().parents[1]
CASES=[
 ('omit_one',"            if normal == 'mantener' and can_open:","            if False:",'test_band_positive_fifo_and_property_then_low'),
 ('open_group',"self.pueue.call('start',ident)","self.pueue.call('start','-g',group)",'test_band_positive_fifo_and_property_then_low'),
 ('gap_58','ADMISSION_GAP_NS = 60_000_000_000','ADMISSION_GAP_NS = 58_000_000_000','test_global_cooldown_instances_59999_60'),
 ('quota',"occupied += bool(set(status) & {'Running','Paused','Locked'})","occupied += bool(set(status) & {'Running'})",'test_quota_full_plus_other_free_counts_paused_locked'),
 ('dependency',"if any(tasks.get(str(d),{}).get('status',{}).get('Done',{}).get('result') != 'Success' for d in dependencies):","if False:",'test_dependencies_and_mixed_reasons'),
 ('completion',"attempt['completed_ns'] = ns if outcome in ('confirmed','not_sent') else None","attempt['completed_ns'] = attempt['reserved_ns'] if outcome in ('confirmed','not_sent') else None",'test_slow_rpc_cooldown_starts_at_completion'),
 ('unknown',"if attempt and attempt['outcome'] in ('reserved','unknown'):","if False:",'test_reused_identity_unknown_does_not_expire'),
 ('identity',"(('id','id'),('group','grupo'),('created_at','created_at'))","(('id','id'),('group','grupo'))",'test_reused_identity_unknown_does_not_expire'),
 ('final_gate',"if final_candidate != candidate:","if False:",'test_final_gate_failure_consumes_permission_without_replacement'),
 ('generation',"if self.generation(state) != state['generation']:\n                return 'generation', None, None, started","if False:\n                return 'generation', None, None, started",'test_retention_generation'),
 ('logging',"log(self.directory,'admision_intento',tick_id=tick,grupo=group,id=ident)","log(self.directory,'admision_intento',tick_id=tick,grupo=group,id=ident,detalle=snapshot)",'test_lost_response_confirmed_only_by_observation'),
]

def run(root,test=None):
    return subprocess.run([sys.executable,'-B','-m','unittest','test_trickle'+('.TrickleTests.'+test if test else '')],cwd=str(root/'tests'),
        env=dict(os.environ,TMPDIR='/tmp',PYTHONDONTWRITEBYTECODE='1'),capture_output=True,text=True,timeout=30)

def main():
    nominal=run(SOURCE)
    if nominal.returncode:raise AssertionError('Nominal failed: '+nominal.stderr)
    print('Nominal: '+nominal.stderr.strip(),flush=True)
    for name,anchor,replacement,test in CASES:
        root=Path(tempfile.mkdtemp(prefix='sen6417-sol-mutant-',dir='/tmp'))/'pueue-desk'
        shutil.copytree(SOURCE,root,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        path=root/'control.py';source=path.read_text()
        if source.count(anchor)!=1:raise AssertionError('Anchor '+name)
        source=source.replace(anchor,replacement)
        if name=='unknown':
            source=source.replace("ns-attempt['completed_ns']","ns-(attempt['completed_ns'] or 0)")
        path.write_text(source)
        result=run(root,test)
        if result.returncode!=1 or 'FAIL:' not in result.stderr or 'ERROR:' in result.stderr:raise AssertionError(name+': '+result.stderr)
        print(name+': AssertionError; '+test,flush=True)
    print(str(len(CASES))+'/'+str(len(CASES))+' killed by assertion',flush=True)

if __name__=='__main__':main()
