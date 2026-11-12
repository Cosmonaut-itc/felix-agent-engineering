# Pueue en el desk y en la Mini

Pueue 4.0.4 estándar, sin binario propio. En el desk corre como servicio systemd
**de usuario** en WSL; en la Mini, como LaunchAgent propio en lugar del de brew.
No instala binarios ni migra clientes.

## Reinicios (U17a)

Si se reinicia `pueued` o la máquina, la cola sigue sola cuando el host está listo:

- Las tareas `Queued` sobreviven y conservan su id. Nada corre antes de que el host
  esté listo: en el desk, Docker responde y la presión lo permite; en la Mini, el
  daemon responde.
- Una tarea que corría cuando murió el daemon o la máquina **no se reejecuta**: queda
  `Done` con resultado `Killed`. El motivo queda en `guardia.log` (evento
  `interrumpida`) y al final de su log de Pueue (`pueue log ID`).
- Una tarea que sigue `Queued` pero ya tiene `task_logs/ID.log` también queda
  `Done/Killed`: Pueue crea el log justo antes de lanzarla (`spawn.rs:134`), así que pudo
  haber corrido, y un log previo del mismo id cae en el mismo lado conservador. Su grupo
  queda **retenido** (evento `retenido`): ni el guardia ni un reinicio lo abren. Tras
  comprobar que no quedan procesos de esa tarea, abrirlo con `control.py reanudar GRUPO`.
- Para repetir una tarea interrumpida se lanza una **corrida nueva desde el cliente**.
  Nadie usa `pueue restart` sobre el id interrumpido: el cliente ya liberó su copia y su
  reserva. `Done/Killed` no prueba que hayan terminado todos sus descendientes.
- Mientras el host arranca, el SSH o el daemon pueden no responder varios minutos.

Lo hace `control.py preparar`, que corre con el daemon parado antes de cada arranque
(`ExecStartPre` en el desk; `io.pao.pueue` en la Mini, que termina con `exec pueued`):

1. Rechaza, sin escribir nada y sin dejar arrancar el daemon, si `pueue status` responde
   (hay otro daemon), si `pueued --version` no es `pueued 4.0.4`, si existe
   `state.json.gz`, si `pueue.yml` fija `pueue_directory` o comprime el estado, o si
   `state.json` o `guardia-state.json` no se leen o no validan. Pueue, ante un estado
   ilegible, arranca vacío y lo sobrescribe ([evidencia](evidence/diag-u17a.txt), línea 11).
2. Pasa a `Done/Killed` las tareas `Running` o `Paused`, y las `Queued` con log.
3. Cierra (`Paused`) todos los grupos abiertos y los anota como propios. Crea **ya cerrados**
   los grupos administrados que falten. `aplicar` nunca usa `group add`, que crea grupos
   abiertos: si falta uno, falla.
4. Escribe primero `guardia-state.json` y después `state.json`, los dos con fsync del
   archivo y del directorio (`F_FULLFSYNC` en macOS). Una muerte entre ambos deja pausas
   con dueño, y la siguiente pasada termina el trabajo. Dos pasadas dejan los mismos bytes.
5. Borra un `pueue.pid` viejo que tras reiniciar la máquina apunta a otro proceso: con él,
   Pueue no arranca (línea 12).

Después el guardia abre sólo los grupos que cerró él o `preparar`. Una pausa manual
nunca se abre sola.

Riesgos aceptados (decisión de Felix del 2026-11-12):

- Si el daemon cae entre lanzar una tarea y guardar `Running` (`spawn.rs:191` y `:246`),
  esa tarea puede correr otra vez. Si el log llegó a disco, `preparar` lo detecta; si un
  apagado brusco lo pierde, no.
- Un `add` ya confirmado puede perderse en un apagado brusco: `State::save` no hace fsync
  (`state.rs:249`). El cliente lo ve como id inexistente.

Otros límites conocidos: en la Mini, tras un SIGKILL del daemon, el proceso de la tarea
sigue vivo (huérfano); la tarea no se reejecuta, pero hay que matarlo a mano.
`pueue add --immediate`, `pueue start ID` y `pueue start -g` saltan la admisión: no se usan
mientras el host arranca. Para abrir un grupo, `control.py reanudar GRUPO`.

### Medido (Mini, instancia privada)

[`evidence/diag-u17a.txt`](evidence/diag-u17a.txt), de
[`evidence/diag-u17a.py`](evidence/diag-u17a.py) por `remote-run` (`diag-u17a-opus`):

