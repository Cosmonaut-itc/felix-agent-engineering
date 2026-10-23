# Una cola de ejecución para los proyectos y agentes de Felix

Investigación del 23 de octubre de 2026. Incluye incorporación de nodos,
prioridades y pausa de pruebas. Es una propuesta, no una implementación.

## Recomendación

Crear `fleet/` dentro de este repositorio, Felix Agent Engineering, cuyo
checkout conserva el nombre `personal-agent-orchestration`. Dar al módulo
una interfaz, protocolo y despliegue propios. No crear otro repositorio ahora.

El objetivo es que cualquier proyecto o agente pueda enviar un trabajo por
un comando común. Los proyectos conservan sus gates y preparación de servicios.
Fleet decide cuándo y dónde ejecutarlos y conserva su estado y evidencia.

PAO ya distribuye configuración en macOS, Linux y Windows y tiene código
operativo, incluido el monitor de Windows [S1, S2]. Extenderlo reduce los lugares
que hay que mantener. Esa distribución todavía no instala los runners ni los
monitores como componentes administrados [S3].

| Opción | Ventaja | Costo | Juicio |
|---|---|---|---|
| Módulo en PAO | Un lugar para contratos, ejecutores, pruebas e instalación | Separar las actualizaciones del runtime de las de skills | Recomendada para el entorno actual |
| Repositorio nuevo | Propiedad y releases independientes desde el inicio | Coordinar versiones, integración e instalación con PAO | Conviene si otros equipos lo adoptan o necesita mantenimiento independiente |

La separación de releases puede existir dentro del mismo repositorio. Extraer
el módulo después requerirá menos cambios si los clientes usan un protocolo
versionado y no importan sus archivos internos.

## Qué existe y dónde

La lectura de código se distingue de lo reportado por otras sesiones. No se
hicieron conexiones SSH, pruebas de carga ni cambios a los equipos.

| Pieza | Estado observado | Ubicación |
|---|---|---|
| Distribución PAO | Implementada. Actualización cada cuatro horas, fast-forward de `origin/main`, enlaces, respaldos y detección de archivos locales | `README.md:184`, `bin/pao:469` [S1] |
| Protección de la MacBook | Hook de advertencia. Permite continuar; no impone una cola | `skills/felix-mode/scripts/laptop-guard:1` [S4] |
| Candado y FIFO de comprador | Implementados. Turnos persistidos con mutex, TTL de pendientes, renovación y cancelación ligada al dueño y la corrida | `comprador./scripts/mini/estado.py:228` [S5] |
| Solicitudes de gate | Registro y reclamación por identidad, SHA y huella del comando; deduplicación opcional | `comprador./scripts/mini/solicitudes.py`, README del área [S6] |
| Evidencia de ejecución | Bundle ligado a SHA, guion literal, logs, recibo y validación de terminalidad/reportes | `comprador./scripts/mini/run.py:44` y `remote-run` [S7] |
| Selección Mini/desktop | Implementada en apotheca y en el binario local. Compara carga/núcleos y, en WSL, consulta CPU Windows | `apotheca/scripts/remote-run:87`, `~/.local/bin/remote-run:82` [S8] |
| Slots de SEN-CheckIn | Checkout propio, BD y puertos por slot para contract/e2e; espera con reintentos. No garantiza FIFO | `scripts/test-hosts/run-suite.sh:23`, `:64`, `:92` [S9] |
| Unión entre proyectos | Un wrapper local de SEN usa el candado de comprador. No está versionado | `SEN-CheckIn/scratchpad/test-hosts/gates-locked.sh:6` [S10] |
| Vigilante de memoria Mini | Rama `feat/vigilante-memoria-mini`, SHA `db73d4c33`. Modo sombra; no termina procesos por defecto. No está en el `main` inspeccionado | `comprador.-vigilante-memoria/scripts/mini/README.md:574` [S11] |
| Monitor Windows | Mergeado en PAO, SHA `a6c1010`, PR #16. Snapshot, umbrales, avisos, mutex y límites de historial. Sólo observa | `scripts/desktop/README.md`, `umbrales.json` [S2] |
| Admisión común y limpieza | U2 y U3 siguen pendientes en el registro del programa. Heartbeat y admisión por host, `nice`/`ionice` y lectura del snapshot son alcance futuro | Store `mini-carga` y contrato U3 [S12] |
| Contención nativa Windows | Launcher con Job Object y detección de pérdida del cliente, más conteo de PowerShell de larga vida, en diseño | Store `desktop-carga` e hilo de protección Windows [S13] |

