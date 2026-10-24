# Fleet local pilot — SEN-3819

CLI opt-in, Python **3.9+**, biblioteca estándar, macOS/Linux POSIX. Un slot y una cola
FIFO por directorio de estado propio. No requiere perfil del proyecto ni instalación.
No se integra con `pao`, Mini, desktop ni servicios activos; este slot sólo aísla el
piloto. Fleet será posteriormente la autoridad host-side bajo un contrato separado.

```sh
export FLEET_STATE_DIR=$(mktemp -d)
fleet/bin/fleet submit --client demo --cwd /ruta/proyecto -- python3 -c 'print(42)'
# {"job":"j_...","attempt":"a_...","client":"demo","accepted":true,"state":"starting"}
fleet/bin/fleet status j_...
fleet/bin/fleet wait j_... --timeout 30
```

`submit` acepta argv literal, sin shell, stdin cerrado, cwd explícito (por defecto el
actual) y una copia del entorno del emisor guardada por job junto a argv/cwd, tanto si
arranca inmediatamente como si espera en cola. Incluye `PATH`, que se usa para resolver
el ejecutable. Para usar un shell debe pedirse explícitamente como argv.
El supervisor y el comando arrancan en sesiones separadas del cliente. El supervisor
ignora SIGINT/SIGHUP; el comando hereda esas disposiciones. No hay cancelación,
timeout de ejecución ni interrupción automática. `wait --timeout` limita sólo la lectura.

`--state-dir` puede ir antes o después del subcomando. Precedencia: opción explícita,
`FLEET_STATE_DIR`, `${XDG_STATE_HOME:-$HOME/.local/state}/fleet-pilot`. El directorio
debe estar vacío o pertenecer al piloto, ser del uid actual y no tener bits de permisos
para grupo/otros. Un directorio preexistente inseguro se rechaza sin cambiar sus
permisos; una BD existente debe pertenecer al uid actual. Se rechazan directorios
reservados `pao`, `mini-carga` y `u3` bajo las raíces de estado, y symlinks de
estado/BD/attempts.
No apuntarlo a estado compartido, ajeno, remoto o a un filesystem sin locks SQLite.

`--client` > `FLEET_CLIENT` > `user@host`. Es una etiqueta declarada, sin autoridad.
`submitted_by` contiene por separado usuario OS, uid, host y PID del cliente.
`supervisor` contiene PID, host, identidad de boot e inicio del proceso. Identidad
ausente, ilegible, zombie o distinta deriva en `view=uncertain`, nunca capacidad libre.

Cada respuesta operativa es un objeto JSON en una línea de stdout; errores también
usan `{"error":"..."}`. `--help` es ayuda humana de argparse. Identidades opacas
`job`/`attempt` permiten consultar la misma ejecución después de cerrar el cliente.
`status`/`wait` incluyen argv, cwd, client, submitted_by, supervisor, timestamps,
`state`, `view`, `outcome`, `exit_code`, `signal`, rutas de logs y `transitions`.
Cada transición tiene secuencia global, job, attempt, origen, destino, emisor y fecha.
`state` en la aceptación es una instantánea; un comando corto puede haber terminado
cuando se lea stdout. La aceptación no espera su finalización.

| Exit | Contrato |
| --- | --- |
| 0 | `submit`: aceptado; `status`: lectura válida, incluso incierta; `wait`: succeeded |
| 1 | `submit`: no se pudo lanzar supervisor, accepted=false, identidad persistida y failed; `wait`: failed |
| 2 | Uso inválido, job desconocido o estado ilegible; JSON error |
| 3 | `submit`: no se confirmó readiness, accepted=false, identidad persistida e incertidumbre; `wait`: incomplete o uncertain |
| 4 | `wait --timeout` agotado, timed_out=true; no cambia la ejecución |
| 130 | Lector interrumpido con SIGINT; la ejecución sigue independiente |

El punto de entrada público maneja SIGINT durante la importación del CLI y su
ejecución con el mismo JSON de interrupción y código 130.

`succeeded`: exit 0 y logs sincronizados/cerrados. `failed`: exit positivo o error
inequívoco de spawn. `incomplete`: señal o fallo de logs. No se confunde SIGKILL con
un exit positivo 137. Si desaparece un log después del fin, la lectura reporta
`incomplete` sin modificar el resultado persistido.

