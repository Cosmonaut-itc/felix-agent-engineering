# Trabajadores GPT desde Claude Code

Ruta **Claude Code → GPT**. Patrón común de esta ruta: **el exit code
miente**. Veta un despacho por `git status`, por el entregable y por las
llamadas de herramienta que completó, nunca por el exit.

## Elegir la ruta

1. **T3 Code.** Primera opción cuando está disponible, como dice la sección
   siguiente.
2. **`codex-run`.** Si T3 no está disponible o su lanzamiento falla. Las
   secciones desde «CLI de Codex con proxy» describen esta ruta y sus
   alternativas.

Al despachar, di en una línea qué ruta usaste y, si caíste a `codex-run`, por
qué.

## T3 Code

`delegate_task`, del MCP `t3-code`, lanza un hijo Codex que pertenece al hilo
de T3. T3 lo sigue, despierta al hilo cuando termina y lo detiene; no queda
proceso que vigilar.

Está disponible cuando se cumplen las tres condiciones:

- Existe la herramienta `mcp__t3-code__delegate_task`. El servidor puede
  conectarse tarde: si el catálogo inicial no la muestra, búscala una vez con
  ToolSearch antes de darla por ausente.
- `orchestrator_capabilities` lista la instancia `codex` con
  `canRunChildTask: true` y el modelo que pide el routing.
- La instancia pasa por el proxy: en `~/.t3/userdata/settings.json`,
  `providerInstances.codex.config.homePath` es
  `/Users/felixddhs/.config/codex-cli`. Con otra ruta, como `~/.codex`, usa
  `codex-run`.

**Despachar.**

```json
{
  "task": "<brief completo>",
  "title": "<encargo corto>",
  "role": "implementation",
  "target": {
    "providerInstanceId": "codex",
    "model": "gpt-6.1-sol",
    "options": { "reasoningEffort": "low" }
  },
  "mode": "async",
  "isolation": "worktree",
  "clientRequestId": "<slug>-<ronda>"
}
```

- `model` lleva el nombre completo. `reasoningEffort` sale del routing, con
  tope `high` aunque T3 ofrezca `xhigh`, `max` o `ultra`. Pasa `options`
  siempre: sin ellas el hijo toma el default del modelo. Comprueba
  `providerInstanceId`, `model` y `options` en la respuesta.
- Deja `runtimeMode` sin fijar. Un modo más estrecho no da sólo lectura: el
  hijo espera una aprobación que nadie da.
- T3 no tiene sandbox de sólo lectura. Un encargo read-only lo dice en el
  brief, con `isolation: "shared"`, y compruebas `git status` del checkout
  antes y después.
- Para escribir, usa `isolation: "worktree"`. El worktree parte del último
  commit local y no copia cambios sin commitear: commitea antes. Localiza el
  worktree del hijo con `t3_worktree_list` e integra tú sus cambios.
- `clientRequestId` cambia en cada ronda y se repite en los reintentos de la
  misma ronda.
- Conserva el `taskId`: es el identificador del encargo. `childThreadId` es el
  almacenamiento del hijo, no un canal para mandarle trabajo.

**Recoger.** Cuando el hijo termina, una notificación despierta al hilo:
termina tu turno en vez de sondear. Usa `task_status` sólo si necesitas el
resultado a mitad de turno; `workState: result_available` y `summary` traen
el resultado. Con `hasPendingQuestion` o `pendingApproval`, el hijo espera:
lee la petición con `t3_pending_request_read` en `childThreadId` y respóndela,
o pregunta al usuario.

**Corregir y revisar.** Cada ronda de corrección o de review es un
`delegate_task` nuevo con el brief original, los hallazgos previos, las
respuestas y las objeciones abiertas. No uses `t3_thread_send` sobre
`childThreadId`.

**Detener.** `task_cancel` con el `taskId` detiene al hijo y a sus
descendientes. No hay tope de tiempo automático: el brief fija los minutos y
cancelas si se pasan.

El trabajador es el mismo Codex. Aplican igual los guardarraíles de
«Escritura» y los gotchas de pedir aprobación, delegación interna, filtro de
contenido, muerte tras una herramienta y cuota. Los de stdin, flags, procesos
y timeouts son del CLI.

## CLI de Codex con proxy

Los trabajadores usan sólo el `CODEX_HOME` conectado al proxy:
`/Users/felixddhs/.config/codex-cli` (alias interactivo `codex`). Su
`config.toml` fija `model_provider = "cpa"` hacia `felix-desktop:8317/v1`, y el
proxy reparte las cuentas y gestiona sus límites; no cambies de cuenta a mano.

`~/.claude/settings.json` fija `env.CODEX_HOME` en esa ruta para las sesiones
nuevas; aun así, pásalo explícito en cada invocación (runtime del plugin o
CLI), porque una sesión abierta antes conserva su entorno y los alias de
`~/.zshrc` no existen en un shell no interactivo:

