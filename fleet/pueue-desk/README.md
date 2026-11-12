# Pueue en el desk y la Mini: cuotas, guardia y reinicios

Pueue **4.0.4** con supervisor del host. El desk usa systemd de usuario en WSL;
la Mini, un LaunchAgent propio. Esta entrega **no instala nada**: el desk no se
contactó y todas las mediciones y pruebas se ejecutaron mediante `remote-run`
en copias privadas de la Mini, con configuración, cola y socket propios.

## Qué sobrevive

- Las tareas `Queued` conservan id, comando, directorio, grupo y entorno.
- El host captura una tarea `Running`/`Paused` interrumpida y la devuelve a
  `Queued` mediante `restart --in-place`, **una sola vez por identidad de tarea**.
  Un segundo reinicio la deja `Done/Killed`, con el motivo en `guardia.log` y
  `guardia-state.json`. Un fallo ordinario o un `Killed` anterior sin captura
  de interrupción no se reintenta.
- Los grupos y sus cuotas persistidas sobreviven. Las cuotas del desk se
  vuelven a aplicar según UTC; la Mini vuelve a `suites=3`.
- Las pausas manuales siguen pausadas. Las pausas de arranque, Docker o presión
  pertenecen al mismo guardia y sólo éste las levanta al estar listo el host.

**Excepción medida en macOS:** SIGKILL de `pueued` deja vivos sus hijos. En el
mismo arranque del sistema, la Mini deja esa tarea fallida y explica
«No se descartan hijos vivos…»; no la duplica. Una parada ordenada del supervisor
mata los hijos en la medición; un cambio de `kern.boottime` acredita un reinicio
completo y permite recuperar. No se promete contener programas que se separen
del grupo de procesos de Pueue.

## Mediciones antes de implementar

Corrida inicial `20261112-011702-585cb7`; confirmación ampliada
`20261112-012401-748e96`. Binarios existentes `/opt/homebrew/bin/pueue` y
`pueued`, ambos 4.0.4. Script reproducible:
[`evidence/diag-reinicio.py`](evidence/diag-reinicio.py). Todos sus recursos
están dentro del directorio de la corrida; el socket relativo `s` queda dentro
de cada instancia privada para respetar el límite de longitud de macOS.

| Caso | Medido en la Mini | Evidencia, líneas de `diag-reinicio-durabilidad.txt` |
| --- | --- | --- |
| a. SIGTERM y nuevo arranque | Daemon sale 0, hijo muerto; Running pasa a Done/Killed; Queued mantiene id. El grupo con cola queda Paused. | [3–5](evidence/diag-reinicio-durabilidad.txt#L3) |
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
   capturar las identidades Running/Paused y marcar todos los grupos Paused en
   disco. También cierra grupos vacíos y tareas diferidas: el auto-pause de Pueue
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
del único `restart --in-place`, fuerza el guardado con `pause --wait` y confirma
el mismo id en disco antes de abrir la admisión. Si la recuperación misma muere
en la ventana entre intención y confirmación, no repite un restart incierto:
conserva el fallo y su motivo. El historial no se elimina al limpiar tareas de
Pueue; `created_at` distingue un id reutilizado.

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
antes de mandar SIGTERM al daemon. Mandar SIGTERM directamente al hijo `pueued`
produce un `Done/Killed` indistinguible de otras muertes por señal; sin captura
positiva se conserva ese fallo. Un SIGKILL abrupto sí deja Running en disco.
Si se mata sólo el supervisor de la Mini y el daemon sigue vivo, el nuevo
supervisor se niega a editar la cola o crear otro daemon hasta que termine.

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
Deshabilita el job de Brew y lo descarga con launchctl; no modifica su plist.
No volver a ejecutar `brew services start pueue` mientras se use este agente.
El retiro restaura la carga/habilitación de Brew que encontró el instalador,
y conserva cola, biblioteca y estado. Se admite repetir instalación y retiro.
Como todo LaunchAgent de GUI, empieza al iniciar sesión de ese usuario; no es
un LaunchDaemon previo al login. Registro del supervisor: `host.log` junto al
estado del guardia.

Ambos instaladores generan `pueue.yml` y `pueue-paths.json` en
`~/.local/lib/pao-pueue-desk`. Conservan las opciones existentes de daemon y
cliente y fijan las rutas estándar compartidas por el cliente normal. Admiten
JSON o el YAML por bloques emitido por Pueue; rechazan compresión y estructuras
que no pueden conservar sin interpretarlas. Las rutas predeterminadas son
`~/.local/share/pueue` en el desk y `~/Library/Application Support/pueue` en la
Mini; runtime usa XDG_RUNTIME_DIR cuando existe. Una cola con rutas personalizadas
requiere preparar y revisar una configuración explícita antes de adoptarla.

## Pruebas y límites

La validación completa se lanza **en la Mini**, nunca en el desk ni en la MacBook:

```bash
~/.local/bin/remote-run start --name dirigida-u17-validacion-final --no-git -- \
  bash fleet/pueue-desk/evidence/verificar.sh
~/.local/bin/remote-run wait ID
```

El guion ejecuta los comandos exigidos:

```bash
/usr/bin/python3 -I -m unittest discover -s fleet/pueue-desk/tests -v
plutil -lint fleet/pueue-desk/launchagents/io.pao.pueue.plist
shellcheck --severity=warning fleet/pueue-desk/*.sh fleet/pueue-desk/evidence/*.sh
/usr/bin/python3 -I fleet/pueue-desk/evidence/verificar-supervisor.py
```

Los dobles cubren recuperación única, segundo reinicio fallido, fallos ordinarios
intactos, idempotencia, id reutilizado, escritura incierta, huérfanos en macOS,
Docker con histéresis y pausas manuales, instalación/retiro y sus modos secos.
La integración privada adicional prueba el supervisor real sin instalarlo:
parada ordenada, recuperación preservando id/datos, cola que corre una sola vez,
segundo reinicio sin tercer intento, SIGKILL sin duplicar al hijo vivo y rechazo
de un segundo daemon tras morir sólo el supervisor.

Corrida final `20261112-014010-adfc0e`: **45 pruebas correctas**, plist OK,
ShellCheck código 0 e integración real `VALIDACIÓN U17: OK`.
Salida completa: [`evidence/validacion.txt`](evidence/validacion.txt), líneas
49–51 y 76–88. La corrida ampliada anterior conserva un fallo de fixture en
[`validacion-primer-config.txt`](evidence/validacion-primer-config.txt): APFS
trató `library` y `Library` como la misma carpeta. Se corrigió el nombre del
fixture y la corrida final pasó; no fue un cambio para ocultar un fallo del
producto.
No medidos: reinicio real de Windows/WSL o de la Mini, servicios reales
systemd/launchd, Docker Desktop real y pérdida de energía. Ningún LaunchAgent,
servicio ni archivo de `~/.local` del usuario fue cambiado por estas pruebas.
