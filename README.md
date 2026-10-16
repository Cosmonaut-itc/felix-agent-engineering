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

## Instalación manual

Revisa el diff y respalda cualquier archivo de destino existente. Después,
desde la raíz de este repositorio, copia de forma interactiva:

```bash
mkdir -p ~/.agents/docs ~/.codex/docs ~/.claude/docs

cp -i docs/orchestration.md ~/.agents/docs/orchestration.md
cp -i docs/agent-routing.md ~/.agents/docs/agent-routing.md
cp -i docs/native-subagents.md ~/.agents/docs/native-subagents.md
cp -i docs/opencode-workers.md ~/.agents/docs/opencode-workers.md
rm -f ~/.agents/docs/cheap-coder-workers.md

cp -i configs/codex/AGENTS.md ~/.codex/AGENTS.md
cp -i configs/codex/docs/claude-workers.md ~/.codex/docs/claude-workers.md

cp -i configs/claude/CLAUDE.md ~/.claude/CLAUDE.md
cp -i configs/claude/docs/codex-delegation.md ~/.claude/docs/codex-delegation.md
```

Instala también las guías referenciadas: cambiar sólo los entrypoints dejaría
apuntadores incompletos. Acepta cada reemplazo cuando el diff corresponda a la
configuración que quieres activar. Editar este repositorio no cambia los
archivos ya instalados en tu equipo.

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

### pstack y felix-mode

Revisa el código del port antes de instalarlo: sus hooks ejecutan scripts al
abrir cada sesión. La skill `verify` de proyecto requiere Claude Code 2.1.200
o posterior. Desde la raíz de este repositorio:

```bash
# Claude Code
claude plugin marketplace add michael-denyer/pstack-claude
claude plugin install pstack@pstack-claude
cp -i configs/claude/pstack-models.md ~/.claude/pstack-models.md

# Codex: la app (~/.codex), no la CLI del proxy del alias `codex`
CODEX_HOME="$HOME/.codex" command codex plugin marketplace add michael-denyer/pstack-claude
CODEX_HOME="$HOME/.codex" command codex plugin add pstack@pstack-claude
cp -i configs/codex/pstack-models.md ~/.codex/pstack-models.md

# felix-mode para ambos runtimes
mkdir -p ~/.agents/skills/felix-mode
cp -i skills/felix-mode/SKILL.md ~/.agents/skills/felix-mode/SKILL.md
ln -s ../../.agents/skills/felix-mode ~/.claude/skills/felix-mode
```

Instala también los entrypoints y el routing de la sección anterior: la
sección «Flujo de ingeniería» y la tabla «Roles de pstack» viajan con ellos.
Codex pide confiar en el hook del plugin desde `/hooks`; con
`session hook: off` en la hoja, el hook no inyecta nada aunque lo confíes.

Retira después lo que pstack duplica:

- **Superpowers en Codex.** En `~/.codex/config.toml`, pon `enabled = false`
  en `[plugins."superpowers@openai-curated"]`.
- **Matt Pocock en Claude Code.** El plugin sigue activo para planificar.
  Bloquea sus skills duplicadas con reglas de permiso en
  `~/.claude/settings.json`:

  ```json
  "permissions": {
    "deny": [
      "Skill(skill:mattpocock-skills:tdd)",
      "Skill(skill:mattpocock-skills:diagnosing-bugs)",
      "Skill(skill:mattpocock-skills:prototype)",
      "Skill(skill:mattpocock-skills:code-review)",
      "Skill(skill:mattpocock-skills:pr)",
      "Skill(skill:mattpocock-skills:implement)",
      "Skill(skill:mattpocock-skills:handoff)",
      "Skill(skill:mattpocock-skills:writing-for-agents)"
    ]
  }
  ```

- **Matt Pocock en Codex.** Mueve fuera de `~/.agents/skills/` las copias de
  `tdd`, `diagnosing-bugs`, `prototype`, `code-review`, `handoff`,
  `implement` y `writing-for-agents`.

Las skills de un repositorio que su `AGENTS.md` exige, como `implement-spec`
o `retro` en Comprador, se revisan repo por repo.
