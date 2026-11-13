"""Prove safety regressions are caught, without editing the working tree.

TMPDIR=/tmp /usr/bin/python3 tests/check_mutations.py
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

BASE=Path(__file__).resolve().parents[1]
CASES=(
    ('desk-fixed-table','desk_action.py','controller = Path(CONTROLLERS[launcher])',
     "controller = Path(CONTROLLERS['remote-run'])",
     'test_c4.DispatcherTests.test_fixed_table_each_launcher_and_all_operations'),
    ('desk-switch','desk_action.py',"and switched_off():",'and False:',
     'test_c4.DispatcherTests.test_switch_blocks_cancel_and_capabilities_but_allows_status'),
    ('desk-switch-lstat','desk_action.py','SWITCH.lstat()',
     "SWITCH.stat()", 'test_c4.DispatcherTests.test_switch_blocks_cancel_and_capabilities_but_allows_status'),
    ('desk-unix-environment','desk_action.py','USER=account.pw_name, LOGNAME=account.pw_name',
     "USER=os.environ.get('USER'), LOGNAME=os.environ.get('LOGNAME')",
     'test_c4.DispatcherTests.test_environment_from_unix_account_and_owned_runtime_directory_only'),
    ('desk-runtime-directory','desk_action.py','stat.S_ISDIR(info.st_mode) and ', '',
     'test_c4.DispatcherTests.test_environment_from_unix_account_and_owned_runtime_directory_only'),
    ('desk-runtime-owner','desk_action.py','info.st_uid == account.pw_uid', 'True',
     'test_c4.DispatcherTests.test_environment_from_unix_account_and_owned_runtime_directory_only'),
    ('desk-missing-status','desk_action.py',"status = 'unknown' if request['op'] == 'status' else 'rejected'",
     "status = 'rejected'", 'test_c4.DispatcherTests.test_missing_controller_by_operation'),
    ('request-admission','accion.py','self.lock.acquire(timeout=wait)', 'self.lock.acquire(timeout=46)',
     'test_deadlines.DeadlineTests.test_judge_reproduction_bounded_lock_while_desk_inflight'),
    ('transport-outside-journal','accion.py',"return self.complete(request, 'cancel', done, deadline)",
     "with self.lock:\n            return self.complete(request, 'cancel', done, deadline)",
     'test_deadlines.DeadlineTests.test_concurrent_post_and_get_desk_desk_and_desk_mini'),
    ('receipt-status-flight','accion.py'," or self.active(action_id)", '',
     'test_deadlines.DeadlineTests.test_inflight_status_cannot_overwrite_later_result_or_repeat_cancel'),
    ('request-minimum','accion.py','if remaining < MIN_TRANSPORT:', 'if False:',
     'test_deadlines.DeadlineTests.test_journal_time_is_subtracted_and_no_launch_without_minimum'),
    ('request-remaining','accion.py',"timeout = min(DESK_TIMEOUT if request['host'] == 'desk' else 10, remaining)",
     "timeout = DESK_TIMEOUT if request['host'] == 'desk' else 10",
     'test_deadlines.DeadlineTests.test_remaining_budget_reaches_executor_for_each_host_and_operation'),
    ('executor-request-remainder','accion.py','timeout = limit if timeout is None else min(limit, timeout)',
     'timeout = limit', 'test_c4.SSHTests.test_executor_caps_transport_by_request_remainder'),
    ('request-late-effect','accion.py',"return answer if self.timer() <= deadline - RECEIPT_RESERVE else result(request, 'unknown')",
     'return answer', 'test_deadlines.DeadlineTests.test_late_effect_is_unknown_and_same_receipt_reconciles'),
    ('receipt-inflight','accion.py'," or self.active(action_id)", '',
     'test_deadlines.DeadlineTests.test_inflight_cancel_duplicate_conflict_pending_and_get_do_not_act'),
    ('receipt-flight-release','accion.py','done.set()', 'pass',
     'test_deadlines.DeadlineTests.test_failed_completion_releases_flight_and_keeps_durable_unknown'),
    ('whois-outside-cache-lock','serve.py','output, _ = process.communicate(timeout=3)',
     'with self.lock:\n                    output, _ = process.communicate(timeout=3)',
     'test_deadlines.HTTPDeadlineTests.test_whois_does_not_serialize_other_requests'),
    ('http-entry-budget','serve.py','post(accion.decode(raw), started=started)', 'post(accion.decode(raw), started=None)',
     'test_deadlines.HTTPDeadlineTests.test_whois_and_body_time_count_from_http_entry'),
    ('ssh-fixed-argv','accion.py',"value['transport'] == 'ssh' and value['argv'] != DESK_ARGV",'False',
     'test_c4.SSHTests.test_other_argv_controller_paths_and_host_rejected'),
    ('ssh-failure-unknown','accion.py',"if code not in ((0,) if remote else (0, 2)):",'if False:',
     'test_c4.SSHTests.test_ssh_failure_is_unknown_no_repeated_cancel_and_same_status'),
    ('whois-fresco','serve.py','allows(self.client_address[0], fresh=True)',
     'allows(self.client_address[0], fresh=False)',
     'test_actions.AuthorizationTests.test_whois_is_fresh_for_every_mutation_even_after_cached_read'),
    ('origin','serve.py',"origins != ['http://' + host]",'False',
     'test_actions.AuthorizationTests.test_each_auth_rejection_prevents_effect'),
    ('digest','accion.py',"if row[0] != digest:",'if False:',
     'test_actions.ActionsTests.test_id_is_bound_to_digest_and_duplicate_does_not_act'),
    ('unknown','accion.py',"intent = result(request, 'unknown')", "intent = result(request, 'rejected', 'not_found')",
     'test_actions.ActionsTests.test_intent_committed_before_effect_and_crash_before_effect'),
    ('status-rejected-unknown','accion.py',"if op == 'status' and response['status'] == 'rejected':",
     "if op == 'status' and response['status'] == 'rejected' and response['error']['code'] == 'not_found':",
     'test_actions.ActionsTests.test_every_status_rejection_preserves_unknown_and_same_id'),
    ('cancelled-old-snapshot','index.html',
     '.filter(t=>!CANCELLED.has(runKey(h,t)))', '',
     'test_actions.UITests.test_production_js_dialog_pending_toast_reasons_and_network'),
    ('controller-decode-unknown','accion.py','except Exception:',
     'except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):',
     'test_r1fix.ExecutorRegressions.test_a2_deep_controller_response_is_http_unknown_and_reconciles'),
    ('open-run-journal','accion.py', "if response['status'] in OPEN and all(previous[k] == request[k] for k in ('host', 'launcher', 'ref')):",
     'if False:', 'test_r1fix.JournalRegressions.test_b2_open_action_blocks_other_id_and_capabilities_after_restart'),
    ('snapshot-identity','accion.py', " or any(control[k] != request[k] for k in ('launcher', 'ref', 'expected'))", '',
     'test_actions.ActionsTests.test_b3_snapshot_identity_binding_rejects_foreign_ref_launcher_and_expected'),
)


def main():
    for name,filename,before,after,test in CASES:
        with tempfile.TemporaryDirectory(prefix='rack-c3-mutation-',dir='/tmp') as folder:
            root=Path(folder)/'rack'
            shutil.copytree(BASE,root,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
            path=root/filename
            text=path.read_text()
            if text.count(before)!=1:raise AssertionError('Mutation anchor is no longer unique: '+name)
            path.write_text(text.replace(before,after))
            result=subprocess.run(['/usr/bin/python3','-B','-m','unittest',test,'-v'],cwd=str(root/'tests'),
                                  env=dict(os.environ,TMPDIR='/tmp'),capture_output=True,text=True,timeout=20)
            if result.returncode!=1 or 'FAIL:' not in result.stderr:
                raise AssertionError(name+' did not fail as expected:\n'+result.stderr)
            print(name+': caught (expected failure)')
            print('\n'.join(line for line in result.stderr.splitlines() if 'AssertionError:' in line or line.startswith('FAILED')))
    print('All '+str(len(CASES))+' mutations caught; original tree untouched.')


if __name__=='__main__':main()
