# Rack de pruebas

Panel de Mac Mini y Felix DESKTOP (WSL), con lectura del rack y cancelación
por lanzador en ambos hosts, con la variante
«C · Lugares». Python 3.9 y biblioteca estándar; sin dependencias de frontend.
Cada host entrega `rack-host@1`; el servidor sirve `rack@1` cada 15 segundos.

## Preparación e instalación

Estos pasos son manuales, después de revisar y fusionar el cambio. No publicar
el puerto por Serve, Funnel, un proxy ni un túnel de red local.

1. En la MacBook, `bash fleet/rack/instalar-desk.sh` copia únicamente la
   instantánea y el despachador autónomo `desk_action.py` a WSL, como
   `~/.local/bin/pao-rack-snapshot` y `~/.local/bin/pao-rack-action`, a través
   del alias SSH `desk`, que entra directo al sshd de Ubuntu. Reemplaza cada
   archivo de forma atómica. No instala servicios, controladores ni llaves.
2. En la Mini, la llave exclusiva del rack es `~/.ssh/id_ed25519_pao_rack`
   (comentario `pao-rack@mini`). Su pública vive en el archivo Windows
   `C:\ProgramData\ssh\administrators_authorized_keys`, que conserva los
   permisos de SYSTEM y Administrators. La línea completa es:

   ```text
   restrict,command="MSYS_NO_PATHCONV=1 wsl -d Ubuntu -u felix -- /usr/bin/python3 -I /home/felix/.local/bin/pao-rack-snapshot" ssh-ed25519 <PUBLICA> pao-rack@mini
   ```

   El shell de OpenSSH en Felix DESKTOP es Git bash. Sin `MSYS_NO_PATHCONV=1`
   convierte `/usr/bin/python3` en una ruta de Windows y el comando falla.
   `restrict` quita PTY y reenvíos; el comando forzado ignora lo que pida el
   cliente y sólo devuelve JSON.
3. Revisar `hosts.json`. La entrada `ssh` ejecuta `/usr/bin/ssh -T` con
   `BatchMode=yes`, `ConnectTimeout=10`, `IdentitiesOnly=yes` y
   `-i ~/.ssh/id_ed25519_pao_rack` contra `Felix@felix-desktop`, el nombre
   del desk en el tailnet. ssh expande `~` por sí mismo. La host key de
   `felix-desktop` debe estar ya en `~/.ssh/known_hosts` de la Mini.
4. Para títulos y proyectos de Linear, poner una API key de sólo lectura en
   `~/.config/pao-rack/linear-key`, con `chmod 600`. Sin ella el panel funciona
   sin títulos y el servidor registra la razón una vez al arrancar.
5. Copiar `fleet/rack/` a la Mini y, allí, ejecutar `bash instalar.sh`.
   Obtiene la IP con `/Applications/Tailscale.app/Contents/MacOS/Tailscale ip -4`,
   verifica los JSON de `serve status` y `funnel status` y aborta si mencionan
   el puerto 8788. También aborta si no puede consultar su configuración.
   Resuelve el dueño mediante `status --json`: `Self.UserID` →
   `User[<id>].LoginName`. Copia `snapshot.py`, `serve.py`, `accion.py`, `desk_action.py`, `index.html` y `hosts.json` a
   `~/.local/lib/pao-rack/` y carga `com.cosmonaut.pao-rack` con KeepAlive y
   `ProcessType` `Interactive`. Sin él, launchd limita al agente: con la Mini
   cargada, `whois` pasa de 3 s y el dueño recibe 403.
   El puerto es **8788**, fijado en `puerto=` de `instalar.sh`; en la Mini
   el 8787 ya lo usa otro servicio. El Funnel existente en 10000 no se modifica.

El LaunchAgent ejecuta `/usr/bin/python3 -I serve.py --bind <IP tailnet>
--port 8788 --owner <LoginName> --name <nombre MagicDNS> --name <nombre corto>`. Logs en `~/Library/Logs/pao-rack.log`.
`bash instalar.sh --desinstalar` descarga el agente y quita sus archivos;
conserva historial, caché, diario de acciones, interruptor, key de Linear, llave SSH y logs.

## Sólo dispositivos del dueño dentro del tailnet

El servidor sólo acepta bind en `100.64.0.0/10` (o `127.0.0.1` para pruebas).
Rechaza con 403 sin datos clientes fuera de `100.64.0.0/10` y
`fd7a:115c:a1e0::/48`. Rechaza cualquier cabecera `Tailscale-Funnel-Request`.
No confía en `X-Forwarded-For`. Resuelve cada IP permitida con
`Tailscale whois --json <ip>`, timeout de 3 segundos y caché de 5 minutos por
IP, comparando `UserProfile.LoginName` con `--owner`. Si no coincide o falla
la consulta o su formato, responde 403. `--owner` es obligatorio fuera del
modo loopback; `--tailscale` permite sustituir el binario en las pruebas.
La caché de 5 minutos sólo se usa para lectura. Cada petición a `/api/accion`
y `/api/accion/<id>` exige whois fresco, incluso en loopback: no hereda la
excepción de lectura local. El servidor de pruebas inyecta un whois falso.

Antes de todo eso exige que la cabecera `Host` nombre a este servidor con su
puerto: la IP de `--bind` o un `--name` (sin distinguir mayúsculas), en una
sola cabecera. Si no,
responde 403. Así una página de otro sitio cuyo nombre resuelva a la IP de la
Mini (DNS rebinding) no lee el panel desde el navegador del dueño.
`instalar.sh` pasa el nombre MagicDNS completo y el corto.

