# Reserva host-side de Fleet, v1

Estado: propuesta aceptada por Fleet para diseño, no interfaz runtime disponible ni autorización de instalación.
Schema del contrato: `fleet/host-reservation@1`.
Propietario: Fleet (165dccf7). Implementador delegado: mini-carga (mcp:73237748).
Ruta de implementación exclusiva: `fleet/hostgate/`.
Tickets: SEN-3822, SEN-3825 y SEN-3830. Spec: SEN-3817.

Ante diferencias con los documentos de outputs del 2026-10-23, prevalecen este
contrato y los criterios adicionales de los tickets. Esos documentos son
antecedentes. Contención no prueba terminalidad; no existe una U3 separada.

El borrador v0 de Mini se acepta como base con las precisiones siguientes.
El diseño disputado fija la serialización y el mecanismo de IPC; cualquier cambio
de semántica requiere acuerdo de Fleet y Mini antes de implementar consumidores.
La revisión y aceptación corresponden a Fleet; PAO solicita el OK concreto de Felix.
Las dependencias de los tickets se conservan: diseño y fixtures pueden avanzar,
pero la integración espera los heads de sus prerrequisitos.

## Frontera y operaciones

La cola duradera, prioridad y selección de nodo pertenecen a Fleet. Hostgate es
la única autoridad de capacidad del equipo físico: no implementa otra cola FIFO.
`reservar` concede o rechaza al momento, con motivo consultable. Un trabajo ya
aceptado puede seguir en cola Fleet aunque caduque su intento pendiente de admisión.

| Operación | Datos mínimos | Resultado y regla |
| --- | --- | --- |
| reservar | operationId, jobId, attemptId, executorId, physicalHostId, demanda y recursos exclusivos, identidad autenticada del dueño host-side | granted con reservationId y generation, o rejected con motivo. Confirmación durable antes de lanzar. |
| latido | reservationId, generation, identidad del supervisor local, secuencia | Sólo su dueño vigente renueva. Un cliente remoto no suplanta al supervisor. |
| dudosa | reserva y generación, motivo, evidencia disponible | Retiene capacidad; no habilita otra carga. |
| soltar | reserva y generación, identidad autorizada y evidencia positiva de terminalidad/limpieza | Repetición converge. Cierre de generación antigua jamás afecta a la nueva. |
| estado | identidad de consulta autorizada y reserva/equipo | Observa reserva, recursos, generación, dueño y motivos de rechazo, incertidumbre, drenado o degradación. Leer no reabre ni libera. |

`operationId` identifica una operación repetible, no otorga permisos.
`leaseId`, etiquetas y referencias declaradas no dan autoridad.
Cada reserva acredita trabajo, intento, ejecutor, equipo físico y generación.
Capacidad, cores/vCPU y límites efectivos proceden de configuración del nodo,
nunca de argumentos/variables del cliente; la demanda del trabajo sí es declarativa.
Windows y WSL comparten el physicalHostId FELIX-DESKTOP y presupuesto físico.
Los identificadores de Mini y demás nodos se leen de configuración comprobada,
sin ramas por nombre de máquina.

## Dueño, espera e incertidumbre

El supervisor host-side posee la reserva, su heartbeat y el cliente del launcher.
El cliente de usuario puede desconectarse sin matar ni duplicar la carga.
En Windows el cliente del launcher no se desacopla de ese supervisor dueño.
La cadencia/TTL son parámetros del host; su vencimiento produce duda, no terminalidad.

Un intento pendiente de admisión guarda PID+inicio+boot y latido del solicitante
host-side. Si muere de forma confirmada, o vence antes de existir carga/reserva
activa, se retira automáticamente sin bloquear a la cola Fleet o al turno legado
compartido durante la migración. Hostgate no añade otra cola. Nunca se borra a mano.
Un hueco entre reservar y crear la carga conserva duda salvo prueba de never-created.
La aceptación durable del trabajo y el turno de admisión son identidades distintas.

Sólo tree=empty o never-created libera una carga Windows contenida.
TTL+5s, disposición, raíz exit0, snapshot sano y SSH caído no prueban árbol vacío.
En Unix se acredita el cierre de toda la carga/recursos controlados: la ausencia o
exit de la raíz por sí sola no demuestra cierre de descendientes. Un contrato que
garantice un único proceso debe acreditarlo; cualquier hueco conserva duda.
PID reutilizado, identidad no verificable, boot diferente y supervisor perdido
conservan reserva y capacidad. No se limpian procesos ajenos ni se usa SIGSTOP.