```bash
CODEX_HOME=/Users/felixddhs/.config/codex-cli codex exec ...
CODEX_HOME=/Users/felixddhs/.config/codex-cli node "$companion_path" task ...
```

No uses `CODEX_HOME=/Users/felixddhs/.codex` (alias `codex-original`): es la
configuración de la app de ChatGPT, en modo directo con la cuenta Gmail, sin
proxy. El banner de `codex exec` debe mostrar `provider: cpa`.

## Script de despacho

Despacha con `~/.claude/skills/felix-mode/scripts/codex-run`. El script
aplica esta guía: usa la CLI con proxy, pasa el prompt por stdin, exige el
nombre completo del modelo y el tope `high`, abre en sólo lectura salvo con
`--write`, aplica el tope de tiempo y deja el run fuera de la sesión. Marca
`done` sólo cuando Codex salió y no queda vivo ningún proceso que registró:
los descendientes que vio por ppid cada segundo y en la foto final. Si no lo
puede confirmar, el run queda `stuck` con los pids que sobreviven. Un proceso
que se separa de su padre entre dos fotos puede escapar: pide en el brief que
todo proceso que lance el trabajador termine antes de su respuesta. Corre en
macOS y Linux. `codex-run --help` da el uso. Prefiérelo al runtime del plugin
y al CLI a mano. Las secciones siguientes explican lo que el script
ya resuelve y lo que sigue en tus manos: el brief, el veto y los commits.

Escribe el brief como texto literal, con Write o con un heredoc entre
comillas (`<<'EOF'`), y lánzalo sólo si el archivo quedó escrito. Un heredoc
sin comillas ejecuta los backticks y los `$()` del Markdown, y el trabajador
arranca con un brief roto.

## Runtime del plugin

