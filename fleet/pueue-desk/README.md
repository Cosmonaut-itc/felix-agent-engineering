# Pueue en el desk

Servicio systemd **de usuario** para Pueue 4.0.4 en WSL, listo para instalar
tras U10 (NVMe). No instala binarios. El [retiro de deskpolicy](../deskpolicy/)
se despliega por separado durante el corte coordinado de los clientes a Pueue.

Desde la MacBook: `fleet/pueue-desk/deploy-desk.sh --seco` comprueba sin escribir;
`fleet/pueue-desk/deploy-desk.sh` instala por `ssh desk`. Requiere `pueue` y
`pueued` en `/home/felix/.local/bin`, systemd de usuario y permiso para
`loginctl enable-linger felix`. El instalador lo pide antes de detener un
`pueued` manual: si se rechaza, termina con error y el daemon manual sigue vivo.
Un fallo de `aplicar` no reinicia `pueued`: `pueue-desk-mode.service` reintenta
cada 30 s y deja el error en `journalctl --user -u pueue-desk-mode`.
Linger inicia el servicio al arrancar la distro; el arranque de Windows debe
iniciar WSL por su mecanismo habitual (SSH despierta la distro).

`control.py` contiene la única tabla de cuotas (día/noche): hasito 1/2,
jale 5/7, jale-verify 1/1, otros 2/2, pesadas 2/3 y rapidas 3/3.
Noche: [05:00Z, 14:00Z). La suma es 14/18; Pueue no impone un máximo global.
`rapidas` es el carril rápido que comparten Hasito, Jale y Bodega para la
prueba dirigida; cada lanzador corta sus tareas a los 600 s. `pesadas` recibe
las suites de horas (`contract` y `e2e` completas de Jale) para que no ocupen
todo `jale`, y `jale-verify` las sesiones de verify, que ya eran de una en una.
El lanzador de Jale las enruta desde SEN-HASO/Jale#705. Los timers
y el arranque aplican el modo UTC vigente, sin interrumpir tareas vivas.
Usar sólo esos grupos; `default` y otros grupos quedan fuera de estas cuotas.
`otros` es de los repositorios sin grupo propio: `remote-run` encola ahí toda
corrida normal sin `REMOTE_RUN_PROYECTO=hasito`, que sólo fija el lanzador de
Hasito.
El instalador elimina los grupos retirados, `suites` y `pg5434`, sólo si no
tienen ninguna tarea. `pg5434` reservaba un lugar para el Postgres del puerto
5434 y nunca recibió tareas: los lanzadores toman ese recurso con
`REMOTE_RUN_RECURSOS`. Su lugar pasó a `otros`.

### Reparto

La tabla sale de la medición del 2026-11-13 sobre el historial de Pueue
(891 tareas del desk en 31 h, 645 de la Mini en 44 h):

- `hasito` tuvo cero tareas corriendo 57 % del tiempo y dos o más sólo 0,8 %.
- Las corridas de Jale de menos de 10 min sumaron 11,8 h corriendo y 153,4 h
  en fila, con espera mediana de 13,8 min.
- `contract` completa de Jale ocupó 52 % de las horas-lugar de `jale`, con
  hasta seis a la vez.
- Las sesiones de verify retuvieron 8 % de las horas-lugar de `jale`, y 29
  salieron con 75 por su candado tras esperar turno.

`jale` se queda en 5/7 con ese enrutamiento, por decisión de Felix: sus lugares
quedan para las corridas cortas, que son las que hacen fila. En la Mini,
`suites` queda en 3 y `rapidas` en 3; ahí la tabla desk no aplica. Después
de adoptar U3.1, los ajustes pasan exclusivamente por el panel/controlador
durable descrito abajo; una llamada directa a `parallel` se considera deriva.

Ver fila: `ssh desk '~/.local/bin/pueue status -g hasito'`.
Pausar a mano, dejando terminar tareas:
`ssh desk '/usr/bin/python3 -I ~/.local/lib/pao-pueue-desk/control.py pausar hasito'`.
Reanudar: `ssh desk '~/.local/bin/pueue start -g hasito'`.
El guardia respeta grupos previamente pausados. **Si el guardia ya había
pausado el grupo, usar siempre el comando `pausar` anterior**: Pueue no distingue
una segunda pausa manual de la suya. Ese comando retira la propiedad del guardia
bajo el mismo candado; una pausa directa de Pueue no puede expresar ese relevo.