## Salud y elección

Fleet consume el adaptador stdlib con fixtures entregado por Mini:
`evaluar(muestra, ahora, boot_actual) -> motivos` para desk y Mini.
No duplica las reglas de sano. Incluye memoria y disco además de CPU.
Snapshot viejo, desconocido, fallido u otro boot es no sano, nunca cero.
Boot actual Windows exige un testigo independiente del snapshot, sin PowerShell,
CIM ni WMI. Dos campos del mismo archivo sólo prueban coherencia interna.
Un snapshot de otro arranque puede parecer fresco tras reiniciar. Hoy no existe
ese testigo: el desk es NO SANO y no elegible para admisión automática, tampoco
como opción degradada, hasta disponer de él.
La unidad Desktop `desk-boot-witness` está pending y bloquea SEN-3835; su aceptación
y evidencia son prerrequisitos de la integración nativa. No se presume disponible.

Nunca se lanza PowerShell/CIM/WMI para medir. Toda lectura, sonda o ejecución
hacia FELIX-DESKTOP usa OpenSSH nativo Windows, ProxyCommand=none.
Prohibidos el alias desk y la lectura /mnt/c vía WSL.
powershell.exe y wsl.exe de larga vida son observaciones, sin veto automático,
atribución de orfandad ni autorización para matar. nativos=[] sigue vigente.

Selección: descartar riesgo y elegir por carga Unix/cores entre elegibles;
cpuRootPct puede descalificar según el lector, no sustituye carga Unix.
Sin sanos se avisa y se marca la opción menos mala como degradada.
Esa clasificación no sobrepasa drenado, incompatibilidad, identidad inválida,
boot no acreditado, salud ilegible, reservas dudosas o presupuesto.
Si faltan datos para ordenar, se explica.
Sólo Felix levanta el veto auto. Disponibilidad y heads fusionados SEN-3822/3825
se comunican a PAO, quien avisa a proyectos.

## Registro de dueño y migración

Cada corrida Mini publica un registro versionado con runId/jobId/attemptId,
reservationId/generation, host/boot, PID e inicio exactos, started y fin.
El diseño acuerda con U1 su lectura y escritura atómica antes de activar.
Puede usar runs/<id>/{pid,exit} compatible con U1 o un equivalente versionado
aceptado por Mini. U1 conserva su regla: ante duda protege.
Un marcador informativo de raíz no sustituye prueba de árbol vacío en admisión.
La ruta host-side de estado se declara en configuración y es distinta del piloto
temporal, del estado del instalador PAO y de copias/worktrees autoactualizables.

Mientras convivan run.sh/mini.lock y Fleet se comparte el mismo turno o el runner
legado pide Fleet; nunca ambos admiten por separado. Secuencia: sombra, bloqueo,
adaptador. Cada activación exige PR, contrato acordado y OK concreto de Felix.
Fleet retiene sus checkouts y artefactos; U2 sólo limpia legados.

SEN/Jale conserva o absorbe sus slots /tmp/sen-test-slots (dos por host legado),
sen-test-db-$slot, puertos y BD por corrida sin segundo presupuesto.
Postgres compartidos 5434/5435 son recursos explícitos cuando correspondan.
Reiniciar WSL puede borrar estado efímero de varios proyectos: mantenimiento
drena el equipo y reconcilia reservas y recursos antes de reapertura explícita.
La desaparición de slots o BD no prueba cierre ni reutilización segura.
Las 12 vCPU WSL no son 12 cores físicos adicionales. No se presupone Jale migrado.

## Pruebas y aceptación

CLI público real con varios clientes y ejecutores aislados: presupuesto conjunto,
operaciones repetidas, generaciones antiguas, cliente muerto en start, reconexión,
turno huérfano, PID reutilizado, heartbeat vencido, tree=unconfirmed y boot nuevo.
Lectores con snapshots inválidos/viejos, null, presión RAM/disco, drenado y
degradación visible; consultar jamás reabre. Sin sondas PowerShell ni ssh desk.
Smokes reales sólo acotados y coordinados por plataforma después de prerrequisitos.
MacBook edita/inspecciona; runtime y gates en Mini bajo run.sh con turno.
No instalación, restart WSL, config de host o cargas ajenas por este contrato.

Aceptación semántica Fleet: sí, con estas correcciones al v0.
Aceptación semántica Mini: pendiente.
Serialización, diseño y evidencia runtime: pendientes.

