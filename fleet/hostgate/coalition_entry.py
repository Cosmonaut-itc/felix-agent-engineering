"""Bootstrap de una creación host: anuncia ready, espera un byte válido y hace execve del argv fijo.

Fleet lo lanza con ``python -I -S -B coalition_entry.py <go-fd> <ready-fd> <argv-digest> -- <argv...>``, en
sesión nueva, con close_fds y tuberías privadas, después del consumo del permiso. Antes del byte sólo usa
módulos del intérprete; no importa código de usuario ni ejecuta el argv. EOF, una lectura corta o
cualquier otro byte salen con 3 sin entrar. El byte llega una sola vez, cuando el registro de la raíz ya
es durable en hostgate y en el ledger de Fleet.
"""
import hashlib
import json
import os
import sys

GO = b'G'


def argv_digest(argv):
    """Digest canónico del argv de la carga: el mismo que Fleet declara en ``launch.argvDigest``."""
    return hashlib.sha256(json.dumps(list(argv), ensure_ascii=True, separators=(',', ':')).encode('utf-8')).hexdigest()


def main(args):
    if len(args) < 5 or args[3] != '--': return 2
    go, ready, expected, argv = int(args[0]), int(args[1]), args[2], args[4:]
    if argv_digest(argv) != expected: return 2
    os.write(ready, b'R')
    os.close(ready)
    try:
        byte = os.read(go, 1)
    finally:
        os.close(go)
    if byte != GO: return 3  # EOF o un byte inválido: nunca se entra
    os.execvp(argv[0], argv)


if __name__ == '__main__':
    try:
        code = main(sys.argv[1:])
    except BaseException:
        code = 3
    os._exit(code if code is not None else 3)