Cada 60 s el guardia lee PSI avg60 y MemAvailable. Pausa con E/S >40,
CPU >30 o RAM <4 GiB; reanuda sólo sus grupos con E/S <20, CPU <15 y RAM ≥6 GiB.
Los bordes intermedios mantienen el estado. `rapidas` sólo mira la memoria:
se pausa con RAM <4 GiB y se reanuda con RAM ≥6 GiB. Registro JSONL UTC, con
lecturas, decisiones (`accion` para los demás grupos y `accion_rapidas`) y acciones: `ssh desk 'tail -f ~/.local/state/pao/pueue-desk/guardia.log'`.
Rota a `.1` al llegar a 1 MiB. Estado y candado comparten ese directorio.
Ante errores de lectura o de Pueue falla sin reanudar; consultar también
`journalctl --user -u pueue-desk-guard.service`.

Retirar: `fleet/pueue-desk/withdraw-desk.sh` (admite `--seco`). Detiene y
deshabilita timers y daemon y quita unidades; conserva cola, estado, biblioteca
y linger. No drenar tareas automáticamente: retirar cuando hayan terminado.

Pruebas: `/usr/bin/python3 -I -m unittest discover -s fleet/pueue-desk/tests -v`.
ShellCheck: `shellcheck --severity=warning fleet/pueue-desk/*-desk.sh`.

## Guardia de la Mac Mini

La Mini comparte la lógica de `control.py` (perfil `--host mini`), pero lee la
carga de 1 minuto con `os.getloadavg()`, los núcleos con `os.cpu_count()` y la
presión de memoria con `/usr/sbin/sysctl -n kern.memorystatus_vm_pressure_level`.
Los umbrales viven en `MINI_THRESHOLDS`; los de `rapidas`, en
`RAPIDAS_THRESHOLDS["mini"]`. `PRESSURE_THRESHOLDS` es sólo del desk, porque
`fleet/rack/snapshot.py` lo lee del controlador instalado allí.

| Acción | Carga1 / núcleos | Presión de memoria |
| --- | --- | --- |
| Pausar admisión | > 2,0 **o** | ≥ 2 (aviso 2 o crítico 4) |
| Reanudar grupos propios | < 1,2 **y** | = 1 (normal) |

Así, con 10 núcleos se pausa por carga >20 y sólo se reanuda por carga <12
con memoria normal. `rapidas` es lo último en pausarse: sólo con carga >3,0 por
núcleo (>30 con 10 núcleos) o memoria ≥2. Se reanuda con el mismo criterio que
los demás. Si un grupo no existe en Pueue, la guardia lo omite y deja de
tenerlo como propio. Los bordes mantienen el estado. Una lectura ausente,
no finita, negativa o un nivel de memoria desconocido causa error, se registra
y no decide ni modifica grupos.

En la Mini, desde este checkout (no instala Pueue):

```sh
/usr/bin/python3 -I fleet/pueue-desk/install-mini.py instalar --seco
/usr/bin/python3 -I fleet/pueue-desk/install-mini.py instalar
/usr/bin/python3 -I fleet/pueue-desk/install-mini.py desinstalar --seco
/usr/bin/python3 -I fleet/pueue-desk/install-mini.py desinstalar
```

`--seco` imprime el plan sin ejecutar comandos ni escribir; las comprobaciones
se ejecutan al instalar. Antes de cualquier cambio exige el LaunchAgent
`sh.brew.pueue` cargado y una respuesta válida de `/opt/homebrew/bin/pueue status
--json` con `suites` y `default`. No arranca, detiene ni reinicia ese daemon,
ni modifica `parallel`. La instalación es idempotente, reemplaza cada archivo de
forma atómica y copia el controlador a
`~/.local/lib/pao-pueue-mini/control.py`, renderiza rutas absolutas en
`~/Library/LaunchAgents/local.pao.pueue-mini.guard.plist` y usa
`launchctl bootstrap gui/$(id -u)`; para retirar usa `bootout` del servicio de
la guardia. El LaunchAgent ejecuta `/usr/bin/python3 -I` cada 60 s
(`StartInterval`, sin `KeepAlive`).

