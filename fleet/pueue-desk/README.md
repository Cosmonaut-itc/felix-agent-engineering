# Pueue en el desk y la Mini: cuotas, guardia y reinicios

Pueue **4.0.4** con supervisor del host. El desk usa systemd de usuario en WSL;
la Mini, un LaunchAgent propio. Esta entrega **no instala nada**: el desk no se
contactó y todas las mediciones y pruebas se ejecutaron mediante `remote-run`
en copias privadas de la Mini, con configuración, cola y socket propios.

## Qué sobrevive

- Las tareas `Queued` conservan id, comando, directorio, grupo y entorno.
- El host captura una tarea `Running`/`Paused` interrumpida y la devuelve a
  `Queued` mediante `restart --in-place`, **una sola vez por identidad de tarea**,
  cuando se acredita que no quedan huérfanos. Un segundo reinicio la deja
  `Done/Killed`, con el motivo en `guardia.log` y `guardia-state.json`. Un fallo
  ordinario, un kill del cliente o un `Killed` sin prueba de interrupción no se reintenta.
- Los grupos y sus cuotas persistidas sobreviven. Las cuotas del desk se
  vuelven a aplicar según UTC; la Mini vuelve a `suites=3`.
- Las pausas manuales siguen pausadas. Las pausas de arranque, Docker o presión
  pertenecen al mismo guardia y sólo éste las levanta al estar listo el host.

**Seguridad en macOS:** SIGKILL de `pueued` deja vivo el `sleep` directo medido.
SIGTERM mata ese hijo directo, pero **una hija en otra sesión sobrevive también
a la parada ordenada**, como puede ocurrir con el adaptador de Hasito. Por eso,
en el mismo arranque del sistema la Mini nunca reencola una tarea interrumpida:
la deja `Done/Killed` con «No se descartan hijos vivos…», incluso si una versión
anterior guardó `clean_stop=true`. Un cambio de `kern.boottime` permite recuperar.
La prueba de una hija separada y la negativa a duplicarla están en
[`validacion-r2.txt`](evidence/validacion-r2.txt), evento `SUITE-OTRA-SESION`.
No se midió un reinicio real de la Mini: las pruebas de ese camino simulan
el identificador del arranque anterior.

## Mediciones antes de implementar

Corrida inicial `20261112-011702-585cb7`; confirmación ampliada
`20261112-012401-748e96`. Binarios existentes `/opt/homebrew/bin/pueue` y
`pueued`, ambos 4.0.4. Script reproducible:
[`evidence/diag-reinicio.py`](evidence/diag-reinicio.py). Todos sus recursos
están dentro del directorio de la corrida; el socket relativo `s` queda dentro
de cada instancia privada para respetar el límite de longitud de macOS.

