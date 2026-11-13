# Menos es más en UI/UX

La regla de Felix: ningún texto ni elemento gratuito, y ningún eyebrow. El
criterio sale de [«Less is more, more or less»](https://jakub.kr/writing/less-is-more),
de Jakub Krehel. Quitar exige más disciplina que agregar, y saber qué no
construir pesa más que saber construirlo. Menos es más es orden, no
minimalismo. Como dice Jony Ive, «la simplicidad trata de poner orden en la
complejidad». A veces agregar contexto simplifica, como un reproductor que
muestra el tiempo restante.

## Cuándo aplica

- **Prototipo.** Aplica todas las reglas. Aquí se decide qué existe en la
  pantalla, y el dueño aprueba esa decisión.
- **Implementación.** Construye lo que muestra el prototipo aprobado. Un
  estado que el prototipo no dibujó, como carga, vacío o error, va en su
  forma mínima y se lista en el reporte.
- **Review de UI.** Cada elemento que falla la prueba, o que no está en el
  prototipo aprobado sin razón en el reporte, es un warning, no un nit. Lo
  mismo vale para cada animación que contradice
  [Movimiento en UI](ui-movimiento.md). Lo que funciona y está aprobado se
  deja en paz.

## Qué más leer

Lee cada archivo cuya condición cumple la tarea:

| Si la tarea | Lee |
|---|---|
| decide la UI con un prototipo | [Prototipo de UI](ui-prototipo.md) |
| anima, quita o revisa movimiento | [Movimiento en UI](ui-movimiento.md) |
| muestra datos de usuario: listas, tablas, montos, nombres, fechas | [Datos reales](ui-datos-reales.md) |
| necesita un componente: toast, menú, sheet, tabla larga, gráfica | [Librerías para UI](ui-librerias.md) |
| es una web que se usa en teléfono | la skill `mobile-native` del repo, o [su copia en vendor](../../../vendor/emilkowalski/mobile-native/SKILL.md) |
| es una ilustración de producto para una landing | la skill `stageshow` del repo, en lugar de este archivo |

Los trabajadores no cargan `felix-mode`. Pon en el brief de cada diseñador,
implementador y revisor de UI la ruta absoluta de este archivo y la de cada
archivo de la tabla que aplica. Las rutas `vendor/` parten de la raíz del
clon de pao, la carpeta que contiene `skills/felix-mode`, por defecto
`~/.local/share/personal-agent-orchestration`. Si el trabajador no puede
leerlas, pega las reglas. Donde `frontend-design` u otra guía pida más
decoración, manda este archivo.

## La prueba

Cada texto, elemento y animación responde una pregunta: ¿qué entiende, hace
o siente el usuario gracias a esto que sin esto no entendería, no haría o no
sentiría? «Siente» vale sólo para el movimiento de un momento raro, como
pide la regla 4. Conserva lo que tiene respuesta. Quita lo que no.

## Reglas

1. **El título se sostiene solo.** Un eyebrow es la etiqueta chica sobre un
   título, como una línea en mayúsculas sobre el encabezado. No lleva
   ninguno. Si la categoría importa, va en el título o en la navegación.
2. **El texto dice lo que el usuario necesita para el siguiente paso.** Un
   subtítulo que repite el título, una ayuda que explica lo obvio o una
   frase de marketing dentro de una herramienta se quitan. Una etiqueta
   nombra su contenido («Recepciones», no «Inicio»). Cada acción da su
   feedback de estado, término, advertencia o error, y un formulario valida
   cada campo al salir de él, no al enviar. Cada pantalla deja ver dónde
   está el usuario, a dónde puede ir y cómo sale.
3. **La decoración se gana su lugar como cualquier otro elemento.** Badges,
   chips, íconos junto a cada etiqueta, tarjetas dentro de tarjetas,
   separadores, emojis, métricas que nadie pidió y botones secundarios que
   repiten una acción pasan la prueba o se van.
4. **Cada animación es intencional.** Tiene un nivel de frecuencia, un
   propósito con nombre y valores de la tabla. Lo que se dispara con teclado
   o se ve cientos de veces al día cambia al instante. Un contenido que
   aparece o desaparece sin transición también falla la prueba, porque el
   usuario pierde de dónde vino. El deleite vive en momentos raros (primer
   uso, éxito, vacío) y el dueño lo aprueba en el prototipo.
5. **Ante la duda, quita.** Un elemento cuya utilidad no puedes decir en una
   frase sale de la pantalla.

## Terminado

- **Prototipo.** Antes de pedir aprobación, recorre la pantalla elemento por
  elemento e intenta quitar cada uno. Lo que se quita sin que el usuario
  pierda nada se va. El reporte lista lo que quitaste.
- **Implementación.** Cada elemento del prototipo aprobado está, y cada
  elemento que agregaste tiene su razón en una frase en el reporte. Un
  elemento aprobado que falla la prueba se queda y se reporta al dueño, que
  decide si sale.
- **Evidencia.** Cada reporte y cada warning de UI traen capturas al ancho
  real del componente y a 390 px, y cada warning trae además su
  `file:line`. Si la web se usa en teléfono, la captura sale de un teléfono real
  que abre el dev server por la IP de la red local, porque la emulación no
  reproduce el hover pegado, el zoom de los inputs, las safe areas ni el
  teclado.