Esta sección y la siguiente aplican sólo si despachas por el plugin. Antes de
su primer despacho en la sesión, ejecuta `/codex:setup` para comprobar
instalación y autenticación del plugin
[openai/codex-plugin-cc](https://github.com/openai/codex-plugin-cc).

Localiza el script `codex-companion.mjs` dentro de la versión instalada del
plugin (bajo `~/.claude/plugins/cache/openai-codex/codex/<versión>/scripts/`)
y resuelve la versión en cada sesión. Ejecutar el script **sin argumentos**
imprime el usage; `task` trata cualquier texto como prompt, incluido
`--help`, y lanza un turno real de Codex.

Prefiere invocar `task` directamente desde Bash: permite pasar modelo,
esfuerzo y contexto explícitos y el mismo controlador lanza y recoge el trabajo
sin consumir un trabajador Claude como adaptador.

Patrón verificado en la versión 1.0.5; confirma su vigencia con el usage:

```bash
node "$companion_path" task \
  --cwd "$checkout_path" \
  --model "$worker_model" \
  --effort "$worker_effort" \
  --prompt-file "$brief_path" \
  --json
```

Para background añade `--background`, conserva el job ID y consulta `status`
y `result` con ese ID hasta el resultado terminal; detén con `cancel` y
confirma el estado. Si reanudas con `--resume-last`, verifica qué hilo se
retoma y vuelve a pasar modelo y esfuerzo.

## Adaptador opcional

El subagente `codex:codex-rescue` es un adaptador de ruta prescrita: un
wrapper que reenvía a `task` y devuelve su stdout. Su definición deja modelo y
esfuerzo sin fijar salvo petición explícita, activa `--write` por defecto y
devuelve **vacío** si la invocación falla. Si lo usas, pásale modelo, esfuerzo
y read-only de forma explícita y comprueba que los reenvía. Un resultado vacío
es fallo de transporte, no ausencia de hallazgos.

Úsalo sólo para trabajo read-only: en modo background el subagente retorna
antes de que termine el job y su proceso hijo muere con él, mientras el JSON
de estado sigue diciendo `running`. Para saber si un job del plugin vive,
mira su `pid` con `ps -p` y la frescura de su `.log` bajo
`~/.claude/plugins/data/codex-openai-codex/state/<repo>/jobs/`.

Los comandos `/codex:review` y `/codex:adversarial-review` usan otro recorrido
con parámetros propios. Para una revisión con modelo y esfuerzo controlados,
usa `task` read-only con un brief de revisión.

## CLI alternativo

`codex-run` usa este CLI. Para invocarlo a mano, comprueba
`codex exec --help`; patrón verificado en codex-cli 0.153.3:

```bash
timeout --signal=KILL "$max_secs" codex exec \
  -C "$checkout_path" \
  -s read-only \
  -m "$worker_model" \
  -c "model_reasoning_effort=\"$worker_effort\"" \
  --json \
  - < "$brief_path"
```

- **Stdin.** Sin TTY, `codex exec` puede quedarse en «Reading additional
  input from stdin...» para siempre, o salir con exit 0 sin hacer nada. Pasa
  siempre el prompt por stdin explícito (`- < brief`) o redirige `</dev/null`.
- **Modelo.** Un alias corto (`sol`) o cualquier `-m` no soportado devuelve
  `400 ... not supported` y **exit 0** con el árbol intacto. Usa el nombre
  completo (`gpt-6.1-sol`).
- **Flags.** Un flag inválido (`--search` no existe) falla en el parseo con
  exit 0 y un output de pocas líneas. La búsqueda web se controla por
  `web_search` en `config.toml`.
- **Directorio confiable.** `-C` debe apuntar a un repo confiado; `-C /tmp`
  falla con «Not inside a trusted directory».
- **Salida.** Usa `-o <file>` o `--output-schema <file>` cuando el consumidor
  necesite salida estructurada.
- **Reanudar.** `codex exec resume <session-id>` rechaza `-s` y `-C` (en
  0.153 rechazaba también `-m` y `-o`; 0.162.1 los acepta). Toma sandbox y
  esfuerzo por `-c` y escribe sólo en el cwd desde el que se lanza, así que
  entra al worktree antes. Evita
  `resume --last` si corrieron varios hilos en la sesión. El session id está
  en el banner del output. Añade `--ephemeral` sólo a reviews y consultas:
  sin rollout no hay `resume`.

## Escritura

Por defecto todo despacho es read-only (`task` sin `--write`, CLI con
`-s read-only`). Habilita escritura sólo para el encargo autorizado, en un
worktree; si debe compartir checkout, commitea un punto de recuperación
antes. En el CLI, `-s workspace-write` no trae red;
`-c 'sandbox_workspace_write.network_access=true'` la habilita.

- **Commits.** En `workspace-write` el trabajador no puede commitear
  (`index.lock`); el orquestador commitea. El brief fija que el árbol de
  partida es correcto y que el trabajador sólo edita archivos; git lo gestiona
  el orquestador. Guardarraíl explícito en el brief: sin `git checkout`,
  `git restore` ni `git reset`, porque un trabajador ya borró trabajo sin
  commitear con ellos.
- **Descartar.** Para descartar cambios de un trabajador, guárdalos primero
  con stash o commit; la restauración directa sobre su árbol los pierde.
- **Suites largas.** Un timeout a mitad de una suite completa deja el árbol
  sin commitear a medias. Pide tests dirigidos y corre la suite completa tú.

## Gotchas de ejecución

Observados en Codex CLI 0.145 a 0.153.

- **Pide aprobación.** Con `default_mode_request_user_input` activo, el
  trabajador propone su plan, pregunta «¿Apruebas?» y sale con exit 0 sin
  escribir. Todo brief de implementación lleva un apartado «Autorización
  previa»: encargo aprobado, diseño ya autorizado, checkout correcto tal como
  está, procede sin pedir confirmación. Si ya pasó, `resume` con «aprobado,
  procede» conserva el contexto.
- **Delegación interna.** Sol abre subagentes `collab` por su cuenta y agota
  el timeout. El brief dice «trabaja tú solo, sin subagentes ni collab, tienes
  N minutos»; parte lotes grandes.
- **Filtro de contenido.** Vocabulario de seguridad («adversarial», «bypass»,
  «atacante», «fail-open», «modelo de amenaza») mata el run con exit 0 o 1 y
  un mensaje de «cybersecurity risk». Redacta en registro de QA e integridad
  de datos; tras un flag relanza en sesión fresca, no con `resume`.
- **Muere tras una herramienta.** A veces todo despacho termina en ~1 min con
  exit 0 y ≤1 llamada de herramienta, aunque un ping responda. Cuenta las
  llamadas de herramienta en el output; al segundo intento así, escala según
  routing en vez de reintentar.
- **Timeouts.** Con `codex-run`, usa `--timeout`. A mano, envuelve siempre en
  `timeout --signal=KILL`. El `timeout` del
  Bash de Claude Code se limita a 600 s, y `run_in_background` muere a los
  ~20 min con su hijo. Para runs largos, daemoniza con `python3` +
  `subprocess.Popen(..., start_new_session=True)` (macOS no tiene `setsid`),
  deja un done-file y sondea con Monitor. El tell del cuelgue real es 0 % CPU,
  no el tiempo transcurrido; un timeout que mata un run productivo cuesta más
  que esperar.
- **Cuota.** No asumas el estado de la cuota en ninguna dirección. Antes de
  despachar en volumen, un ping real con esfuerzo low; si falla por límite,
  el fallback es Claude según routing.
- **Procesos.** La app de ChatGPT trae su propio binario `codex`: para contar
  o matar workers usa `pgrep -f "codex exec"` y la edad del output, no
  `pgrep -x codex`. Codex 0.162 abre una sesión nueva para cada comando de
  herramienta, así que matar el grupo de `codex exec` no las detiene. Para
  un run de `codex-run`, usa `codex-run stop`, que termina el árbol completo.

Referencias primarias:

- [Codex CLI](https://developers.openai.com/codex/cli/)
- [Referencia de `codex exec`](https://developers.openai.com/codex/cli/reference/)
- [Codex app server](https://developers.openai.com/codex/app-server/)
