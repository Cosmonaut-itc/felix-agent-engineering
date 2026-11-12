# Pueue 4.0.4: pendientes que siguen tras un reinicio (U17a)

El host prepara la cola con el daemon detenido y abre sus pausas cuando está
listo. Una ejecución interrumpida falla visiblemente y no se reencola. Pueue
es el binario estándar 4.0.4, sin parches. No se modifican Hasito, Jale ni
apotheca, ni las reglas 1 a 6 del contrato común.

## Reinicios y contrato con clientes

- Las tareas `Queued` conservan su id y esperan la admisión del host. En el
  desk deben responder Docker y los umbrales de presión permitir la apertura.
- Una tarea `Running` o `Paused` cuando murió el daemon queda `Done/Killed`.
  Si una `Queued` tiene **cualquier** `task_logs/<id>.log`, incluso vacío,
  antiguo o con reloj atrasado, también queda `Done/Killed`. Su grupo queda
  cerrado y fuera de `owned` hasta que el operador lo resuelva y solicite
  admisión. Nunca se clasifica por mtime.
- El motivo queda en `~/.local/state/pao/pueue-desk/guardia.log`, con id,
  identidad `created_at`, grupo y causa `sin_parada`, `log_existente` o
  `parada` (esta última la registra `ExecStop` en el desk). Una parada
  ordenada ya persistida por Pueue conserva su `Killed`; un kill manual
  tampoco habilita ninguna recuperación.
- Los clientes conservan los 10 minutos ante un `Done` no ordinario ajeno a
  su propio `stop`. Con U17a terminan en «interrupción no recuperada», código
  125. Los fallos de transporte, incluido daemon sin responder, conservan
  el plazo de 2 horas. Que la cola siga cerrada no es un fallo de transporte.
- Para relanzar, comprobar primero que no quedan procesos supervivientes y
  crear **una corrida nueva del cliente**. No usar `pueue restart` sobre el
  id interrumpido. `Done/Killed` no acredita la muerte de los descendientes;
  en macOS un hijo puede sobrevivir a SIGKILL del daemon.
- Los clientes encolan normalmente en sus grupos asignados. No se admiten
  `add --immediate`, `start <id>` ni aperturas directas de grupos, que evaden
  la admisión. La reanudación operativa se solicita con `control.py reanudar`.

**Riesgos aceptados al usar Pueue estándar.** Si el daemon cae entre lanzar
un proceso (`spawn.rs:191`) y guardar `Running` (`:246`), el log permite
rechazar la repetición sólo si ese archivo sobrevivió. Un apagado abrupto
puede perder tanto ese log como el nuevo estado y repetir la ejecución.
También puede perderse un `add` confirmado: `State::save` hace rename, pero
no fsync (`state.rs:249`). SIGKILL de un proceso no demuestra durabilidad
ante un corte de energía. U17a no promete resolver esas dos ventanas raras.
Fuentes: [fuentes-u17a.txt](evidence/fuentes-u17a.txt).

## Preparación y admisión

`control.py preparar --host desk|mini` valida la versión exacta, las rutas
estándar y el esquema completo de `state.json`. Rechaza un daemon que
responda, estado comprimido, JSON truncado, tareas/grupos incompatibles y
configuración ambigua. Ante un rechazo no sustituye la cola ni arranca el
daemon; el motivo va al log acotado. Un archivo ausente es primer arranque.
No genera configuración propia ni cambia el socket del cliente normal.
Rechaza variables XDG relativas para no preparar una ruta diferente a la
que resolvería Pueue. Un grupo `Reset` valida y se conserva byte a byte,
pero bloquea el arranque hasta revisión manual: el loop estándar lo abre
sin guardia y elimina sus tareas (`task_handler.rs:100–125`). La preparación
no completa por su cuenta un reset destructivo interrumpido.

Con `control.lock` tomado, la transformación pura `prepare_queue` crea los
grupos administrados faltantes **ya pausados**, no transforma `Reset`, cierra
los grupos `Running` incluso vacíos y registra su propiedad. Respeta las
pausas manuales fuera de `owned`. Guarda primero la intención `owned` y
luego la cola, ambos con temporal, fsync, rename y fsync del directorio.
Repetir la preparación es idempotente. Un log de una `Queued` retira la
propiedad de su grupo antes de guardar el fallo, para exigir intervención.
El reloj sólo fecha el resultado, nunca decide si empezó.

El guardia lee Docker con límite de 3 s antes de PSI. Docker caído implica
pausa; con Docker sano, E/S >40, CPU >30 o RAM <4 GiB también pausan. Sólo
E/S <20, CPU <15 y RAM ≥6 GiB permiten abrir. Los bordes intermedios
mantienen el estado. En la Mini no se requieren Docker ni PSI. Antes de
abrir se aplican las cuotas vigentes. La pausa guarda propiedad **antes**
de `pause --wait`; la apertura la retira **después** de `start -g`. Una
muerte entre intención y efecto se reconcilia en el siguiente ciclo.