Los checkouts inspeccionados fueron PAO `a6c1010`, comprador `main`
`bdc436194`, su rama vigilante `db73d4c33`, apotheca `main` `dfcc2e30`
y SEN-CheckIn `codex/sen-3074-3078-barda` `812d0d573`.
El runner de SEN inspeccionado difiere de su `main`; no se acredita como
desplegado. El código de la rama vigilante tampoco acredita una instalación.

## Qué impide que hoy sea plug and play

- El candado de la Mini consultado vive en la MacBook. No coordina por sí solo
  clientes en otras computadoras. La admisión global por host sigue pendiente [S12].
- Hay deriva de versiones. El `remote-run` versionado de comprador usa host fijo;
  el archivo instalado en `~/.local/bin` tiene otro SHA-256 y agrega `auto`.
  Apotheca conserva otra implementación con cambios propios [S7, S8].
- Hay deriva de rutas. El `main` de comprador conserva `.comprador-jarvis/mini.lock`;
  la rama vigilante migra a `~/.local/state/comprador/mini/mini.lock` y conserva
  compatibilidad con versiones anteriores. Copiar sólo un archivo dividiría la exclusión [S5, S11].
- Los slots de SEN aíslan sus propias corridas. No son un presupuesto común
  con apotheca. Además, `MAX_SLOTS` se acepta del cliente [S9].
- Las suites unit y estáticas de SEN también toman slot, pero `api-unit` puede
  usar la BD compartida de 5435. Tener checkouts distintos no aísla esa BD [S9].
- Los documentos de apotheca indican `auto`, pero las sesiones de operación
  reportaron desktop abierto **sin auto** a las 22:00 UTC. La consulta PowerShell
  desde WSL puede sobrevivir al timeout del cliente; U3 propone leer el snapshot
  ya producido. `remote-run hosts` también ejecuta la consulta actual [S8, S12, S13].
- No basta ordenar por carga. La app arm64 de apotheca no funciona en los AVD
  x86 del desktop. Windows y WSL comparten la misma máquina física [S14].

El monitor Windows está instalado y tomando muestras según su sesión de
operación. Esa sesión también reportó WSL con 12 vCPU después de la ventana
coordinada. No se verificó de nuevo aquí. Su tarea actual depende de una sesión
interactiva abierta. Los documentos que aún dicen 16 núcleos no deben alimentar
el scheduler como si fueran capacidad descubierta [S13, S14].

## Contrato propuesto para entrar a la cola

Un CLI común, independiente del agente, con salida JSON y códigos de salida
estables. Estos comandos son ejemplos propuestos y todavía no existen.

```sh
pao fleet submit --priority normal --tag review -- pnpm test
pao fleet submit --profile gate --priority urgent
pao fleet status --json
pao fleet pause <job-id>
pao fleet resume <job-id>
pao fleet cancel <job-id>
pao fleet wait <job-id>
```

El primer comando funciona sin configuración del proyecto si el nodo tiene
las herramientas requeridas. Un archivo opcional `.pao/jobs.json` define
perfiles con etapas, recursos y preparación específica. El CLI empaqueta
una instantánea del directorio de trabajo y registra SHA y huella del contenido,
incluidos cambios locales cuando se admitan. No supone que el SHA describa
archivos sin commit.

