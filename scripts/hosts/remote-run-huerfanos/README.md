# Esperas huérfanas de remote-run

LaunchAgent cada 5 minutos: identifica shells de espera de más de 10 minutos
cuyo `~/remote-run/runs/<id>` desapareció. Envía TERM a la espera y a sus
ancestros contiguos `remote-run wait`; a los supervivientes, KILL tras 5 segundos.
Revalida la ausencia antes de cada señal. Nunca actúa sobre carpetas existentes.

`./instalar.sh` instala desde HEAD publicado, verifica SHA-256 y guarda SHA,
blob y sumas en `~/bin/remote-run-huerfanos.origen`. Sustituye `__HOME__` en
el plist y recarga `gui/$(id -u)`. Conserva los temporales de procedencia.
`./instalar.sh --en-seco` ejecuta los bytes publicados por SSH en la Mini,
sin instalar ni enviar señales. Log: `~/Library/Logs/remote-run-huerfanos.log`.

Python 3.9, biblioteca estándar. Pruebas (en host remoto):
`python3 -I -m unittest discover -s scripts/hosts/remote-run-huerfanos/tests -v`.
