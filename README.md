# Personal Agent Orchestration

Configuración documental para que Codex y Claude Code compartan contrato de
orquestación, selección de trabajadores y cierre de encargos, aunque cada
harness use un transporte distinto.

## Estructura

| Ruta | Propósito |
|------|-----------|
| [`docs/orchestration.md`](docs/orchestration.md) | Fuente canónica del contrato común: roles, descendientes y cierre. |
| [`docs/agent-routing.md`](docs/agent-routing.md) | Tabla de modelos, esfuerzo, escalado, review y UI/UX. |
| [`docs/native-subagents.md`](docs/native-subagents.md) | Contexto, configuración y seguimiento de subagentes nativos en Codex y Claude Code. |
| [`docs/opencode-workers.md`](docs/opencode-workers.md) | Despacho programático de cheap coders Go: CLI/HTTP, modelo, controles y cierre. |
| [`configs/codex/`](configs/codex/AGENTS.md) | Entry point global de Codex, con el contrato inlineado, y guía Codex → Claude. |
| [`configs/claude/`](configs/claude/CLAUDE.md) | Entry point global de Claude Code, con el contrato inlineado, y guía Claude Code → GPT. |
| [`configs/opencode/`](configs/opencode/opencode.json) | Perfiles DeepSeek V4.1 Flash, MiniMax M3 y Kimi K3, con instrucciones compartidas. |
| [`skills/felix-mode/`](skills/felix-mode/SKILL.md) | Router de ingeniería sobre pstack: precedencia, modelos por rol, revisión y autonomía. |
| [`configs/*/pstack-models.md`](configs/claude/pstack-models.md) | Hojas de modelos de pstack para Claude Code y Codex, derivadas del routing. |
| [`manifest`](manifest) | Registro único de archivos, skills, retiradas y plugins. |
| [`bin/pao`](bin/pao) | Instalador, actualizador y diagnóstico para macOS, Linux y Git Bash. |
| [`vendor/`](vendor/mattpocock/UPSTREAM) | Skills de Matt Pocock, licencia y versión de origen. |
| [`handbook/index.html`](handbook/index.html) | Handbook de consulta rápida del flujo: qué hace el usuario, playbooks, revisión, skills y modelos. |

## Contrato común

El [contrato](docs/orchestration.md) distingue al **orquestador**, responsable
del resultado final, del **trabajador**, que resuelve el encargo, y del
**adaptador**, que transporta una ruta prescrita. Cada delegación pasa por
**Despachar → Recoger → Vetar → Cerrar**.

Los entrypoints globales llevan ese contrato inlineado, de modo que rige desde
el primer turno sin una lectura previa. `docs/orchestration.md` es la fuente
canónica: al editarlo, replica el cambio en ambos entrypoints.

El [routing](docs/agent-routing.md) es la única fuente para elegir modelo,
esfuerzo y pases de review. Los entrypoints apuntan a esa política y copian
su tabla de modelos, que debe mantenerse sincronizada; las guías
describen cómo ejecutarla en cada entorno.

## Rutas de ejecución

| Ruta | Mecanismo | Guía |
|------|-----------|------|
| Codex → GPT | Subagentes nativos de Codex. | [`native-subagents.md`](docs/native-subagents.md), rama Codex |
| Claude Code → Claude | Subagentes nativos de Claude Code. | [`native-subagents.md`](docs/native-subagents.md), rama Claude Code |
| Codex → Claude | Proceso externo `claude -p`, seguido hasta su terminación. | [`claude-workers.md`](configs/codex/docs/claude-workers.md) |
| Claude Code → GPT | Runtime del plugin openai/codex-plugin-cc, adaptador opcional o CLI de Codex. | [`codex-delegation.md`](configs/claude/docs/codex-delegation.md) |
| Codex o Claude Code → OpenCode Go | `opencode run` con perfil/modelo explícitos; servidor HTTP para despacho asíncrono. | [`opencode-workers.md`](docs/opencode-workers.md) |

