# Pueue en el desk

Servicio systemd **de usuario** para Pueue 4.0.4 en WSL, listo para instalar
tras U10 (NVMe). No instala binarios. El [retiro de deskpolicy](../deskpolicy/)
se despliega por separado durante el corte coordinado de los clientes a Pueue.

Antes de adoptar el control durable, desde la MacBook:
`fleet/pueue-desk/deploy-desk.sh --seco` comprueba sin escribir;
`fleet/pueue-desk/deploy-desk.sh` instala por `ssh desk`. Requiere `pueue` y
`pueued` en `/home/felix/.local/bin`, systemd de usuario y permiso para
`loginctl enable-linger felix`. El instalador lo pide antes de detener un
`pueued` manual: si se rechaza, termina con error y el daemon manual sigue vivo.
Un fallo de `aplicar` no reinicia `pueued`: `pueue-desk-mode.service` reintenta
cada 30 s y deja el error en `journalctl --user -u pueue-desk-mode`.
Linger inicia el servicio cuando arranca la distro. Este README no promete que
Windows arranque WSL sin intervención tras un reinicio; describe lo que pasa
cuando la distro arranca (ver «Arranque y reinicios», U3.4).

Tras adoptar, `deploy-desk.sh` ya no es el camino de instalación; usar
`install-desk.py` como se describe abajo, también al cambiar la tabla.

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
Ningún instalador elimina grupos. Los retirados, `suites` y `pg5434`, se quitan
con `control.py retirar suites` (o `pg5434`), que se niega si el grupo tiene
alguna tarea; mientras sigan, la guardia los ignora. `pg5434` reservaba un lugar
para el Postgres del puerto 5434 y nunca recibió tareas: los lanzadores toman ese
recurso con `REMOTE_RUN_RECURSOS`. Su lugar pasó a `otros`.

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
Reanudar en un desk adoptado:
`ssh desk '/usr/bin/python3 -I ~/.local/lib/pao-pueue-desk/control.py reanudar hasito'`.
Ese comando retira la razón manual y comprueba cuotas, horario y presión antes de abrir.
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

El canal del panel espera hasta 1 s por `control.lock`: un tick breve de la
guardia o del snapshot puede terminar antes de atender el ajuste. Si vence sin
entrar, `slots` y `undo` devuelven `rejected/lock_busy`, `effects=false`, sin
escribir deseo ni recibos en el host. El rack guarda ese rechazo terminal y
permite una acción nueva. Las capacidades muestran «El control está ocupado.
Vuelve a intentarlo.», sin atribuir una pausa. Consultar un recibo con el candado
ocupado conserva `unknown`; nunca reenvía la mutación.

Sólo las RPC de `control.py rack` en la Mini tienen límite de 3 s. Las guardias
del desk y de la Mini, y `aplicar`, esperan a Pueue como antes: un `status` o una
pausa lentos no cancelan el cierre protector por ese límite del panel.

La propiedad de pausas vive separada del perfil: `pressure`, `manual` y
`ambiguous`. La intención de pausar se guarda antes de la RPC. La adopción importa
`guardia-state.json`, conserva como manual cualquier pausa sin dueño verificable,
y retiene referencias legacy/ajenas con motivo, sin asignarles cuotas ni abrirlas.
Si observa un grupo Running, suelta inmediatamente su razón `pressure`, incluso
en la banda de histéresis. Una pausa posterior de otro actor no le pertenece;
las pausas que siguen siendo propias por presión conservan su admisión habitual.
El JSON anterior se renombra a `guardia-state.imported.json`; deja de ser escribible
como autoridad. Una lectura corrupta o una base desaparecida nunca aplica la tabla
por defecto ni permite abrir grupos. `control.adopted` impide reinicializarla.

La protección por presión sigue cerrando los grupos Running existentes aunque
la autoridad de cuotas sea ilegible, falte un grupo, haya una cuota inválida o
falle una RPC. Conserva el diagnóstico y el deseo; no crea grupos ni aplica el
perfil por defecto. No abre mientras falle la reconciliación, y un fallo al
pausar un grupo no impide proteger los demás. Se conserva la escalera #118.
Si no puede guardar propiedad en SQLite, una marca durable
`pause-GRUPO.manual` retiene esa pausa como manual incluso después de reparar
la autoridad. La marca no asigna cuotas ni concede permiso para abrir; sólo
`control.py reanudar GRUPO --host mini`, con reconciliación y sensores frescos,
la retira. No borrar estas marcas para recuperar admisión automáticamente.

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

   El modo `--seco` sólo imprime el plan; no ejecuta el preflight. Al instalar,
   el preflight de configuración/perfil ocurre antes de bootout o escrituras.
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