El trabajo lleva `schemaVersion`, `jobId`, `idempotencyKey`, proyecto, dueño,
referencia a la instantánea, comando como argv, requisitos, recursos, prioridad,
tags y política de interrupción. El recibo conserva nodo, versión del ejecutor,
intentos, logs, resultado y artefactos. Repetir el envío con la misma clave
no crea otro trabajo.

Los adaptadores de T3, Codex y Claude llaman a este mismo contrato. No necesitan
una cola por agente. Un adaptador MCP puede añadirse después sin cambiar la
semántica. El acceso remoto identifica al emisor y limita proyectos y prioridades;
no se publica un ejecutor de shell anónimo.

## Modelo de nodos y admisión

Un registro declarativo mantiene un archivo por equipo. Declara identidad
estable, transporte, ejecutores, SO, arquitectura, capacidades y límites.
Las credenciales quedan fuera del repositorio. Las rutas y el usuario se
resuelven en la instalación local.

La incorporación propuesta es `enroll`, comprobación de capacidades y una
corrida de prueba. Después el nodo aparece entre los candidatos. Añadir otra
computadora no exige editar `if mini / else desktop` en cada proyecto.

Una cola duradera común mantiene prioridad, estado de trabajos y asignaciones.
Puede empezar con un coordinador y SQLite local en ese coordinador, accesible
por SSH. No compartir el archivo SQLite por red ni sincronizarlo con Git.
Esto agrega un punto de disponibilidad conocido, sin requerir Kubernetes,
Redis ni otro servicio de infraestructura desde el inicio.

Cada nodo confirma la admisión y mantiene un supervisor local. Todos los
proyectos usan esa autoridad para reservar memoria, CPU, slots, puertos,
BD compartidas o dispositivos exclusivos. La elección del coordinador es
una propuesta hasta recibir esa confirmación.

Windows y WSL son ejecutores de un mismo equipo con presupuesto compartido.
La primera integración puede admitir pruebas Unix y observar la carga nativa;
admitir cargas Windows requiere integrar su launcher y contrato propios.
Requisitos como `ios`, `android-arm64` o `docker` se filtran antes de comparar carga.

La salud se obtiene de snapshots con fecha, versión y boot verificables.
Consultar salud no debe arrancar diagnósticos costosos ni despertar un host
drenado. El alias actual `ssh desk` ejecuta un helper de arranque; observación
y activación necesitan operaciones distintas [S14].

Un heartbeat vencido pasa la asignación a estado dudoso. No libera
automáticamente la capacidad ni autoriza ejecutar otra copia. El supervisor
debe acreditar terminalidad o contener su propia carga antes de reasignarla.
La generación de la reserva impide que un dueño anterior libere la nueva.

## Prioridades, tags y pausa de pruebas

La prioridad es un campo validado con valores iniciales `urgent`, `high`,
`normal` y `background`. Los tags como `review`, `e2e` o `proyecto:jale`
sirven para filtrar. Si una etiqueta eleva prioridad, debe hacerlo mediante
una regla central explícita, no por texto libre interpretado por cada agente.

El scheduler elige el trabajo elegible de mayor prioridad. Dentro de una
clase y cuota conserva FIFO. El envejecimiento de pendientes, las cuotas
por proyecto y los límites de urgentes evitan que una ráfaga deje sin servicio
a los demás. Elevar prioridad tiene una política por emisor; una petición
fuera de permiso se rechaza con su motivo. No requiere aprobación humana
en cada envío ordinario.

Adelantar pendientes y desplazar trabajos en ejecución son operaciones distintas.
La política de interrupción se declara en el perfil y queda en el recibo.

| Política | Qué ocurre ante una tarea urgente | Capacidad que puede liberarse |
|---|---|---|
| `never`, por defecto | El trabajo actual continúa; el urgente adelanta pendientes | Ninguna del trabajo actual |
| `checkpoint` | Termina la etapa actual, guarda avance, cierra recursos propios y queda pausado | Sólo los recursos que el ejecutor confirme liberados |
| `restartable` | Cancela su intento, confirma cleanup y lo deja pendiente para repetir | Los recursos liberados después del cierre |