| Caso | Medido en la Mini | Evidencia, líneas de `diag-reinicio-durabilidad.txt` |
| --- | --- | --- |
| a. SIGTERM y nuevo arranque | Daemon sale 0, sleep directo muerto; Running pasa a Done/Killed; Queued mantiene id. El grupo con cola queda Paused. | [3–5](evidence/diag-reinicio-durabilidad.txt#L3) |
| b. SIGKILL y nuevo arranque | Daemon sale -9, hijo **vivo**; en disco sigue Running, al restaurar pasa a Done/Killed; Queued conserva id. | [7–9](evidence/diag-reinicio-durabilidad.txt#L7) |
| c. Frecuencia de escritura y add reciente | 8/8 adds ya estaban en disco al responder y los 8 sobreviven a SIGKILL inmediato. En 1,2 s de reposo no hay escritura periódica. | [14–15](evidence/diag-reinicio-durabilidad.txt#L14) |
| d. Grupos, parallel y pausas | Preserva grupo manual Paused y parallel=3; un grupo vacío Running sigue Running. `parallel` sin otra mutación todavía no está en disco; tras `pause --wait` restaura el valor nuevo. | [5](evidence/diag-reinicio-durabilidad.txt#L5), [16–17](evidence/diag-reinicio-durabilidad.txt#L16) |
| e. restart --in-place | Conserva id, comando original y expandido, path, grupo y entorno; vuelve a Queued. Solo no se guarda, ni tras 800 ms; `pause --wait` lo persiste y sobrevive a otro SIGKILL. | [10–13](evidence/diag-reinicio-durabilidad.txt#L10) |

La lectura del [código de 4.0.4 y documentación de systemd](evidence/fuentes.txt)
explica esas mediciones: el loop de 300 ms **no** es un reloj de guardado;
`add` escribe antes de confirmar, `restart` y `parallel` no guardan, `pause`
sí guarda. Pueue usa archivo temporal + rename, sin fsync en esa ruta. No se
midió pérdida de energía, apagado forzoso de WSL ni corrupción del filesystem:
un SIGKILL de proceso no demuestra durabilidad frente a esos casos. Tampoco
se reinició la Mini real.

## Arranque y recuperación

`host.py` es el proceso principal del servicio. Mantiene un candado de ciclo
completo y ejecuta, en este orden:

1. Verificar versión 4.0.4 y que no haya otro daemon vivo en el socket/PID.
   Un PID reutilizado por otro programa se retira sin matar ese programa.
2. Bajo `control.lock`, validar el JSON de la cola **con el daemon detenido**,
   capturar las identidades Running/Paused y los Done/Killed acreditados por
   la auditoría del último shutdown, y marcar todos los grupos Paused en disco. También cierra grupos vacíos y tareas diferidas: el auto-pause de Pueue
   sólo alcanza a grupos que ya contienen tareas Queued.
3. Iniciar `pueued`, esperar su socket y comprobar que la cola está cerrada.
4. Ejecutar `recuperar`, aplicar las cuotas y ejecutar el guardia; todo bajo
   el mismo candado. Los timers nunca pueden reanudar con `starting=true`.

La única edición externa de `state.json` ocurre **antes** de arrancar el daemon,
para cerrar los grupos; conserva las tareas. El JSON incompatible o comprimido
aborta el arranque, evitando el fallback de Pueue a una cola vacía. Los archivos
propios se escriben por reemplazo atómico con fsync, permisos 0600 y candado.
No se borra la cola ni se hace `reset` o `clean`.

`recuperar` cruza id + `created_at` + inicio de ejecución con la captura; no
interpreta cualquier `Killed` como reinicio. Registra la intención **antes**
de `restart --in-place`, fuerza el guardado con `pause --wait` y confirma
el mismo id e identidad en disco antes de abrir la admisión. Si muere entre
intención y confirmación, reconcilia con admisión cerrada: el mismo Done/Killed
con el mismo inicio todavía no tuvo otra ejecución y permite completar el
restart; si ya está Queued, sólo confirma y persiste la cola. Una ejecución
posterior con otro inicio consume la recuperación y no se reintenta otra vez.
El historial no se elimina al limpiar tareas de Pueue; `created_at` distingue
un id reutilizado.

Para volver a ejecutar el comando idempotente en una instalación normal:

```bash
/usr/bin/python3 -I ~/.local/lib/pao-pueue-desk/control.py recuperar
# En la Mini: añadir --host mini --pueue-bin /opt/homebrew/bin/pueue.
```

En una configuración privada también se necesitan `--config`, `--data-dir` y
`--state-dir`. No ejecutar recuperar contra una cola de otro supervisor.
No cambiar aliases durante la recuperación: Pueue reexpande el comando al
reiniciar. La ejecución normal conserva el entorno de la tarea, no el entorno
actual del supervisor.

Para apagar o reiniciar usar el **servicio/supervisor**, que captura las tareas
antes de mandar SIGTERM al daemon, también al salir por una excepción del guardia.
Un SIGTERM directo a `pueued` produce Done/Killed en disco. El supervisor activa
una auditoría de 4.0.4 en `daemon-audit.log` (0600): cruza el evento de matar todas
las tareas al apagar con los finales Killed posteriores y excluye las selecciones
Kill/Reset/DaemonShutdown recibidas del cliente. Una orden manual queda excluida
hasta observar su final; prevalece sobre una captura que todavía la vio Running.
Una ejecución posterior del mismo id puede recuperarse.
Si un mensaje manual queda truncado o intercalado, la atribución se rechaza de
forma conservadora. Tampoco se usa un antiguo kill --all si después el daemon
volvió a iniciar tareas. El registro se conserva aunque muera sólo el supervisor;
se consume con el daemon detenido antes de rotarlo a `.1` en el siguiente arranque.
El archivo actual crece durante cada activación: incluye las órdenes y respuestas
del protocolo y debe conservarse para atribuir el shutdown. Sin esa evidencia no
se atribuyen Done/Killed arbitrarios a una caída. La prueba primaria adicional está
en [`diag-auditoria-r2.txt`](evidence/diag-auditoria-r2.txt).

Un SIGKILL abrupto sí deja Running en disco. Si se mata sólo el supervisor de
la Mini y el daemon sigue vivo, el nuevo supervisor se niega a editar la cola o
crear otro daemon hasta que termine.

## Desk: Docker y presión deciden cuándo correr

`control.py` contiene la tabla de cuotas del desk:

| Grupo | Día | Noche |
| --- | --- | --- |
| hasito | 3 | 4 |
| jale | 2 | 3 |
| otros | 1 | 1 |
| pg5434 | 1 | 1 |

Noche: **[05:00Z, 14:00Z)**. La suma es 7/9 incluyendo pg5434; Pueue no impone
un máximo global. `pg5434` queda reservado; los clientes usan su grupo de
proyecto. `default` y los grupos ajenos quedan cerrados al arrancar y no se
abren automáticamente; no enviarles corridas. La Mini administra sólo suites,
con 3 turnos, sin reloj de día/noche.

El guardia corre al arrancar y cada 60 s. `docker info` debe responder con
código 0 en **3 s**; ruta por defecto `/usr/bin/docker`, configurable mediante
`--docker-bin` o `PAO_DOCKER_BIN`. Si no responde, pausa todos los grupos de la
tabla mediante `pause --wait`, con motivo **docker**, sin necesitar `/proc`.
No mata las tareas que ya estaban vivas.

Con Docker listo aplica la misma histéresis de U11: pausa con PSI avg60 E/S >40,
CPU >30 o MemAvailable <4 GiB; reanuda sólo sus grupos con E/S <20, CPU <15 y
RAM ≥6 GiB. Los valores intermedios mantienen el estado. Docker de vuelta con
presión alta no abre la cola. Un error de lectura no reanuda. Arranque, Docker
y presión comparten `guardia-state.json`; el formato antiguo de U11 se migra
conservando sus pausas.

Para fijar otra ruta de Docker de forma persistente, un drop-in de **ambos**
servicios `pueued.service` y `pueue-desk-guard.service` debe incluir
`Environment=PAO_DOCKER_BIN=/ruta/docker`; `host.py` y `control.py` la leen.

Ver la fila y por qué está pausada:

```bash
~/.local/bin/pueue status -g hasito
tail -f ~/.local/state/pao/pueue-desk/guardia.log
cat ~/.local/state/pao/pueue-desk/guardia-state.json
journalctl --user -u pueued -u pueue-desk-guard
```

El log JSONL incluye UTC, motivo, lecturas y acciones; rota a `.1` a 1 MiB.
`owned` enumera pausas propias del guardia; los grupos Paused fuera de esa lista
son manuales. El registro `recuperacion_rechazada` incluye id y motivo del fallo.

Pausa manual, incluso si el guardia ya lo había pausado:

```bash
/usr/bin/python3 -I ~/.local/lib/pao-pueue-desk/control.py pausar hasito
```

Este comando retira la propiedad del guardia bajo el mismo candado. Una segunda
pausa directa de Pueue no expresa ese relevo. Para **forzar la reanudación**:

```bash
/usr/bin/python3 -I ~/.local/lib/pao-pueue-desk/control.py reanudar hasito
```

La fuerza es inmediata; el próximo ciclo volverá a pausar si falta Docker o hay
presión alta. En la Mini añadir `--host mini --pueue-bin /opt/homebrew/bin/pueue`
y usar `suites`. Una pausa manual nunca se levanta sola.

## Servicios e instalación pendiente

### Desk (sin ejecutar en este encargo)

Desde la MacBook, `fleet/pueue-desk/deploy-desk.sh --seco` comprueba sin escribir;
sin `--seco` instala por `ssh desk`. Requiere los binarios en
`/home/felix/.local/bin`, systemd de usuario y permiso para
`loginctl enable-linger felix`. El instalador pide linger antes de detener un
daemon manual y se niega a relevarlo con tareas vivas. No instala binarios.

`pueued.service` ahora ejecuta el supervisor con `Restart=always`, pausa de 5 s,
sin límite de intentos, `KillMode=mixed`, `SendSIGKILL=yes` y timeout de 30 s.
TERM llega primero al supervisor para capturar la parada; cuando éste termina,
systemd mata los procesos restantes del cgroup antes de la siguiente activación.
Así la recuperación del desk presupone el cgroup limpio; esto es **inferido de
la documentación**, no medido en WSL. La variante `on-failure` anterior no
reinicia tras SIGTERM/salida 0, por eso se usa `always`. Una parada solicitada a
systemd sigue siendo una parada explícita.

Guardia y modo conservan `Requires=pueued.service` y `After=pueued.service`.
El orden de cierre → daemon → recuperar → cuotas → guardia está dentro de un
único supervisor y candado; no depende de que Type=simple signifique «listo».
El timer de cuotas corre a 05:00Z y 14:00Z y recupera eventos perdidos. Un fallo
del servicio de cuotas reintenta cada 30 s; si falla la inicialización del host,
la cola queda cerrada y systemd reintenta el arranque completo.

Si ya había un servicio activo de U11, el instalador prepara los nuevos archivos
sin matar sus tareas. El supervisor entra en el siguiente reinicio del servicio;
para activarlo inmediatamente, esperar a que drene la cola y reiniciar el
servicio. Linger arranca el usuario al iniciar la distro; Windows debe iniciar
WSL por su mecanismo habitual. Docker Desktop puede tardar hasta el inicio de
sesión interactivo: mientras tanto la cola espera con motivo docker.

Retiro: `fleet/pueue-desk/withdraw-desk.sh [--seco]`; conserva cola, estado,
biblioteca y linger. Retirar con la cola drenada.

### Mini (artefactos entregados; no instalados)

En una copia del repositorio **en la Mini**:

```bash
fleet/pueue-desk/deploy-mini.sh --seco
fleet/pueue-desk/withdraw-mini.sh --seco
```

Sin `--seco`, `deploy-mini.sh` sustituye `sh.brew.pueue` por
[`io.pao.pueue.plist`](launchagents/io.pao.pueue.plist), con `KeepAlive`,
`RunAtLoad`, reintentos cada 5 s y el mismo supervisor/recuperar. Exige la cola
sin tareas vivas y verifica que coincide con el archivo de estado estándar.
Antes de sustituir el agente pausa **todos** los grupos con `pause --wait`,
repite el preflight y comprueba que siguen pausados. Si apareció una tarea,
aborta sin sustituir el agente y deja los grupos pausados para su revisión.
Las tareas que entren después esperan en cola. Las pausas que ya eran manuales
se conservan; `suites` vuelve a abrirse sólo si estaba abierto antes del relevo.
El modo `--seco` describe estos pasos y no pausa ni escribe.
Deshabilita el job de Brew y lo descarga con launchctl; no modifica su plist.
No volver a ejecutar `brew services start pueue` mientras se use este agente.
El retiro restaura la carga/habilitación de Brew que encontró el instalador,
y conserva cola, biblioteca y estado. Se admite repetir instalación y retiro.
Como todo LaunchAgent de GUI, empieza al iniciar sesión de ese usuario; no es
un LaunchDaemon previo al login. Registro del supervisor: `host.log` junto al
estado del guardia.

Ambos instaladores generan `pueue.yml` y `pueue-paths.json` en
`~/.local/lib/pao-pueue-desk`. Conservan las opciones existentes de daemon y
cliente. **Antes de cambiar servicios, ambos rechazan rutas shared explícitas
que difieran de las rutas administradas**, con el nombre del campo y un mensaje
que pide conservar el servicio actual: así el cliente sin `--config` no acaba
en otro socket o cola. Esto incluye `unix_socket_path`, `pueue_directory` y
`runtime_directory`, también al actualizar una instalación ya administrada.
Admiten JSON o el YAML por bloques emitido por Pueue; conservan las otras
opciones de shared y rechazan compresión y estructuras que no pueden interpretar. Las rutas predeterminadas son
`~/.local/share/pueue` en el desk y `~/Library/Application Support/pueue` en la
Mini; runtime usa XDG_RUNTIME_DIR cuando existe. Una configuración personalizada
no se adopta automáticamente ni se sobrescribe para hacerla encajar.

## Pruebas y límites

La validación R2 se lanza **en la Mini**, nunca en el desk ni en la MacBook:

```bash
~/.local/bin/remote-run start --name dirigida-u17-r2 --no-git -- \
  'bash fleet/pueue-desk/evidence/verificar-r2.sh && /usr/bin/python3 -I fleet/pueue-desk/evidence/diag-auditoria.py'
~/.local/bin/remote-run wait ID
```

El guion ejecuta la suite completa, `plutil -lint`, el ShellCheck exacto de
`.github/workflows/check.yml`, el ShellCheck de U17, `bin/pao check` y la
integración privada con Pueue real. No instala nada. Las regresiones se encuentran
en `tests/test_review_r2.py` y verifican los seis hallazgos:

| Hallazgo | Regresión principal |
| --- | --- |
| C1 | clean_stop en la Mini no demuestra que haya muerto una suite en otra sesión |
| W1 | rutas custom JSON/YAML en ambos hosts abortan antes de escribir; instaladores no sustituyen el servicio |
| W2 | SIGTERM directo recupera el mismo id una vez; kill manual por id/grupo/all y shutdown del cliente quedan intactos |
| W3 | caída antes de restart completa la intención; caída después sólo persiste Queued |
| W4 | timeout del guardia captura la tarea recién iniciada antes de terminar el daemon |
| W5 | pause --wait precede al relevo; una tarea que aparece en el preflight aborta el cambio |

Para demostrar el rojo sobre el producto anterior, preparar sólo sus tres
archivos de implementación y ejecutar los mismos tests nuevos:

```bash
mkdir -p .u17-baseline
for p in control host mini; do
  git show "8d4ed90f:fleet/pueue-desk/$p.py" > ".u17-baseline/$p.py"
done
~/.local/bin/remote-run start --name dirigida-u17-r2-base --no-git -- \
  '/usr/bin/python3 -I fleet/pueue-desk/evidence/verificar-base-r2.py'
~/.local/bin/remote-run wait ID
```

Corrida verde `20261112-023200-55c8e2`: **62 pruebas correctas**, plist OK,
ShellCheck CI + U17 código 0, `bin/pao check` válido e integración
`VALIDACIÓN U17 R2: OK`. Corrida de contraste `20261112-023242-e66a61`:
16 pruebas nuevas contra el producto anterior, 23 aserciones fallidas esperadas
(incluidos subcasos de W1), cero errores y los seis hallazgos en rojo.

El verificador exige que cada hallazgo produzca al menos una aserción fallida,
sin errores de importación o preparación. Resultado:
[`baseline-r2.txt`](evidence/baseline-r2.txt). Resultado verde con el producto
corregido: [`validacion-r2.txt`](evidence/validacion-r2.txt). Las evidencias de la
primera entrega se conservan como histórico; sus afirmaciones sobre clean_stop
y el fallo conservador de toda intención incompleta quedaron sustituidas por R2.

La integración privada prueba parada ordenada sin duplicación, suite en otra
sesión que sobrevive, SIGTERM directo con kill manual concurrente, recuperación
conservando id/datos bajo **boot anterior simulado**, segundo reinicio simulado
sin tercer intento, SIGKILL sin duplicar al hijo vivo y rechazo de un segundo
daemon tras morir sólo el supervisor.

No medidos: reinicio real de Windows/WSL o de la Mini, servicios reales
systemd/launchd, Docker Desktop real y pérdida de energía. Ningún LaunchAgent,
servicio ni archivo de `~/.local` del usuario fue cambiado por estas pruebas.
