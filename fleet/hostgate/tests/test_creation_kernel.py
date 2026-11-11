"""SEN-5650 4c: la autoridad de creación sobre el kernel Darwin real.

En la coalición compartida de remote-run sólo se afirma sobre identidades propias; el cierre real
con evidencia correlacionada sólo corre en coalición propia (HOSTGATE_OWN_COALITION=1)."""
import json
import os
import subprocess
import sys
import unittest
from hostgate import scope
from hostgate.darwin_coalition import DarwinCoalition
from hostgate.tests.test_creation_authority import Authority
from hostgate.tests.test_darwin_coalition import Own


class Real(Authority):
    """Dueño = este proceso de prueba, con su uniqueid real; hijos propios terminados por identidad exacta."""
    ATTEMPTS = 500

    def setUp(self):
        provider = DarwinCoalition()
        native = provider._native
        boot = native.boot()
        self.own = Own(native, boot)
        self.addCleanup(lambda: self.assertEqual(self.own.close(), []))
        self.prepare(boot, provider, pid=os.getpid(), start=(0, 0, native.unique(os.getpid())[0], 1))
        self.retries = 0

    def until(self, call, prefix):
        """Reintenta con operationId nuevos mientras el barrido pida otro intento; occupied no es reintento.

        Cada llamada lleva su prefijo: repetir un operationId sería replay del resultado viejo."""
        for attempt in range(self.ATTEMPTS):
            result = call('%s-%d' % (prefix, attempt))
            codes = [(r['code'], r['retryable']) for r in result['reasons']]
            status = result.get('scopeStatus', {}).get('status')
            if codes != [('unix-scope-unconfirmed', True)] or status not in (None, 'scan-retry'):
                return result
            self.retries += 1
        self.fail('sin barrido aceptado tras %d intentos' % self.ATTEMPTS)

    def started_child(self):
        """authorize real, consume y una raíz propia /bin/sleep registrada por observe_root real."""
        issued = self.until(lambda op: self.authorize(op), 'auth')
        self.assertEqual((issued['exitCode'], issued['permit']['state']), (0, 'issued'), issued['reasons'])
        permit = issued['permit']['permitId']
        self.assertIs(self.consume('consume', permit)['creationAllowed'], True)
        child = subprocess.Popen(['/bin/sleep', '60'], stdin=subprocess.DEVNULL)
        self.own.popens.append(child)
        birth = self.own.birth(child.pid)
        started = self.start('start', permit, dict(pid=birth.pid, unique=birth.unique))
        self.assertEqual((started['exitCode'], started['state']), (0, 'running'), started['reasons'])
        return child, birth

    def release_until(self, prefix):
        return self.until(lambda op: self.release(op), prefix)


@unittest.skipUnless(sys.platform == 'darwin', 'kernel Darwin real')
class SharedKernel(Real):
    def test_own_root_retains_until_terminated(self):
        """Variante: ninguna."""
        child, birth = self.started_child()
        busy = self.release_until('busy')
        self.assertEqual((busy['state'], busy['scopeStatus']['status']), ('running', 'occupied'), busy)
        if busy['scopeStatus']['novelCount'] <= 64:
            self.assertIn([birth.pid, birth.unique], busy['scopeStatus']['novel'])
        self.assertEqual(self.row()['creation']['state'], 'registered')
        self.own.terminate(birth)
        child.wait()
        after = self.release_until('after')
        # Novedades ajenas pueden retener la reserva; la raíz propia terminada ya no figura.
        self.assertIn(after['state'], ('running', 'released'), after)
        self.assertNotIn([birth.pid, birth.unique], after.get('scopeStatus', {}).get('novel', []))


@unittest.skipUnless(sys.platform == 'darwin' and os.environ.get('HOSTGATE_OWN_COALITION') == '1',
                     'sólo en una conexión ssh propia, no multiplexada')
class OwnKernel(Real):
    def test_own_root_closes_with_correlated_real_evidence(self):
        """Variante: ninguna."""
        child, birth = self.started_child()
        busy = self.release_until('busy')
        self.assertEqual(busy['scopeStatus']['novel'], [[birth.pid, birth.unique]], busy)
        self.own.terminate(birth)
        child.wait()
        closed = self.release_until('closed')
        self.assertEqual((closed['exitCode'], closed['state'], closed['permit']['state']), (0, 'released', 'closed'),
                         closed['reasons'])
        row = self.row()
        evidence = scope.evidence_from_json(row['closureEvidence'])
        self.assertEqual(scope.closure_reasons(evidence, row, mode='effective'), [])
        print('OWN_COALITION_CREATION_OK', json.dumps({'retries': self.retries, 'closedBy': row['creation']['closedBy']}),
              flush=True)


if __name__ == '__main__':
    unittest.main()
