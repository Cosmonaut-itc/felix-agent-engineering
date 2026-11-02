# Menos es más en UI/UX

La regla de Felix: ningún texto ni elemento gratuito, y ningún eyebrow. El
criterio sale de [«Less is more, more or less»](https://jakub.kr/writing/less-is-more),
de Jakub Krehel. Quitar exige más disciplina que agregar, y saber qué no
construir pesa más que saber construirlo.

## Cuándo aplica

- **Prototipo.** Aplica todas las reglas. Aquí se decide qué existe en la
  pantalla, y el dueño aprueba esa decisión.
- **Implementación.** Construye lo que muestra el prototipo aprobado. Un
  estado que el prototipo no dibujó, como carga, vacío o error, va en su
  forma mínima y se lista en el reporte.
- **Review de UI.** Cada elemento que falla la prueba, o que no está en el
  prototipo aprobado sin razón en el reporte, es un warning, no un nit.

Los trabajadores no cargan `felix-mode`. Pon la ruta de este archivo en el
brief de cada diseñador, implementador y revisor de UI. Si el trabajador no
puede leerla, pega las reglas. Donde `frontend-design` u otra guía pida más
decoración, manda este archivo.

## La prueba

Cada texto, elemento y animación responde una pregunta: ¿qué entiende o hace
el usuario gracias a esto que sin esto no entendería o no haría? Conserva lo
que tiene respuesta. Quita lo que no.

## Reglas

1. **El título se sostiene solo.** Un eyebrow es la etiqueta chica sobre un
   título, como una línea en mayúsculas sobre el encabezado. No lleva
   ninguno. Si la categoría importa, va en el título o en la navegación.
2. **El texto dice lo que el usuario necesita para el siguiente paso.** Un
   subtítulo que repite el título, una ayuda que explica lo obvio o una
   frase de marketing dentro de una herramienta se quitan.
3. **La decoración se gana su lugar como cualquier otro elemento.** Badges,
   chips, íconos junto a cada etiqueta, tarjetas dentro de tarjetas,
   separadores, emojis, métricas que nadie pidió y botones secundarios que
   repiten una acción pasan la prueba o se van.
4. **Anima un cambio de estado que el usuario necesita seguir.** Pesa la
   frecuencia. Lo que se usa cientos de veces al día se queda quieto o con la
   animación más corta. Un menú que se abre 200 veces al día con 300 ms de
   animación cuesta un minuto diario de espera.
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
