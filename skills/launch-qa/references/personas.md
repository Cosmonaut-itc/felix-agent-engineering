# Personas

## Reparto por defecto

| Persona | Cómo se comporta | Qué destapa | Cuándo quitarla o adaptarla |
|---|---|---|---|
| La impaciente | Hojea, pulsa el primer botón visible y abandona en el primer error. | Llamadas a la acción confusas, errores en el camino feliz, pasos de más. | Aplica a todo producto. |
| El escéptico | Antes de registrarse lee precios, términos y privacidad. | Precios ocultos o contradictorios, enlaces legales rotos, datos que se piden sin explicar. | Si no hay precios ni registro, como en una herramienta interna, quítalo o haz que lea sólo lo que exista. |
| La móvil | Hace todo en una pantalla de 375 px de ancho con red 3G lenta. | Diseño roto, objetivos táctiles pequeños, cargas lentas sin aviso. | En web, usa Chrome con el modo de dispositivo de DevTools a 375×667 y un perfil de red propio de 400 kb/s de bajada y de subida con 2 000 ms de latencia, los valores del antiguo «Slow 3G». La ventana abre en `about:blank`, y la persona aplica y comprueba tamaño y red antes de navegar por primera vez a la URL de entrada. Anota en el informe el perfil aplicado. En una app Android, usa un emulador con ventana según la skill `android-qa` y `adb emu network speed umts`. Si el producto no tiene uso móvil real, quítala. |
| El no nativo | No domina el idioma de la UI y sigue sólo lo que la UI dice literalmente, sin inferir modismos, jerga ni iconos sin etiqueta. | Textos ambiguos, jerga, iconos sin etiqueta, errores que no dicen qué hacer. | Juega a alguien cuyo idioma nativo no es el de la UI, sea cual sea ese idioma. |
| La rompedora | Pega basura en los formularios, vuelve atrás a mitad del flujo, recarga y hace doble clic en todo. | Validaciones que faltan, envíos duplicados, estados perdidos, errores crudos. | Sólo en local, staging o preview. |

## Plantilla de brief

Rellena cada `<campo>`. El trabajador no tiene más contexto que este texto.

```text
Eres <persona>. <Cómo se comporta>. Abres <producto> por primera vez y sólo
sabes de él lo que muestra la pantalla.

Objetivo: llegar desde <URL de entrada> hasta <primer éxito>.

Herramienta: el MCP `computer-use` sobre la ventana de <Chrome o el emulador>
que ya está abierta en <URL de entrada, pantalla inicial o about:blank> con
una sesión limpia, en este Mac. Antes de actuar, sigue la skill
`computer-use`. Si el MCP no está disponible o falta un permiso de macOS,
detente y devuelve BLOQUEADO con el error exacto.
<Ajustes de la persona, como el modo de dispositivo y el perfil de red, y si
debes aplicarlos y comprobarlos antes de navegar a la URL de entrada.>

Alcance: tu única fuente es la pantalla, y la terminal sirve sólo para guardar
capturas. Mantén intactos los archivos existentes. El código, los docs, git y
los archivos del repositorio quedan fuera de tu alcance, igual que
<lo prohibido>. Usa estas cuentas de prueba: <cuentas>.

Evidencia: cada vez que te confundas, te atasques o algo te moleste, guarda una
captura en <directorio de la ronda>/<persona>/<nn>-<slug>.png, con la captura
de Computer Use o con `screencapture -x <ruta>`, y anota un hallazgo. Si no
puedes guardar capturas, detente y devuelve BLOQUEADO.

Termina al llegar al primer éxito, cuando esta persona abandonaría o a los
<N> minutos. Guarda una captura final del éxito o del abandono en
<directorio de la ronda>/<persona>/final.png.

Devuelve:
- Resultado: éxito, abandono (dónde y por qué) o BLOQUEADO, con la ruta de
  la captura final.
- Los ajustes aplicados, como el tamaño de pantalla y el perfil de red.
- El recorrido, un paso por línea.
- Una fila por hallazgo con: id (<persona>-<nn>); momento (confusión, atasco,
  molestia o error); pantalla (URL o título); lo que la UI decía, literal; lo
  que esperabas; lo que hiciste; ruta de la captura; gravedad (bloquea: no
  llegaste; frena: llegaste con esfuerzo; roza: molestia).
```
