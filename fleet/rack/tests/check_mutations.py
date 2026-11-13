"""Prove four safety regressions are caught, without editing the working tree.

TMPDIR=/tmp /usr/bin/python3 tests/check_mutations.py
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

BASE=Path(__file__).resolve().parents[1]
CASES=(
    ('whois-fresco','serve.py','allows(self.client_address[0], fresh=True)',
     'allows(self.client_address[0], fresh=False)',
     'test_actions.AuthorizationTests.test_whois_is_fresh_for_every_mutation_even_after_cached_read'),
    ('origin','serve.py',"origins != ['http://' + host]",'False',
     'test_actions.AuthorizationTests.test_each_auth_rejection_prevents_effect'),
    ('digest','accion.py',"if row[0] != digest:",'if False:',
     'test_actions.ActionsTests.test_id_is_bound_to_digest_and_duplicate_does_not_act'),
    ('unknown','accion.py',"intent = result(request, 'unknown')", "intent = result(request, 'rejected', 'not_found')",
     'test_actions.ActionsTests.test_intent_committed_before_effect_and_crash_before_effect'),
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
    print('All four mutations caught; original tree untouched.')


if __name__=='__main__':main()
