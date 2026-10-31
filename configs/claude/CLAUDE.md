# CLAUDE.md global

El contrato siguiente rige delegar, vetar y cerrar trabajo delegado, y
ejecutar un encargo recibido de otro agente. Es copia sincronizada de
`~/.agents/docs/orchestration.md`, la fuente canónica: al cambiar una,
cambia la otra.

Antes de elegir trabajador, esfuerzo, revisión o tratamiento UI/UX, lee
`~/.agents/docs/agent-routing.md`. Su tabla de modelos se reproduce aquí como
referencia rápida (10 = mejor; en costo, más económico); el routing es la
fuente: al cambiar una, cambia la otra.

| Modelo        | costo | inteligencia | gusto | Cuándo |
|---------------|-------|--------------|-------|--------|
| gpt-6-astra   | 5     | 10           | 7     | Problemas más complejos que Sol no resolvió o que claramente lo superan. |
| gpt-6.1-sol   | 9     | 9            | 6     | Opción habitual para ejecución técnica; reviews de cambios en high. |
| gpt-6.1-terra | 10    | 8            | 6     | Computer Use y QA con usuarios simulados (`launch-qa`); cuesta la mitad que gpt-6.1-sol. |
| gpt-6-luna    | 10    | 7            | 4     | Primera opción Codex para encargos claros y fáciles de verificar; cuesta la mitad que gpt-5.6-luna. |
| opus-5.5      | 8     | 9            | 9     | Gusto visual y decisiones de producto. |
| sonnet-5.5    | 6     | 7            | 7     | Trabajo en el entorno Claude que no requiera el juicio de Opus. |
| haiku-5.5     | 10    | 6            | 4     | Exploración, lectura y extracción de alto volumen, y encargos mecánicos fáciles de verificar en el entorno Claude. |
| DeepSeek V4.1 Flash · Go | 10* | 8 | 6 | Primera opción económica para encargos técnicos acotados y verificables. |
| MiniMax M3 · Go | 9 | 8 | 7 | Implementación frontend/backend y trabajo visual contra un diseño aprobado. |
| Kimi K3 · Go | 6 | 9 | 8 | Escalón de mayor capacidad en Go para encargos complejos y contexto amplio; reservar cuota para trabajo que lo justifique. |

*DeepSeek: costo 10 sólo con la promoción 4× vigente y verificada; si no, 8.

Para delegar en los cheap coders de OpenCode Go, consulta la selección y
el esfuerzo en ese routing; antes del primer despacho, lee
`~/.agents/docs/opencode-workers.md` para preparar perfiles, permisos,
autenticación, recogida y cancelación. Aplica sus controles y el alcance
autorizado en «Estado de validación» al preparar cada encargo.

Con gpt-6-astra parte de esfuerzo `low`: su low rinde más que el high de
gpt-5.6-sol. Sube a `medium` sólo con incertidumbre real y a `high` cuando
medium no baste. Las reviews de cambios requieren gpt-6.1-sol en `high`.

Para delegar en Codex, lee `~/.claude/docs/codex-delegation.md`. Si corres en
un hilo de T3 Code con su `delegate_task` disponible, despacha por ahí
primero. Si no, usa sólo la CLI conectada al proxy, pasando en cada invocación
`CODEX_HOME="$HOME/.config/codex-cli"` (alias `codex`); el proxy gestiona
cuentas y límites. Hoy esa CLI existe sólo en la MacBook. No uses `~/.codex`
(alias `codex-original`): es la app de ChatGPT, en directo y sin proxy.

## Flujo de ingeniería

Antes de actuar en una tarea de ingeniería que toque más de un archivo,
cambie una firma que otros llaman, implique una decisión de diseño o sea un
bug de causa desconocida o un problema de rendimiento, invoca la skill
`felix-mode` y sigue su playbook. Sólo una tarea que no cumple ninguno de
esos criterios, como una pregunta trivial o un cambio contenido en un archivo
con su test obvio, se hace directo y se verifica sobre el artefacto real.
Un encargo que te delegó otro agente sigue el contrato como trabajador y no
activa `felix-mode`, salvo que el encargo lo pida.

Roles de pstack con modelo Claude, generados desde el routing:

@~/.claude/pstack-models.md

## Máquinas

La MacBook de Felix sólo edita código y sirve Metro. Gates, pruebas, builds,
contenedores, bases de datos y servidores corren en la Mac Mini o en Felix
DESKTOP, como diga el `AGENTS.md` del repositorio. Si no lo dice, en la Mac
Mini.

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