`pause` responde primero con una solicitud de pausa. Sólo cambia a `paused`
cuando el ejecutor confirma un punto seguro. Si el trabajo no lo soporta,
responde que no puede pausarse. `resume` vuelve a someter las etapas restantes
a admisión; no ejecuta saltándose la cola.

No usar `SIGSTOP` como pausa general. Conserva RAM, conexiones y candados,
y los timeouts externos siguen corriendo. Si se incorpora suspensión del SO
para un ejecutor concreto, éste sigue reservando esos recursos y debe probar
que sus servicios admiten la suspensión.

Una suite dividida en etapas o shards permite pausar entre ellos. Un test
individual largo sin soporte cooperativo puede continuar hasta terminar;
repetirlo requiere optar por `restartable`. La reanudación conserva la misma
instantánea y entorno, o invalida el avance que ya no se pueda reutilizar.
QA interactiva y trabajos con efectos externos empiezan como no interrumpibles.

La prioridad nunca desactiva los safeguards. Un urgente no puede terminar
procesos ajenos, ignorar una arquitectura incompatible ni arrancar en un
nodo puesto en mantenimiento. El fallback degradado ya acordado para U3
debe ser visible y distinguir sobrecarga recuperable de prohibiciones,
incompatibilidad y estado ilegible [S12].

## Salvaguardas que hay que preservar

- Propiedad por corrida, host, namespace, boot y hora de creación; un PID solo
  no identifica una carga. Nunca traducir un PID de WSL para terminar Windows [S2].
- Protección de cargas vivas o dudosas, linaje validado y modo sombra del
  vigilante Mini. El modo que termina procesos necesita una entrega aparte [S11].
- Instantánea verificable, guion literal, resultado remoto acreditado y
  conservación de evidencia cuando cae el transporte [S7, S8].
- Aislamiento de checkout, BD y puertos. Serializar también una BD compartida
  si el proyecto no puede crear una por corrida [S9].
- Salud desconocida no equivale a cero carga. Snapshot vencido, `unknown` y
  error de persistencia tienen semántica explícita. La latencia `idle` válida
  y un historial `capped` no son por sí solos fallos de captura [S2, S12].
- El monitor Windows observa. El futuro Job Object controla únicamente hijos
  que cree su launcher. La limpieza nativa general sigue deshabilitada [S13].

## Cómo distribuir mejoras sin cambiar corridas activas

Usar PAO para instalar y descubrir releases de fleet. El runtime se instala
en directorios de versión inmutables; cada corrida conserva la versión con
la que empezó. El auto-update actual de skills no debe sustituir el código
de un supervisor en uso.

Desplegar primero a un nodo de prueba. Verificar contrato, protocolo y salud;
después drenar los demás, esperar sus corridas y activar la versión. Conservar
la última versión funcional y una estrategia compatible para el estado
persistido. Un rollback de binarios no revierte por sí solo una migración de datos.

Un estado común debe mostrar versión deseada y real por nodo, salud, cola,
recursos reservados y motivo de bloqueo. Los proyectos consumen el CLI instalado;
sus wrappers no conservan copias editables del scheduler.

## Migración recomendada

1. Consolidar este inventario y los contratos en `fleet/`. Fijar los esquemas,
   la política de prioridades y qué parte del estado pertenece a cada componente.
2. Reconciliar las variantes de `remote-run` mediante pruebas de contrato.
   Incorporar el mejor comportamiento de cada una; no sobrescribir una con otra.
   Entregar un CLI y una versión instalable sin cambiar corridas activas.
3. Introducir la cola común y admisión por host con un solo proyecto piloto.
   Probar dos clientes simultáneos, desconexión, reinicio y reserva dudosa.
4. Migrar comprador, apotheca y SEN por adaptadores. Conservar sus gates y
   aislamiento. Usar un puente de transición para que clientes viejos y nuevos
   no puedan reservar el mismo recurso por autoridades independientes.