El guion `tests/private_slots.py --run-private` admite Mini y desk únicamente
dentro de `remote-run`, con Pueue estándar y directorio/configuración propios.
En Linux usa `~/.local/bin/{pueue,pueued}`; admite rutas explícitas con
`--pueue` y `--pueued`. Comprueba tanto pérdida de `parallel` sin guardado como
persistencia tras una operación guardadora y reconciliación en ambos casos.

### Lugares del desk desde el rack (U3.2)

El desk usa el mismo `rack-host-control@1`, recibos SQLite, undo y `control.lock`
que la Mini. Su tabla vigente tiene **seis** grupos: hasito 1/2, jale 5/7,
jale-verify 1/1, otros 2/2, pesadas 2/3 y rapidas 3/3 (día/noche).
Suman **14/18**. `pg5434` fue retirado; `default` queda fuera del reparto y sin ±.
No se absorben grupos ajenos en el perfil; su propiedad de pausa se conserva
por separado, incluso cuando el grupo ya no existe.
Si aparece trabajo no terminal fuera del reparto (incluido `default`), se publica
sólo su conteo `foreign_work`: no se exponen tareas ni entornos. Bloquea aumentos
sólo en ± y undo; permite reducciones seguras. La reconciliación hacia el deseo
durable (horario o ajuste ya aceptado) sigue aplicando su tabla sin tocar el grupo ajeno.
La guardia conserva sus razones de presión y su capacidad de reabrir.

Cada grupo admite 1..8 lugares, contando también las cuotas de grupos pausados.
El host coteja revisión, hash de tabla/horarios, generación y vector vivo completo.
Sólo admite una reducción monotónica si el vector ya excede el tope. Aplica
bajadas antes de subidas y verifica cada RPC. Un parcial conserva el deseo y
su recibo; `guardia` y `aplicar` usan el mismo reconciliador, sin un job nuevo.
Ni las guardias ni el timer heredan el timeout de las RPC del panel.

Un ± dura **hasta el próximo evento estricto 05:00/14:00 UTC**, no hasta que un
timer consiga ejecutarse ni hasta que cambie el nombre del modo. Se guarda la
identidad del evento y el vencimiento absoluto. Consulta y ensayo proyectan
los eventos perdidos sin escribir ni recuperar. La guardia y `aplicar` retiran
ajustes vencidos y reconcilian la tabla actual. Un `slots` real ordinario también
recupera un evento perdido antes de juzgar el clic: responde conflicto y pide
actualizar, indicando efectos de horario si hubo recuperación. Una llamada anticipada a
`aplicar` conserva el ajuste vigente. Un undo tras el evento se rechaza sin RPC;
antes del evento restaura números y metadatos originales, sin renovar vigencia.
Una intención incompleta vencida conserva `partial`/`unknown` mientras falle la
tabla nueva; termina `aborted/expired` sólo tras verificar su convergencia, y
jamás se presenta como el ajuste anterior aplicado.

`slots.disabled` apaga nuevos ± y undo; no impide consultar ni completar una
intención aceptada. `maintenance` retiene también reconciliación/aperturas.
Una autoridad ilegible/ausente con marcador nunca habilita la tabla por defecto:
no hay aperturas ni aumentos; la guardia conserva su cierre protector y #118.
Tras perder `parallel`, se repone el deseo sin abrir pausas ambiguas. Esta
entrega mantiene `reorder` y `restart_queue` en false; U3.3 habilita `mode` sólo en el desk.
Un controlador instalado que nunca fue adoptado responde `unsupported`, sin
crear archivos; una consulta de recibo devuelve `unknown`. Un marcador o SQLite
perdido/corrupto conserva el diagnóstico de recuperación, sin readoptar.

### Instalar U3.2 en un desk que ya tiene servicio y timers

Pasos para el operador, **no ejecutados por las pruebas**:

