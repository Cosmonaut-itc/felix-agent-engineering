# vendor

Copias sin modificar de skills de terceros. No se editan a mano:
`bin/pao vendor-sync` reescribe cada directorio entero desde su upstream, y
el workflow [`vendor-sync.yml`](../.github/workflows/vendor-sync.yml) lo corre
a diario.

| Directorio | Upstream | Seguimiento | Licencia |
|------------|----------|-------------|----------|
| [`mattpocock/`](mattpocock/UPSTREAM) | [mattpocock/skills](https://github.com/mattpocock/skills), de Matt Pocock | Último release semver | MIT, © 2026 Matt Pocock. Ver [`mattpocock/LICENSE`](mattpocock/LICENSE). |
| [`emilkowalski/`](emilkowalski/UPSTREAM) | [emilkowalski/skills](https://github.com/emilkowalski/skills), de Emil Kowalski | Rama por defecto, sin releases | MIT, © 2026 Emil Kowalski. Ver [`emilkowalski/LICENSE`](emilkowalski/LICENSE). |

Cada fila `upstream` del [`manifest`](../manifest) declara un directorio, su
repositorio y su seguimiento: `tags` copia el release semver más alto y
`head` copia la rama por defecto. El `UPSTREAM` de cada directorio registra el
repositorio, el commit copiado y, con `tags`, el tag.

Sólo se copian las skills que el manifest declara. Una fila `skill` además la
instala en `~/.agents/skills` y `~/.claude/skills`. Una fila `ref` sólo la
copia, para que una referencia de felix-mode la cite por ruta sin sumarla al
contexto de cada sesión.

Las referencias de felix-mode adaptan estas skills y enlazan sus archivos por
ruta relativa. Esos enlaces son el mapa de qué referencia depende de qué
archivo de upstream: `bin/pao check` falla si uno apunta a un archivo que no
existe, y `bin/pao vendor-refs RUTA...` lista las referencias que citan el
directorio de skill de cada ruta. Cuando el workflow copia un cambio, su
commit nombra esas referencias para revisarlas.
