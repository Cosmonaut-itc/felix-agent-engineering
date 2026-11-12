# Rack de pruebas

Panel de sólo lectura de Mac Mini y Felix DESKTOP (WSL), con la variante
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
   `User[<id>].LoginName`. Copia los cuatro archivos a
   `~/.local/lib/pao-rack/` y carga `com.cosmonaut.pao-rack` con KeepAlive.
   El puerto es **8788**, fijado en `puerto=` de `instalar.sh`; en la Mini
   el 8787 ya lo usa otro servicio. El Funnel existente en 10000 no se modifica.

El LaunchAgent ejecuta `/usr/bin/python3 -I serve.py --bind <IP tailnet>
--port 8788 --owner <LoginName>`. Logs en `~/Library/Logs/pao-rack.log`.
`bash instalar.sh --desinstalar` descarga el agente y quita sus archivos;
conserva historial, caché, key de Linear, llave SSH y logs.

## Sólo dispositivos del dueño dentro del tailnet

El servidor sólo acepta bind en `100.64.0.0/10` (o `127.0.0.1` para pruebas).
Rechaza con 403 sin datos clientes fuera de `100.64.0.0/10` y
`fd7a:115c:a1e0::/48`. Rechaza cualquier cabecera `Tailscale-Funnel-Request`.
No confía en `X-Forwarded-For`. Resuelve cada IP permitida con
`Tailscale whois --json <ip>`, timeout de 3 segundos y caché de 5 minutos por
IP, comparando `UserProfile.LoginName` con `--owner`. Si no coincide o falla
la consulta o su formato, responde 403. `--owner` es obligatorio fuera del
modo loopback; `--tailscale` permite sustituir el binario en las pruebas.
Los cambios de identidad pueden tardar hasta 5 minutos en reflejarse.

Después de instalar, desde otro dispositivo del dueño conectado al tailnet:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' http://<IP-TAILNET-MINI>:8788/
# 200
curl -sS -o /dev/null -w '%{http_code}\n' -H 'Tailscale-Funnel-Request: ?1' http://<IP-TAILNET-MINI>:8788/
# 403
curl --connect-timeout 3 http://<IP-LAN-MINI>:8788/
# Conexión rechazada: no debe responder HTTP por la IP de red local.
```

Comprobar también desde un dispositivo de otro dueño: debe dar 403. No usar
Funnel para verificarlo. Revisar de nuevo `Tailscale serve status --json` y
`Tailscale funnel status --json`: no deben exponer 8788.

## Datos y operación

`hosts.json` es una lista de `{id,name,transport,argv}`. `local` ejecuta la
instantánea como subproceso y admite `{rack_dir}` en argv; `ssh` ejecuta el
argv sin shell. Un host futuro será otra entrada (no se implementan VMs).
Los hosts se consultan en paralelo con timeout de 25 segundos. Si falla un
host se conserva su último dato bueno, marcado como antiguo. Las lecturas
parciales se indican en `snapshot.errors`. No se exportan comandos, rutas ni
entornos de Pueue. `body.sh` se recorre por líneas, extrayendo sólo MODE,
BASE_REPO y REQUESTED_SHA; nunca se ejecuta ni registra su contenido.

El clon del desk no conserva ramas remotas al día, así que la instantánea
sólo entrega el SHA y el repositorio (`owner/nombre`, sacado de
`remote.origin.url`). El servidor resuelve el ticket con el `gh` de la Mini,
que ya tiene sesión: primero `commits/<sha>/branches-where-head` y luego
`commits/<sha>/pulls`, y toma el primer `sen-<n>` del nombre de rama. Guarda
el resultado en `commits.json`; un acierto no se vuelve a consultar y un
fallo se reintenta cada 30 minutos, con máximo diez consultas por ciclo.

El historial `~/.local/state/pao/rack/history.jsonl` conserva 30 días, sin
duplicar `(host,id,start)`. El estimado usa la mediana de las últimas 20
corridas exitosas con igual host, grupo y nombre sin sufijos `-sol`, `-opus`
o `-rN`; necesita tres muestras. No se muestran tareas Done en la API.
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

## Pruebas locales

```bash
/usr/bin/python3 -m unittest discover -s fleet/rack/tests -v
bash -n fleet/rack/instalar.sh
bash -n fleet/rack/instalar-desk.sh
```

Las pruebas usan Pueue y Tailscale falsos, un repositorio git temporal y HTTP
en loopback. No usan red externa ni SSH. `tests/rack-fixture.json` deriva de
la instantánea realista del brief, enriquecida con títulos y estimados de `proto/data.js`, sin comandos ni rutas, y permite capturas
reproducibles de ambos tamaños.
