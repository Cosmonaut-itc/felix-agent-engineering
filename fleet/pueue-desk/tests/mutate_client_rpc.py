"""CC3-P1 mutants: emitter, stock verdict and CC2 relay; each dies by assertion."""
from pathlib import Path
import shutil
import sys
import tempfile
from mutate_client_actions import SOURCE, run

W = 'test_client_rpc.WireTests.'
CASES = [
    ('emitter life is read, never assumed ended', 'client_actions.py',
     "            emitter_alive = self.alive(process['pid'], process['birth'])",
     '            emitter_alive = False',
     'test_client_actions.ActionTests.test_child_intent_precedes_gate_timeout_unknown'),
    ('life D is read, never assumed alive', 'client_actions.py',
     "            life_alive = self.alive(daemon['pid'], daemon['birth'])",
     '            life_alive = True',
     W+'test_emitter_dies_between_claim_and_frame_3'),
    ('life D is read after frame 3, never assumed alive', 'client_actions.py',
     "            life_alive = self.alive(daemon['pid'], daemon['birth'])",
     '            life_alive = True',
     W+'test_emitter_dies_after_frame_3'),
    ('gated without claim is not a not-sent', 'client_actions.py',
     "            plan, claim, seal = found['plan'], found['claim'], found['seal']",
     "            plan, claim, seal = found['plan'], found['claim'], found['seal']\n"
     "            if claim is None:\n                return (True, False, 'startup'), None, None",
     W+'test_emitter_dies_before_claim'),
    ('closed is sealed as closed', 'client_rpc.py',
     "            return dict(peer=peer, outcome='closed', reply=None, reason=None)",
     "            return not_sent('startup', peer)",
     W+'test_lost_reply_is_s3_and_never_attributed'),
    ('greeting checked before frame 3', 'client_rpc.py',
     '            c.pueue_greeting(greeting)', '            pass',
     W+'test_other_protocol_version_fails_closed_before_frame_3'),
    ('peer life checked before frame 1', 'client_rpc.py',
     "        if pid != daemon['pid'] or born != daemon['birth']:", '        if False:',
     W+'test_other_daemon_life_never_receives_the_secret'),
    ('claim loser stays silent', 'client_rpc.py',
     '        return None  # The loser leaves without connecting and without a seal.', '        pass',
     W+'test_claim_loser_never_connects_nor_seals'),
    ('coordinated profile rejects remote-run mutators', 'client_actions.py',
     "        if r['launcher'] == 'remote-run' and r['op'] != 'finish' and self.exclusion != 'filtered':",
     '        if False:',
     W+'test_coordinated_profile_rejects_remote_run_mutators_without_emitter'),
    ('CC1 never kills the emitter', 'client_actions.py',
     '                child.stdin.close()  # EOF before the dispatch: the gate never opens.',
     '                child.stdin.close()  # EOF before the dispatch: the gate never opens.\n'
     '            if child is not None:\n                child.kill()',
     'test_client_actions.ActionTests.test_child_intent_precedes_gate_timeout_unknown'),
    ('Q of CC1 names its seal', 'client_actions.py',
     "            raise Unavailable('startup')\n        return dict(life=life, identity=observed['identity'], seal=receipt)",
     "            raise Unavailable('startup')\n        return dict(life=life, identity=observed['identity'], seal=None)",
     W+'test_success_seal_attributes_hold_with_one_frame'),
    ('success without Q stays retriable', 'client_actions.py',
     "        if stock and record['response']['status'] == 'unknown':",
     "        if stock and record['response']['reason'] == 'rpc_unresolved':",
     W+'test_success_without_q_stays_retriable_until_cc2_reads_q'),
    ('apply uses the collector Q', 'client_actions.py',
     "                verdict, after, post = self.actions.stock_verdict(current, post=proposal.get('post'))",
     '                verdict, after, post = self.actions.stock_verdict(current, post=None)',
     W+'test_cc2_relay_retains_on_deadline_and_retries_incompatible_q'),
    ('local read error leaves no seal', 'client_rpc.py',
     "        except OSError:\n            raise Abort() from None\n        return dict(peer=peer, outcome='replied'",
     "        except OSError:\n            return dict(peer=peer, outcome='closed', reply=None, reason=None)\n"
     "        return dict(peer=peer, outcome='replied'",
     W+'test_local_error_reading_reply_aborts_without_seal'),
    ('partial request write leaves no seal', 'client_rpc.py',
     "            if written == 0:\n                return not_sent('startup', peer)\n            raise Abort() from None",
     "            if written == 0:\n                return not_sent('startup', peer)\n            return not_sent('startup', peer)",
     W+'test_partial_request_write_aborts_without_seal'),
    ('protected turn recovers the cached stock proposal', 'client_actions.py',
     '                proposal = self.cached(item)',
     "                proposal = None if self.actions.stock(item['record'].get('process')) else self.cached(item)",
     W+'test_cc2_stock_relay_under_contention'),
    ('emitter ignores TERM while waiting for the reply', 'client_rpc.py',
     '    for number in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT, signal.SIGPIPE):',
     '    for number in (signal.SIGHUP, signal.SIGINT, signal.SIGPIPE):',
     W+'test_term_during_reply_wait_still_seals'),
    ('secret follows the data directory', 'control.py',
     "        return runtime / ('pueue_' + user + '.socket'), data / 'shared_secret'",
     "        return runtime / ('pueue_' + user + '.socket'), runtime / 'shared_secret'",
     'test_client_rpc.EndpointTests.test_separate_runtime_and_data_paths'),
    ('reused id cannot displace the attributed retirement identity', 'client_actions.py',
     "        if stock and record['response']['status'] == 'retired':",
     '        if False:', W+'test_retired_receipt_survives_reused_id_and_finish'),
    ('oversized reply header alone never seals replied', 'client_rpc.py',
     "            if length > REPLY_MAX_BYTES:\n                raise Abort()  # The local limit prevents reading a complete frame.\n"
     '            reply = reply_class(receive(connection, length, wait))',
     "            reply = 'unrecognized' if length > REPLY_MAX_BYTES else reply_class(receive(connection, length, wait))",
     W+'test_oversized_reply_header_without_body_never_seals'),
]


def main():
    nominal = run(SOURCE, 'test_client_rpc', timeout=300)
    if nominal.returncode:
        raise AssertionError(nominal.stdout + nominal.stderr)
    for number, (rule, name, old, new, test) in enumerate(CASES, 1):
        with tempfile.TemporaryDirectory(prefix='cc3-p1-mutant-', dir='/tmp') as folder:
            root = Path(folder)/'pueue-desk'
            shutil.copytree(SOURCE, root, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            path = root/name
            source = path.read_text()
            if source.count(old) != 1:
                raise AssertionError('Nonunique anchor: ' + rule)
            path.write_text(source.replace(old, new))
            result = run(root, test, timeout=90)
            if result.returncode != 1 or 'FAIL:' not in result.stderr or 'ERROR:' in result.stderr:
                raise AssertionError(rule + ' did not fail by assertion:\n' + result.stdout + result.stderr)
            print(str(number) + ' ' + rule + ': assertion', flush=True)
    print(str(len(CASES))+'/'+str(len(CASES))+' mutants; nominal green; '+sys.version.split()[0], flush=True)


if __name__ == '__main__':
    main()
