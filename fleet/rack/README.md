# Rack de pruebas

Panel de Mac Mini y Felix DESKTOP (WSL), con lectura del rack y cancelación
por lanzador en la Mini, con la variante
«C · Lugares». Python 3.9 y biblioteca estándar; sin dependencias de frontend.
Cada host entrega `rack-host@1`; el servidor sirve `rack@1` cada 15 segundos.

## Preparación e instalación

Estos pasos son manuales, después de revisar y fusionar el cambio. No publicar
el puerto por Serve, Funnel, un proxy ni un túnel de red local.

1. En la MacBook, `bash fleet/rack/instalar-desk.sh` copia únicamente la
   instantánea a WSL, a través del alias SSH `desk`, que entra directo al
   sshd de Ubuntu. Reemplaza el archivo de forma atómica. No instala servicios.
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
   `User[<id>].LoginName`. Copia `snapshot.py`, `serve.py`, `accion.py`, `index.html` y `hosts.json` a
   `~/.local/lib/pao-rack/` y carga `com.cosmonaut.pao-rack` con KeepAlive.
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

## Cancelar una corrida de la Mini

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
ejecutor usa cwd `/`, un entorno nuevo con HOME del usuario Unix, PATH fijo y
TMPDIR=/tmp; no hereda tokens ni configuración del panel. Tiene plazo de 10 s,
termina únicamente su grupo de procesos si vence, recoge al hijo y limita la
respuesta JSON a 16 KiB. Un timeout, respuesta inválida o fallo de transporte
significa `unknown`, porque el efecto pudo ocurrir antes del corte.

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
Los grupos `otros` y `pg5434` quedan deshabilitados. Jale directo en la Mini
fuera de Pueue no está cubierto por este contrato.

El transporte de escritura es independiente del de instantáneas:

| Host | `actions.transport` | `actions.controllers` | `actions.reason` |
| --- | --- | --- | --- |
| mini | local | remote-run → `~/.local/lib/remote-run/run-control.py` | null |
| desk | disabled | remote-run → misma ruta; jale → `~/.local/lib/jale/pueue-control.py` | canal de escritura del desk pendiente |

`actions` tiene exactamente `transport`, `controllers` y `reason`. Se valida
al cargar `hosts.json` y antes de ejecutar: sólo se admiten esas rutas fijas
por lanzador. Un transporte local requiere al menos un controlador y razón
null; uno deshabilitado requiere una razón legible de hasta 240 caracteres.
Sin configuración, el host queda deshabilitado. El ejecutor elige la ruta de
esa tabla y la resuelve bajo el home del usuario Unix; la API no permite
enviar rutas ni argv. La razón que muestra la UI viene de esta configuración,
sin una excepción fija para el desk.

C4 añadirá el transporte con llave/comando forzado dedicado al mismo `Executor`
y la tabla explícita de `hosts.json`. No reutilizar la llave de lectura ni
convertir el `argv` público en un comando recibido del cliente. C3 no instala
canales en el desk ni cambia cuotas, horarios o el orden de la fila.

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
se interpreta como prueba de que no hubo efecto.

Además de Host permitido, cliente del tailnet y rechazo de Funnel, cada POST
exige `Origin` exactamente igual a `http://<Host servido>`, JSON y
`X-Rack-Action: 1`. `Sec-Fetch-Site`, si está presente, debe ser `same-origin`;
no sustituye Origin. No hay CORS ni confianza en cabeceras de proxy.
Whois se vuelve a consultar sin caché en cada petición de acciones. El GET
también requiere whois fresco: puede continuar la limpieza ya aceptada por
el lanzador. No abre una cancelación nueva.

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
pendientes en sessionStorage para continuar al recargar esa pestaña; nunca
reenvía `cancel` automáticamente después de un error de red. La fila y el
aviso distinguen incertidumbre y falta de confirmación del fin de procesos.

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

## Pruebas locales sin hosts reales

```bash
cd fleet/rack
TMPDIR=/tmp /usr/bin/python3 -m unittest discover -s tests -v
TMPDIR=/tmp /usr/bin/python3 tests/check_mutations.py
TMPDIR=/tmp /usr/bin/python3 tests/browser_fixture.py --port 0
```

El último comando imprime una URL en 127.0.0.1 para abrir en un navegador.
Ofrece una corrida sintética de la Mini y otra del desk. Tras confirmar, el
controlador falso responde `cancelling` durante 7 s y luego `cancelled`/130.
Ctrl-C cierra el servidor y borra su directorio temporal. No inicia recolector,
no conecta a hosts y no usa 8787. El fixture genera el controlador bajo /tmp,
en la misma ruta relativa configurada, y usa el ejecutor de producción sin
sustituir argv. Conserva la corrida en sus instantáneas para acreditar que
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
frente a instantáneas anteriores rompe las pruebas específicas;
borra las copias al salir.

`tests/rack-fixture.json` conserva la instantánea realista para las pruebas de
lectura y render existentes, sin comandos ni rutas.
