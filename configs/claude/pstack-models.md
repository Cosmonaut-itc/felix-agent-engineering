# pstack model configuration

Cada fila es el asiento Claude de un rol de la sección «Roles de pstack» de
`~/.agents/docs/agent-routing.md`. Esa tabla manda, y esta hoja se actualiza
con ella. Donde la tabla pone GPT o Go primero (`strongest judgment`,
`arena cross-judge pool`, `interrogate reviewers` y los delegados de código),
`felix-mode` despacha ese modelo por Transporte y la fila sólo corre cuando la
tabla pide Claude. Ninguna fila se omite: sin ella, pstack vuelve a su panel
por defecto, que incluye modelos fuera del routing. `session hook: off` apaga
el enrutamiento automático del plugin, porque lo activa la sección «Flujo de
ingeniería» del `CLAUDE.md` global.

feature, refactoring: sonnet @medium
bug-fix: sonnet @medium
perf-issue: sonnet @medium
hillclimb: sonnet @medium
judgment and prose: inherit-parent
strongest judgment: opus @high
how explorer: haiku @low
how explainer: sonnet @medium
why investigators: haiku @low
why synthesizer: sonnet @medium
reflect tooling: haiku @low
reflect judgment, divergent, synthesizer: sonnet @medium
arena runners: opus @high
arena cross-judge pool: opus @high
swarm workers: haiku @low
architect runners: opus @high
interrogate reviewers: opus @high

default effort: medium
session hook: off
