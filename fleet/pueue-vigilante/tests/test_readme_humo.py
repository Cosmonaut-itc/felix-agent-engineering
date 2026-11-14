from __future__ import annotations

from pathlib import Path
import re
import shlex
import unittest

README = Path(__file__).resolve().parents[1] / 'README.md'
SINTAXIS_DE_SHELL = set(' \t"\';&|$<>()*?`\\')


def ordenes_pueue_add(texto):
    for bloque in re.findall(r'```sh\n(.*?)```', texto, re.S):
        for linea in bloque.splitlines():
            argv = shlex.split(linea, comments=True)
            if argv[:2] == ['pueue', 'add'] and '--' in argv:
                yield linea, argv[argv.index('--') + 1:]


class ReadmeHumo(unittest.TestCase):
    def test_una_orden_con_sintaxis_de_shell_llega_a_pueue_en_una_sola_cadena(self):
        ordenes = list(ordenes_pueue_add(README.read_text(encoding='utf-8')))
        self.assertTrue(ordenes, 'el README no tiene órdenes pueue add en bloques sh')
        for linea, orden in ordenes:
            with self.subTest(linea=linea):
                partidos = [a for a in orden if SINTAXIS_DE_SHELL & set(a)]
                self.assertFalse(
                    len(orden) > 1 and partidos,
                    'pueue add une con espacios los argumentos que siguen a -- y '
                    'ejecuta el resultado con sh -c; pasa la orden como una sola '
                    f'cadena entre comillas simples: {partidos}',
                )


if __name__ == '__main__':
    unittest.main()
