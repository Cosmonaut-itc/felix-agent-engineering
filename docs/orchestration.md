# Contrato de orquestación

Contrato común para Codex y Claude Code, y fuente canónica de este texto.
Se replica íntegro en los entrypoints globales `~/.codex/AGENTS.md` y
`~/.claude/CLAUDE.md`, que lo cargan sin lectura previa: al cambiar este
documento, actualiza también ambas copias.

## Roles

- **Orquestador:** recibe la tarea del usuario, decide el reparto, conserva
  la responsabilidad y veta el resultado final.
- **Trabajador:** resuelve el encargo recibido y devuelve evidencia.
- **Adaptador:** transporta un encargo a otro runtime siguiendo una ruta
  prescrita. Devuelve identificadores y resultados; no decide nuevos
  encargos ni acepta el trabajo por su cuenta.

## Preparar

Delega encargos acotados que aporten ahorro de tiempo, aislamiento de
contexto o una revisión independiente. Resuelve directamente lo que cueste más
coordinar que hacer.

Antes de despachar, determina:

- Qué contexto recibirá el trabajador y qué material debes proporcionarle.
- Qué modelo y esfuerzo efectivos usará, conforme a routing.
- En qué directorio trabajará, con qué permisos y sobre qué archivos.
- Cómo identificarlo, recoger su resultado, continuarlo y detenerlo.

## Despachar → Recoger → Vetar → Cerrar

### 1. Despachar

Entrega objetivo, contexto imprescindible, alcance, restricciones, criterios
verificables de aceptación, formato de resultado y presupuesto de trabajo.
Declara si el encargo permite descendientes.

Registra la ruta y los identificadores devueltos. Esta fase termina cuando
el lanzamiento queda identificado y puede seguirse. Un recibo de lanzamiento
no demuestra ejecución ni éxito.

### 2. Recoger

Sigue el encargo por sus identificadores hasta obtener un resultado terminal.
Conserva resultado, evidencia, cambios, errores y pendientes.

Cuando intervenga un adaptador, recoge también el resultado del trabajador
real. La terminación del adaptador no implica que el encargo haya terminado.

Esta fase termina con el resultado terminal y la evidencia disponible, o con
un bloqueo o cancelación identificados explícitamente.

### 3. Vetar

Contrasta todos los criterios de aceptación con la evidencia. Lee el diff
completo cuando haya cambios y verifica las afirmaciones materiales.

Para corregir un resultado, devuelve el criterio incumplido, la evidencia y
el cambio requerido. Cada nueva ejecución vuelve a pasar por recogida y veto.

Esta fase termina con una decisión de aceptación, corrección o rechazo
fundamentada para cada criterio.

### 4. Cerrar

Integra únicamente resultados aceptados dentro de la autorización vigente.
Confirma que el trabajador y sus descendientes terminaron o fueron detenidos.
Conserva cambios y evidencia antes de limpiar recursos. En T3 Code, limpiar
incluye archivar el hilo de cada hijo, como indica
`~/.agents/docs/t3-delegation.md`.

Informa el resultado real y los pendientes, incluida cualquier revisión
obligatoria que no pudiste realizar. Un encargo bloqueado o cancelado puede
cerrarse como tal; la tarea sólo se declara completada cuando cumple sus
criterios de aceptación.

## Descendientes

Un trabajador puede crear descendientes nativos cuando el encargo lo permita
y defina alcance, presupuesto y límite de descendientes. Cada padre recoge,
veta y cierra su propio subárbol antes de devolver el resultado.

Las decisiones de cruzar entre harnesses pertenecen al orquestador. Un
adaptador puede ejecutar ese cruce prescrito. Los trabajadores resuelven en
el runtime de destino y devuelven el resultado por la ruta de origen, sin
abrir un puente de vuelta.

## Contexto, aislamiento e independencia

Un contexto separado no garantiza archivos, base de datos o navegador
separados. Antes de permitir escrituras simultáneas, asigna superficies
disjuntas o worktrees. Serializa operaciones que compartan estado mutable.

Una revisión independiente recibe un encargo propio y evidencia primaria.
Dale contexto nuevo para que el razonamiento del autor no condicione el
primer juicio del revisor.
