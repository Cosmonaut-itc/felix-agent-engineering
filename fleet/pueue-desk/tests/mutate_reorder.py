"""Directed safety mutants. Run with each supported interpreter; no real Pueue."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

SOURCE=Path(__file__).resolve().parents[1]
T='test_reorder.ReorderTests.'
CASES=[
 ('coordinated clients','control.py','        if not self.coordinated_reorder:\n','        if False:\n',T+'test_paused_reasons_manual_guard_ambiguous_and_startup'),
 ('pause intent owner','control.py',"        state['reasons'][g] = ['reorder']","        state['reasons'][g] = []",T+'test_prepared_intent_precedes_pause'),
 ('prepared durable before pause','control.py',"revision=state['revision'], phase='prepared', at=self.clock(), process=None, pause_owner=request['action_id'],","revision=state['revision'], phase='paused', at=self.clock(), process=None, pause_owner=request['action_id'],",T+'test_prepared_intent_precedes_pause'),
 ('etag exact','control.py',"if request['expected'] != expected or gen != state['generation'] or before != state['desired']:","if gen != state['generation'] or before != state['desired']:",T+'test_stale_etag_revision_policy_generation_reject_without_pause'),
 ('paused never reordered','control.py',"if groups[g]['status'] != 'Running' or reasons or os.path.lexists(self.pause_fence(g)):","if False:",T+'test_paused_reasons_manual_guard_ambiguous_and_startup'),
 ('undo restores exact before','control.py',"            target = deepcopy(previous['before'])","            target = deepcopy(previous['target'])",T+'test_normal_and_exact_undo'),
 ('undo exact set','control.py',"or self.clock() > previous.get('undo_until', 0) or previous['target'] != rows","or self.clock() > previous.get('undo_until', 0)",T+'test_add_after_compare_preserved_but_strict_undo_conflicts'),
 ('undo expires','control.py',"self.clock() > previous.get('undo_until', 0)","False",T+'test_undo_stale_identity_priority_or_expiry_never_pauses'),
 ('identity before enqueue','control.py',"                    if self.task_identity(task) != row['identity']:\n                        raise ValueError('recovery_error')","                    if False:\n                        raise ValueError('recovery_error')",T+'test_identity_reuse_retains_and_no_resurrection_of_cancel'),
 ('PID birth','control.py',"            if helper.birth(identity['pid']) == identity['birth']:","            if True:",T+'test_orphan_editor_pid_reuse_and_revocation'),
 ('unknown lock blocks admission','control.py',"if any(t['group']==g and isinstance(t['status'],dict) and set(t['status']) & {'Locked','Stashed','Paused'} for t in snap['tasks'].values()):","if False:",T+'test_unknown_lock_concurrent_never_enqueued_or_opened'),
 ('fresh pressure','control.py',"reasons <= {'pressure'} and decide('mini',fresh,threshold) == 'reanudar'","reasons <= {'pressure'}",T+'test_new_pressure_transfers_pause_without_start'),
 ('second sensor','control.py',"if decide('mini',fresh,threshold) != 'reanudar' or time.monotonic()-sampled > self.reorder_rpc:","if False:",T+'test_second_fresh_sensor_after_start_intent_blocks_open'),
 ('pause budget','control.py',"        if .4 + len(changes)*self.reorder_cost > self.reorder_budget:","        if False:",T+'test_limits_permutation_i32_booleans_and_budget'),
 ('alias preflight','editor_prioridad.py',"if task['command'] != task['original_command']:","if False:",'test_reorder.HelperTests.test_multiline_unicode_whitespace_null_and_bytes'),
 ('multiline key inert','editor_prioridad.py',"    integer(priority)\n    text = raw.decode('utf-8')","    integer(priority)\n    raw = raw.replace(b'priority = 999',b'priority = 5')\n    text = raw.decode('utf-8')",'test_reorder.HelperTests.test_multiline_unicode_whitespace_null_and_bytes'),
]


def run(root,test):
    return subprocess.run([sys.executable,'-B','-m','unittest','-v',test],cwd=str(root/'tests'),
        env=dict(os.environ,TMPDIR='/tmp',PYTHONDONTWRITEBYTECODE='1'),capture_output=True,text=True,timeout=30)


def main():
    nominal=run(SOURCE,'test_reorder')
    if nominal.returncode:raise AssertionError(nominal.stdout+nominal.stderr)
    for number,(rule,name,old,new,test) in enumerate(CASES,1):
        with tempfile.TemporaryDirectory(prefix='u36-mutant-',dir='/tmp') as folder:
            root=Path(folder)/'pueue-desk';shutil.copytree(SOURCE,root,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
            path=root/name;source=path.read_text()
            if source.count(old)!=1:raise AssertionError('Ambiguous anchor: '+rule)
            path.write_text(source.replace(old,new))
            result=run(root,test)
            if result.returncode!=1 or 'FAIL:' not in result.stderr or 'ERROR:' in result.stderr:
                raise AssertionError(rule+' not killed by assertion:\n'+result.stdout+result.stderr)
            print(str(number)+' '+rule+': assertion',flush=True)
    print(str(len(CASES))+'/'+str(len(CASES))+' mutants; nominal green; '+sys.version.split()[0],flush=True)


if __name__=='__main__':main()