Después de instalar, desde otro dispositivo del dueño conectado al tailnet:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' http://<IP-TAILNET-MINI>:8788/
# 200
curl -sS -o /dev/null -w '%{http_code}\n' -H 'Tailscale-Funnel-Request: ?1' http://<IP-TAILNET-MINI>:8788/
# 403
curl -sS -o /dev/null -w '%{http_code}\n' -H 'Host: otro.example:8788' http://<IP-TAILNET-MINI>:8788/
# 403
curl --connect-timeout 3 http://<IP-LAN-MINI>:8788/
# Conexión rechazada: no debe responder HTTP por la IP de red local.
```

Comprobar también desde un dispositivo de otro dueño: debe dar 403. No usar
Funnel para verificarlo. Revisar de nuevo `Tailscale serve status --json` y
`Tailscale funnel status --json`: no deben exponer 8788.

## Datos y operación

`hosts.json` es una lista de `{id,name,transport,argv,actions}`. `local` ejecuta la
instantánea como subproceso y admite `{rack_dir}` en argv; `ssh` ejecuta el
argv sin shell. Un host futuro será otra entrada (no se implementan VMs).
Los hosts se consultan en paralelo con timeout de 25 segundos. Si falla un
host se conserva su último dato bueno, marcado como antiguo. Las lecturas
parciales se indican en `snapshot.errors`. No se exportan comandos, rutas ni
entornos de Pueue. `body.sh` se recorre por líneas, extrayendo sólo MODE,
BASE_REPO, REQUESTED_SHA, BRANCH y SLOT_KIND; nunca se ejecuta ni registra su contenido.

El clon del desk no conserva ramas remotas al día, así que la instantánea
entrega la rama solicitada cuando no es un SHA, además del SHA y el
repositorio (`owner/nombre`, sacado de
`remote.origin.url`). El servidor resuelve el ticket con el `gh` de la Mini,
que ya tiene sesión: primero `commits/<sha>/branches-where-head` y luego
`commits/<sha>/pulls`, y toma el primer `sen-<n>` del nombre de rama. Guarda
el ticket, la primera rama y la primera línea del mensaje del commit en
`commits.json`; las entradas antiguas se completan en la próxima consulta.
Una entrada completa no se vuelve a consultar; los datos ausentes se
reintentan cada 30 minutos, con máximo diez commits por ciclo.

Las corridas `remote-run` de Mini y del grupo `hasito` del desk leen sólo
`src` y `mirror` de `meta`, sin exportar `cmd`. Del espejo se consultan rama
y último asunto con Git; si fallan quedan en null. Las tareas incluyen su
prioridad de Pueue. Un ticket ausente se deduce también de `sen-<n>` en la
rama, sin distinguir mayúsculas.

El historial `~/.local/state/pao/rack/history.jsonl` conserva 30 días, sin
duplicar `(host,id,start)`. El estimado usa la mediana de las últimas 20
corridas exitosas con igual host, grupo y nombre sin sufijos `-sol`, `-opus`
o `-rN`; necesita cinco muestras. Devuelve mediana (típico), p10 y p90, con
interpolación lineal entre las duraciones ordenadas. La barra sólo se pone
amarilla al superar p90. No se muestran tareas Done en la API.
`tickets.json` cachea Linear por seis horas, con máximo diez consultas por
ciclo. Ambos archivos se reemplazan atómicamente. `--state-dir` y `--hosts`
permiten usar ubicaciones independientes.

Los umbrales CPU de la guardia desk se leen del `control.py` instalado en
`~/.local/lib/pao-pueue-desk/`. Si falta, se usan 30/15, los valores
vigentes al implementar. La guardia Mini tiene un solo
punto de extensión, `mini_guard()`, y por ahora devuelve null.

La página no agrega controles al prototipo. Sólo añade los estados mínimos
pedidos: host antiguo, servidor sin respuesta, progreso sin estimado o
excedido y tarea pausada; una lectura de capacidad ausente se muestra como
«—» o «Sin lectura de CPU». Los lugares se apilan en móvil.

Cada host con tareas `Running` y una lectura de consumo muestra debajo una
tabla ordenada por CPU y después por RAM, de mayor a menor, con una fila final
«Fuera de las corridas». Los porcentajes usan los núcleos y la memoria total
del host. En macOS, una sola llamada a `ps` obtiene CPU reciente y RSS de los
árboles de procesos hijos de `pueued`; en Linux, dos lecturas de `/proc` en una
muestra de aproximadamente un segundo calculan CPU por delta de contadores
y RSS, y la memoria usada del host es total menos disponible. Se incluyen
contenedores Docker atribuibles por sufijo de PID del árbol o, en Linux,
por `sen-test-db-N` y el cwd `worktrees/sen-test-N`. Una sola llamada a
`docker stats` lee todos los contenedores mientras se muestrean los procesos,
porque tarda unos 2 segundos. Los contenedores sin dueño único quedan en el
resto del host, sin sumar otra vez su consumo al total. Si Docker falta o
falla, se conserva el consumo de procesos y la nota indica que no incluye
contenedores. Una tarea cuyo árbol no se encontró muestra «—». `usage` es
opcional en `rack-host@1`: una instantánea antigua conserva su presentación.
La lectura adicional comparte un presupuesto de 4 segundos; argv, cwd y
nombres de contenedores se usan localmente y no se exportan.

La fila muestra proyecto, objetivo, rama y tiempo en espera, con descripción
del ticket o, en su ausencia, asunto, rama u objetivo. Se ordena por prioridad
descendente e id ascendente; muestra ocho tareas por grupo y permite ver el
resto. No permite reordenar ni cambiar cupos u horarios.

## Actividad y referencias

El aviso aparece tras más de 15 minutos sin salida; los arranques Jale con
`MODE=launch` y `SLOT_KIND=verify` indican que esperan liberar sus servidores.
La actividad usa sólo tamaño y `mtime`: el más reciente entre el log de Pueue
(`~/Library/Application Support/pueue/task_logs/<id>.log` en Mini,
`~/.local/share/pueue/task_logs/<id>.log` en desk) y el `log` de
`~/.local/state/jale/pueue/job-…/` o `~/remote-run/runs/<id>/`.
Sin archivos, `last` es null; no se calcula un tiempo de silencio.
La fila conserva tipo y referencia, sin consultar salida hasta correr.

El menú ⋯ ofrece «Cancelar…» y «Copiar referencia»: host e id, grupo, ticket, producto,
objetivo, rama, referencia propia y tiempos. Conserva el menú al actualizar.
`manifest` instala el guion ejecutable en `~/.local/bin/rack-inspeccionar`:

```bash
rack-inspeccionar desk:77
rack-inspeccionar 'rack desk:77 · jale · SEN-5731 · …'
rack-inspeccionar mini:288 --json
```

Consulta el panel por Tailscale y muestra metadatos, actividad y comandos de
lectura para seguir por SSH. Los comandos `remote-run` se ejecutan en la
MacBook y fijan `REMOTE_RUN_HOST` a la máquina de la referencia.
`--json` devuelve la tarea original de la API.
`--url` o `RACK_URL` sustituyen la URL predeterminada
`http://felixs-mac-mini.tail9f9a98.ts.net:8788/api/rack.json`.
Códigos: referencia inválida 2, tarea ausente 3, panel inaccesible 4.