Afecta sólo a `suites`, `rapidas` y `default`. No reclama grupos ya pausados, y suelta la
propiedad si observa que alguien los arrancó. Para tomar manualmente una pausa
que hizo la guardia, usar el mismo candado y retirar su propiedad. El grupo
sigue en pausa, ahora como pausa manual:

```sh
/usr/bin/python3 -I ~/.local/lib/pao-pueue-mini/control.py pausar suites --host mini
```

Para reanudarlo después: `/opt/homebrew/bin/pueue start -g suites`.
Una segunda pausa directa de Pueue no expresa ese relevo. El estado
`guardia-state.json`, `control.lock` y el log JSONL UTC viven en
`~/.local/state/pao/pueue-mini`. Verlo con
`tail -f ~/.local/state/pao/pueue-mini/guardia.log`; rota a `guardia.log.1` a
1 MiB. Los errores de ejecución de launchd aparecen en `launchd.err.log` del
mismo directorio. Desinstalar reanuda los grupos que pausó la guardia y vacía su propiedad; las
pausas manuales no son suyas y siguen en pausa. Conserva el log.
Si instalar o desinstalar fallan con la guardia descargada, el guion reanuda
sus grupos y, si no puede, vuelve a cargarla con la propiedad intacta; en ambos
casos sale con error. Si tampoco puede recargarla, el error nombra los grupos que
siguen en pausa y el comando `pueue start -g` para reanudarlos.

**Límite:** sólo frena la admisión: las tareas que ya corren siguen. Las
corridas de `remote-run` encolan en `suites`, o en `rapidas` con `--carril rapido`; las corridas de Jale en la Mini
no pasan por Pueue y esta guardia no las frena.

Pruebas en la Mini, siempre mediante su Pueue:

```sh
REMOTE_RUN_HOST=mini ~/.local/bin/remote-run start --name dirigida-u18 -- '/usr/bin/python3 -I -m unittest discover -s fleet/pueue-desk/tests -v'
REMOTE_RUN_HOST=mini ~/.local/bin/remote-run wait ID
```

Mutaciones dirigidas (también dentro de `remote-run`):
`/usr/bin/python3 -I fleet/pueue-desk/tests/mutate_mini.py`.

## Lugares durables de la Mini (U3.1)

El perfil administrado es `suites=3`, `default=1`, `rapidas=3`, tope **7**.
Cada componente está entre 1 y 8; un grupo pausado sigue consumiendo su cuota.
El controlador valida el vector entero visto, revisión, hash de perfil y generación
del daemon. Un clic sólo admite delta −1/+1. Desde un exceso heredado permite
bajar de forma monotónica; nunca aumentar sobre el tope. Reducir no mata tareas
activas: sólo limita las admisiones siguientes.

Tras la adopción, `~/.local/state/pao/pueue-mini/control.sqlite3` es la autoridad.
Una transacción `synchronous=FULL`/`fullfsync=ON` guarda deseo, revisión y recibo
antes de toda RPC. El ajuste dura **hasta otro cambio manual**, sin vencimiento.
Los overrides incluyen actor fijo `Felix`, fecha e ID, y deshacer restaura los
overrides anteriores, no sólo el número. El índice de ID/digest/resultado no se poda.
Una revisión/vector distinto impide deshacer. El ensayo devuelve before/target,
subidas, bajadas y argv previsto sin escribir, recuperar ni cambiar cuotas.

