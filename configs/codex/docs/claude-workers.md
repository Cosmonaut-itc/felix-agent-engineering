# Trabajadores Claude desde Codex

Ruta **Codex → Claude** mediante un proceso externo `claude -p`.

## Preparar y lanzar

Antes de la primera invocación, comprueba `claude --version` y `claude --help`.
Los nombres de routing `opus-5.5`, `sonnet-5.5` y `haiku-5.5` se pasan con los
alias `opus`, `sonnet` y `haiku`.

El modo `-p` **omite el diálogo de confianza del workspace**: ejecuta sólo
desde directorios confiables y entrega por stdin sólo material confiable.

`--safe-mode` conserva auth, modelo, herramientas integradas y permisos, pero
desactiva CLAUDE.md, skills, plugins, hooks, MCP y agentes personalizados. El
trabajador no recarga la política del orquestador, así que el brief debe
contener sus restricciones y referencias; si un documento local no le es
accesible, incluye su contenido pertinente.

Patrón de lectura; sustituye las variables por los valores seleccionados:

```bash
env CLAUDE_CODE_EFFORT_LEVEL="$worker_effort" \
  claude --safe-mode -p \
  --model "$worker_model" \
  --effort "$worker_effort" \
  --permission-mode dontAsk \
  --tools "Read,Grep,Glob" \
  --output-format json \
  --no-session-persistence \
  < "$brief_path"
```

La variable de entorno y el flag fijan el mismo esfuerzo para que un valor
heredado no lo sustituya. Cuando todo el material llega por stdin, usa
`--tools ""`. `--tools` omite `Agent`: el trabajador no puede abrir otra
delegación salvo que el despacho autorice descendientes controlados.

Para escritura autorizada usa un worktree, `--permission-mode acceptEdits` y
reglas `--allowedTools` precisas para shell. La alternativa `--bare` no usa
OAuth ni keychain: requiere `ANTHROPIC_API_KEY` o `apiKeyHelper`.

## Recoger

Conserva PID, salida y código de terminación. Exige salida cero y JSON
parseable; valida `result` contra los criterios de aceptación e inspecciona
el diff si hubo cambios. Salida cero y JSON válido no prueban aceptación, y
una respuesta textual no prueba que una edición o un test ocurrió.

No hay steering ni lifecycle integrado: el controlador gestiona timeout y
cancelación. El modo efímero no reanuda historial; para corregir, envía un
nuevo brief con la evidencia previa.

Referencias: [no interactivo](https://code.claude.com/docs/en/headless),
[permisos](https://code.claude.com/docs/en/permissions),
[esfuerzo](https://code.claude.com/docs/en/model-config#effort-level).