Se conserva el formato de U11: `guardia-state.json` es una lista de grupos.
Admite también grupos como `default` cerrados durante el arranque. No hay
estado v2, recuperación de tareas, supervisor ni auditoría del daemon.
`aplicar` no crea grupos online: si faltan, exige preparar con daemon parado.

`control.py` mantiene la única tabla de cuotas: hasito 3/4, jale 2/3,
otros 1/1, pg5434 1/1 (día/noche), y suites=3 en la Mini. Noche es
[05:00Z, 14:00Z). No hay límite global de Pueue.

## Operación del desk

Desde la MacBook, `fleet/pueue-desk/deploy-desk.sh --seco` valida sin
instalar; sin `--seco` transporta por `ssh desk`. Requiere binarios 4.0.4
en `/home/felix/.local/bin`, systemd de usuario y permiso de linger. No
instala binarios. Rechaza relevar un daemon manual con tareas vivas y pide
linger antes de detenerlo. No se ejecutó ningún comando en el desk en este
encargo.

La unidad ejecuta `preparar` en `ExecStartPre`, `parada` en `ExecStop`, y usa
`KillMode=mixed`, `Restart=always`, reintentos de 5 s sin límite de arranque.
El guardia y cuotas conservan `Requires` y `After=pueued.service`. Un fallo
al aplicar cuotas no reinicia el daemon. Linger inicia el servicio cuando
arranca la distro; Windows debe iniciar WSL mediante su mecanismo habitual.

Pausa manual:

```sh
ssh desk '/usr/bin/python3 -I ~/.local/lib/pao-pueue-desk/control.py pausar hasito'
```

Solicitud de reanudación, sujeta a Docker y presión:

```sh
ssh desk '/usr/bin/python3 -I ~/.local/lib/pao-pueue-desk/control.py reanudar hasito'
```

Si el guardia ya había pausado un grupo, usar siempre `pausar` para tomar
su propiedad. Pueue no distingue una segunda pausa manual directa de la suya.

**Mantenimiento:** `fleet/pueue-desk/maintenance-desk.sh` (admite `--seco`)
para primero los timers de cuotas y guardia, después sus servicios y al
final `pueued.service`. Un `systemctl --user stop pueued.service` aislado
no basta: el siguiente tick del guardia puede levantarlo por `Requires`.
Para volver, habilitar/iniciar daemon y ambos timers; siempre pasa por
`preparar`. `withdraw-desk.sh` usa el mismo orden, deshabilita timers y
daemon y retira las unidades. Conserva cola, estado, biblioteca y linger.

## Mini: relevo recuperable (W6)

`deploy-mini.sh [--seco] [--esperar MIN]` y `withdraw-mini.sh` corren en la
propia Mini. El preflight comprueba versión, `brew list --pinned`,
configuración sin rutas personalizadas ni compresión, validez del estado e
ids en disco iguales a los del daemon. Requiere el plist de brew para el
retorno. No instala ni fija binarios automáticamente.

El instalador copia `control.py`, `mini.py` y ambos plists antes de cambiar
servicios. Carga primero `io.pao.pueue-guardia`, que ejecuta
`mini.py reconcile` cada 15 s, también al login. Bajo `control.lock`, escribe
la intención en `mini-handoff.json` antes de pausar o retirar un servicio.
Tiene sólo destino, origen, plazo y fase (`drain` o `switch`); es estado del
relevo de servicios, no de recuperación de tareas.

1. En `drain`, anota los grupos abiertos en `owned` y los pausa con `--wait`.
   Las pendientes pueden quedarse en cola. Si hay tareas `Running/Paused`,
   espera hasta `--esperar` (máximo 120 min); por defecto aborta y solicita
   admisión de sus pausas sin detener el daemon original.
2. Persiste `switch`, deshabilita y retira el origen y habilita/carga el
   destino. Si muere el solicitante, el job conoce ambos servicios y
   completa este intervalo sin daemon. No se limita a abrir grupos.
3. El daemon propio ejecuta `preparar --host mini --exec /opt/homebrew/bin/pueued`.
   `execv` conserva el PID que vigila launchd. Usa KeepAlive y reintentos de
   5 s. El reconciliador libera el candado mientras espera el arranque.
4. El siguiente tick confirma el socket y solicita admisión al guardia.
   Al retirar, confirma primero brew y la admisión; sólo después borra los
   plists propios y retira el job, como último efecto. Si muere durante la
   limpieza, ya hay un daemon y no dejó una pausa propia sin dueño.

Con servicios sanos, la reparación necesita como máximo dos ticks de 15 s
más el arranque y las RPC acotadas. Las pruebas modelan cada frontera con
SIGKILL real y un launchd falso. La cota de un minuto en launchd real sigue
pendiente de validación de despliegue: este encargo prohíbe modificar los
LaunchAgents o el daemon del usuario. Un error persistente del sistema,
estado ilegible o Docker caído bloquean la admisión de forma visible.

Ambos LaunchAgents requieren sesión gráfica, igual que el de brew. No
cambian auto-login ni el arranque del sistema operativo. Una instalación
repetida no borra pausas manuales; un relevo pendiente se completa antes
de aceptar otro.