Un trabajador externo por CLI tiene un ciclo de vida distinto del subagente
nativo. Cada guía explica qué contexto y controles ofrece.

El MCP antiguo `cheap-coder` (`stealth/ox-alpha`) sigue **deprecado**. Los
perfiles Go usan OpenCode y no reactivan ese MCP. Para retirar el antiguo:

```bash
claude mcp remove cheap-coder -s user
# y elimina el bloque [mcp_servers.cheap-coder] de ~/.codex/config.toml
```

## Flujo de ingeniería con pstack

[pstack](https://github.com/cursor/plugins/tree/main/pstack) aporta los
playbooks de ejecución (investigación, bug fix, feature, refactor, prototipo,
PR), sus principios y la verificación sobre el artefacto real. Se instala el
port [pstack-claude](https://github.com/michael-denyer/pstack-claude) para
Claude Code y Codex con su hook apagado, y
[`felix-mode`](skills/felix-mode/SKILL.md) lo adapta:

- La sección «Flujo de ingeniería» de los entrypoints globales invoca
  `felix-mode` en toda tarea no trivial.
- `felix-mode` corre `poteto-mode` con la precedencia usuario > `AGENTS.md`
  del repositorio > contrato y routing > `felix-mode` > pstack.
- Los modelos por rol salen de la sección «Roles de pstack» del
  [routing](docs/agent-routing.md#roles-de-pstack). Cada hoja
  `pstack-models.md` copia sólo lo que su runtime acepta.
- La planificación sigue en las skills de Matt Pocock. Superpowers y las
  skills de Matt Pocock que pstack duplica se retiran.

## Alcance

Este repositorio define el contrato de orquestación, el routing y el flujo de
ingeniería comunes a los proyectos. Cada repositorio declara en su propio
`AGENTS.md`, `CLAUDE.md` o docs locales sus gates, su tracker, su skill
`verify` y, si los necesita, project playbooks en `.agents/playbooks/`.

## Instalación con pao

Requiere Bash 3.2 o posterior, Git y las utilidades del sistema. En Windows,
usa Git Bash con permiso para crear symlinks NTFS, por ejemplo con el modo de
desarrollador activo. Si no puede crear un enlace nativo, la instalación falla.

Revisa el código del port de pstack antes de instalarlo: sus hooks ejecutan
scripts al abrir cada sesión.

```bash
git clone https://github.com/cosmonaut-itc/personal-agent-orchestration.git \
  ~/.local/share/personal-agent-orchestration
cd ~/.local/share/personal-agent-orchestration
bin/pao install
```

El clon debe estar en `main`. `manifest` declara todo lo que se instala.
`pao` retira las skills duplicadas y crea enlaces absolutos desde los dos
runtimes al clon. Respalda los archivos y directorios reales antes de
reemplazarlos, aunque su contenido sea idéntico. Informa la ruta de cada
respaldo. Una segunda instalación conserva los enlaces que ya están bien.
No instala `configs/opencode/` ni `outputs/`.

El estado vive en `${XDG_STATE_HOME:-$HOME/.local/state}/pao/`. Allí están
`env`, `status`, las últimas 2000 líneas de `update.log` y `backups/`.
Las skills retiradas que eran directorios reales pasan a
`~/.agents/skills-retired/`.

El job corre cada cuatro horas y al cargar el LaunchAgent en macOS. Usa
LaunchAgent en macOS, cron en Linux y el Programador de tareas en Windows.
Hace `git fetch` y sólo avanza `main` con un fast-forward de `origin/main`.
Un conflicto local o una rama divergente queda registrado como error.
Después sincroniza enlaces y plugins. Si encuentra un archivo real durante
la actualización, lo conserva y lo marca `held`. En macOS notifica los
errores y los archivos `held`. Sin red, el job sólo avisa; pasa a error si
lleva más de un día sin poder hacer `fetch`.

```bash
# Desde el clon, o con su ruta absoluta.
bin/pao status
bin/pao sync
bin/pao uninstall
```

`status` muestra el último resultado, la rama, los cambios locales, la
distancia respecto a `origin/main` y si el job está instalado. `uninstall`
quita el job y los enlaces al clon. Conserva los respaldos y las skills
retiradas. No desinstala plugins.

Para instalar sin job usa `bin/pao install --no-schedule`. Para omitir los
plugins añade `--no-plugins`. Esa preferencia se guarda para las siguientes
sincronizaciones. Una instalación posterior sin esa opción vuelve a activar
su gestión.

### Actualizaciones de skills y plugins

Las 19 skills de Matt Pocock se distribuyen en `vendor/mattpocock/`, con su
licencia MIT y el tag y commit de origen en `UPSTREAM`. No se editan a mano.
El workflow diario ejecuta `bin/pao vendor-sync`, valida el resultado y
corre las pruebas antes de hacer un commit a `main`. También se puede lanzar
con `workflow_dispatch`. El job local recoge ese commit en la siguiente
actualización.

pstack se instala y actualiza con el CLI de cada runtime si está disponible.
Si falta un CLI, queda `skipped`. Codex siempre gestiona el plugin de la app
con `CODEX_HOME="$HOME/.codex"`. No usa la configuración de la CLI del proxy.
Reinicia las sesiones abiertas después de actualizar pstack. La skill
`verify` de proyecto requiere Claude Code 2.1.200 o posterior.

### Pasos manuales

Confía en el hook de pstack desde `/hooks` en Codex. Con `session hook: off`
en la hoja de modelos, el hook no inyecta contenido aunque lo confíes.

Desinstala el plugin anterior de Matt Pocock para evitar nombres duplicados.
`pao` avisa si lo encuentra habilitado en `~/.claude/settings.json` e imprime
el comando `claude plugin uninstall mattpocock-skills@<marketplace>` con su
identificador. Quita también sus reglas `Skill(skill:mattpocock-skills:...)`
de `permissions.deny`. `pao` no cambia ese JSON.

Si aún tienes Superpowers en Codex, pon `enabled = false` en
`[plugins."superpowers@openai-curated"]` de `~/.codex/config.toml`.
Las skills exigidas por el `AGENTS.md` de cada proyecto se revisan allí.

### Perfiles OpenCode Go

Los perfiles requieren OpenCode y el proveedor Go conectado. Conserva el
archivo de configuración existente: combina la clave `agent` y las variantes
de `provider.opencode-go.models.kimi-k3` de
[`configs/opencode/opencode.json`](configs/opencode/opencode.json) con la
configuración global o del proyecto, y copia `worker-prompt.md` junto al JSON
para resolver su referencia relativa. Revisa colisiones de nombres antes de
combinar. No reemplaces proveedores, MCP ni otras preferencias existentes.

Los perfiles parten de lectura y búsqueda; los permisos de implementación
se fijan por encargo mediante overrides de ejecución. Kimi K3 está habilitado
con low de inicio y high al escalar. Go recibió y aceptó ambos valores en
pruebas reales; el dueño autorizó su uso aceptando que el nivel interno
aplicado no tiene confirmación observable. Sigue la
[guía de transporte](docs/opencode-workers.md) para consultar la evidencia,
las comprobaciones pendientes y los controles de cada despacho.

## Créditos

- [pstack](https://github.com/cursor/plugins/tree/main/pstack) es obra de
  Lauren Tan (poteto). Se instala como plugin por el port
  [pstack-claude](https://github.com/michael-denyer/pstack-claude) de Michael
  Denyer, con licencia MIT. Este repositorio no copia su código.
- Las [skills de Matt Pocock](https://github.com/mattpocock/skills) son
  © 2026 Matt Pocock, con licencia MIT. `vendor/mattpocock/` las copia sin
  modificar junto con su [`LICENSE`](vendor/mattpocock/LICENSE). Ver
  [`vendor/README.md`](vendor/README.md).
