# Prototipo de UI

Cuando el playbook Prototype de pstack decide una pantalla o un componente,
aplica estas reglas sobre sus pasos. Vienen del
[prototype](../../../vendor/emilkowalski/prototype/SKILL.md) de Emil Kowalski.
Cada variante explora una dirección distinta, porque tres tonos de la misma
idea no le enseñan nada al dueño.

- **Prototype 1.** Cada variante lleva un nombre de dirección, como
  «Quieta», «Editorial» o «Densa», y un eje: layout, densidad,
  personalidad, movimiento o modelo de interacción. Dos variantes en la
  misma posición de un eje cuentan como una, y se reemplaza una por otra
  dirección. Van tres por defecto y cinco como máximo. Termina cuando cada
  variante tiene nombre y eje, y no se repite ninguna posición.
- **Prototype 3.** El directorio scratch queda fuera de producción, como
  pide pstack, con los tokens del proyecto (colores, radios, espaciado,
  fuentes, curvas) para que cada variante se vea nativa del producto. El
  contenido es del dominio, con nombres y montos plausibles como los de
  [Datos reales](ui-datos-reales.md), y cada interacción funciona.
- **Prototype 4.** El switcher es
  [PICKER.md](../../../vendor/emilkowalski/prototype/PICKER.md), copiado tal
  cual, porque es parte de la herramienta y no del diseño que se juzga.
  Muestra una variante a la vez, a tamaño real y en su contexto: un toast con una página
  detrás, una tarjeta con sus vecinas. Cambiar de variante es instantáneo.
- **Prototype 5.** Antes de mostrarlas, cada variante pasa
  [Menos es más](menos-es-mas.md) y, si se mueve,
  [Movimiento en UI](ui-movimiento.md). Recorre todas, con la consola
  limpia, y toma una captura de cada una al ancho real y a 390 px. Si dos
  variantes convergieron al construirlas, quita una y dilo.
- **Prototype 6.** Presenta la tabla «Variante · Eje · Cuándo gana · Costo»,
  sin favorito dentro de la tabla. Tu recomendación va después, razonada
  por la frecuencia de uso y la personalidad del producto. Si el dueño pide
  otra ronda, diverge alrededor de la variante que eligió.

El deleite de un momento raro (primer uso, éxito, vacío) se muestra animado
en el prototipo, porque ahí lo aprueba el dueño. La variante elegida no se
integra desde el prototipo. El prototipo se tira y Feature construye la
dirección aprobada.

Una ilustración de producto para una landing sigue la skill `stageshow` del
repo, no este archivo.