## Cancelar una corrida de la Mini o del desk

«Cancelar…» consulta las capacidades de esa corrida con su lanzador antes de
habilitarse. Abre la confirmación aprobada «¿Cancelar <ticket> · <objetivo> en
<host>? La sesión que la espera la verá como cancelada». «No» y Escape cierran
sin actuar. Al confirmar, la fila muestra «Cancelando…» hasta que el lanzador
confirma el resultado; el aviso dura 8 segundos. Sólo el recibo `cancelled`
con `result.exit: 130` acredita una cancelación. La pestaña conserva en
sessionStorage las identidades canceladas `(host, task_id, ref)`: una instantánea
anterior no vuelve a mostrar la misma corrida, tampoco al recargar. Otra
referencia con el mismo ID, o el mismo ID en otro host, sigue visible.
`already_finished` conserva la
salida anterior; `stale_state` pide actualizar y decidir de nuevo. No se escala
una petición observada en fila a detener una corrida que ya arrancó.

El panel nunca ejecuta kill/remove/stash de Pueue. Envía `rack-cancel@1` por
stdin a `/usr/bin/python3 -I ~/.local/lib/remote-run/run-control.py`, una ruta
fija resuelta con el usuario Unix del servicio. No hay shell ni TTY. El único
ejecutor (`Executor`, con el intercambio acotado compartido de `desk_action.py`) usa cwd `/`, un entorno nuevo con HOME del usuario Unix, PATH fijo y
TMPDIR=/tmp; no hereda tokens ni configuración del panel. Tiene plazo de 10 s para Mini (12 s para
cancel, ver «Procedencia de la cancelación») y 40 s para desk,
termina únicamente su grupo de procesos si vence, recoge al hijo y limita la
respuesta JSON a 16 KiB durante la ejecución (como máximo 16 385 bytes
capturados); si excede el límite, termina y recoge sólo su grupo propio.
Un timeout, respuesta inválida o fallo de transporte
significa `unknown`, porque el efecto pudo ocurrir antes del corte.

### Procedencia de la cancelación (C5)

`rack-cancel@1` admite en `cancel` la clave opcional `origin`, con el único
valor `"rack-panel"`. Con ella el controlador guarda el origen junto al instante
de aceptación, y la espera de la sesión, en la MacBook, añade la línea
«Cancelada desde el panel del rack a las HH:MM; no la relances sin preguntar»
en su hora local. La extensión es **por corrida**: el controlador anuncia
`capabilities.cancel_origin: true` sólo si el manifiesto de esa corrida lo
declara, y sólo lo declaran las corridas nuevas del productor actualizado.

El navegador, la API HTTP, el diario SQLite y la envoltura SSH
`rack-desk-action@1` no cambian ni llevan `origin`. Lo añade el canal local
junto al controlador (`desk_action.send`): el ejecutor de la Mini, o el
despachador del desk. Para un cancel, ese canal consulta antes `capabilities`
de la misma corrida al mismo controlador y añade `origin` sólo si la respuesta
es válida (esquema, `ref`, `queued`/`running` booleanos) y `cancel_origin` es
exactamente `true`. Si falta, es falsa o inválida, el controlador falla o el
sondeo vence, envía el cancel anterior sin origen. El sondeo nunca bloquea la
cancelación ni cambia `expected`, y su proceso termina y se recoge antes del
cancel, para no retener el candado del controlador. `status` y `capabilities`
siguen siendo una sola llamada, sin sondeo.

Presupuestos, con un único plazo monotónico: el cancel conserva su ventana
de 10 s y el sondeo sólo usa el margen por encima, hasta 2 s. En la Mini el
cancel local tiene hasta 12 s dentro del presupuesto HTTP de 14 s y su reserva
de recibo; en el desk el despachador da 12 s interiores al cancel y el límite
SSH exterior sigue en 40 s (presupuesto HTTP 44 s). Con menos margen se omite
el sondeo. Si el plazo vence durante el sondeo, no se inicia el cancel y el
resultado es `unknown`. No hay reintentos: cada `action_id` emite un único
cancel, y un duplicado, un GET o un reinicio sólo consultan `status`.

La compatibilidad se decide por camino. Un despachador del desk nuevo añade
el origen aunque el servidor del panel sea anterior, porque la envoltura no
cambió; un despachador anterior cancela sin origen. La Mini lo añade sólo con
el ejecutor actualizado. Un controlador anterior, o una corrida sin la
declaración, conserva la cancelación y el mensaje de siempre. La UI mantiene
su promesa actual; no anuncia la procedencia, porque depende de la versión de
cada pieza. Instalación: cerrar la admisión con el interruptor del host,
esperar a que terminen las llamadas cancel/capabilities admitidas, instalar
controladores y productores de Hasito y Jale, y después el canal de PAO en el
desk (`desk_action.py`) y el servidor de la Mini, en cualquier orden; reabrir.

La instantánea añade por tarea:

