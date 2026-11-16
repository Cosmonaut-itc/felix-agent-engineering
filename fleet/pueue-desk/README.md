# Pueue en el desk

El [contrato local de clientes CC0](CLIENT-CONTROL.md) define el esquema y
corpus común para coordinar mutaciones. No instala el coordinador ni cambia
la ejecución: la compuerta de clientes coordinados sigue apagada hasta CC8.

Servicio systemd **de usuario** para Pueue 4.0.4 en WSL, listo para instalar
tras U10 (NVMe). No instala binarios. El [retiro de deskpolicy](../deskpolicy/)
se despliega por separado durante el corte coordinado de los clientes a Pueue.

El desk ya tiene la autoridad durable de U3.3. Para actualizar desde la MacBook:
`fleet/pueue-desk/deploy-desk.sh --seco` valida sin escribir y
`fleet/pueue-desk/deploy-desk.sh` publica por el SSH existente. También se puede
usar `install-desk.py` desde el checkout en WSL, como se describe abajo.
Ambos actualizan la unidad y hacen `daemon-reload` **sin reiniciar pueued**.
No relevan ni adoptan incidentalmente un daemon manual. No instalan binarios,
llaves ni listeners. Un fallo de cuotas no reinicia el daemon.

Cuando arranca la distro, systemd de usuario inicia el servicio habilitado con
linger. Esto no acredita que Windows inicie WSL sin intervención tras Update.
La entrega no promete reinicio desatendido de Windows.

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
El instalador conserva `suites`, `pg5434` y otros grupos existentes. Sus pausas
se registran por separado del perfil. Un retiro explícito y vacío puede hacerse
con `control.py retirar suites --host desk` (o `pg5434`), bajo el mismo candado,
con intención recuperable. Si hay cualquier tarea, incluso terminal, se rechaza.
Nunca se retiran grupos como efecto incidental del despliegue.

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
Ese comando retira la razón manual/ambigua y comprueba cuotas, horario, generación, presión y Docker antes de abrir.
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
deshabilita timers y daemon; conserva unidades, cola, autoridad, biblioteca
y linger, y deja `maintenance` para impedir reactivaciones. No drenar tareas automáticamente: retirar cuando hayan terminado.

Pruebas: `/usr/bin/python3 -B -I -m unittest discover -s fleet/pueue-desk/tests -v`.
`-B` evita reutilizar bytecode generado al alternar perfiles del mismo tamaño
en los módulos temporales del arnés de migración.
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

Instalador histórico de **sólo guardia**, antes de adoptar U3.5 (no instala Pueue).
El relevo del daemon se describe en «Reinicios de la Mini (U3.5)»; los comandos
con `--relevo` administran ese relevo. Sin ese flag se conserva la actualización
de guardia de U3.1, sin drenar ni reiniciar el daemon; una guardia adoptada ya
cargada permanece cargada durante la publicación atómica de su código.