5. Aplicar prioridades, cuotas y envejecimiento. Probar que un urgente adelanta
   pendientes y que background recibe servicio bajo carga sostenida.
6. Habilitar `checkpoint` y `restartable` por perfil. Probar pausa real,
   recursos liberados, reanudación y evidencia del intento interrumpido.
7. Probar el alta de un nodo vacío y un despliegue gradual con rollback.
   Esto demuestra el objetivo de incorporación y distribución entre equipos.

U1, U2, U3 y el launcher ya tienen sesiones responsables. La extracción debe
reutilizar sus contratos y entregas, con un solo dueño por cambio. Este informe
no modifica ni reabre esos trabajos y no declara terminadas sus protecciones.

## Fuentes consultadas

- S1. [Instalación y distribución PAO](../README.md:184) y [actualizador](../bin/pao:469).
- S2. [Monitor Windows](../scripts/desktop/README.md:1), [umbrales](../scripts/desktop/umbrales.json:1) y [contrato nativo futuro](../scripts/desktop/README.md:176).
- S3. [Manifest de PAO](../manifest:1).
- S4. [Hook de la MacBook](../skills/felix-mode/scripts/laptop-guard:1).
- S5. [Candado y FIFO en main de comprador](/Users/felixddhs/VSCODE/REPOS/comprador./scripts/mini/estado.py:28).
- S6. [Registro de gates de comprador](/Users/felixddhs/VSCODE/REPOS/comprador./scripts/mini/README.md:197).
- S7. [Lanzador con evidencia](/Users/felixddhs/VSCODE/REPOS/comprador./scripts/mini/run.py:44) y [runner versionado](/Users/felixddhs/VSCODE/REPOS/comprador./scripts/mini/remote-run:13).
- S8. [Runner de apotheca](/Users/felixddhs/VSCODE/REPOS/apotheca/scripts/remote-run:87) y [runner instalado localmente](/Users/felixddhs/.local/bin/remote-run:82).
- S9. [Runner de SEN](/Users/felixddhs/VSCODE/REPOS/SEN-CheckIn/scripts/test-hosts/run-suite.sh:23) y [documentación de hosts](/Users/felixddhs/VSCODE/REPOS/SEN-CheckIn/docs/agents/test-hosts.md:30).
- S10. [Wrapper local entre proyectos](/Users/felixddhs/VSCODE/REPOS/SEN-CheckIn/scratchpad/test-hosts/gates-locked.sh:6). `git ls-files --error-unmatch` confirmó que no está versionado.
- S11. [Vigilante en su rama](/Users/felixddhs/VSCODE/REPOS/comprador.-vigilante-memoria/scripts/mini/README.md:574) y [migración de candado en esa rama](/Users/felixddhs/VSCODE/REPOS/comprador.-vigilante-memoria/scripts/mini/estado.py:36).
- S12. [Estado del programa Mini](/Users/felixddhs/.claude/orchestrate/mini-carga/units.tsv:1), [contrato U3](/Users/felixddhs/.claude/encargos/mini-carga/contrato-lectura-desk-u3.md:1) e [hilo de protección Mini](t3-thread://v1/mcp:73237748-290d-4655-9227-3eb30326ebaa).
- S13. [Estado del programa Windows](/Users/felixddhs/.claude/orchestrate/desktop-carga/units.tsv:1) e [hilo de protección Windows](t3-thread://v1/3012744a-767b-4a1e-a950-c7cdb5583e0c).
- S14. [Entorno desktop y compatibilidad de la app](/Users/felixddhs/VSCODE/REPOS/apotheca/docs/agents/felix-desktop.md:24). También se leyó el alias `desk` en la configuración SSH local, sin conectar.

Principios aplicados de pstack. `Encode Lessons in Structure` lleva las reglas
de prioridad y admisión a un contrato ejecutable. `Separate Before Serializing
Shared State` conserva BD, puertos y checkouts separados, y serializa sólo los
recursos realmente compartidos.