```json
{"control":{"launcher":"remote-run","ref":"20261112-184713-d976b4",
 "expected":{"task_id":77,"state":"Queued"},"reason":null}}
```

La identidad se deriva exclusivamente de los campos de la cola: directorio
`~/remote-run/runs/<ref>` o comando fijo de Jale que apunta a
`~/.local/state/jale/pueue/job-<8 alfanuméricos>/task.sh`. Este dato no presume
compatibilidad: sólo el controlador confirma capacidades. No añade lecturas
de `body.sh`, `meta` ni logs. Sin lanzador reconocido, `launcher/ref` son null;
fuera de Queued/Running, `expected` es null. `reason` explica por qué no hay
acción. Instantáneas antiguas conservan lectura y deshabilitan cancelación.
Los grupos `otros` y `pg5434` sin lanzador reconocido quedan deshabilitados;
una corrida reconocida consulta capacidades independientemente del grupo.
Jale directo en la Mini fuera de Pueue no está cubierto por este contrato.
Una tarea con la etiqueta de Pueue `apotheca-remote-run` es del remote-run de
Bodega (repo apotheca). Usa la misma ruta `~/remote-run/runs/<ref>` que Hasito,
así que la etiqueta manda: `launcher/ref` son null, el proyecto es `apotheca`
(«Bodega» en la UI) y `reason` dice que su cancelación todavía no está
disponible desde el panel.

El transporte de escritura es independiente del de instantáneas:

| Host | `actions.transport` | `actions.controllers` | `actions.reason` |
| --- | --- | --- | --- |
| mini | local | remote-run → `~/.local/lib/remote-run/run-control.py` | null |
| desk | ssh | remote-run → misma ruta; jale → `~/.local/lib/jale/pueue-control.py` | null |

El presupuesto se mide con reloj monotónico desde la entrada al manejador HTTP,
antes de whois y de leer el cuerpo. La UI conserva **45 s desk / 15 s Mini**;
el servidor trabaja hasta **44 s / 14 s**, dejando **1 s para entregar HTTP**.
Whois conserva su límite de **3 s**, sin candado durante el proceso. La lectura
del cuerpo completo tiene **5 s** como máximo (hasta 8 s desde la entrada,
incluyendo whois), también con clientes que lo envían byte a byte.

| Parte de la petición | Desk | Mini |
| --- | --- | --- |
| Whois fresco | ≤ 3 s | ≤ 3 s |
| Cuerpo HTTP completo (POST) | ≤ 5 s | ≤ 5 s |
| Admisión al diario | candado ≤ 0.5 s; espera SQLite ≤ 0.5 s | igual |
| Transporte | `min(40, 44 − transcurrido − 1)` s | `min(10, 14 − transcurrido − 1)` s |
| Reserva para guardar el recibo (objetivo) | 1 s | 1 s |
| Espera HTTP por diario y transporte, desde entrada | ≤ 44 s | ≤ 14 s |
| Margen de entrega HTTP antes del plazo UI | 1 s | 1 s |

La cuenta no suma siempre todos los máximos: **whois + cuerpo + diario +
transporte restante + reserva + entrega ≤ 45/15 s**. Por ejemplo, tras 3 s
de whois, 5 s de cuerpo y 1 s de admisión, quedan **34 s desk / 4 s Mini**
de transporte, 1 s de recibo y 1 s de entrega. El mínimo útil antes del
transporte es **1 s**. El tiempo real del diario y de la validación se resta
antes de invocar al ejecutor; sus consultas SQLite comprueban el plazo y
sus esperas no recuperan un presupuesto completo. El plazo de intercambio
incluye también la creación del proceso.

El manejador espera al trabajador sólo hasta el límite monotónico de 44/14 s,
contado desde la entrada, y devuelve `unknown` con la identidad si no terminó.
Esa espera es independiente del candado y de SQLite: una apertura, escritura o
confirmación FULL bloqueada en E/S no queda acotada por el timeout de SQLite
ni por su callback de progreso. El trabajador conserva la intención durable,
la exclusión y el plazo original mientras acaba; nunca recupera presupuesto
ni lanza un cancel nuevo después del límite. La reserva de 1 s ayuda a
completar normalmente el recibo, pero no es la garantía del plazo HTTP.
El seguimiento usa el mismo ID. Los GET de IDs aún desconocidos en memoria
usan como máximo 14 s; al arrancar se cargan las identidades del diario.

Si no queda ese mínimo, no se llama al controlador. Con una intención ya
confirmada se guarda `rejected/deadline`: «No se lanzó una cancelación nueva:
se agotó el plazo. Consulta el mismo recibo». Si no se pudo entrar al diario,
la respuesta es 503/unknown con ese mensaje: no se lanzó nada nuevo, pero
puede existir un recibo anterior con efecto para ese ID. No se afirma que
una acción anterior no ocurrió. Si el transporte pudo actuar, vence o no
puede confirmarse el guardado, queda `unknown` y se consulta el mismo ID.
La UI muestra el texto de rechazo mediante su tratamiento habitual de errores.

El candado sólo protege lectura/intención/guardado del diario, nunca un
transporte. Un registro en memoria excluye `cancel` y `status` simultáneos del
mismo `action_id`; los GET y POST duplicados devuelven el recibo guardado
mientras está en vuelo. Otro cuerpo sigue dando 409 y `pending_reason` sigue
bloqueando un segundo ID de la misma corrida. El registro se libera también
si falla el guardado; la intención durable permite reconciliar después.
Así una consulta lenta no pisa una cancelación posterior ni un cancel pisa
un status posterior. Desk y Mini pueden transportar en paralelo, también
dos acciones independientes del desk. No se cambian los plazos del navegador.