| Pregunta | Resultado | Línea |
|---|---|---|
| SIGKILL con `Running`, `Queued` y una pausa manual: qué queda en disco | la tarea sigue `Running`, los grupos tal cual | 3 |
| Qué restaura Pueue | `Running`→`Done/Killed`; pausa sólo el grupo con `Queued`; `vacio` y `default` siguen abiertos | 4 |
| Un grupo abierto sin cola admite un `add` al arrancar | sí, corre en el acto | 5 |
| `Queued` con log creado, al reanudar | se lanza otra vez: el centinela tiene 2 líneas y el log viejo se trunca | 6–7 |
| Estado editado sin daemon (`Running`→`Killed`, grupo nuevo `Paused`) | Pueue lo restaura tal cual; el grupo nuevo no admite hasta `start` | 8–10 |
| `state.json` truncado | arranca con 0 tareas y lo sobrescribe | 11 |
| `pueue.pid` con el pid de otro proceso vivo | `pueued` sale con 1 | 12 |
| SIGTERM con una `Running` | guarda `Done/Killed`; los grupos quedan abiertos | 13 |

El agente de brew de la Mini es `sh.brew.pueue`, sin `KeepAlive`, y `pueue` no está
fijado en brew ([`evidence/sondeo-mini.txt`](evidence/sondeo-mini.txt), líneas 3–22, sólo
lectura).

## Desk

Desde la MacBook: `fleet/pueue-desk/deploy-desk.sh --seco` comprueba sin escribir;
`fleet/pueue-desk/deploy-desk.sh` instala por `ssh desk`. Requiere `pueue` y `pueued`
4.0.4 en `/home/felix/.local/bin`, systemd de usuario y permiso para
`loginctl enable-linger felix`. El instalador lo pide antes de detener un `pueued`
manual: si se rechaza, termina con error y el daemon manual sigue vivo. Si
`pueued.service` ya estaba activo, el preparar nuevo se aplica en su próximo arranque.

`pueued.service`: `ExecStartPre=control.py preparar`, `Restart=always`,
`StartLimitIntervalSec=0` (un rechazo se reintenta cada 5 s sin límite y queda en el
journal), `KillMode=mixed` (SIGTERM sólo a `pueued`; después systemd mata lo que quede) y
`ExecStop=control.py parada`, que sólo anota en `guardia.log` qué tareas corta.
Un fallo de `aplicar` no reinicia `pueued`: `pueue-desk-mode.service` reintenta cada 30 s.
Linger inicia el servicio al arrancar la distro; el arranque de Windows debe iniciar
WSL por su mecanismo habitual (SSH despierta la distro).

`control.py` contiene la única tabla de cuotas: hasito 3/4, jale 2/3, otros 1/1 y
pg5434 1/1 (día/noche). Noche: [05:00Z, 14:00Z). La suma es 7/9 incluyendo pg5434;
Pueue no impone un máximo global. Los timers y el arranque aplican el modo UTC vigente,
sin interrumpir tareas vivas. Usar sólo esos grupos; `default` y otros grupos quedan
fuera de estas cuotas. El instalador elimina `suites` sólo si no tiene ninguna tarea.

Cada 60 s el guardia comprueba `docker info` (3 s; binario `--docker-bin`,
`PAO_DOCKER_BIN` o `/usr/bin/docker`). Si falla, pausa los grupos de la tabla con motivo
`docker` y no abre nada. Con Docker, lee PSI avg60 y MemAvailable: pausa con E/S >40,
CPU >30 o RAM <4 GiB; reabre sus grupos con E/S <20, CPU <15 y RAM ≥6 GiB. Los bordes
intermedios mantienen el estado. Guarda la propiedad **antes** de pausar: una muerte
entre los dos pasos no deja una pausa sin dueño.

Ver fila: `ssh desk '~/.local/bin/pueue status -g hasito'`.
Pausar a mano, dejando terminar tareas:
`ssh desk '/usr/bin/python3 -I ~/.local/lib/pao-pueue-desk/control.py pausar hasito'`.
Reanudar: `ssh desk '/usr/bin/python3 -I ~/.local/lib/pao-pueue-desk/control.py reanudar hasito'`.
`reanudar` retira la pausa manual o la retención y pide admisión al guardia: abre sólo si
Docker y la presión lo permiten; si no, el guardia lo abrirá cuando lo hagan. **Si el
guardia ya había pausado el grupo, pausar siempre con `pausar`**: Pueue no distingue una
segunda pausa manual de la suya.

**Parada de mantenimiento.** `pueue-desk-guard.service` y `pueue-desk-mode.service` tienen
`Requires=pueued.service`: con sus timers activos, el siguiente disparo vuelve a levantar
el daemon. Para dejarlo parado: `fleet/pueue-desk/deploy.py parar` (admite `--seco`), que
detiene primero los dos timers, después sus servicios y al final `pueued.service`.
Volver: `fleet/pueue-desk/deploy.py arrancar` (daemon y timers; `preparar` corre antes).

Retirar: `fleet/pueue-desk/withdraw-desk.sh` (admite `--seco`). Detiene y deshabilita
timers y daemon, en ese orden, y quita unidades; conserva cola, estado, biblioteca y
linger. No drena tareas: retirar cuando hayan terminado.

## Mini

