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
 ('etag exact','control.py',"if expected != request['expected'] or gen != state['generation'] or state['desired'] != before:","if gen != state['generation'] or before != state['desired']:",T+'test_stale_etag_revision_policy_generation_reject_without_pause'),
 ('paused never reordered','control.py',"if groups[g]['status'] != 'Running' or reasons or os.path.lexists(self.pause_fence(g)):","if False:",T+'test_paused_reasons_manual_guard_ambiguous_and_startup'),
 ('undo restores exact before','control.py',"            target = deepcopy(previous['before'])","            target = deepcopy(previous['target'])",T+'test_normal_and_exact_undo'),
 ('undo exact set','control.py',"or self.clock() > previous.get('undo_until', 0) or previous['target'] != rows","or self.clock() > previous.get('undo_until', 0)",T+'test_add_after_compare_preserved_but_strict_undo_conflicts'),
 ('undo expires','control.py',"self.clock() > previous.get('undo_until', 0)","False",T+'test_undo_stale_identity_priority_or_expiry_never_pauses'),
 ('identity before enqueue','control.py',"                    if self.task_identity(task) != row['identity']:\n                        raise ValueError('recovery_error')","                    if False:\n                        raise ValueError('recovery_error')",T+'test_identity_reuse_retains_and_no_resurrection_of_cancel'),
 ('PID birth','control.py',"            if helper.birth(identity['pid']) == identity['birth']:","            if True:",T+'test_orphan_editor_pid_reuse_and_revocation'),
 ('unknown lock blocks admission','control.py',"if any(t['group']==g and isinstance(t['status'],dict) and set(t['status']) & {'Locked','Stashed','Paused'} for t in snap['tasks'].values()):","if False:",T+'test_unknown_lock_concurrent_never_enqueued_or_opened'),
 ('fresh pressure','control.py',("reasons <= {'pressure'} and decide('mini',fresh,threshold) == 'reanudar'","if decide('mini',fresh,threshold) != 'reanudar' or time.monotonic()-sampled > self.reorder_rpc:"),("reasons <= {'pressure'}","if False:"),T+'test_new_pressure_transfers_pause_without_start'),
 ('second sensor','control.py',"if decide('mini',fresh,threshold) != 'reanudar' or time.monotonic()-sampled > self.reorder_rpc:","if False:",T+'test_second_fresh_sensor_after_start_intent_blocks_open'),
 ('pause budget','control.py',"        if .4 + len(changes)*self.reorder_cost > self.reorder_budget:","        if False:",T+'test_limits_permutation_i32_booleans_and_budget'),
 ('alias preflight','editor_prioridad.py',"if task['command'] != task['original_command']:","if False:",'test_reorder.HelperTests.test_multiline_unicode_whitespace_null_and_bytes'),
 ('multiline key inert','editor_prioridad.py',"    integer(priority)\n    text = raw.decode('utf-8')","    integer(priority)\n    raw = raw.replace(b'priority = 999',b'priority = 5')\n    text = raw.decode('utf-8')",'test_reorder.HelperTests.test_multiline_unicode_whitespace_null_and_bytes'),
 ('64 pending limit','control.py',"not (0 if allow_short or request['op']=='undo' else 2) <= len(e['queue']) <= 64", "not (0 if allow_short or request['op']=='undo' else 2) <= len(e['queue']) <= 65",T+'test_64_pending_65_rejects_and_views_are_pure'),
 ('16 KiB before effect','control.py',"if len(canonical(request).encode()) > 16384:","if False:",T+'test_request_bytes_limit_before_any_effect'),
 ('capabilities readonly','control.py','    def view(self):\n','    def view(self):\n        self.recover_reorders()\n',T+'test_dry_capabilities_have_no_effect_even_with_pending'),
 ('log blocks enqueue','control.py',"                        if os.path.lexists(data/'task_logs'/(str(row['id'])+'.log')):","                        if False:",T+'test_no_log_evidence_before_enqueue_and_offline_identity'),
 ('offline lock identity','control.py',"                if self.task_identity(task) != row['identity']:\n                    raise ValueError('Identidad de reorder cambió; conservar cierre')", "                if False:\n                    raise ValueError('Identidad de reorder cambió; conservar cierre')",T+'test_no_log_evidence_before_enqueue_and_offline_identity'),
 ('private identity fields','control.py',"fields = ('id','created_at','group','command','original_command','path','envs','dependencies','label')", "fields = ('id','created_at','group','command','original_command','path','dependencies','label')",T+'test_every_private_identity_field_blocks_foreign_repair'),
 ('read helper never writes pyc','control.py',"        exec(compile(path.read_bytes(),str(path),'exec'),module.__dict__)","        spec=importlib.util.spec_from_file_location('priority_editor',path)\n        spec.loader.exec_module(module)",T+'test_capabilities_do_not_create_bytecode_even_when_python_allows_it'),
 ('prepared transaction atomic','control.py',"self.store(db, state)\n\n            db.execute('INSERT INTO host_actions", "self.store(db, state)\n            db.commit()\n\n            db.execute('INSERT INTO host_actions",T+'test_failed_prepared_transaction_has_no_rpc_or_orphan_owner'),
 ('helper checks previous Queued before write','editor_prioridad.py',"    verify_locked(plan,json.loads(reply.stdout))","    pass",'test_reorder.HelperTests.test_helper_entrypoint_rejects_stashed_previous_without_writing_toml'),
 ('ambiguous Stashed never enqueued','control.py',"                    if owned_lock:","                    if owned_lock or name=='Stashed':",T+'test_stock_stashed_without_preparer_is_not_resurrected'),
 ('host has preparer','control.py',"        if not self.reorder_host_ready(state):", "        if False:",T+'test_unprepared_host_cannot_enable_or_pause'),
 ('rejected id cannot become effect','control.py',"            if request['op'] in ('slots', 'mode', 'undo', 'reorder') and not request['dry_run']:","            if request['op'] in ('slots', 'mode', 'undo') and not request['dry_run']:",T+'test_rejected_id_remains_terminal_after_conditions_change'),
 ('installer load has zero writes','install-reorder.py',"exec(compile((ROOT/'control.py').read_bytes(),c.__file__,'exec'),c.__dict__)","import importlib.util\nspec=importlib.util.spec_from_file_location('u36_install',ROOT/'control.py')\nspec.loader.exec_module(c)",'test_reorder.InstallTests.test_installer_load_cannot_write_bytecode_before_preflight'),

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
            edits=zip(old,new) if isinstance(old,tuple) else [(old,new)]
            for before,after in edits:
                if source.count(before)!=1:raise AssertionError('Ambiguous anchor: '+rule)
                source=source.replace(before,after)
            path.write_text(source)
            result=run(root,test)
            if result.returncode!=1 or 'FAIL:' not in result.stderr or 'ERROR:' in result.stderr:
                raise AssertionError(rule+' not killed by assertion:\n'+result.stdout+result.stderr)
            print(str(number)+' '+rule+': assertion',flush=True)
    print(str(len(CASES))+'/'+str(len(CASES))+' mutants; nominal green; '+sys.version.split()[0],flush=True)


if __name__=='__main__':main()