`actions` tiene exactamente `transport`, `controllers` y `reason`; el transporte
`ssh` requiere además `argv`, que debe coincidir **entero** con `DESK_ARGV` de
`accion.py`. Sólo se acepta ese canal SSH para `id: desk` con lectura `ssh`.
Se valida al cargar `hosts.json` y antes de ejecutar: sólo se admiten las rutas
fijas por lanzador. Un transporte habilitado requiere controladores y razón
null; uno deshabilitado requiere una razón legible de hasta 240 caracteres.
Sin configuración, el host queda deshabilitado. La API no admite rutas ni argv.
El desk también puede tener grupos en pausa: una corrida Queued conserva la
consulta de capacidades de su lanzador; la pausa no implica incompatibilidad.

### Canal SSH del desk e instalación manual

Estos pasos los ejecuta el orquestador **después del merge**. No se ejecutan en
las pruebas. El despachador funciona con Python 3.8+; la instantánea existente
asume Python 3.9, y los controladores tienen contrato Python 3.9. Verificar
Python 3.9+ en WSL al instalar el conjunto.

1. Instalar instantánea/despachador con `bash fleet/rack/instalar-desk.sh` desde
   la MacBook. Los controladores compatibles se instalan desde Hasito y Jale,
   sin modificarlos desde este repositorio.
2. Crear en la **Mini** una llave nueva, nunca reutilizar la de lectura:

   ```bash
   # Ejecutar sólo si la ruta nueva no existe; nunca sobrescribir otra llave.
   test ! -e ~/.ssh/id_ed25519_pao_rack_accion
   ssh-keygen -t ed25519 -N '' -C pao-rack-accion@mini -f ~/.ssh/id_ed25519_pao_rack_accion
   chmod 600 ~/.ssh/id_ed25519_pao_rack_accion
   cat ~/.ssh/id_ed25519_pao_rack_accion.pub
   ```

   Ejecutar `ssh-keygen` sólo tras el éxito del `test` anterior. No copiar la
   privada al desk. La llave no lleva passphrase porque la usa el servicio;
   sólo autoriza el comando forzado de esta sección.
3. Agregar su pública como **otra línea** de
   `C:\ProgramData\ssh\administrators_authorized_keys`, preservando las ACL de
   SYSTEM/Administrators y la línea de lectura. Línea exacta propuesta, donde
   `<PUBLICA_ACCION>` es sólo el bloque base64 de la nueva pública:

   ```text
   from="100.89.145.51",restrict,command="MSYS_NO_PATHCONV=1 wsl -d Ubuntu -u felix -- /usr/bin/python3 -I /home/felix/.local/bin/pao-rack-action" ssh-ed25519 <PUBLICA_ACCION> pao-rack-accion@mini
   ```

   **Activar `from=` sólo tras verificar en los logs de OpenSSH de Windows
   que éste ve `100.89.145.51` como origen de la Mini.** Si no está verificado,
   omitir únicamente `from="100.89.145.51",` hasta completar esa comprobación.
   No sustituirlo a ciegas por una IP de WSL. Git bash requiere
   `MSYS_NO_PATHCONV=1`, igual que la lectura. `restrict` impide TTY y reenvíos;
   el comando forzado no usa `SSH_ORIGINAL_COMMAND`.
4. Confirmar por un canal de confianza que la host key Windows del nombre
   `felix-desktop` ya está en `~/.ssh/known_hosts` de la Mini. No usar
   `accept-new`, ni desactivar su verificación. Instalar el panel actualizado
   en la Mini con `bash instalar.sh`.

El argv cerrado de escritura, sin comando remoto ni datos de la corrida:

```text
/usr/bin/ssh -F /dev/null -T -o BatchMode=yes -o IdentitiesOnly=yes -o IdentityAgent=none -o ConnectTimeout=10 -o StrictHostKeyChecking=yes -o UserKnownHostsFile=~/.ssh/known_hosts -o ClearAllForwardings=yes -o PermitLocalCommand=no -o ControlMaster=no -o ControlPath=none -i ~/.ssh/id_ed25519_pao_rack_accion Felix@felix-desktop
```

`-F /dev/null` evita comandos/proxies configurados por el usuario y la
configuración global; no se usa agente ni una conexión multiplexada que pudiera
haber autenticado con la llave de lectura. SSH expande los `~` de las opciones.
La llave de lectura y su argv permanecen independientes.

El panel escribe por stdin una envoltura **interna**, cerrada y de hasta 16 KiB:

```json
{"schema":"rack-desk-action@1","launcher":"jale","request":{"schema":"rack-cancel@1","op":"cancel","ref":"job-AbCd1234","action_id":"action-0001","expected":{"task_id":77,"state":"Queued"}}}
```

El despachador valida también la petición interior por operación, tipos
exactos, identidades y claves duplicadas; acepta sólo capabilities, cancel y
status. El stdin tiene plazo de 5 s y límite de 16 KiB. No recibe una ruta ni
argumentos de controlador. Su **propia tabla fija**, independiente de
`hosts.json`, es:

| Lanzador | Ruta en WSL |
| --- | --- |
| remote-run | `/home/felix/.local/lib/remote-run/run-control.py` |
| jale | `/home/felix/.local/lib/jale/pueue-control.py` |

Invoca `/usr/bin/python3 -I <ruta fija>`, sin argumentos adicionales ni TTY,
con cwd `/` y un entorno nuevo: `HOME=/home/felix`, `PATH=/usr/bin:/bin`,
`TMPDIR=/tmp`, y USER/LOGNAME calculados con la cuenta Unix efectiva. Sólo pasa
`XDG_RUNTIME_DIR=/run/user/<uid>` si existe como directorio propiedad de esa
cuenta. No transmite el entorno de SSH, tokens ni overrides de pruebas.
Comparte con el ejecutor el intercambio acotado: stdout de hasta 16 KiB,
plazo de 10 s para el controlador (12 s para cancel, con el sondeo de
procedencia), terminación de su propio grupo y recogida.
Devuelve la respuesta del controlador, verificando esquema e identidad; el
panel conserva además su saneamiento y validación semántica antes de la UI.