1. Llevar el checkout verificado al desk. Comprobar Pueue **4.0.4 estándar** y
   el `pid_path` configurado; no publicar `status --json`, pues incluye `envs`.
   El instalador exige `--pid-file` explícito y valida su identidad. Preparar
   respaldo del controlador y propiedad anteriores; si ya hay SQLite, usar su
   API `backup` bajo `control.lock`, nunca copiar una base abierta.
2. Desde ese checkout **en WSL**, con los dos timers existentes activos:

   ```sh
   /usr/bin/python3 -I fleet/pueue-desk/install-desk.py --pid-file /ruta/configurada/pueue.pid --seco
   /usr/bin/python3 -I fleet/pueue-desk/install-desk.py --pid-file /ruta/configurada/pueue.pid
   ```

   `--seco` y la instalación comparten el preflight de lectura: controlador
   existente, ambos timers activos, autoridad válida o tabla vigente, y PID de
   `pueued`. El ensayo no crea archivos y distingue lo comprobado de los pasos
   pendientes. La instalación comprueba lo mismo antes de detener nada;
   apaga cuotas nuevas, detiene los dos timers y espera sus
   oneshots, toma el candado, publica atómicamente el controlador y adopta/importa
   propiedad, o conserva la autoridad y sus recibos. Reconcilia, reactiva los
   mismos timers y retira sólo su propio switch. No reinicia `pueued`, no edita
   su configuración ni instala unidades. Conserva `control.pre-slots.py`.
3. Ejecutar después `fleet/rack/instalar-desk.sh` desde la máquina de despliegue
   y actualizar el rack en la Mini (`fleet/rack/instalar.sh`): sólo actualizan
   snapshot/despachador y panel, con las llaves existentes. Verificar capacidades
   y ensayo por el canal forzado antes del canario. El canario productivo lo
   ejecuta el orquestador tras el merge; no forma parte de esta instalación.
4. Si falla, conservar marcador, SQLite y switch; reinstalar esta versión.
   Retirar manualmente el switch de un intento fallido sólo tras verificar
   ambos timers activos y deseo reconciliado. Un switch previo se conserva.
   No readoptar una base perdida/corrupta: restaurar respaldo verificado bajo
   el mismo candado. `deploy-desk.sh`/`withdraw-desk.sh` rechazan una autoridad
   ya adoptada para impedir volver a una guardia o tabla ciegas.

### Cambiar GROUP_LIMITS después de adoptar

Cambiar la tabla en el checkout revisado y ejecutar primero `install-desk.py --seco`
y luego `install-desk.py` con el mismo `--pid-file` de los pasos anteriores.
El ensayo toma el `control.lock` existente sin crear archivos e imprime la migración:
retira ajustes vigentes, fija el deseo a la tabla nueva del evento actual, cambia
`policy_hash` y sube una revisión. La instalación hace esos cuatro cambios en una
sola transacción y después reconcilia reducciones antes que aumentos. Conserva
recibos y razones de pausa; nunca readopta ni reinicia el daemon.

Ambos caminos validan la estructura de la autoridad anterior, salvo el hash y
la correspondencia del deseo con la tabla sustituida. Exigen cero recibos
`applying`, `partial` o `unknown`: si hay alguno, recuperar primero con el
controlador anterior. Una base perdida, ilegible o corrupta se rechaza sin efectos.
La comprobación se repite bajo el candado antes de publicar. El controlador en
operación sigue rechazando un hash diferente; sólo el instalador puede migrarlo.
`deploy-desk.sh` no sirve para actualizar un desk adoptado. La Mini presenta el
mismo límite con `MINI_PROFILE`; su migración queda para una unidad aparte.

Para revertir la entrega, deshacer el recibo antes de su evento, o apagar cuotas
con `slots.disabled` y volver al panel anterior, **conservando este controlador y
los timers** para recuperación/expiración. No borrar autoridad ni restaurar el
controlador anterior sobre un deseo vigente. Una retirada completa exige una
migración explícita de deseo/propiedad y queda fuera de U3.2.

Prueba privada dirigida: `tests/private_slots.py --run-private --host desk`
comprueba pérdida/reposición de `parallel`, las entradas `aplicar` y `guardia`,
retiro de ajustes al evento, undo vencido sin RPC tras perder el timer y
recuperación de otro evento por un clic ordinario (reloj/sensores inyectados,
daemon stock real).
No encola tareas y siempre espera al PID exacto de su daemon al terminar.