La guardia existente de 60 s también reconcilia el deseo: reducciones primero,
verificación de cada RPC y después aumentos. No hay otro timer o recuperador.
Un fallo ofrece `partial` con observado/deseado, o `unknown` cuando no se puede
leer el resultado; consultar el mismo recibo, sin enviar otra acción. El siguiente
tick converge al deseo aceptado incluso con acciones nuevas apagadas. Se mantienen
la histéresis y la escalera de `rapidas` (#118).

La propiedad de pausas vive separada del perfil: `pressure`, `manual` y
`ambiguous`. La intención de pausar se guarda antes de la RPC. La adopción importa
`guardia-state.json`, conserva como manual cualquier pausa sin dueño verificable,
y retiene referencias legacy/ajenas con motivo, sin asignarles cuotas ni abrirlas.
El JSON anterior se renombra a `guardia-state.imported.json`; deja de ser escribible
como autoridad. Una lectura corrupta o una base desaparecida nunca aplica la tabla
por defecto ni permite abrir grupos. `control.adopted` impide reinicializarla.

La generación se identifica con el `pid_path` configurado, su fecha y la identidad
`ps` del PID concreto. No se buscan ni terminan procesos por nombre. Tras reiniciar
`pueued`, se reponen cuotas, pero las pausas observadas quedan ambiguas y retenidas.
**No se promete reapertura automática ni recuperación de tareas tras reboot**:
`restart_queue=false`. Para resolver una pausa de un grupo administrado, Felix
comprueba la causa por SSH y usa, por ejemplo:

```sh
/usr/bin/python3 -I ~/.local/lib/pao-pueue-mini/control.py reanudar suites --host mini
```

El comando reconcilia primero, exige presión fresca baja y vuelve a verificar
vector/generación antes de admitir. No hay botón de reanudación en el panel.
`pausar suites --host mini` toma propiedad manual antes de la pausa.

### Instalar en la Mini (pasos; no forman parte de las pruebas)

1. Desde el checkout verificado **en la Mini**, para la primera adopción comprobar
   el perfil vivo 3/1/3,
   Pueue estándar 4.0.4 y el `pid_path` de su configuración. El valor predeterminado
   es `~/Library/Application Support/pueue/pueue.pid`; si la instalación lo
   personaliza, añadir `--pid-file /ruta/configurada/pueue.pid` en ambos comandos.
   No modificar la configuración de Pueue para ajustarla a esta suposición.
2. Preparar copia de seguridad del controlador/plist anteriores y del estado de
   guardia, con la guardia detenida o bajo `control.lock`. No copiar una SQLite
   abierta con `cp`: usar la API SQLite `backup` bajo ese candado.
3. Revisar el plan y luego ejecutar:

   ```sh
   /usr/bin/python3 -I fleet/pueue-desk/install-mini.py instalar --cuotas --seco
   /usr/bin/python3 -I fleet/pueue-desk/install-mini.py instalar --cuotas
   ```

   El preflight de configuración/perfil ocurre antes de bootout o escrituras.
   Después se bloquean sólo las cuotas con `slots.disabled`, se descarga la guardia,
   se espera su candado, se adopta/importa y se reemplaza atómicamente el controlador.
   Sólo tras `bootstrap` se retira el switch creado por esa instalación; uno anterior
   se conserva. Las reinstalaciones detectan la autoridad ya adoptada aunque se
   omita `--cuotas`, y conservan sus ajustes/recibos. No instala ni reinicia
   `pueued`, ni cambia sus LaunchAgents.
4. Instalar después el rack actualizado (`fleet/rack/instalar.sh`). Verificar
   capacidades y ensayo por el canal existente. Leer también la guía del rack.
   Si un intento falló, **conservar** SQLite, marcador y switch; reinstalar la misma
   versión. Si el switch era del intento fallido, quitarlo a mano sólo después de
   verificar guardia cargada, deseo legible y vector reconciliado. Nunca re-adoptar
   una base ausente o corrupta: restaurar una copia verificada bajo el candado.

### Revertir sin perder la intención de Felix

Deshacer con el recibo original mientras la revisión/vector sigan iguales devuelve
exactamente los valores y overrides anteriores; no reduce la revisión ni borra la
auditoría. Para apagar esta entrega, crear `slots.disabled` en el directorio Mini:
se apagan capacidades/cuotas nuevas, y siguen lectura, cancelación y reconciliación.
Se puede restaurar la versión anterior del rack manteniendo **este controlador y
su guardia**. No devolver la guardia antigua ni borrar SQLite mientras haya deseo
durable: `desinstalar` lo rechaza antes de bootout. Una retirada completa requiere
migrar explícitamente propiedad y deseo; no la simula abriendo grupos directamente.

Canario de instalación, sólo supervisado por Felix: sin alerta, grupo vacío con
cuota ≥2, guardar before completo y ensayar. Con tope lleno, bajar `suites 3→2`
y **deshacer inmediatamente ese recibo** (`2→3`); comprobar valores y overrides
originales. Así se ejercitan bajada, subida y undo sin dejar un override nuevo
para el mismo número. Cada envío usa revisión/vector recién confirmados. Si se
intercala otro ajuste, el undo estricto se rechaza: no encadenar undos antiguos
ni forzar una restauración. Ante unknown, consultar el mismo ID, nunca lanzar la
inversa a ciegas. Las pruebas del repositorio usan exclusivamente dobles y un
daemon privado, sin este canario productivo.