| Fallo propio del despachador | Salida | Respuesta |
| --- | --- | --- |
| Envoltura inválida, claves extra/duplicadas, tamaño, stdin vencido o argv extra | 2 | rejected / invalid_request |
| Lanzador desconocido | 2 | rejected / unsupported |
| Controlador ausente, capabilities | 0 | queued=false, running=false |
| Controlador ausente, cancel nuevo | 0 | rejected / unsupported |
| Controlador ausente, status | 0 | unknown / unsupported |
| Interruptor apagado, cancel | 0 | rejected / unsupported, no se invoca controlador |
| Interruptor apagado, capabilities | 0 | queued=false, running=false |
| Plazo del controlador vencido | 4 | unknown / timeout |
| Salida excesiva o JSON/esquema/identidad inválidos | 5 | unknown / invalid_response |
| Controlador no arranca o termina con otro código | 6 | unknown / controller_failed |

Una respuesta válida del controlador conserva su código 0 o 2. Los errores
propios usan textos fijos sin rutas, logs ni secretos. Un fallo de transporte
SSH, **incluida cualquier salida no cero**, se convierte en `unknown` en el
panel, aunque el despachador haya podido responder antes de cortarse. El
panel nunca repite automáticamente una cancelación incierta; consulta status
con el mismo ID mediante el diario existente.

El límite de transporte del desk es **40 s**, reducido por el presupuesto
restante de la petición descrito junto a la tabla de transporte. Su reparto
orientativo es 10 s de conexión, 10 s de arranque WSL, 10 s de controlador y
10 s de holgura, incluida la entrada JSON; no es una medición de WSL en
producción. Un arranque más lento deja unknown y se reconcilia. No se promete
que la limpieza del trabajo termine dentro de esos plazos. Matar SSH local
no garantiza matar el proceso remoto: el despachador limita su controlador.

Para apagar nuevas acciones en **WSL**, sin tocar lectura ni llaves:

```bash
mkdir -p /home/felix/.local/state/pao/rack
touch /home/felix/.local/state/pao/rack/actions.disabled
# Reactivar:
rm /home/felix/.local/state/pao/rack/actions.disabled
```

Se comprueba con `lstat` antes de cada cancel y capabilities. Cualquier entrada
(archivo, directorio o enlace colgante), o error distinto de ausencia,
apaga las acciones. Status permanece disponible
para reconciliar acciones aceptadas, aunque pueda continuar su limpieza.
El interruptor no revoca una acción que ya superó la comprobación. Es distinto
del interruptor del panel en la Mini y no se copia ni borra al instalar.
Para revocar acceso, borrar **sólo** la línea con la pública/comment de acciones
de `administrators_authorized_keys`; la de lectura sigue funcionando. Borrar
la de lectura tampoco revoca acciones. Conservar el diario y los recibos;
quitar una llave no corta sesiones ni revoca una acción ya en vuelo.

Este canal conserva el contrato y los controladores; el presupuesto y la
concurrencia del panel conservan la autorización y las garantías del diario.
No toca Pueue desde el panel, no cambia cuotas/horarios/prioridades ni permite
comandos generales, Jale directo de Mini o lanzadores no reconocidos. La
instalación Windows/ACL, comprobación de origen y canario de waiters/130 reales
se verifican por el orquestador tras el merge; las pruebas locales son falsas.

### API y autorización

`POST /api/accion` admite sólo `cancel` y la consulta `capabilities` (ensayo sin
cancelar). Envoltura cerrada, sin claves adicionales:

```json
{"schema":"rack-action@1","op":"cancel","host":"mini","launcher":"remote-run",
 "ref":"20261112-184713-d976b4","action_id":"action-0001",
 "expected":{"task_id":77,"state":"Queued"}}
```

`capabilities` usa los mismos campos salvo `action_id`. El servidor comprueba
host, identidad y estado de la instantánea, con máximo 45 s de antigüedad,
y transmite el `expected` exacto al lanzador, que cierra la carrera real.
`GET /api/accion/action-0001` consulta el recibo; no acepta referencias o rutas
nuevas desde el cliente. Respuesta común `rack-action@1`: `action_id`, `op`,
`host`, `launcher`, `ref`, `status`, `result` y `error`; las capacidades añaden
`capabilities`. Errores del controlador se traducen a mensajes fijos sin
rutas, comandos, logs o secretos. 409 indica conflicto, 403 rechazo de acceso
o interruptor, 400 petición inválida. Un resultado incierto se expresa como
`unknown` aun cuando HTTP falle; un ID no hallado responde 404/unknown y nunca
se interpreta como una cancelación fallida. La UI reenvía una vez el mismo
cuerpo con el mismo ID ante ese 404, protegido por el diario idempotente.

Además de Host permitido, cliente del tailnet y rechazo de Funnel, cada POST
exige `Origin` exactamente igual a `http://<Host servido>`, JSON y
`X-Rack-Action: 1`. `Sec-Fetch-Site`, si está presente, debe ser `same-origin`;
no sustituye Origin. No hay CORS ni confianza en cabeceras de proxy.
Whois se vuelve a consultar sin caché en cada petición de acciones. El GET
también requiere whois fresco: puede continuar la limpieza ya aceptada por
el lanzador. No abre una cancelación nueva.

Una acción abierta (`cancelling`, `unknown` o `cancel_unconfirmed`) bloquea
otra cancelación de la misma combinación host/lanzador/ref en el diario,
incluso desde otra pestaña o tras reiniciar el servidor. El POST nuevo da
409 con el ID abierto y capacidades muestra la misma razón. La fila pendiente
sigue visible aunque Pueue indique Done o Stashed. La exclusión tras un recibo
`cancelled` incluye encolado y arranque: un intento nuevo vuelve a aparecer.


### Diario e interruptor