### Día/Noche del dashboard (U3.3)

La cabecera del desk permite elegir Día o Noche por el mismo canal
`rack-host-control@1` (`op: "mode"`, `change: {"mode": "dia"|"noche"}`).
Aplica la tabla completa del destino (14/18), retira los ajustes de lugares y
verifica reducciones antes de aumentos. Reducir `parallel` deja seguir las tareas
vivas. Pedir el modo vigente es `same_mode`, sin restablecer cuotas.
La Mini conserva `fijo`, perfil 3/1/3, y rechaza `mode` sin mutar Pueue.
`reorder` y `restart_queue` siguen deshabilitados.

El host fija el vencimiento al próximo evento **estricto** 05:00/14:00 UTC;
volver al modo automático retira el adelanto. Timer y guardia existentes resuelven
el evento por identidad, también si no corrió el timer o confirma el mismo modo.
Un adelanto nocturno a las 16:00 de México muestra «Adelantado · vuelve a día a
las 08:00» y «Ajustes manuales hasta las 23:00». A las 23:00 vence y pasa a
«Automático · cambia a día a las 08:00». Las notas vienen del host con
`America/Mexico_City`; el reloj del navegador no decide la vigencia. Tras calcular
las notas se vuelve a validar el evento antes de aceptar modo, undo o ensayo:
si cruzó, responde stale_state o undo_conflict sin cambiar la política ni emitir
RPC. Sin tzdata se deshabilita mode, conservando ±, undo y recuperación UTC.
La vista publica las tablas desde GROUP_LIMITS; el canal valida las notas del
modo y las horas 00:00–23:59, manteniendo las dos notas si coexisten adelanto y ±.

El recibo conserva modo, adelanto y ajustes anteriores. Undo exige la revisión
posterior exacta, vector y generación observados y ningún evento cruzado;
restaura también los vencimientos originales. Si cambió algo devuelve
`409 undo_conflict`: «El horario o los lugares cambiaron; no se puede deshacer
este ajuste». Un adelanto o undo que aumente cualquier grupo se rechaza entero
con `foreign_work` cuando hay trabajo vivo fuera de los seis grupos. Los cambios
que sólo bajan pasan; reconciliar la tabla del evento conserva la excepción de U3.2.
Un parcial conserva deseo/observado y se recupera por el mismo motor; un resultado
incierto se consulta con su ID, nunca se repite. `slots.disabled` cubre modo y undo.

**Instalación sobre U3.2 ya adoptada:** usar los mismos dos comandos de
`install-desk.py --pid-file … --seco` y sin `--seco` documentados arriba, desde
el checkout revisado en WSL. El preflight y la instalación toman `control.lock`.
La única ampliación de la política JSON en SQLite es `mode_override: null`;
no cambia tablas ni `policy_hash`, revisión, ajustes, vencimientos ni recibos.
Ambos rechazan la migración si hay recibos `applying`, `partial` o `unknown`,
sin apagar timers ni escribir archivos. La publicación conserva
`control.pre-mode.py`; no reinicia, readopta ni borra la base de `pueued`.
Después actualizar despachador/snapshot con `fleet/rack/instalar-desk.sh` y
panel con `fleet/rack/instalar.sh`; verificar capacidades/ensayo antes del
canario, que ejecuta el orquestador después del merge.

**Reversión:** primero volver al panel/despachador anteriores, conservando esta
autoridad/controlador para timer y guardia. Para retirar también el controlador,
esperar a que venza el adelanto y que el reconciliador confirme tabla y recibos
terminales; verificar `mode_override=null`, sin deriva ni pendientes. Bajo el
mismo candado y con disparadores/oneshots detenidos como en el instalador,
restaurar `control.pre-mode.py` y reactivar los timers. El campo nulo adicional y
los recibos se conservan; U3.2 los tolera. No restaurar un SQLite viejo ni borrar
recibos. Con un adelanto activo el controlador anterior falla cerrado: no usar
ese procedimiento para cancelar un modo pendiente o desconocido.

Pruebas dirigidas: `tests/test_mode.py`, `tests/mutate_slots.py --mode-only` y
`tests/private_slots.py --run-private --host desk --mode`. La última se ejecuta
sólo mediante `remote-run`: daemon/configuración propios, siete tareas sintéticas
acotadas, reducción 18→14 sin detenerlas, evento 05Z y undo exacto, con limpieza
por la puerta privada y espera del PID propio.

