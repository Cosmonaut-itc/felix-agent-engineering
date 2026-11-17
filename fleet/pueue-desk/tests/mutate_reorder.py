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
 ('recovery leaves the guard clock alone without pending reorders','control.py',"        if self.host != 'mini' or all(a['phase'] == 'terminal' for a in self.reorder_actions()):\n            return True\n","",'test_trickle.TrickleTests.test_reorder_recovery_without_pending_work_leaves_fresh_sample_budget'),
 ('initial reject reports the marker reason','control.py',"({fence} if fence else set())","({'manual'} if fence else set())",T+'test_pause_marker_rejects_with_its_own_reason'),
 ('recovery keeps the marker reason','control.py',"            if fence:\n                reasons.add(fence)","            if fence:\n                reasons.add('manual')",T+'test_recovery_keeps_pressure_marker_and_guard_reopens_after_retiring_it'),
 ('recovery never opens under a marker','control.py',"reasons <= {'pressure'} and not fence and decide(","reasons <= {'pressure'} and decide(",T+'test_recovery_keeps_pressure_marker_and_guard_reopens_after_retiring_it'),
 ('pending reorder is still recovered','control.py',"all(a['phase'] == 'terminal' for a in self.reorder_actions())","any(a['phase'] == 'terminal' for a in self.reorder_actions())",T+'test_every_boundary_recovers_and_duplicate_is_historic'),
 ('CLI observer never fsyncs between timestamps','tests/private_reorder.py',"c.priority_helper().write_trace(root/'trace-cli.json', marks)","c.atomic_json(root/'trace-cli.json', marks)",'test_reorder.HarnessTests.test_cli_observer_keeps_only_fixed_markers_never_payload'),
 ('client plan never follows a link','editor_prioridad.py','info = os.lstat(path)','info = os.stat(path)','test_reorder.HelperTests.test_private_plan_lstat_rejects_link_and_public_permissions_for_string_path'),
 ('light client still checks revocation','editor_prioridad.py',"or os.path.exists(os.path.join(root, 'revoked'))",'or False','test_reorder.HelperTests.test_light_client_validates_plan_revocation_and_deadline_before_exec'),
 ('light client still checks deadline','editor_prioridad.py',"        remaining(plan)\n        trace_mark(root, marks, 'cli_exec')","        pass\n        trace_mark(root, marks, 'cli_exec')",'test_reorder.HelperTests.test_light_client_validates_plan_revocation_and_deadline_before_exec'),
 ('interrupted trace retains complete boundary','editor_prioridad.py',"temporary = path[:-5] + '.pending'",'temporary = path','test_reorder.HelperTests.test_interrupted_trace_keeps_previous_complete_boundary'),
 ('gated client skips editor imports','editor_prioridad.py',"if __name__ != '__main__' or '--client' not in sys.argv:",'if True:','test_reorder.HelperTests.test_gated_client_does_not_import_editor_only_dependencies'),
 ('client uses running isolated interpreter','control.py',"subprocess.Popen([sys.executable,'-I','-S',","subprocess.Popen(['python3','-I','-S',",T+'test_one_paused_snapshot_replaces_parent_status_reads_per_id'),
 ('client skips site on running interpreter','control.py',"subprocess.Popen([sys.executable,'-I','-S',","subprocess.Popen([sys.executable,'-I',",T+'test_one_paused_snapshot_replaces_parent_status_reads_per_id'),
 ('helper uses running isolated interpreter','control.py',"EDITOR=shlex.join([sys.executable,'-I','-S',","EDITOR=shlex.join(['python3','-I','-S',",T+'test_one_paused_snapshot_replaces_parent_status_reads_per_id'),
 ('helper skips site on running interpreter','control.py',"EDITOR=shlex.join([sys.executable,'-I','-S',","EDITOR=shlex.join([sys.executable,'-I',",T+'test_one_paused_snapshot_replaces_parent_status_reads_per_id'),
 ('controller gate timing retained','control.py',"trace['gate_released'] = helper.host_clock()","pass",T+'test_one_paused_snapshot_replaces_parent_status_reads_per_id'),
 ('helper partial identity timing','editor_prioridad.py',"trace_mark(root, marks, 'identity_begin')","pass",'test_reorder.HelperTests.test_trace_contains_only_shared_clock_and_retains_identity_failure'),
 ('kernel process creation conversion','editor_prioridad.py','(now - usage.start) * scale.numer / scale.denom / 1e9','0','test_reorder.HelperTests.test_process_creation_uses_kernel_uptime_without_wall_clock'),
 ('stock CLI response boundary','tests/private_reorder.py',"(b'Received message: Edit(', 'response')","(b'Received message: Edit(', 'request')",'test_reorder.HarnessTests.test_cli_observer_keeps_only_fixed_markers_never_payload'),
 ('coordinated clients','control.py','        if not self.coordinated_reorder:\n','        if False:\n',T+'test_paused_reasons_manual_guard_ambiguous_and_startup'),
 ('pause intent owner','control.py',"        state['reasons'][g] = ['reorder']","        state['reasons'][g] = []",T+'test_prepared_intent_precedes_pause'),
 ('prepared durable before pause','control.py',"revision=state['revision'], phase='prepared', at=self.clock(), process=None, pause_owner=request['action_id'],","revision=state['revision'], phase='paused', at=self.clock(), process=None, pause_owner=request['action_id'],",T+'test_prepared_intent_precedes_pause'),
 ('etag exact','control.py',"if expected != request['expected'] or gen != state['generation'] or state['desired'] != before:","if gen != state['generation'] or before != state['desired']:",T+'test_stale_etag_revision_policy_generation_reject_without_pause'),
 ('paused never reordered','control.py',"if groups[g]['status'] != 'Running' or reasons or fence:","if False:",T+'test_paused_reasons_manual_guard_ambiguous_and_startup'),
 ('undo restores exact before','control.py',"            target = deepcopy(previous['before'])","            target = deepcopy(previous['target'])",T+'test_normal_and_exact_undo'),
 ('undo exact set','control.py',"or self.clock() > previous.get('undo_until', 0) or previous['target'] != rows","or self.clock() > previous.get('undo_until', 0)",T+'test_add_after_compare_preserved_but_strict_undo_conflicts'),
 ('undo expires','control.py',"self.clock() > previous.get('undo_until', 0)","False",T+'test_undo_stale_identity_priority_or_expiry_never_pauses'),
 ('identity before enqueue','control.py',"                    if self.task_identity(task) != row['identity']:\n                        raise ValueError('recovery_error')","                    if False:\n                        raise ValueError('recovery_error')",T+'test_identity_reuse_retains_and_no_resurrection_of_cancel'),
 ('PID birth','control.py',"            if observed_birth == identity['birth']:","            if True:",T+'test_orphan_editor_pid_reuse_and_revocation'),
 ('unknown lock blocks admission','control.py',"if any(t['group']==g and isinstance(t['status'],dict) and set(t['status']) & {'Locked','Stashed','Paused'} for t in snap['tasks'].values()):","if False:",T+'test_unknown_lock_concurrent_never_enqueued_or_opened'),
 ('fresh pressure','control.py',("reasons <= {'pressure'} and not fence and decide('mini',fresh,threshold) != 'pausar'","if decide('mini',fresh,threshold) == 'pausar' or time.monotonic()-sampled > self.reorder_rpc:"),("reasons <= {'pressure'} and not fence","if False:"),T+'test_new_pressure_transfers_pause_without_start'),
 ('second sensor','control.py',"if decide('mini',fresh,threshold) == 'pausar' or time.monotonic()-sampled > self.reorder_rpc:","if False:",T+'test_second_fresh_sensor_after_start_intent_blocks_open'),
 ('hysteresis keeps running first sample','control.py',"reasons <= {'pressure'} and not fence and decide('mini',fresh,threshold) != 'pausar'","reasons <= {'pressure'} and not fence and decide('mini',fresh,threshold) == 'reanudar'",T+'test_hysteresis_reopens_only_our_pause_with_two_fresh_samples'),
 ('hysteresis keeps running second sample','control.py',"if decide('mini',fresh,threshold) == 'pausar' or time.monotonic()-sampled > self.reorder_rpc:","if decide('mini',fresh,threshold) != 'reanudar' or time.monotonic()-sampled > self.reorder_rpc:",T+'test_hysteresis_reopens_only_our_pause_with_two_fresh_samples'),
 ('pause budget','control.py',"        if self.reorder_estimate(len(changes), len(canonical(snapshot).encode())) > self.reorder_budget:","        if False:",T+'test_limits_permutation_i32_booleans_and_budget'),
 ('calibrated 64 rejection before pause','control.py',"self.reorder_estimate(len(changes), len(canonical(snapshot).encode())) > self.reorder_budget", "len(changes)*.001 > self.reorder_budget",T+'test_calibrated_admission_rejects_64_before_pause_and_accepts_five'),
 ('status size admission before pause','control.py','self.reorder_estimate(len(changes), len(canonical(snapshot).encode()))','self.reorder_estimate(len(changes), 0)',T+'test_status_size_rejects_before_pause_and_accepts_small_control'),
 ('status size visible admission','control.py','self.reorder_estimate(len(queue), state_bytes)','self.reorder_estimate(len(queue), 0)',T+'test_status_size_rejects_before_pause_and_accepts_small_control'),
 ('six second common budget','control.py','    reorder_budget = 6.0','    reorder_budget = 3.0',T+'test_status_size_rejects_before_pause_and_accepts_small_control'),
 ('RPC shares remaining budget','control.py',"timeout = min(timeout or remaining,remaining)","timeout = self.timeout",T+'test_rpc_uses_remaining_common_budget_and_never_sends_after_expiry'),
 ('expired RPC never sent','control.py',"            if remaining <= 0:\n                raise TimeoutError('Presupuesto de pausa agotado')","            if False:\n                raise TimeoutError('Presupuesto de pausa agotado')",T+'test_rpc_uses_remaining_common_budget_and_never_sends_after_expiry'),
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

 ('daemon aliases, never client aliases','control.py','        return read_aliases(alias)','        return {}',T+'test_daemon_config_not_edit_client_selects_effective_alias_file'),
 ('alias added after add','control.py',"        if words and words[0] in aliases:","        if False:",T+'test_alias_added_after_enqueue_rejects_before_any_effect'),
 ('daemon settings unchanged since birth','control.py',"if max(source.stat().st_mtime_ns, source.stat().st_ctime_ns) > int(sec)*10**9+int(usec)*1000:","if False:",T+'test_daemon_config_not_edit_client_selects_effective_alias_file'),
 ('semantic TOML before write','editor_prioridad.py','    if expected is not None:', '    if False:', 'test_reorder.HelperTests.test_semantic_toml_fingerprint_precedes_write'),
 ('reorder preacceptance RPC scope','control.py','    def reorder_handle(self, request):\n        with self.reorder_deadline():', '    def reorder_handle(self, request):\n        with __import__("contextlib").nullcontext():',T+'test_reorder_reads_and_recovery_are_scoped_but_guard_reads_unbounded'),
 ('recovery reconciliation scope','control.py',"if self.host == 'mini' and any(a['phase'] != 'terminal' for a in self.reorder_actions()):",'if False:',T+'test_reorder_reads_and_recovery_are_scoped_but_guard_reads_unbounded'),
 ('safe failure diagnostic retained','control.py',"latest['failure'] = dict(kind=type(error).__name__, phase=latest['phase'],", "latest['ignored'] = dict(kind=type(error).__name__, phase=latest['phase'],",T+'test_failed_edit_preserves_safe_diagnostic_and_recovery_finishes'),
 ('precise pause envelope','tests/private_reorder.py',"pause_start_envelope_seconds=span('pause_begin','start_effect')", "pause_start_envelope_seconds=span('prepared','start_effect')",'test_reorder.HarnessTests.test_intervals_use_rpc_envelope_and_keep_partial_evidence'),
 ('scenario failure retained','tests/private_reorder.py',"        return False\n\n\nclass Crash", "        return True\n\n\nclass Crash",'test_reorder.HarnessTests.test_failed_scenario_emits_partial_and_next_scenario_runs'),

 ('client waits before reading plan','editor_prioridad.py',"if sys.argv[1] == '--client' and sys.stdin.buffer.read(1) != b'1':",'if False:','test_reorder.HelperTests.test_client_gate_precedes_plan_read_and_every_rpc'),
 ('client config reused','control.py',"        if target.exists():\n            return target\n        data = Path(authority['pid_file']).parent", "        if False:\n            return target\n        data = Path(authority['pid_file']).parent",T+'test_client_configuration_is_built_once_per_action'),
 ('one parent snapshot','control.py',"snapshot = getattr(self, '_reorder_edit_snapshot', None) or self.pueue.status()", "snapshot = self.pueue.status()",T+'test_one_paused_snapshot_replaces_parent_status_reads_per_id'),

 ('shared helper clock','editor_prioridad.py','return time.clock_gettime(time.CLOCK_MONOTONIC)','return time.monotonic()', 'test_reorder.HelperTests.test_helper_deadline_uses_shared_kernel_clock_not_process_epoch'),
 ('manual resume through authority','README.md','Para reanudarlo después: `/usr/bin/python3 -I ~/.local/lib/pao-pueue-mini/control.py reanudar suites --host mini`.','Para reanudarlo después: `/opt/homebrew/bin/pueue start -g suites`.','test_reorder.InstallTests.test_manual_resume_documentation_uses_authority'),

 ('editor wait shares deadline','control.py','time.sleep(min(.005, helper.remaining(plan)))','time.sleep(.005)',T+'test_one_paused_snapshot_replaces_parent_status_reads_per_id'),
 ('fresh quotas before reorder admission','control.py',"and before == state['desired'] and self.generation(state) == state['generation']","and self.generation(state) == state['generation']",T+'test_late_quota_drift_blocks_opening_after_reconciliation'),

 ('daemon alias path unambiguous','control.py',"if configured is not None and not configured.startswith(('/', '~/')):",'if False:',T+'test_daemon_config_not_edit_client_selects_effective_alias_file'),

 ('cached daemon config cannot disappear','control.py',("        if not source.is_file():\n            raise ValueError('edit_unsafe')\n        daemon_pid", "if max(source.stat().st_mtime_ns, source.stat().st_ctime_ns) > int(sec)*10**9+int(usec)*1000:"),("        daemon_pid", "if source.exists() and max(source.stat().st_mtime_ns, source.stat().st_ctime_ns) > int(sec)*10**9+int(usec)*1000:"),T+'test_daemon_config_not_edit_client_selects_effective_alias_file'),
 ('preserved mtime is not cached settings proof','control.py',"if max(source.stat().st_mtime_ns, source.stat().st_ctime_ns) > int(sec)*10**9+int(usec)*1000:","if source.stat().st_mtime_ns > int(sec)*10**9+int(usec)*1000:",T+'test_daemon_config_not_edit_client_selects_effective_alias_file'),

 ('process query failure cannot release ownership','control.py','return False  # identity query is inconclusive','return True  # identity query is inconclusive',T+'test_identity_query_failure_holds_and_exit_before_signal_is_harmless'),
 ('exit between birth and signal','control.py',"                except ProcessLookupError:\n                    continue  # exited between birth observation and signal", "                except ProcessLookupError:\n                    signaled = True\n                    continue  # exited between birth observation and signal",T+'test_identity_query_failure_holds_and_exit_before_signal_is_harmless'),

 ('original editor failure stage retained','control.py',"action['editor_failure']=dict(stage=stage,kind=type(error).__name__,errno=getattr(error,'errno',None),","action['ignored_failure']=dict(stage=stage,kind=type(error).__name__,errno=getattr(error,'errno',None),",T+'test_one_paused_snapshot_replaces_parent_status_reads_per_id'),

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