`~/.local/state/pao/rack/actions.sqlite3` es el diario autoritativo, modo 0600,
con commits SQLite `synchronous=FULL`. La tabla `actions` guarda `action_id`
(clave primaria sensible a mayúsculas), digest SHA-256 del JSON canónico,
petición, respuesta, created y updated. Se confirma `unknown` **antes** de
llamar al controlador; después se guarda su recibo. El mismo ID/cuerpo no
vuelve a cancelar; un cuerpo diferente con ese ID es 409/idempotency_conflict.
El diario no se rota ni depende de history.jsonl. No borrarlo para reparar
un timeout o liberar un ID.

Tras una caída entre intención, efecto y recibo, un POST repetido devuelve lo
que sabe el diario. Para `cancelling`, `cancel_unconfirmed` y `unknown`, el GET
consulta únicamente `status` con la misma referencia e ID. Todo `rejected`
de esa consulta conserva `unknown`, sea `not_found`, `unsupported`,
`stale_state`, `idempotency_conflict` o `invalid_request`: un rechazo tardío
no demuestra que la cancelación aceptada no tuvo efecto. No se reenvían
mensajes privados del controlador. La UI guarda las acciones
pendientes en sessionStorage para continuar al recargar esa pestaña. Tras
un error de red consulta el recibo; sólo un 404 autoriza el reenvío único
del mismo `cancel`, cuerpo e ID. La fila y el
aviso distinguen incertidumbre y falta de confirmación del fin de procesos.

Sólo para el desk, el servidor comparte entre pestañas un límite por
`action_id`: como máximo un `status` cada **2 s durante el primer minuto**
desde la intención, y cada **30 s después**. Dentro del intervalo devuelve
el recibo guardado. La hora del último intento se confirma en SQLite antes
del transporte y sobrevive al reinicio. Una consulta en vuelo también excluye
otra de esa acción. La Mini conserva sus consultas habituales. `unknown`,
`cancelling` y `cancel_unconfirmed` siguen abiertos: el paso del tiempo o un
`rejected` de status nunca prueban que no hubo efecto.

#### Cerrar a mano un recibo sin confirmación

Si SSH/WSL falló antes de crear el recibo del controlador, o la cancelación
permanece sin confirmar, Felix o el orquestador pueden cerrar el recibo
**en la Mini**, fuera del panel. Primero apaga nuevas acciones con el
interruptor local indicado abajo y deja terminar los transportes en vuelo.
Comprueba en Pueue del desk (su interfaz habitual o `pueue status`) la tarea
exacta del recibo (`host`, lanzador, ref y `expected.task_id`): confirma con
el lanzador que ya no está cancelándose ni tiene limpieza pendiente. Si hay
procesos o recursos de estado incierto, conserva el recibo abierto. No basta
con que el controlador responda not_found ni con ver Done o una salida 130.

Después ejecuta, sustituyendo `action-0001` por el ID que muestra el panel:

```bash
TMPDIR=/tmp PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -I "$HOME/.local/lib/pao-rack/accion.py" --state-dir "$HOME/.local/state/pao/rack" close-receipt action-0001
```

Con un directorio de estado personalizado usa el mismo `--state-dir` que
el servidor. El subcomando usa el mismo candado de escritura SQLite,
transacción y confirmación `synchronous=FULL` que el servidor, y no ejecuta
transportes. Guarda un terminal `rejected/manually_closed`: «Recibo cerrado
a mano tras comprobar la corrida en Pueue. No acredita una cancelación.»
Un ID inexistente o terminal por otra causa se rechaza con salida 2;
repetir un cierre manual devuelve el mismo recibo sin volver a escribir.
Un transporte anterior no puede sobrescribirlo. El próximo GET muestra el
mensaje y la UI deja de consultar; `pending_reason` deja de bloquear la
corrida. Para otro intento usa un ID nuevo. Reactiva las acciones sólo
después de la comprobación. No borres el diario ni recibos de los lanzadores.

Para apagar acciones localmente sin reinstalar ni afectar lectura:

```bash
mkdir -p ~/.local/state/pao/rack
touch ~/.local/state/pao/rack/actions.disabled
# Para reactivarlas:
rm ~/.local/state/pao/rack/actions.disabled
```

Con `--state-dir` usar ese directorio. El interruptor bloquea nuevas
cancelaciones y capacidades; los recibos y consultas de acciones ya aceptadas
siguen disponibles para no abandonarlas. No revoca retrospectivamente una
cancelación en curso.

Instalar el panel con `bash instalar.sh` después de revisión. El controlador
compatible se instala con el procedimiento del repo Hasito; el instalador del
rack no lo copia ni lo cambia. Si falta, la UI no habilita la acción. La
instalación y canarios productivos se hacen en otro paso, nunca con las
pruebas de este directorio.

## Lugares por grupo en la Mini (U3.1)

Cada carril de la Mini muestra `N/M en uso`, los botones − y + y la cifra de
lugares. La tarjeta de capacidad dice `N en total · tope 7`. Si corren más
corridas que lugares, el carril avisa que no entran nuevas hasta bajar de esa
cifra. Un botón que no aplica queda con `aria-disabled` y explica la razón al
pasar el cursor o al pulsarlo:

- mínimo 1 y máximo 8 por grupo;
- con el tope lleno, «Quita un lugar a otro grupo para añadirlo aquí»;
- un cambio en curso en el host, propio o de otra pestaña;
- una instantánea vieja o una deriva que la guardia está reponiendo;
- un host sin el control nuevo, y el desk, que todavía no admite este cambio.

Al pulsar, la página envía `POST /api/accion` con
`{schema:'rack-action@1', op:'slots', host:'mini', action_id, dry_run:false,
expected:{revision, policy_hash, slots}, change:{group, delta}}`. `expected`
es exactamente lo que vio Felix. Mientras espera, el carril dice «Aplicando…».
Con el resultado, un aviso de 8 s ofrece **Deshacer**, que envía `op:'undo'`
con `undo_of` y la revisión nueva. La cifra aplicada se mantiene hasta que la
instantánea alcance esa revisión, y la página pide una instantánea nueva.
Ante un error de red, el carril dice «Resultado desconocido · consultando…» y
consulta con el mismo ID. Si el panel no tiene la intención (404), reenvía el
mismo cuerpo una sola vez; el host aplica a lo sumo una vez por ID. Un
resultado parcial dice «Aplicado parcialmente · intentando completar», porque
la guardia lo completa. El pendiente se conserva en `sessionStorage`
(`rack-slots`) y sobrevive a recargar la página.