### Arranque y reinicios (U3.4)

`pueued.service` arranca así, cada vez que la distro o systemd lo inician:

1. `ExecStartPre` ejecuta `control.py preparar --host desk` con el daemon
   detenido. Exige Pueue 4.0.4 estándar, la configuración que escribe Pueue
   (sin `env_vars`, `shell_command` ni rutas propias, `compress_state_file:
   false`) y un `state.json` íntegro: zona horaria válida en cada fecha, también
   anidada, ids y grupos coherentes. Si algo falla, no escribe nada y `pueued`
   no arranca; systemd reintenta cada 5 s sin límite de arranques.
2. Bajo `control.lock` guarda primero la intención en SQLite y después escribe
   la cola de forma atómica. Cierra **todos** los grupos, también vacíos y
   `default`, y fija las cuotas del horario vigente; los ajustes y adelantos
   vencidos no se reponen. Si se cae a la mitad, preparar otra vez llega a las
   mismas decisiones sin duplicar motivos.
3. Cada tarea recibe una decisión:
   - Running o Paused: falla como `Killed` y **no se reejecuta**.
   - Queued sin log: conserva id y lugar.
   - Queued con log previo: falla igual, porque pudo haber empezado.
   - Stashed, Success, Failed y cancelaciones previas: se quedan como están.
   - Locked: es una edición desconocida. Pueue la deja en Stashed y su grupo queda retenido.
4. `ExecStart` es el `pueued` estándar. Al arrancar, la guardia reconcilia
   cuotas y modo, toma una lectura fresca de presión y espera a que
   `docker info` responda. Después abre sólo los grupos que cerró el arranque
   (motivo `startup`). Las pausas manuales y las retenidas siguen cerradas; las
   de presión siguen su histéresis habitual.
5. `ExecStop` (`control.py parada`) anota las tareas vivas antes de que
   systemd detenga el daemon con `KillMode=mixed`. Si el daemon cae, `Restart=always` lo
   vuelve a preparar y arrancar; los hijos que quedaban en el cgroup mueren con
   la unidad. La guardia y el modo usan `Requisite=pueued.service`: nunca
   arrancan el daemon, y si fallan no lo reinician.

**Qué ve Felix en el panel tras un reinicio:**

- En la tarea cortada: «SEN-123 · … · Interrumpida al reiniciar Pueue; relanzar manualmente». Relanzar es una corrida nueva con id nuevo.
- En un grupo retenido: «Retenido al reiniciar: <motivo> · resolver con control.py reanudar <grupo>». No tiene botón.
- Si se espera a Docker: «Cerrado al reiniciar · espera a Docker».

La fila `default` sigue oculta mientras esté vacía. Sólo aparece si guarda una tarea cortada.

Los motivos de retención son:

- «una tarea en fila ya tenía registro»
- «una tarea quedó en edición»
- «grupo fuera del reparto» (`default` y grupos ajenos que estaban abiertos)
- «ya estaba pausado sin motivo»
- «Pueue arrancó sin preparar la cola»: Pueue se inició fuera de la unidad y sus pausas no tienen origen comprobable.

**Resolver un grupo retenido**: revisar la causa por SSH y luego

```sh
ssh desk '/usr/bin/python3 -I ~/.local/lib/pao-pueue-desk/control.py reanudar pesadas'
```

`reanudar` reconcilia, exige presión fresca baja y abre sólo ese grupo. Funciona
también con `default` o con un grupo ajeno, siempre que exista en Pueue. Para
dejarlo cerrado, no hacer nada: la guardia nunca abre una retención.

**Mantenimiento** (actualizar Pueue, mover datos, revisar la cola):

```sh
ssh desk '/usr/bin/python3 -I ~/.local/lib/pao-pueue-desk/control.py mantenimiento entrar'
# … trabajo …
ssh desk '/usr/bin/python3 -I ~/.local/lib/pao-pueue-desk/control.py mantenimiento salir'
```