Layout privado: `state.db` (SQLite WAL), `attempts/<attempt>/{stdout,stderr}` y
`supervisor.log`. Un solo intento por job. Las transacciones usan `BEGIN IMMEDIATE`;
una tabla de transiciones legales y un único escritor de estados validan
`queued → starting → running → finished` o `starting → finished` por fallo de spawn.
La creación registra `null → queued` con el cliente como emisor. El supervisor
termina, libera y reclama el siguiente FIFO en la misma transacción; no hay daemon
residente. Las fechas sirven de observación; FIFO se decide por secuencia de inserción.

El esquema usa `user_version=2`; una BD del piloto con otra versión se rechaza en
submit, lecturas y supervisor. No hay migrador ni fallback al entorno del supervisor.
El entorno persistido puede contener secretos: queda bajo la privacidad del directorio,
sin cifrado ni aislamiento frente al mismo uid. No se incluye en respuestas de control,
transiciones ni logs del supervisor. El comando sí puede escribir su entorno en sus logs.

La aceptación de un nuevo supervisor requiere exec exitoso y un handshake después
de persistir su identidad (hasta diez segundos). Un fallo inequívoco de lanzamiento
queda como failed y no se comunica como aceptación. Una caída entre persistencia,
exec y handshake conserva el slot. Consultar las identidades devueltas; **no reenviar**
automáticamente: no hay idempotencia ni recovery. Los trabajos detrás de un dueño
incierto siguen queued y `wait` retorna 3. Los lectores nunca liberan ni reencolan.

Se espera que el comando sea compatible con supervisión local: no debe daemonizar,
cambiar de sesión ni mover descendientes fuera de su grupo. Si queda un descendiente
en el grupo original, se conserva el slot y se informa incertidumbre hasta que ese
grupo quede vacío. Un proceso que escapa del grupo no puede verificarse en este
piloto: no usar esos comandos ni activar el piloto como autoridad real. Matar el
supervisor no demuestra fin del comando: conserva capacidad incluso tras su autoexit.
Un error al persistir el resultado también conserva capacidad; no existe reconciliación.

Sin prioridad, cancelación, nodos, SSH, snapshots, leases, recovery ni autoridad host-side
activa. Guardar el entorno no congela ejecutables ni contenido del cwd: no hay snapshot
inmutable del runtime ni aislamiento de seguridad. No editar o actualizar el runtime
mientras haya supervisores activos.
Git Bash/Windows no está soportado en este piloto.

## Validación exclusivamente en Mini

MacBook: sólo edición e inspección; no ejecutar este CLI, tests, compilación ni gates.
En un checkout de Mini preparado por el coordinador, dentro de su turno liviano:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s fleet/tests -p 'test_cli.py' -v
```

La suite usa únicamente subprocess del CLI público; no importa Fleet, abre SQL ni
inspecciona tablas privadas. HOME, XDG_STATE_HOME, estado y fixtures son temporales
y propios; ninguna dependencia se instala. Las barreras tienen autoexit de seis
segundos. Prueba aceptación antes de soltar la barrera, resultados, señales, FIFO,
admisión concurrente, lectores interrumpidos, aislamiento y supervisor perdido.
Comprueba entornos y resolución por PATH distintos con el segundo job en cola,
retención por un descendiente del mismo grupo tras salir la raíz y rechazo sin cambios
de un directorio público. La versión de esquema se inspecciona estáticamente; no hay
una prueba runtime que construya una BD privada.
Sólo envía señales a procesos creados por sus propias fixtures. La prueba de fallo
de lanzamiento provoca un error abriendo el log privado, antes de crear supervisor.
El harness registra cada admisión antes de lanzar submit, también en llamadas directas
y wrappers. Una aceptación real guardada sin atribuir prueba que el guard de limpieza
falla conservando el temporal; después recupera esa respuesta pública y acredita cierre.
Timeout, JSON ilegible o pérdida de stdout dejan la admisión pendiente. Un error JSON2
no prueba ausencia de efectos: sólo se refuta creación en un estado nuevo/exclusivo si
su BD no existía antes y sigue ausente con el cliente terminado, sin borrado intermedio.
La limpieza exige todas las admisiones resueltas y ausencia de supervisores y grupos
propios publicados por el CLI, observada con señal 0; si no puede confirmarla, falla
y conserva el directorio temporal.
No está conectada a CI. El coordinador debe guardar resultado y review del diff;
esta entrega no declara ejecución ni aprobación de pruebas.