En el servidor, `slots` y `undo` siguen el diario de Cancelar:

1. Esquema cerrado.
2. Intención `unknown` confirmada antes del transporte, en `actions.sqlite3`.
3. Mismo ID con otro cuerpo da 409.
4. El interruptor `actions.disabled` da 403.
5. Un cambio abierto del mismo host da `busy`.
6. Una instantánea de más de 45 s, o con otra revisión, deseo u observado, da
   `stale_state`.

`dry_run:true` no escribe en el diario ni reserva el ID. El transporte es fijo:
`/usr/bin/python3 -I ~/.local/lib/pao-pueue-mini/control.py rack --host mini`,
con el cuerpo por stdin. Esa ruta la fija `host_control` en `hosts.json`, sólo
con transporte local. Un GET sobre un recibo abierto consulta `status` al host.
Si el host no conoce el ID, reenvía el mismo cuerpo una vez. La instantánea
lleva `control`, la vista saneada de `capabilities`. Sin esa vista no hay
cambios de lugares. Las capacidades de modo, reordenar y reiniciar la fila se
anuncian como no disponibles.

Instalación, después de revisar y fusionar, en este orden:

1. En la Mini, `install-mini.py instalar --seco` y después `instalar`. Ver
   «Lugares por grupo (U3.1)» en `fleet/pueue-desk/README.md`: instala el
   control nuevo y adopta el perfil.
2. Copiar `fleet/rack/` a la Mini y ejecutar `bash instalar.sh`. Copia
   `hosts.json` con `host_control`.

Para revertir, reinstalar `fleet/rack/` desde el commit anterior. Sin
`host_control`, el panel rechaza los cambios de lugares como `unsupported` y
deja los botones sin efecto. Volver también a la guardia anterior no cambia
`parallel`.

## Pruebas locales sin hosts reales

```bash
cd fleet/rack
TMPDIR=/tmp /usr/bin/python3 -m unittest discover -s tests -v
TMPDIR=/tmp PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 tests/check_mutations.py
TMPDIR=/tmp PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -I ../pueue-desk/tests/mutate_slots.py
TMPDIR=/tmp /usr/bin/python3 tests/browser_fixture.py --port 0
```

El último comando imprime una URL en 127.0.0.1 para abrir en un navegador.
Ofrece una corrida sintética de la Mini y dos del desk (remote-run y Jale),
en grupos pausados. Tras confirmar, el
controlador falso responde `cancelling` durante 7 s y luego `cancelled`/130.
Ctrl-C cierra el servidor y borra su directorio temporal. No inicia recolector,
no conecta a hosts y no usa 8787. El fixture genera ambos controladores bajo /tmp y usa el ejecutor de
producción. Su runner comprueba el argv SSH exacto y sustituye sólo la conexión
por un shim Python; el despachador real valida la envoltura y ejecuta los
controladores sintéticos. No se ejecuta ningún binario SSH. Conserva la corrida en sus instantáneas para acreditar que
la fila cancelada no reaparece aunque el recolector todavía la incluya.

Las pruebas son Python 3.9/biblioteca estándar, con whois, transporte, reloj y
controladores falsos. Una guardia de auditoría impide ejecutar ssh, Pueue,
Tailscale o controladores instalados. La prueba JS ejecuta el script real con
DOM, red, reloj y almacenamiento sintéticos, mediante Node si está disponible,
sin paquetes de frontend. Prueba diálogo, No, pendiente, avisos de 8 s,
capacidades, razón configurable, conflictos e incertidumbre/red con el mismo
ID; después del recibo renderiza la instantánea anterior y recarga la página
con almacenamiento conservado. Las pruebas del ejecutor también lanzan
controladores sintéticos generados en /tmp, bajo la guarda global: observan
stdin, aislamiento, entorno y ausencia de TTY, y verifican que el plazo
termina el grupo conocido y recoge el proceso. En macOS, el shim de Python
puede añadir variables de SDK; se comprueba que ninguna variable del panel
llega al controlador. `check_mutations.py` copia el artefacto a /tmp y
acredita que quitar whois fresco, Origin, digest, intención unknown,
conservación de unknown ante rechazos de status o exclusión de canceladas
frente a instantáneas anteriores rompe las pruebas específicas. También
retira la selección por tabla del desk, el interruptor, la validación del argv
y unknown ante fallo SSH. También muta lstat, entorno/runtime, ausencia de
controlador, admisión, mínimo/plazo restante, efecto tardío, transporte fuera
del candado, exclusión de status/cancel, liberación tras fallo y origen del
presupuesto HTTP; también la espera independiente de E/S, el espaciado
del desk, la conservación del cierre manual y su fin de sondeo en la UI.
`tests/test_c5.py` cubre la procedencia: sondeo y un único cancel con
`origin` en la Mini y en el desk, envoltura SSH sin `origin`, degradación ante
controladores anteriores, corridas sin declaración y respuestas inválidas o
vencidas, parser cerrado, presupuestos con reloj virtual, sondeo que retiene
stdout y candado, duplicado/GET/reinicio sin reenviar cancel, y canales
viejos y nuevos por camino. Sus mutaciones (`c5-*`) usan `exchange` en vez de
`send`, añaden origen antes de SSH o en el servidor, aceptan valores
truthy, bloquean el cancel ante un sondeo fallido, abren el parser, sondean
toda operación, ignoran el margen, no recogen el sondeo, reinician el plazo o
reenvían cancel en GET. Borra las copias al salir.

`tests/rack-fixture.json` conserva la instantánea realista para las pruebas de
lectura y render existentes, sin comandos ni rutas.