`entrar` escribe el marcador `maintenance` con fsync y detiene, en este orden:
timers, después `pueue-desk-guard`/`pueue-desk-mode` y al final `pueued`. Su
`ExecStop` anota las tareas cortadas. Mientras exista el marcador, ni la guardia
ni el timer reabren ni reconcilian. `install-desk.py`, `deploy-desk.sh` y su
`--seco` se niegan a instalar. `salir` quita el marcador y arranca `pueued`, que
vuelve a preparar, y después los timers.

**Instalar U3.4 en el desk adoptado sin reiniciar `pueued`.** Estos pasos no se
ejecutan en las pruebas. Primero, desde la máquina de despliegue, actualizar
el rack: `fleet/rack/instalar-desk.sh` (snapshot y despachador) y
`fleet/rack/instalar.sh` (panel). El panel anterior rechazaría los motivos
nuevos (`startup`). Después, desde el checkout revisado **en WSL**:

```sh
/usr/bin/python3 -I fleet/pueue-desk/install-desk.py --pid-file /ruta/configurada/pueue.pid --seco
/usr/bin/python3 -I fleet/pueue-desk/install-desk.py --pid-file /ruta/configurada/pueue.pid
```

Ambos hacen, antes de cualquier efecto:

- lee `systemctl --user show-environment` y aplica la misma validación que
  `preparar` a configuración, versión y cola;
- exige que el `pid_path` adoptado sea el que encontrará `preparar`;
- rechaza el mantenimiento y los recibos `applying`, `partial` o `unknown`.

La instalación real hace después lo de U3.3: apaga cuotas nuevas, detiene
timers y toma el candado. Luego, bajo `control.lock`, añade a la autoridad el
registro `startup` (fase `none`) sin readoptar ni tocar recibos. Publica el
controlador, guardando `control.pre-u34.py`, y reconcilia. Copia
`pueued.service`, `pueue-desk-guard.service` y `pueue-desk-mode.service`,
guardando las anteriores en `~/.local/lib/pao-pueue-desk/units.pre-u34/`, y hace
`daemon-reload`. **No reinicia `pueued`.** Por último comprueba que MainPID y
ActiveState siguen iguales y que la unidad cargada ya tiene `Restart=always`,
`KillMode=mixed` y el `ExecStartPre` de `preparar`. Si algo no coincide, falla
en voz alta. Para verificarlo a mano:

```sh
systemctl --user show pueued.service -p MainPID -p ActiveState -p NeedDaemonReload \
  -p Restart -p KillMode -p ExecStartPre
```

El MainPID debe ser el de antes y `NeedDaemonReload=no`. La nueva política rige
desde el próximo arranque del daemon, sea un reinicio de Windows, una caída o
`mantenimiento salir`. Las tareas que ya corren no se tocan.

**Revertir:** con el daemon vivo, restaurar las tres unidades desde
`units.pre-u34/` y hacer `systemctl --user daemon-reload`, sin reiniciar
`pueued`. Para volver también al controlador anterior, primero no debe quedar
ningún motivo `startup` (guardia ya pasó) ni retenciones sin resolver. Después,
con timers detenidos y bajo el candado como en el instalador, restaurar
`control.pre-u34.py` y reactivar los timers. El campo `startup` de la autoridad
se conserva; U3.3 lo ignora. No restaurar un SQLite viejo ni borrar recibos.

**Riesgos raros aceptados** (Pueue 4.0.4 estándar, sin binario propio):

1. Si `pueued` lanza una tarea y cae antes de guardar `Running`, la cola
   guardada la muestra Queued y podría repetirse. La comprobación del log previo
   la detecta casi siempre, porque la marca como fallida y retiene el grupo,
   pero no lo elimina del todo.
2. Pueue guarda `state.json` sin fsync: un `add` confirmado puede perderse ante
   un corte eléctrico. Las pruebas con SIGKILL no demuestran durabilidad ante
   apagón.

Pruebas: `tests/test_startup.py` (dobles: S-W1, O-W1, O-W2, cada estado de
tarea, caída en cada frontera, mantenimiento, instalador) y
`tests/mutate_startup.py`. En el rack: `fleet/rack/tests/test_startup.py` y
`check_mutations.py u34`. `tests/test_private.py --run-private` corre **sólo**
mediante `remote-run` en el desk. Usa un `pueued` privado en unidades
transitorias `pao-u34-<cand>-*` con HOME/XDG propios para ejercitar `ExecStartPre`,
`ExecStop`, Requisite, SIGKILL con `Restart=always` y una cola corrupta.
