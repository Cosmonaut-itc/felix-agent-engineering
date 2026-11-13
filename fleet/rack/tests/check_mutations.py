"""Prove safety regressions are caught, without editing the working tree.

TMPDIR=/tmp PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 tests/check_mutations.py
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

BASE=Path(__file__).resolve().parents[1]
CASES=(
    ('http-journal-wait','accion.py',
     'if not self.wait(done, max(0, deadline - self.timer())):',
     'if not self.wait(done, 44):',
     'test_review_r1.AdminAndUIReviewTests.test_actual_http_wait_times_out_blocked_io_without_virtual_waiter'),
    ('http-post-boundary','serve.py',
     "respond('post', accion.decode(raw), started=started)",
     "post(accion.decode(raw), started=started)",
     'test_review_r1.HTTPJournalTests.test_a_slow_commit_returns_before_ui_deadline_and_never_launches_late'),
    ('http-get-boundary','serve.py',
     "respond('get', self.path[len('/api/accion/'):], started=started)",
     "get(self.path[len('/api/accion/'):], started=started)",
     'test_review_r1.HTTPJournalTests.test_get_slow_journal_and_post_slow_result_or_transport_return_unknown'),
    ('desk-status-interval','accion.py',
     "if row[3] is not None and now - row[3] < interval:", 'if False:',
     'test_review_r1.ReviewActionsTests.test_b_reproduction_missing_controller_receipt_has_bounded_transports'),
    ('desk-status-backoff','accion.py',
     'interval = 2 if now - row[2] < 60 else 30', 'interval = 2',
     'test_review_r1.ReviewActionsTests.test_desk_intervals_first_minute_then_30_seconds_and_persist_after_restart'),
    ('desk-status-first-minute','accion.py',
     'interval = 2 if now - row[2] < 60 else 30', 'interval = 30',
     'test_review_r1.ReviewActionsTests.test_desk_intervals_first_minute_then_30_seconds_and_persist_after_restart'),
    ('desk-status-persist','accion.py',
     "db.execute('UPDATE actions SET last_status=? WHERE action_id=?', (now, action_id))",
     'pass',
     'test_review_r1.ReviewActionsTests.test_desk_intervals_first_minute_then_30_seconds_and_persist_after_restart'),
    ('mini-status-unchanged','accion.py', "if request['host'] == 'desk':", 'if True:',
     'test_review_r1.ReviewActionsTests.test_mini_still_consults_every_get'),
    ('manual-close-save','accion.py', "response = result(request, 'rejected', 'manually_closed')",
     "response = result(request, 'unknown')",
     'test_review_r1.ReviewActionsTests.test_manual_close_durable_idempotent_rejects_missing_terminal_and_unblocks'),
    ('manual-close-late-transport','accion.py',
     "if current and (current.get('error') or {}).get('code') == 'manually_closed':", 'if False:',
     'test_review_r1.ReviewActionsTests.test_manual_cli_and_late_transport_share_sqlite_writer_lock'),
    ('manual-close-ui-stop','index.html', 'PENDING.delete(key); persistActions();',
     'persistActions();',
     'test_review_r1.AdminAndUIReviewTests.test_ui_manual_close_shows_server_message_and_stops_polling_after_reload'),
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
    ('request-remaining','accion.py',"else LOCAL_CANCEL_TIMEOUT if op == 'cancel' else 10, remaining)",
     "else LOCAL_CANCEL_TIMEOUT if op == 'cancel' else 10, 99)",
     'test_deadlines.DeadlineTests.test_remaining_budget_reaches_executor_for_each_host_and_operation'),
    ('executor-request-remainder','accion.py','timeout = limit if timeout is None else min(limit, timeout)',
     'timeout = limit', 'test_c4.SSHTests.test_executor_caps_transport_by_request_remainder'),
    ('request-late-effect','accion.py',"return answer if self.timer() <= deadline - RECEIPT_RESERVE else result(request, 'unknown')",
     'return answer', 'test_deadlines.DeadlineTests.test_late_effect_is_unknown_and_same_receipt_reconciles'),
    ('receipt-inflight','accion.py'," or self.active(action_id)", '',
     'test_deadlines.DeadlineTests.test_inflight_cancel_duplicate_conflict_pending_and_get_do_not_act'),
    ('receipt-flight-release','accion.py','done.set()\n\n    def post', 'pass\n\n    def post',
     'test_deadlines.DeadlineTests.test_failed_completion_releases_flight_and_keeps_durable_unknown'),
    ('whois-outside-cache-lock','serve.py','output, _ = process.communicate(timeout=3)',
     'with self.lock:\n                    output, _ = process.communicate(timeout=3)',
     'test_deadlines.HTTPDeadlineTests.test_whois_does_not_serialize_other_requests'),
    ('http-entry-budget','serve.py',"respond('post', accion.decode(raw), started=started)", "respond('post', accion.decode(raw), started=None)",
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
    ('controller-decode-unknown','accion.py','except Exception:\n            # This is the controller boundary',
     'except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):\n            # This is the controller boundary',
     'test_r1fix.ExecutorRegressions.test_a2_deep_controller_response_is_http_unknown_and_reconciles'),
    ('open-run-journal','accion.py', "if response['status'] in OPEN and all(previous[k] == request[k] for k in ('host', 'launcher', 'ref')):",
     'if False:', 'test_r1fix.JournalRegressions.test_b2_open_action_blocks_other_id_and_capabilities_after_restart'),
    # C5: origin "rack-panel" negotiated by the local channel next to the controller.
    ('c5-mini-exchange','accion.py','code, response = desk_channel.send(argv, request, self.runner, env, timeout)',
     'code, response = desk_channel.exchange(argv, request, self.runner, env, timeout)',
     'test_c5.LocalChannelTests.test_mini_post_probes_then_one_cancel_with_origin'),
    ('c5-desk-exchange','desk_action.py',"code, response = send(['/usr/bin/python3', '-I', str(controller)]",
     "code, response = exchange(['/usr/bin/python3', '-I', str(controller)]",
     'test_c5.LocalChannelTests.test_desk_dispatcher_adds_origin_and_ssh_envelope_never_carries_it'),
    ('c5-origin-before-ssh','accion.py',"request = dict(schema='rack-desk-action@1', launcher=launcher, request=request)",
     "request = dict(schema='rack-desk-action@1', launcher=launcher, request=dict(request, origin='rack-panel') if request.get('op') == 'cancel' else request)",
     'test_c5.LocalChannelTests.test_desk_dispatcher_adds_origin_and_ssh_envelope_never_carries_it'),
    ('c5-truthiness','desk_action.py',"and caps.get('cancel_origin') is True)","and caps.get('cancel_origin'))",
     'test_c5.DegradationTests.test_only_exact_true_from_valid_answer_enables_origin'),
    ('c5-probe-blocks-cancel','desk_action.py','        except Exception:\n            pass','        except Exception:\n            raise',
     'test_c5.DegradationTests.test_only_exact_true_from_valid_answer_enables_origin'),
    ('c5-parser-open','desk_action.py',"keys = {'schema', 'op', 'ref'}","keys = {'schema', 'op', 'ref'} | ({'origin'} & set(request))",
     'test_c5.DispatcherScopeTests.test_parser_rejects_origin_in_envelope_and_inner_request'),
    ('c5-probe-all-ops','desk_action.py',"if request.get('op') != 'cancel':\n        return exchange","if False:\n        return exchange",
     'test_c5.DispatcherScopeTests.test_status_and_capabilities_make_exactly_one_call'),
    ('c5-probe-no-margin','desk_action.py','probe = min(PROBE_LIMIT, max(0, timeout - min(CANCEL_RESERVE, timeout)))','probe = PROBE_LIMIT',
     'test_c5.BudgetTests.test_slow_probe_keeps_cancel_window_and_reduced_budget_skips_probe'),
    ('c5-probe-not-reaped','desk_action.py','            pass\n        process.wait()\n        raise','            pass\n        raise',
     'test_c5.BudgetTests.test_probe_holding_stdout_and_lock_is_killed_and_reaped_before_cancel'),
    ('c5-deadline-restart','desk_action.py','return exchange(argv, request, runner, env, remaining)','return exchange(argv, request, runner, env, timeout)',
     'test_c5.BudgetTests.test_slow_probe_keeps_cancel_window_and_reduced_budget_skips_probe'),
    ('c5-local-cancel-limit','accion.py',"LOCAL_CANCEL_TIMEOUT = 12","LOCAL_CANCEL_TIMEOUT = 10",
     'test_deadlines.DeadlineTests.test_full_budget_limits_mini_cancel_twelve_others_unchanged'),
    ('c5-resend-get','accion.py',"return self.complete(request, 'status', done,","return self.complete(request, 'cancel', done,",
     'test_c5.JournalTests.test_one_cancel_per_action_id_across_duplicate_lost_answer_restart_and_capability_change'),
    ('c5-server-decides','accion.py',"request = dict(schema='rack-desk-action@1', launcher=launcher, request=request)",
     "request = dict(schema='rack-desk-action@1', launcher=launcher, request=dict(request, origin='rack-panel') if request.get('op') == 'cancel' and desk_channel.origin_supported(desk_channel.exchange(list(config['argv']), dict(schema='rack-desk-action@1', launcher=launcher, request=dict(schema='rack-cancel@1', op='capabilities', ref=request['ref'])), self.runner, env, 2)[1], request['ref']) else request)",
     'test_c5.CompatibilityTests.test_new_desk_channel_adds_origin_old_desk_channel_never_does'),
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
                                  env=dict(os.environ,TMPDIR='/tmp',PYTHONDONTWRITEBYTECODE='1'),capture_output=True,text=True,timeout=20)
            if result.returncode!=1 or 'FAIL:' not in result.stderr:
                raise AssertionError(name+' did not fail as expected:\n'+result.stderr)
            print(name+': caught (expected failure)')
            print('\n'.join(line for line in result.stderr.splitlines() if 'AssertionError:' in line or line.startswith('FAILED')))
    print('All '+str(len(CASES))+' mutations caught; original tree untouched.')


if __name__=='__main__':main()