`deploy-mini.sh` y `withdraw-mini.sh` corren **en la propia Mini** y admiten `--seco` y
`--esperar MIN` (minutos de espera a que terminen las tareas en marcha; 0 por defecto).
Antes de instalar hay que fijar la versión: `brew pin pueue` (hoy no lo está). La
comprobación previa también exige `pueued 4.0.4`, `pueue.yml` sin `pueue_directory` propio
ni compresión, que el `state.json` por defecto coincida con `pueue status` y que exista
`~/Library/LaunchAgents/sh.brew.pueue.plist`.

- `io.pao.pueue`: `control.py preparar --host mini --exec` con `RunAtLoad`, `KeepAlive`
  y `ThrottleInterval` 30. Sustituye el proceso por `pueued`, así launchd vigila el mismo
  PID. Un rechazo va sólo a `guardia.log` y launchd reintenta cada 30 s.
- `io.pao.pueue-guardia`: `mini.py vigilar` cada 30 s y al cargar. Completa un relevo
  interrumpido, acota `host.log` y reabre las pausas propias.

**Relevo e interrupciones (W6).** Instalar y retirar anotan el relevo en
`guardia-state.json` antes de tocar servicios, con el candado tomado:

1. Cargan `io.pao.pueue-guardia` (instalar) y anotan `relevo` en fase `preparando` junto
   con la propiedad de los grupos abiertos; después los pausan y esperan sin tareas en
   marcha. Si no se vacía, revierten el relevo y reabren.
2. Pasan a fase `relevando` y dejan cargado sólo el servicio objetivo: instalar coloca y
   carga `io.pao.pueue` y hace `disable` y `bootout` de brew; retirar descarga
   `io.pao.pueue`, quita su plist y hace `enable` y `bootstrap` de brew. El plist propio
   sólo está en `LaunchAgents` mientras es el servicio vigente, así que un login no carga
   un segundo daemon.
3. Esperan a que responda el objetivo (con `kickstart`), reabren las pausas propias y
   cierran el relevo. Retirar quita al final el plist del vigilante y lo descarga.

Si el proceso muere en cualquier punto, incluido SIGKILL, `vigilar` revierte un relevo en
`preparando` y completa uno en `relevando`, siempre en una sola pasada: en menos de un
minuto responde un daemon y ningún grupo queda pausado sin dueño. Nunca responden dos:
`preparar` no arranca mientras otro responda, y Pueue no arranca con el `pueue.pid` de un
daemon vivo. `tests/test_mini.py` lo comprueba con SIGKILL antes y después de cada
llamada externa de instalar y de retirar.

**Parada de mantenimiento.** `mini.py parar` descarga primero el vigilante y después
`io.pao.pueue`; `mini.py arrancar` los vuelve a cargar. Los plists quedan en su sitio: un
login también los carga.

## Registros y su límite

Todo en `~/.local/state/pao/pueue-desk/`, junto con `guardia-state.json` y `control.lock`:

- `guardia.log` (JSONL UTC: lecturas, decisiones, `interrumpida`, `retenido`, `cerrado`,
  relevos y errores) rota a `.1` al llegar a 1 MiB.
- `host.log` (sólo Mini: salida de `pueued`, nivel WARN) se copia a `.1` y se trunca al
  llegar a 1 MiB, en cada pasada de `vigilar`; `pueued` escribe con `O_APPEND`.
- En el desk, la salida de `pueued` va al journal de usuario, que acota journald
  (`SystemMaxUse`, por defecto 10 % del sistema de archivos y 4 GiB como máximo).
  `journalctl --user -u pueued -u pueue-desk-guard`.

## Pruebas

En la Mini: `/usr/bin/python3 -I -m unittest discover -s fleet/pueue-desk/tests -v`
(`test_private.py` usa una instancia privada de Pueue y se salta sin `pueued` 4.0.4),
`plutil -lint fleet/pueue-desk/launchagents/*.plist` y
`shellcheck --severity=warning fleet/pueue-desk/*.sh fleet/pueue-desk/evidence/*.sh`.
Todo junto: `remote-run start --name dirigida-… --script fleet/pueue-desk/evidence/verificar-u17a.sh`.
Última corrida, `20261112-101657-9c241c`: 52 pruebas OK en 416 s, ninguna saltada; W6 con
SIGKILL en 25 llamadas de instalar y 22 de retirar, antes y después de cada una; `plutil` y
ShellCheck limpios ([`evidence/dirigida-u17a.txt`](evidence/dirigida-u17a.txt), líneas 64–92).

Sin comprobar todavía: que systemd reintente un `ExecStartPre` fallido con
`Restart=always` y que `KillMode=mixed` limpie el cgroup (desk); qué deja en disco
`wsl --terminate` o un reinicio de Windows; que la Mini inicie sesión sola tras reiniciar,
sin la cual no cargan los LaunchAgents; y la retención efectiva de journald en el desk.
