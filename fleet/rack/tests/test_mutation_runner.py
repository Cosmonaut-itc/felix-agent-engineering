"""A failed Node harness is not a behavioral mutation kill."""
import subprocess
import unittest
import contextlib
import io
from unittest.mock import patch
import check_mutations as runner


class MutationRunnerTests(unittest.TestCase):
    def test_touch_mutant_dies_by_behavior_not_dom_exception(self):
        cases=tuple(case for case in runner.REORDER_CASES if case[0]=='order-touch-scroll')
        self.assertEqual(len(cases),1)
        output=io.StringIO()
        with patch.object(runner,'REORDER_CASES',cases),patch.object(runner.sys,'argv',['check_mutations.py','--reorder-only']),contextlib.redirect_stdout(output):
            runner.main()
        self.assertNotIn('TypeError:',output.getvalue(),'complete the DOM before firing touch')
        self.assertIn('touch body must not capture the pointer',output.getvalue())

    def test_wrapped_node_exceptions_are_not_assertion_kills(self):
        for name in ('TypeError','ReferenceError','SyntaxError','RangeError','Error','URIError','CustomFailure'):
            with self.subTest(error=name):
                output=('FAIL: test_production_ui\nAssertionError: 1 != 0 : '+name+
                        ': synthetic DOM failure\n    at row (synthetic.js:1:1)\nFAILED (failures=1)\n')
                result=subprocess.CompletedProcess([],1,stdout='',stderr=output)
                with self.assertRaisesRegex(AssertionError,'completar el doble o afirmar la conducta'):
                    runner.assert_mutation_failure('order-touch-scroll',result)

    def test_node_behavior_assertion_is_a_kill(self):
        output=('FAIL: test_production_ui\nAssertionError: 1 != 0 : AssertionError [ERR_ASSERTION]: '
                'touch body must not capture\n    at row (synthetic.js:1:1)\nFAILED (failures=1)\n')
        runner.assert_mutation_failure('order-touch-scroll',subprocess.CompletedProcess([],1,stdout='',stderr=output))
        output=('FAIL: test_recovery\nAssertionError: 1 != 0 : node:internal/assert/utils:146\n'
                '  throw error;\n  ^\n\nAssertionError [ERR_ASSERTION]: cached group\n'
                '    at [eval]:28:8\nFAILED (failures=1)\n')
        runner.assert_mutation_failure('order-cached-recovery-owner',subprocess.CompletedProcess([],1,stdout='',stderr=output))

    def test_handled_python_exception_can_explain_behavior_assertion(self):
        output=('FAIL: test_64_receipt\nTraceback (most recent call last):\n  File "test.py", line 1\n'
                'ValueError: Respuesta demasiado grande\n\nDuring handling of the above exception, another exception occurred:\n\n'
                'AssertionError: El recibo válido debe atravesar el transporte\nFAILED (failures=1)\n')
        runner.assert_mutation_failure('order-reply-64k',subprocess.CompletedProcess([],1,stdout='',stderr=output))


if __name__=='__main__':unittest.main()
