# vendor

Copias sin modificar de skills de terceros. No se editan a mano:
`bin/pao vendor-sync` las reescribe enteras desde el último release de
upstream, y el workflow [`vendor-sync.yml`](../.github/workflows/vendor-sync.yml)
lo corre a diario.

| Directorio | Upstream | Licencia |
|------------|----------|----------|
| [`mattpocock/`](mattpocock/UPSTREAM) | [mattpocock/skills](https://github.com/mattpocock/skills), de Matt Pocock | MIT, © 2026 Matt Pocock. Ver [`mattpocock/LICENSE`](mattpocock/LICENSE). |

`mattpocock/UPSTREAM` registra el repositorio, el tag y el commit copiados.
Sólo se copian las skills que declara [`manifest`](../manifest).
