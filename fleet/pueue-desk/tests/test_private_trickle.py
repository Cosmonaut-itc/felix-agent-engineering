"""Exercise the remote harness against the guarded local double, without a daemon."""
import shlex
import unittest
from unittest.mock import patch
import private_trickle as harness
import test_trickle as fixture


class PrivateHarnessTests(unittest.TestCase):
    def test_guard_harness_matches_A_events_and_clocks_and_quotes_commands(self):
        f=fixture.TrickleTests();f.setUp();self.addCleanup(f.doCleanups)
        f.tasks.clear()
        original_call=f.p.call
        commands=[]
        def call(*args):
            if args[:2]==('group','add'):
                f.p.groups[args[2]]=dict(status='Running',parallel_tasks=args[4]);return ''
            if args[0]=='add':
                ident=len(f.tasks)+1;f.add_task(ident,args[3],offset=ident)
                commands.append(args[-1]);return str(ident)
            return original_call(*args)
        f.p.call=call
        constructor=fixture.control.MiniControl
        def make(p,directory,**kwargs):
            f.engine=constructor(p,directory,clock=lambda:f.now,generation=lambda:f.gen,**kwargs)
            f.engine.boot_clock=lambda:(fixture.BOOT,f.ns)
            return f.engine
        def shape():
            data=f.p.status()
            return ({g:v['status'] for g,v in data['groups'].items()},
                    {int(k):(t['group'],next(iter(t['status']))) for k,t in data['tasks'].items()})
        events=[]
        with patch.object(fixture.control,'MiniControl',side_effect=make),patch.object(harness.time,'time',return_value=f.now):
            harness.guard_scenario(fixture.control,f.p,f.root,shape,lambda event,**fields:events.append(dict(event=event,**fields)))
        self.assertEqual([e['event'] for e in events],['guard_mid','guard_mid_again','permit','guard_low'])
        self.assertEqual(events[1]['evento'],'tope_alcanzado')
        self.assertTrue(events[-1]['reopened'])
        self.assertEqual([shlex.split(cmd) for cmd in commands],[['/bin/sleep','30']]*2)


if __name__=='__main__':unittest.main()