## Logs y pruebas

`guardia.log` rota a `.1`; cada archivo tiene máximo 1 MiB, incluso para
errores enormes o un archivo heredado que ya superara el límite. Es el único registro propio con append. Los plists y las
unidades descartan stdout/stderr del daemon/control, por lo que `host.log`
no se crea ni crece. La instalación acota `host.log` y `.1` heredados a
1 MiB cada uno. No se altera la política global del journal. Los logs de
las tareas son salida de clientes, fuera del registro operativo del host.
No se habilita `RUST_LOG`, ni existe `daemon-audit.log`.

Medición previa, corrida `diag-u17a-sol`, id `20261112-085629-5a5e3e`:

| Caso medido | Resultado estándar 4.0.4 | Evidencia |
|---|---|---|
| Disco tras SIGKILL | `Running` y `Queued` persistidos | [línea 2](evidence/diag-u17a-sol.txt#L2) |
| Restaurar `Running` | `Done/Killed`, mismo inicio | [línea 3](evidence/diag-u17a-sol.txt#L3) |
| Restaurar `Queued` | mismo id; grupo `work` pausado | [línea 3](evidence/diag-u17a-sol.txt#L3) |
| Pausa manual y grupo vacío | manual sigue pausado; default sigue abierto | [línea 3](evidence/diag-u17a-sol.txt#L3) |
| `Queued` con log antiguo, después abrir | se ejecuta; el log antiguo se trunca | [línea 4](evidence/diag-u17a-sol.txt#L4) |

La primera solicitud por turno normal (`20261112-084703-5a3cfa`) se canceló
sin ejecutar el diagnóstico tras nueve minutos de espera. Se midió con
`REMOTE_RUN_TURNO=directo`, siempre mediante `remote-run`, en una instancia
privada dentro de la corrida. No se tocaron servicios. El guion reproducible
es [diag-u17a.py](evidence/diag-u17a.py).

Prueba dirigida en Mini:

```sh
REMOTE_RUN_HOST=mini remote-run start --name dirigida-u17a-sol --script fleet/pueue-desk/evidence/verificar-u17a.sh
remote-run wait ID
```

El guion habilita `PAO_PUEUE_PRIVATE=1` y ejecuta:

```sh
/usr/bin/python3 -I -m unittest discover -s fleet/pueue-desk/tests -v
plutil -lint fleet/pueue-desk/launchagents/*.plist
shellcheck --severity=warning fleet/pueue-desk/*-desk.sh fleet/pueue-desk/*-mini.sh fleet/pueue-desk/evidence/verificar-u17a.sh
```

Incluye dobles de Docker/PSI, propiedad y pausas manuales, preparación
idempotente, estado ilegible, mantenimiento y SIGKILL por frontera del
instalador y del reconciliador. Las privadas comprueban TERM/KILL,
pendientes con mismo id, grupos vacíos, log antiguo y 500 ciclos de
`add`/SIGKILL sin duplicar centinelas. Si aparece la ventana `Queued` + log,
se comprueba el fallo conservador y se solicita admisión del grupo antes
del siguiente experimento, que usa un id nuevo. Las privadas sólo se habilitan en
Mini con ese indicador; el resto usa dobles en directorios temporales.

Pendiente fuera de este entorno: systemd/cgroup real, WSL/Windows Update,
reboot físico, auto-login de Mini y launchd real; cortes de energía y
barreras del almacenamiento; contrato completo de los tres clientes. No
se ha ejecutado ni instalado nada en el desk, ni desplegado en la Mini.

Respecto de `ca148b17`, se conservan los auxiliares atómicos, validación de
cola, Docker y timeout, cuotas, guardia Mini e intención antes del efecto.
Se excluyen `host.py`, supervisor, recuperación, estado v2, configuración y
socket gestionados, bitácora de auditoría y pruebas de recuperación. La
base de esta implementación es U11 en `origin/main`, no aquel commit.

El primer pase dirigido (`20261112-090936-370225`, SHA `53b0cdd9`) capturó
una ventana real en la tarea 42: `Queued` con log pasó a `Killed` y dejó
`suites` cerrada. El arnés erró al esperar que la siguiente tarea avanzara
sin resolver esa pausa. Se corrigió sólo el arnés para solicitar admisión
después de comprobar el fallo conservador y antes de encolar el siguiente
id. No se reencola la fallida. Salida y causa:
[primer pase](evidence/dirigida-u17a-sol-primer-pase.txt),
[registro del caso 42](evidence/ventana-real-42.jsonl).

El segundo pase (`20261112-091815-75e3ae`, SHA `d9740ac5`) completó los
500 ciclos sin duplicados y observó 44 ventanas `Queued` + log. Dos
consultas al doble Python de Pueue excedieron 10 s antes de ejecutar el
instalador. La lectura de estado de esa fixture pasó a `cat`, manteniendo
las mutaciones en el doble y el timeout de producción de 10 s. Se conserva
la salida [del segundo pase](evidence/dirigida-u17a-sol-segundo-pase.txt).
