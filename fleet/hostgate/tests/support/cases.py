"""Adaptación mecánica de parametrización a unittest, sin dependencias."""
import unittest


def parametrize(names, values):
    def decorate(fn):
        fn.parameters = (names.replace(' ', '').split(','), values)
        return fn
    return decorate


def suite(namespace):
    result = unittest.TestSuite()
    for name, fn in namespace.items():
        if not name.startswith('test_') or not callable(fn):
            continue
        parameters = getattr(fn, 'parameters', None)
        if parameters is None:
            result.addTest(unittest.FunctionTestCase(fn, description=name))
        else:
            names, values = parameters
            for i, value in enumerate(values):
                args = (value,) if len(names) == 1 else value
                def run(fn=fn, args=args):
                    fn(*args)
                result.addTest(unittest.FunctionTestCase(run, description='%s[%d]' % (name, i)))
    return result