```sh
/usr/bin/python3 -I fleet/pueue-desk/install-mini.py instalar --solo-guardia --seco
/usr/bin/python3 -I fleet/pueue-desk/install-mini.py instalar --solo-guardia
/usr/bin/python3 -I fleet/pueue-desk/install-mini.py desinstalar --solo-guardia --seco
/usr/bin/python3 -I fleet/pueue-desk/install-mini.py desinstalar --solo-guardia
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
`pause-GRUPO.manual` conserva el origen del cierre en ambos hosts. En un cierre
protector contiene exactamente `pressure\n` sólo si la guardia leyó el estado en
esa pasada y no había razones `manual` ni `ambiguous`; sin esa prueba escribe
`manual\n`. Un fallo al soltar propiedad tras un arranque externo siempre escribe
`manual\n`; también se sube una marca pressure si no pudo retirarse antes de
soltar esa propiedad. Una marca previa distinta de los bytes exactos `pressure\n` nunca se
rebaja a presión; contenido vacío, desconocido, ilegible o un enlace roto retiene
como manual. La marca no asigna cuotas ni evita los gates de admisión.

El preparador del reinicio incorpora la marca antes de inferir pausas sin dueño.
En la primera pasada sana, la guardia guarda `pressure` en SQLite y **después**
borra su marca; la presión y los gates normales deciden cuándo reabrir.
`pausar GRUPO` sube una marca existente a `manual\n` con fsync antes de guardar
la pausa manual o enviar su RPC; sin marca no crea una. Las marcas manuales
siguen reteniendo después de reparar SQLite y sólo `control.py reanudar GRUPO`
(con `--host mini` en la Mini), con reconciliación y sensores frescos, las retira.
No borrar marcas a mano para recuperar admisión.

En la Mini, la generación se identifica con el `pid_path` configurado, su fecha y la identidad
`ps` del PID concreto (el desk usa otra; ver «Identidad del daemon del desk»). No se buscan ni terminan procesos por nombre. Tras reiniciar
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
   /usr/bin/python3 -I fleet/pueue-desk/install-mini.py instalar --solo-guardia --cuotas --seco
   /usr/bin/python3 -I fleet/pueue-desk/install-mini.py instalar --solo-guardia --cuotas
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
durable: `desinstalar --solo-guardia` lo rechaza antes de bootout. Una retirada completa requiere
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
   su configuración. En U3.4 también publica unidades y hace daemon-reload, sin restart. Conserva `control.pre-slots.py`.
3. Ejecutar después `fleet/rack/instalar-desk.sh` desde la máquina de despliegue
   y actualizar el rack en la Mini (`fleet/rack/instalar.sh`): sólo actualizan
   snapshot/despachador y panel, con las llaves existentes. Verificar capacidades
   y ensayo por el canal forzado antes del canario. El canario productivo lo
   ejecuta el orquestador tras el merge; no forma parte de esta instalación.
4. Si falla, conservar marcador, SQLite y switch; reinstalar esta versión.
   Retirar manualmente el switch de un intento fallido sólo tras verificar
   ambos timers activos y deseo reconciliado. Un switch previo se conserva.
   No readoptar una base perdida/corrupta: restaurar respaldo verificado bajo
   el mismo candado. En U3.4 `deploy-desk.sh` usa el mismo instalador;
   `withdraw-desk.sh` realiza el mantenimiento ordenado descrito abajo.

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
`deploy-desk.sh` delega esta actualización al mismo instalador.

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

### Cambiar MINI_PROFILE después de adoptar y relevar

Con la Mini adoptada y `mini_handoff.phase=done`, editar sólo `MINI_PROFILE`
en el checkout revisado, conservando los grupos suites/default/rapidas y valores
enteros de 1 a 8 que en conjunto respeten el tope existente de **7** lugares.
Desde ese checkout en la Mini, ejecutar primero:

```sh
/usr/bin/python3 -I fleet/pueue-desk/install-mini.py instalar --relevo --seco
```

Después, ejecutar el mismo comando sin `--seco`. Ambos toman el `control.lock`
existente y validan configuración, cola, PID, generación, dueño del daemon,
guardia habilitada, autoridad y recibos. El ensayo anuncia el hash, la revisión,
el deseo resultante y si conserva o retira ajustes, sin escribir. La instalación
anuncia esos mismos datos, publica la biblioteca y migra `policy_hash`, deseo y
una revisión en una transacción FULL, sin reiniciar el daemon, descargar la
guardia ni abrir grupos. Reconcilia las cuotas con reducciones antes de aumentos.
Conserva `startup`, `mini_handoff`, razones de pausa y todos los recibos.

Los ajustes `slots` vigentes conservan sus valores absolutos y metadatos sobre
la base nueva. Si el conjunto supera el tope de 7, se retiran todos los
ajustes activos y el deseo pasa a la base nueva, como en la migración del desk;
los recibos permanecen. La revisión anterior cambia una sola vez. Repetir con
la tabla ya migrada y la biblioteca idéntica al checkout no escribe; volver al
perfil anterior usa el mismo procedimiento y vuelve a migrar, sin resucitar
ajustes retirados.

Una base corrupta o ilegible, recibos `applying/partial/unknown`, preparación
pendiente, relevo distinto de `done` o generación no vigente bloquean la migración
antes de publicar o cambiar cuotas. Recuperar primero con la versión anterior;
nunca borrar/readoptar SQLite. El runtime mantiene la comprobación estricta del
hash: sólo este instalador inicia una migración. Antes de publicar el primer
archivo guarda bajo `control.lock` una intención durable `table-migration.json`
con los hashes origen/destino y la biblioteca completa. Si hay un corte, el
login y la guardia instalados completan esa intención sin necesitar el checkout
ni un daemon vivo; repetir el instalador también la completa antes del preflight.
Al repetir desde A una transición pendiente A→B, primero completa B y después
migra de vuelta a A. Sólo esa recuperación acreditada admite el hash anterior;
retira la intención tras validar estrictamente biblioteca y autoridad destino.
La reparación de una biblioteca distinta, aun con el mismo perfil, usa esa misma
puerta: instalar sobre `done`, dueño PAO y generación vigente. Repetir una
retirada o converger desde `aborted` no inicia una intención de tabla.
Si se acepta mantenimiento después de un corte, la intención espera mientras
el relevo propio pasa por `maintenance`, `switch` y `verify`; `continuar` y los
ticks reconstruyen primero el daemon y `done`, y después completan la tabla.
Durante esa espera el runtime conserva la lectura estricta de su perfil.
Si la publicación parcial dejó biblioteca y autoridad de perfiles distintos,
mantenimiento rechaza antes de detener servicios: completar primero la intención
con la guardia, login o reinstalación, y repetir mantenimiento.
El camino legacy `--cuotas` sigue rechazando
cualquier instalación que contenga `mini_handoff`.

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


### Identidad del daemon del desk (DESK-GEN)

El desk guarda `linux-proc-v1:` seguido de 64 hexadecimales SHA-256. Identifica
el `pueued` del `pid_path` configurado con `boot_id`, PID, campo 22 (`starttime`,
ticks desde el arranque) de `/proc/PID/stat` y la firma del pid file
`dev/ino/mtime_ns/ctime_ns/size`. No depende de `ps lstart`, `btime` ni del reloj
de pared: un ajuste de reloj en WSL no simula un reinicio. El lector valida
nombre, PID, estado vivo y formato, fija el archivo abierto y compara dos
lecturas coherentes. Cualquier lectura ilegible o incoherente detiene la admisión.
La Mini conserva su fórmula y validación anteriores, sin leer `/proc`.

La firma detecta cambios observables, incluida una reescritura con mtime
restaurado mediante ctime/inode. No demuestra un historial que termine con
exactamente las mismas observaciones. El boot_id es del kernel/VM de WSL;
un reinicio de distro con VM viva se distingue también por ticks y firma.

La conversión de un hash anterior (64 hex) ocurre sólo en `install-desk.py`,
incluso si la tabla ya está vigente, y exige **todos los grupos Running**,
incluidos `default`, `rapidas` y grupos ajenos al reparto. No prueba continuidad
histórica del daemon: es la política explícita para convertir cuando ninguna
pausa puede quedar oculta. Conserva razones, revisión, deseo, ajustes, actor,
fechas y recibos. Convierte `startup.previous_generation` sólo si coincide con
la generación sustituida; un enlace histórico distinto se conserva. No hay
testigo ni ventana temporal, ni conversión desde consultas, guardia o panel.

Antes de instalar, resolver deliberadamente cualquier pausa con el controlador
vigente y comprobar presión baja. Usar los comandos de instalación de abajo,
primero con `--seco`: anuncia `convertir legado con todos los grupos Running`,
`vigente` o `reinicio`, sin escribir. Un arranque durable distinto de ausente o
`ready` (por ejemplo `intent` o `prepared`) también impide instalar.

El instalador comprueba identidad completa y MainPID antes de efectos, bajo
`control.lock` después de detener timers y guardia, antes del commit de SQLite
y antes de retirar mantenimiento. Repite la condición Running durante una
conversión. Si aparece una pausa, indica `Conversión detenida; grupos Paused:`
con sus nombres. Antes de efectos no escribe nada; después conserva
`maintenance` y `slots.disabled`, no reactiva timers y requiere recuperación
deliberada. Si el fallo fue después de publicar código, conservarlo y la
autoridad, resolver la causa bajo mantenimiento y reinstalar; retirar sólo las
marcas del intento fallido después de verificar el deseo reconciliado. No
readoptar, borrar razones ni restaurar una SQLite antigua sobre otro daemon.

Una generación ya nueva igual a la actual no se convierte; una distinta sigue
la reconciliación de reinicios de U3.4, incluidas pausas `ambiguous`. El panel
transporta el token completo y compara exactamente: una petición anterior al
cambio se rechaza como `stale_state` y requiere refrescar.

La reversión al código anterior no se automatiza. Hacerla bajo mantenimiento
con un respaldo verificado de esta instalación (los `.pre-u34` pueden ser más
antiguos). Tras volver, los grupos `Paused` quedan `ambiguous` una vez y se
resuelven deliberadamente con `control.py reanudar <grupo> --host desk`, con
las comprobaciones habituales. Volver después al código nuevo requiere otra
conversión con todo Running; no es una reversión transparente de SQLite.

### Reinicios del desk (U3.4)

`pueued.service` ejecuta `control.py preparar --host desk` como `ExecStartPre`,
y después el **pueued estándar 4.0.4**. Usa `Restart=always`, `RestartSec=5`,
`StartLimitIntervalSec=0`, `KillMode=mixed` y `OOMPolicy=continue`. Guardia y modo se ordenan después
del daemon; la guardia existente es el único recuperador periódico. Un error
al reconciliar deja los grupos cerrados, sin reiniciar el daemon.

Bajo `control.lock`, el preparador valida configuración, binarios, cola y
SQLite sin escribir. Rechaza JSON duplicado, estado comprimido, Reset, enums o
tipos desconocidos, identidades inconsistentes y fechas RFC3339 inválidas,
incluidos offsets `+00:99`, `-00:99`, `+24:00` y fechas anidadas. Un `state.json`
ausente en un desk adoptado bloquea, igual que una autoridad ausente/corrupta.
No se sustituye por una cola vacía ni una tabla por defecto. Un PID anterior
vivo o no identificable también bloquea; no se mata por nombre. systemd espera
la terminación anterior antes del próximo `ExecStartPre`.

La transacción SQLite guarda la intención de arranque **antes** del rename de
`state.json`. Incluye hashes de origen/destino, cambios de estado y propiedad de
pausas. Luego escribe cola con flush/fsync/rename/fsync del directorio y confirma
la preparación en SQLite. Caer antes/después de cada frontera se recupera de
forma repetible; un hash ajeno bloquea sin sobrescribir. El evento UTC vigente
se proyecta de nuevo al recuperar y al terminar: ni adelantos ni ajustes vencidos
se reponen. No hay una transacción conjunta ficticia entre SQLite y Pueue.

Todos los grupos quedan Paused offline, incluidos vacíos, `default` y grupos
fuera del reparto. Los administrados ausentes se crean en esa imagen cerrada.
La razón `startup` es independiente de las cuotas y de `manual`, `pressure`,
`ambiguous`. Sólo una preparación confirmada permite reconocer la nueva
generación como propia. La guardia reconcilia reducciones, después aumentos,
verifica todo el vector/generación y exige presión fresca y `docker info`
correcto antes de retirar sólo sus razones liberadas. Los grupos ajenos al
reparto quedan retenidos hasta resolución explícita, sin apropiarse de sus cuotas.
`control.py reanudar default` o `reanudar suites` admite un grupo existente por
el mismo gate de Docker, presión, mantenimiento, cuotas y generación; no lo añade
al reparto. Un grupo inexistente se rechaza. Observar Running tras un start externo
libera la propiedad startup: una nueva pausa externa no se reabre automáticamente.

- `Queued` sin log conserva id, estado, prioridad, dependencias y lugar.
- `Running` y tareas `Paused` quedan `Done/Killed` y no se reejecutan.
- `Queued` con un log existente, incluso un enlace roto, falla de forma
  conservadora y retiene su grupo por lanzamiento ambiguo. No se usa mtime.
- Un `Locked` desconocido queda para revisión; stock lo restaura como Stashed.
  Nunca se convierte en Queued. Stashed y resultados terminales previos se conservan.
- `ExecStop` registra identidades activas antes de una parada administrada; si
  stock ya guardó su `Killed`, la próxima preparación conserva su motivo.
  Un Killed previo sin esa evidencia no se atribuye a un reinicio ni a cancelación.
  Por eso las paradas operativas pasan por systemd: un `pueue shutdown` externo
  puede terminar antes de que ExecStop observe las identidades y deja sólo Killed,
  sin motivo atribuible. Stock no distingue ese caso de un kill o cancelación previa.

El snapshot conserva para la tarea identificada por id + created_at el texto
**«Interrumpida al reiniciar Pueue; relanzar manualmente»**. No fabrica recibos
ni procedencia de cancelación. El panel conserva esas interrupciones mientras existan la tarea y su identidad
atribuida, aunque sean terminales o tengan más de 24 h; no aplica un tope de 64.
Las agrupa bajo cada carril en una sola fila plegable: **«N interrumpidas por
reinicio»** y **«No vuelven solas · relanzar es corrida nueva»**. Al abrirla
muestra la hora local del corte, ticket (o «Sin ticket»), asunto o rama y
alcance de cada corrida. Arranca plegada y recuerda el despliegue por host y
grupo durante los refrescos de la instantánea; no ofrece relanzar ni limpiar.
La espera automática de cuotas/Docker se muestra como espera con reanudación
automática, sin instrucciones manuales. Junto al grupo realmente retenido muestra
**«Retenido al reiniciar · <motivo> · resolver con control.py reanudar <grupo>»**
en la celda de lugares libres, sin repetir instrucciones en el host. La espera
conserva «reanudación automática»; una pausa ambigua de la Mini también se
explica allí. Un grupo lleno no muestra el aviso de pausa.
Los motivos `pausa manual` y `pausa de origen desconocido` se muestran como
**«Pausa manual»** y **«Pausa de origen desconocido»**, con la misma instrucción
`resolver con control.py reanudar <grupo>`, sin atribuir la pausa al reinicio.
El lanzamiento ambiguo con log previo conserva **«Retenido al reiniciar»** y
su motivo.
No incorpora botón ni repite la instrucción SSH en la cabecera. `default` vacío
sigue oculto; una terminal atribuida al reinicio lo hace visible.

Felix revisa la causa y resuelve por el SSH existente, por ejemplo:

```sh
/usr/bin/python3 -I ~/.local/lib/pao-pueue-desk/control.py reanudar jale --host desk
```

La reanudación solicita admisión por el mismo motor; si falla Docker, presión,
cuotas, generación o mantenimiento, conserva el cierre. Relanzar una tarea
interrumpida significa una **corrida nueva del cliente**, nunca `pueue restart`
del id anterior. Antes de relanzar hay que revisar procesos supervivientes;
`Killed` es un estado de cola y no demuestra por sí mismo el retiro de hijos.
`KillMode=mixed` cubre el cgroup de la unidad del desk, sin buscar descendientes
por un PID viejo ni prometer lo mismo en otros gestores.

`OOMPolicy=continue` (OOM-DESK): cuando el kernel mata un proceso por falta de
memoria, sólo muere ese proceso y su tarea falla; `pueued` y las demás corridas
siguen. Con el valor de systemd por omisión (`stop`), un solo `next-server` sin
memoria detenía toda la unidad con SIGKILL a todas las corridas, y el reinicio
las marcaba interrumpidas (2026-11-15, 16 corridas en dos OOM). Si el elegido es
el propio `pueued`, la unidad cae y `Restart=always` la levanta con el preparador.
La prueba privada lo comprueba con un tope de memoria sólo en su unidad transitoria
y un contraste con `OOMPolicy=stop`.

Residuo conocido: si el kernel elige al proceso líder de una tarea, Pueue la marca
terminada y libera su lugar, pero los hijos de ese líder siguen vivos dentro de
`pueued.service`, sin tarea, hasta que terminen solos o pare la unidad. Es el mismo
caso de una tarea que termina dejando hijos en segundo plano, que ya ocurría antes de
este cambio. Para detectarlos, comparar `cgroup.procs` de la unidad con los árboles
de procesos de las tareas `Running`. Retirarlos es la unidad ORFANOS-DESK.

#### Mantenimiento e instalación sobre U3.3 adoptada

Los siguientes pasos son para el operador; las pruebas no los ejecutan en el
servicio compartido. Conservar respaldo SQLite mediante `backup` bajo
`control.lock`, y de controlador/unidades. No copiar una SQLite abierta con cp.

1. Desde el checkout revisado en WSL, ejecutar el ensayo y después la instalación:

   ```sh
   /usr/bin/python3 -I fleet/pueue-desk/install-desk.py --pid-file /run/user/1000/pueue.pid --seco
   /usr/bin/python3 -I fleet/pueue-desk/install-desk.py --pid-file /run/user/1000/pueue.pid
   ```

   Usar el pid_path real. Se aceptan las rutas estándar XDG absolutas; las rutas
   propias, `daemon.env_vars` complejo o `shell_command` explícito se rechazan,
   también en seco, **antes** de copiar, detener, tocar unidades o linger.
   No se arregla la configuración borrando campos. El mismo validador se usa
   en el preparador. El instalador consulta sólo las claves de rutas XDG/PUEUE del
   entorno del gestor (`systemctl --user show-environment`) y usa HOME como cwd,
   igual que la unidad. No usa el cwd ni las rutas XDG de la consola instaladora.
   El preflight comprueba también la cola, perfil y PID vivo, ambos binarios y
   MainPID/ActiveState antes de cualquier efecto.
2. Bajo `control.lock`, migrar aditivamente `startup_version=1`. Conservar deseo,
   modo, adelanto, ajustes, revisión, vencimientos y todos los recibos. Recibos
   applying/partial/unknown rechazan sin efectos, incluso con esquema ya vigente.
   No se readopta ni se borra la autoridad. La migración de tabla de U3.2 sigue
   siendo explícita; esta entrega no cambia tabla ni horarios.
3. La instalación crea `maintenance` antes de detener **primero timers**, después
   guardia/modo. Publica controlador y las cinco unidades, guarda copias
   `.pre-u34`, hace `daemon-reload` y conserva el MainPID. No para ni reinicia
   `pueued`. Antes de retirar mantenimiento comprueba lo efectivamente cargado:
   mismo MainPID y ActiveState, NeedDaemonReload=no, Restart=always, KillMode=mixed,
   OOMPolicy=continue
   y la ruta/argumentos del ExecStartPre entregado. Un drop-in divergente hace
   fallar la instalación y conserva mantenimiento. El preparador se estrenará
   en el siguiente arranque natural.
   Sólo retira sus propias marcas tras comprobar el deseo y reactiva timers;
   un mantenimiento previo, incluido un enlace roto, se conserva. Ante fallo,
   deja mantenimiento y cuotas apagadas para completar o revertir explícitamente.
   Una reinstalación conserva esas marcas previas: retirarlas manualmente sólo
   después de verificar publicación, unidad cargada, autoridad y cuotas. Reactivar
   los timers después de quitar el mantenimiento propio; no borrar marcas ajenas.
4. Comprobar sin reiniciar:

   ```sh
   systemctl --user show pueued.service -p MainPID -p ExecStartPre -p ExecStart -p ExecStop -p Restart -p RestartUSec -p KillMode -p OOMPolicy -p StartLimitIntervalUSec -p NeedDaemonReload
   systemctl --user cat pueued.service
   systemctl --user is-active pueue-desk-mode.timer pueue-desk-guard.timer
   ```

   Comparar MainPID antes/después y exigir NeedDaemonReload=no, Restart=always,
   el ExecStartPre nuevo, KillMode=mixed, OOMPolicy=continue y StartLimitIntervalUSec=0.
   `OOMPolicy` rige desde el `daemon-reload`, sin reiniciar `pueued`. No usar
   `status --json` como evidencia pública: contiene entornos con secretos.
5. Actualizar snapshot/despachador y panel con los instaladores del rack ya
   existentes. Conservar el canal forzado y las llaves actuales; verificar
   capacidades y ensayo. `reorder` sigue no disponible; `restart_queue` conserva
   su valor histórico false como capacidad de acción del panel, no como permiso
   para ejecutar reintentos.

Para una parada de mantenimiento explícita, `withdraw-desk.sh` crea la marca
bajo el candado, detiene los timers, detiene guardia/modo, deshabilita disparadores
y para el daemon al final. Conserva cola, autoridad y unidades. La marca impide
que guardia/timer reactiven el daemon. Requisite (además de la marca y After)
impide que un timer arranque el daemon ausente. No usarlo con trabajo ajeno
como ensayo de esta entrega.

Para revertir **antes del próximo arranque**, crear/conservar maintenance,
parar timers y luego guardia/modo, tomar control.lock, restaurar
`control.py.pre-u34` y cada unidad `.pre-u34`, y hacer daemon-reload. No reiniciar
pueued ni restaurar una base SQLite vieja: U3.3 tolera el campo adicional y los
recibos siguen vigentes. Retirar sólo las marcas propias tras verificar el
controlador anterior, su deseo y las unidades cargadas; reactivar timers.
Después de estrenar un arranque U3.4, conservar este controlador mientras existan
razones startup o interrupciones pendientes: volver sólo al panel anterior es
seguro. Para quitar también el controlador, resolver primero esas razones con
Felix y retirar los campos/razones de arranque en una migración bajo candado;
U3.3 rechaza una razón startup desconocida. No borrar evidencia ni abrir grupos
directamente para forzar la reversión.

#### Límites aceptados de stock y verificación

Se mantienen los dos riesgos raros elegidos por Felix: stock puede lanzar una
tarea antes de guardar Running y volverla a ejecutar tras una caída; y un add
confirmado puede perderse ante un apagón porque State::save no hace fsync.
La comprobación de logs reduce la primera ventana, sin eliminarla. Nuestro
fsync no cambia el save de Pueue. SIGKILL no acredita durabilidad ante apagón.

Pruebas dirigidas: `tests/test_startup.py`, `tests/mutate_startup.py`,
`fleet/rack/tests/test_startup.py` y `fleet/rack/tests/check_mutations.py --startup-only`.
`tests/test_private.py --run-private` se ejecuta sólo dentro de remote-run en el
desk: daemon/config/data/PID privados y unidades transitorias `pao-u34-injerto-*`,
con limpieza y comprobación al terminar. Acredita restauración stock y orden
ExecStartPre → daemon → guardia, Restart=always tras una salida correcta,
propiedades leídas de la unidad entregada con RestartSec=5, SIGKILL del MainPID
verificado, retiro de hijos de su cgroup y cola corrupta que reintenta. La vaciedad
del cgroup tras detener se exige por aserción; se limpian sólo nombres exactos
de esta corrida, nunca por prefijo.
No reinicia WSL, Windows ni el servicio productivo. El arranque de Windows/WSL
queda como verificación separada del orquestador.

### Reinicios de la Mini (U3.5)

La Mini conserva `MINI_GROUPS=(suites, default, rapidas)`, perfil 3/1/3 y tope 7.
No se readopta su autoridad ni se alteran los ajustes de Felix. `mini.py arrancar`
es el arranque fijo del LaunchAgent `local.pao.pueue-mini.daemon`: toma el candado,
llama al mismo `prepare_start` de `control.py preparar --host mini` y hace `exec`
del **pueued estándar 4.0.4 de Homebrew**. Tiene RunAtLoad, KeepAlive incondicional
y ThrottleInterval=10; una salida correcta también vuelve a pasar por preparar.
Las rutas de entorno XDG/PUEUE de la consola no se propagan al cliente/daemon:
ambos usan HOME, cwd HOME y configuración estándar previamente validada.

El preparador comprueba socket y PID anterior muerto, configuración, cola y
SQLite antes de escribir. Conserva la fórmula Mini de identidad (pid_path, su
mtime y `ps lstart/comm`); no lee `/proc`. Sólo el MainPID único de su LaunchAgent
puede preparar y hacer exec. Además exige que Homebrew esté descargado **y deshabilitado**.
No arranca un segundo daemon si el socket falla pero el PID o su dueño son dudosos.
Sus archivos propios usan fsync, F_FULLFSYNC en macOS, rename y fsync de directorio;
SQLite sigue usando synchronous=FULL/fullfsync. Esto no modifica State::save stock.

Todos los grupos, incluidos vacíos, default y ajenos, se publican Paused offline
con las cuotas deseadas vigentes. Queued sin log conserva id/lugar/prioridad y
dependencias; Running/Paused fallan como Done/Killed. Queued con log (también
enlace roto) falla y retiene su grupo por lanzamiento ambiguo. Locked desconocido
queda retenido, nunca se reencola; stock lo convierte en Stashed. Stashed,
Success/Failed y cancelaciones terminales previas se conservan. Un state.json
ausente/corrupto o SQLite ilegible bloquea; no hay tabla por defecto de rescate.
La intención de preparación precede al rename de cola y se recupera repetiblemente.

Si `arrancar` bloquea, mirar `~/.local/state/pao/pueue-mini/arranque.log`
(y `arranque.log.1`): fecha, fase y motivo seguro del rechazo. Cada archivo está
acotado a 64 KiB, con permisos 0600; KeepAlive sigue reintentando cada 10 s.
No contiene comandos, entornos ni el texto arbitrario de excepciones/subprocesos.
La salida cruda de `pueued` sigue en `/dev/null`: Pueue 4.0.4 imprime el comando
de una tarea en su log `info` (`spawn.rs:245`); redirigirlo al registro propio
filtraría comandos y podría filtrar secretos. El registro propio diagnostica el
preparador y la autorización, no los fallos internos posteriores al exec stock.

La **misma guardia de 60 s** repara el relevo y los recibos aceptados del rack;
conserva sensores, umbrales y escalera de rapidas. No se instala otro recuperador.
Orden: control.lock → validar autoridad → deseo vigente → intenciones sin abrir →
reducciones/aumentos → verificar vector/generación → sensores frescos → retirar
sólo razones propias. La Mini no recibe la admisión de una tarea de SEN-6417.
`slots.disabled`, mantenimiento y las pausas manuales siguen siendo independientes.

El snapshot conserva «Interrumpida al reiniciar Pueue; relanzar manualmente» para
la identidad atribuida. El panel agrupa las interrumpidas en un plegable por grupo,
con conteo, consecuencia y horas/detalles; abrirlo persiste tras cada snapshot y
queda aislado por host/grupo. Un grupo retenido muestra «Retenido al reiniciar · motivo ·
resolver con control.py reanudar grupo», sin botón. Default vacío sigue oculto.
Para resolver por el SSH existente, después de revisar el motivo y posibles
procesos supervivientes:

```sh
/usr/bin/python3 -I ~/.local/lib/pao-pueue-mini/control.py reanudar suites --host mini
```

Esto verifica cuotas/generación/presión; no hace un start directo ni reinicia ids.
Relanzar una interrumpida significa una corrida nueva del cliente. Una parada
administrada registra identidades antes del bootout. Un shutdown/bootout externo
que stock alcance a guardar como Killed antes de que nadie registre la identidad
es indistinguible de una cancelación previa: se conserva Killed sin inventar motivo
ni recibo. **Límite aceptado (C-W1, decisión del dueño del 2026-11-15):** este
caso externo, que incluye el reinicio de macOS, el cierre de sesión, `kill` al PID
de `pueued` y `pueue shutdown`, deja la tarea `Killed` sin el motivo de reinicio. La
tarea sigue terminal y no se reejecuta. Un envoltorio que intercepte la parada se
retomará sólo si la medición del orden real de señales en un reinicio lo justifica.
El negativo SIGTERM sin record_stop acredita terminalidad y ausencia de reejecución;
no resuelve esa atribución. SIGKILL no prueba el retiro de descendientes en macOS; revisarlos antes
de relanzar. No se busca ni se mata por nombre.

#### Instalar el relevo (operador; reinicia el daemon, no ejecutado por las pruebas)

1. Partir de U3.1 adoptada y sana. Guardar respaldos del controlador/plists y de
   SQLite mediante `backup` bajo `control.lock`; no copiar una SQLite abierta.
   La primera adopción sigue siendo explícita por `--solo-guardia --cuotas`.
2. Desde este checkout **en la Mini**, ejecutar primero el ensayo:

   ```sh
   /usr/bin/python3 -I fleet/pueue-desk/install-mini.py instalar --relevo --seco
   ```

   El ensayo sí llama al validador y a la proyección pura comunes con preparar,
   valida la imagen exacta y rechaza preparación pendiente, lee cola/autoridad/perfil/PID y dueño
   launchd, comprueba ambos binarios 4.0.4 y rechaza recibos applying/partial/unknown
   antes de todo efecto. Usa el lock existente abierto en lectura y no crea
   directorios ni migra, recupera, copia, pausa o descarga servicios. Una
   configuración compleja no se arregla borrando campos.
   Admite el plist de Homebrew 4.0.4 con el binario resuelto de pueued,
   `--verbose`, `WorkingDirectory=/opt/homebrew/var` y sus rutas de log stock.
   Rechaza otros argumentos, binarios y cualquier `EnvironmentVariables`.
   `--verbose` sólo ajusta tracing; no selecciona configuración, socket ni PID.
   El directorio de trabajo sí puede cambiar el fallback `pueue.yml`: el ensayo
   exige que origen y destino elijan el mismo archivo (primero el estándar
   `~/Library/Application Support/pueue/pueue.yml`, después el cwd), o que
   ninguno tenga archivo. Si difieren, conserva el origen sin efectos.
3. Sólo en la ventana aprobada, ejecutar el mismo comando sin `--seco`.
   Publica primero las dos plantillas launchagents en la biblioteca instalada,
   después mini.py/control.py y el plist de la guardia existente, con reemplazos
   atómicos, sin descargar la guardia, y comprueba que siga cargada. El paquete
   instalado es autosuficiente: el próximo job no requiere este checkout. Si se interrumpe esa
   publicación inicial, el daemon original sigue vivo; repetir el instalador.
   Después migra aditivamente `startup_version` y `mini_handoff` en policy bajo
   control.lock: conserva deseos, revisión, ajustes, pausas y todos los recibos.
   No borra ni readopta SQLite. Una reinstalación terminada no reinicia otra vez.
4. `mini_handoff` conserva destino, fase, enable/disable originales y el efecto
   pendiente y una deadline absoluta durable (nula cuando no se pidió espera).
   Cada disable/bootout/enable/bootstrap del relevo tiene intención
   FULL previa. Publica el plist nuevo **deshabilitado**, reclama sólo grupos
   Running con razón handoff antes de pause --wait y deja drenar las activas.
   Mientras drena, no acepta ajustes nuevos del rack. No mata trabajo para acelerar
   el relevo. Sin `--esperar` rechaza tareas Running o Paused antes de escribir.
   Para esperar explícitamente hasta 10 minutos: añadir `--esperar 10` al ensayo
   y al comando real (admite 0..120 minutos). Al vencer, marca aborted, conserva
   el daemon origen y registra `retained_owner` (`homebrew` o `prepared`). La
   intención versión 2 conserva `source`: gestor verificado, PID y generación
   Mini (fecha del pid file e identidad de ps). El aborto retiene ese origen,
   sin deducirlo del destino. Si se pide el gestor que ya sigue vivo, converge
   a done/withdrawn sin publicar, pausar, drenar ni tocar launchd, incluso con
   tareas Running; repetirlo no escribe. Un
   retiro abortado conserva el wrapper con preparación offline para KeepAlive
   y login, sin reactivar Homebrew; la guardia repara un dueño ausente desde
   `source`. Una intención anterior sin origen sólo se migra si verifica un
   único gestor vivo con su PID; si no puede acreditarlo, conserva el cierre.
   Transfiere las razones handoff a pressure
   en el perfil y a ambiguous en grupos ajenos. Un grupo ajeno permanece cerrado
   con motivo y se resuelve con `control.py reanudar <grupo> --host mini`, pasando
   las mismas comprobaciones de cuotas y sensores. Nunca abre
   directamente: la guardia reconcilia y toma sensores frescos; las pausas
   manuales y de terceros permanecen. Una Paused no bloquea indefinidamente.
5. Deshabilita Homebrew antes de bootout, verifica que terminó el PID anterior,
   habilita y carga el nuevo. Si el instalador o el propio job muere entre bootout
   y bootstrap, la siguiente pasada del job continúa desde SQLite; también tras
   login. Verifica dueño/cuotas y entrega sólo su pausa a la guardia. Una pausa
   manual agregada durante el drenado no se pierde.
6. Actualizar snapshot/validador/panel con el despliegue del rack existente.
   Conservar llaves/canal y acceso sólo tailnet del dueño. No añade listener.

**Tiempo sin daemon:** desde que termina el origen hasta que prepara y hace exec
el destino; normalmente no incluye un tick entero de guardia porque el solicitante
ejecuta bootstrap. Tras morir justo en ese hueco, la siguiente pasada nominal de
60 s lo repara, más preparación/arranque. La apertura de grupos puede esperar otro
tick/sensores. No hay cota finita si launchd, disco o sensores fallan. La medición
privada acelera sólo el job a 2 s y registra tiempos; no convertirlos en un SLA.
La instalación normal drena activas; un reinicio/crash con Running persistido las
deja fallidas, sin reejecución. Las pendientes conservan su id y orden.

#### Mantenimiento y vuelta a Homebrew

```sh
/usr/bin/python3 -I ~/.local/lib/pao-pueue-mini/mini.py mantenimiento
/usr/bin/python3 -I ~/.local/lib/pao-pueue-mini/mini.py continuar
```

Mantenimiento registra primero su intención y marcador durable, detiene/deshabilita
la guardia y después los daemons. Registra identidades activas antes de pararlos.
Ni wrapper ni recuperador reabren aunque una sesión relance el job. Continuar
publica la intención de arranque cerrado, carga la guardia y retira sólo su propio
marcador reconocido antes de completar el relevo. Un marcador ajeno, incluido un
enlace roto, se conserva y exige resolución del operador. Tras una interrupción
se puede repetir mantenimiento; continuar repara por el mismo diario.
Tras un relevo abortado, mantenimiento/continuar conserva el gestor que retuvo
el aborto; no completa el destino rechazado. Cambiar de gestor requiere de nuevo
la petición explícita `instalar/desinstalar --relevo`.

Para revertir el **gestor** desde la instalación sana:

```sh
/usr/bin/python3 -I fleet/pueue-desk/install-mini.py desinstalar --relevo --seco
/usr/bin/python3 -I fleet/pueue-desk/install-mini.py desinstalar --relevo
```

Repite validación/drenado, deshabilita y descarga el daemon nuevo, prepara offline
esa vuelta y habilita/carga Homebrew. Restaura su disable original, deja el plist
nuevo deshabilitado y conserva la guardia, código, autoridad y evidencia: siguen
siendo dueños de pausas/recibos. No elimina grupos, historia ni SQLite. Después
de retirar U3.5, los arranques futuros de Homebrew vuelven a carecer de preparador;
las pausas ambiguas se resuelven por control.py. No retirar también el controlador
mientras existan razones startup/interrupciones. `--solo-guardia` rechaza una
autoridad que ya tiene mini_handoff para impedir ese downgrade accidental.

Los LaunchAgents de gui/$UID existen después del login. RunAtLoad no crea esa
sesión tras reboot; no se promete servicio antes del login ni se cambia autologin.
Se conservan los dos riesgos raros aceptados: spawn antes de guardar Running puede
repetir una tarea y un add confirmado puede perderse ante apagón porque stock no
hace fsync. Nuestros fsync/SIGKILL no eliminan ni certifican esas ventanas.

Aceptación dirigida: `tests/test_handoff.py`, `tests/mutate_handoff.py`,
`fleet/rack/tests/test_mini_startup.py`, `check_mutations.py --startup-only` y
`mini_startup_browser_fixture.py`. `tests/private_handoff.py --run-private` requiere
remote-run autorizado en macOS: daemon/config/cola/PID propios y etiquetas
`pao.u35.injerto.*` dentro de la corrida; limpia etiquetas exactas y verifica PID,
comm y --config antes de terminar el daemon. No toca los LaunchAgents reales.

### SEN-6417: una tarea en la banda media (sólo desk adoptado)

Los umbrales anteriores no cambian. Cuando `decide` devuelve `mantener`, la
Guardia intenta **una candidatura global**, sin abrir el grupo: `pueue start
<id>`. Sólo participan grupos administrados excepto `rapidas`, Paused y con
razones efectivas exactamente `pressure` (un tombstone manual también bloquea).
Una marca con bytes exactos `pressure\n` aporta la misma razón que SQLite;
conserva la selección FIFO y los gates de la candidatura.
La Mini y la vía legacy no obtienen admisión incremental. Por debajo de los
umbrales de reanudar se abren grupos como antes, sin consultar este permiso.

Se elige FIFO global por fecha `enqueued_at` UTC, id numérico y grupo; esto no
reproduce las prioridades de Pueue. Sólo Queued, con identidad y dependencias
presentes Done/Success. Running, Paused y Locked cuentan contra el
`parallel_tasks` vigente. No se alteran cuotas ni se mata trabajo. Pueue 4.0.4
arranca por id aun en Paused y omite su cuota; el candado PAO sólo serializa
escritores cooperantes. Un cliente directo entre la última lectura y start
puede superar cuota: stock Pueue no ofrece compare-and-start atómico.

`control.sqlite3.guard_admission`, fila 1, JSON versión 1, conserva el último
intento y la última confirmación independientemente de policy/revisión. El
intento incluye boot id, CLOCK_BOOTTIME en ns de reserva/finalización, UTC,
generación, grupo/id, created_at/enqueued_at y resultado. No se exportan los
metadatos de identidad al registro. La reserva FULL se confirma antes del RPC,
sin mantener transacción durante Docker/Pueue. Tras ella se repite una sola vez
el gate final: mantenimiento/arranque/recibos, Docker cuando aplica, vector,
generación, muestra y evento. Toda la secuencia tiene presupuesto de 10 s;
la escritura previa al efecto también debe caber. No hay bucle de revalidación.
Permanece la ventana no atómica con otros clientes del daemon.

Entre intentos terminados se exigen 60 segundos completos desde la finalización
observada, también si no se envió el RPC (`not_sent`). Un RPC lento no adelanta
el permiso siguiente. Otro boot exige 60 s de uptime; retroceso/reloj ilegible
retiene. Cambiar reloj civil, generación, horario o reinstalar no borra el
permiso. Un fallo/timeout después de invocar el cliente puede haber tenido
efecto: sólo la misma identidad Running/Done, inicio compatible y grupo aún
Paused permiten `confirmed`; lo demás queda `unknown`. Al recuperar,
`reserved` se trata como `unknown`. Confirmar una ejecución recuperada consume
el permiso desde el reloj actual, no desde el intento perdido.

El JSONL conserva `lectura`/`decision` y añade `tick_id`. Siempre termina con un
resumen `admision_una`, `tope_alcanzado`, `retenido` o `guard_fin`;
`bloqueos` contiene `{grupo|null, operacion:one|resume|unknown, causa}`. Las causas son
`fresh_sample`, `docker`, `generation`, `desired`, `event`, `admission`,
`startup`, `authority`, `maintenance`, `reason`, `pressure_high`, `dependency`,
`quota`, `task_changed`, `cooldown`, `admission_unknown`, `clock`, `log`, `rpc`.
Antes de una decisión, una muestra ilegible usa `fresh_sample` y operación
`unknown`, sin atribuir un fallo de autoridad. Las causas globales no se repiten por grupo. Cada resumen tiene como máximo 16
bloqueos y menos de 4 KiB. Se mantiene la rotación a 1 MiB y `.1`.
`admision_intento` y `apertura_intento` se escriben antes del efecto; si falla el
registro se retienen aperturas/admisiones y aún se intentan cierres protectores.
Sólo se registran campos operativos permitidos: nunca tareas crudas, envs,
comandos, rutas ni mensajes arbitrarios de excepciones.

**Recuperación operativa de unknown.** Primero mantener `maintenance`, detener
los dos timers/oneshots y tomar el mismo `control.lock`. Inspeccionar una
proyección privada de la fila y de la tarea: generación, id, grupo,
created_at/enqueued_at e inicio; nunca volcar `status --json` ni envs a logs.
Si la ejecución de esa identidad queda probada, conservar `last_confirmed` y
finalizar `confirmed` con boot/CLOCK_BOOTTIME actuales. Si se prueba que no se
envió, o que el cliente ya terminó y no existe posibilidad de efecto tardío,
finalizar `not_sent` con esos relojes. Usar `control.py resolver-admision` bajo mantenimiento: el comando toma
`control.lock`, valida la fila y la evidencia de A (identidad creada/encolada,
generación, grupo Paused, Running/Done e inicio entre reserva y ahora) y finaliza
con CLOCK_BOOTTIME fiable. Si no hay evidencia, sólo
`control.py resolver-admision --sin-efecto` permite cerrar `not_sent`: el operador
certifica que el cliente terminó y que no puede llegar ningún efecto tardío;
ver Queued o estar en otro boot no lo demuestra. El cambio de boot sólo rebasa
reserva/finalización tras esa certificación. Conserva fila, UTC original e
historial; una ejecución confirmada actualiza `last_confirmed`. Exige 60 s
completos desde resolución y rechaza reloj ilegible o retroceso al finalizar.
Registra `admision_resolucion_intento` y `admision_resuelta` sin metadatos de tarea.
Sin evidencia suficiente sigue retenida: no borrarla, restaurar una DB antigua,
vencerla por 60 s ni cambiar la generación para liberarla. La reapertura normal
con presión baja permanece disponible con sus gates.

**Instalación y reversión (operador; no son comandos automáticos del panel).**
En el checkout del desk ejecutar primero
`/usr/bin/python3 -I fleet/pueue-desk/install-desk.py --pid-file <pid_path configurado> --seco`,
y después el mismo comando sin `--seco`. El plan valida la tabla antes de
`engine.view`, sin crear nada. La migración aditiva ocurre bajo mantenimiento,
con timers/oneshots detenidos y `control.lock`; crea sólo si falta y rechaza
corrupción. Conserva policy, recibos y permiso existente, incluso unknown.
Antes de publicar respalda el controlador inmediatamente anterior en
`control.py.pre-sen6417-<UTC con microsegundos>`, además de los respaldos viejos.
Se mantienen los fences DESK-GEN, `--pid-file` configurado, daemon-reload y
MainPID/generación constantes: no se reinicia ni configura pueued. Fallos
conservan mantenimiento. Actualizar después snapshot/validador/panel del rack.

Para revertir, detener timers/oneshots bajo mantenimiento, tomar control.lock y
restaurar ese respaldo fechado y los archivos del rack anteriores. Conservar
SQLite completa, tabla guard_admission y recibos; nunca restaurar una DB
histórica. Recargar unidades si cambiaron y retirar sólo las marcas propias
tras comprobar deseo/generación/cola. El código anterior ignora la tabla extra;
reinstalar esta versión la valida sin reiniciar el permiso. Observar una
admisión real en mantener, su cooldown, y una reapertura con presión baja
natural; no provocar carga ajena. Medir espera por causa y recaída de presión:
los escenarios del sondeo son condicionados, no ahorro medido ni pronóstico.

Ensayo SEN-6417 (sólo remote-run autorizado): `tests/private_trickle.py
--run-private` crea configuración, socket y daemon propios. Usa `shlex.join`
para las tareas sintéticas, comprueba Pueue 4.0.4, `start <id>` con Paused y
exceso de parallel=1, luego admisión en mantener, cooldown y reapertura baja con
la guardia de esta versión. Sólo emite grupos, ids y estados; verifica el PID
de su Popen y su config antes de terminarlo. El ensayo no instala ni demuestra
reducción de espera real. `tests/test_private_trickle.py` valida la integración
del arnés con el contrato A mediante dobles, sin daemon.

Regresión SEN-6487: `tests/test_pause_fence.py` reproduce el cierre protector
con fallo de escritura y reinicio administrado, con y sin presión previa en
SQLite, y comprueba las retenciones manuales y el orden de recuperación.
Mutaciones dirigidas: `/usr/bin/python3 -I fleet/pueue-desk/tests/mutate_pause_fence.py`,
siempre mediante `remote-run` en el host de pruebas.
