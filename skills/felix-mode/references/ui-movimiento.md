# Movimiento en UI

«Animar algo y animarlo bien son dos cosas muy distintas», escribe Jakub
Krehel. La regla 4 de [Menos es más](menos-es-mas.md) decide si algo se
anima. Este archivo decide cómo, con la filosofía de animación de Emil
Kowalski y las charlas de diseño de Apple. Cada valor sale de una tabla y
se puede defender.

Recorre las secciones en orden. Las dos primeras pueden terminar en cero
líneas de código, y ese resultado es correcto. Los valores exactos y su
razón están en
[STANDARDS.md](../../../vendor/emilkowalski/review-animations/STANDARDS.md).
Si el repo ya define curvas o duraciones como tokens, úsalas y extiéndelas
en el mismo sistema.

## 1. Filtro

| Frecuencia | Ejemplos | Decisión |
|---|---|---|
| Teclado, o 100+ veces al día | Atajos, paleta ⌘K, menú contextual, cambio de pestaña, escanear renglones | Cambia al instante. El hover sólo cambia color u opacidad. |
| Decenas de veces al día | Hover, navegar una lista, seleccionar una fila | Casi imperceptible: opacidad o color en menos de 150 ms, o nada. |
| Ocasional | Modal, drawer, sheet, toast | Animación estándar. |
| Rara o primera vez | Onboarding, éxito, estado vacío | Aquí vive el deleite, con aprobación del dueño en el prototipo. |

Las pestañas son pares y cambian al instante, también en React Native
(`animation: 'none'`).

Nombra el propósito antes de seguir: **feedback**, **continuidad espacial**
(de dónde vino o a dónde fue), **indicar estado**, **evitar un salto
brusco**, **explicar** (marketing u onboarding) o **deleite** (sólo en el
nivel raro). Sin nombre, no se construye.

Los datos que el usuario lee o usa, como gráficas, tablas y montos, se
quedan quietos. Se animan sólo para mostrar un cambio real de valor que el
usuario provocó.

La personalidad del producto acota el resto. Un panel de operación como los
admin de Jale, Bodega o Hasito anima poco y corto. Una landing o un primer
uso admiten más.

## 2. Herramienta más barata

Baja por la tabla y detente en la primera que alcance:

| Necesidad | Herramienta |
|---|---|
| Hover, press, color, un estado que controla una clase | Transición CSS |
| Entrada al montar, sin estado de JS | `@starting-style` |
| Movimiento fijo que debe seguir fluido mientras la página carga | Animación CSS de `transform` y `opacity`, que corre en el compositor |
| Control desde JS con rendimiento de CSS | WAAPI (`element.animate()`) |
| Springs, layout, salidas al desmontar, valores que sigue un gesto | `motion` |

En React Native, sigue la skill `animate-expo` del repo, o
[su copia en vendor](../../../vendor/emilkowalski/animate-expo/SKILL.md).
Ahí están sus tablas de spring, duración y propiedades. Si lo que hace
falta es un componente (toast, drawer, menú, sheet), resuélvelo con
[Librerías para UI](ui-librerias.md) antes de animar nada.

## 3. Propiedades

Anima `transform` y `opacity`. Cualquier otra propiedad necesita una razón.
Las excepciones documentadas son `clip-path`, como en mantener para
confirmar, y la altura medida de un acordeón:

| Costo | Propiedades | Cámbiala por |
|---|---|---|
| Sólo composición | `transform`, `opacity` | Ya es la más barata. |
| Pintura | `box-shadow`, `border-radius`, `color` | `filter: drop-shadow()` en lugar de `box-shadow`. Chrome compone `filter`; en otros navegadores puede repintar, así que mide. |
| Layout | `width`, `height`, `padding`, `margin`, `top`, `left` | `scale()` para crecer, `translate()` para mover, `opacity` en lugar de `display`, con `visibility: hidden` al terminar para que deje de recibir clics. |

- **Transición con nombre.** Lista las propiedades exactas en cada
  `transition`, para que sólo se anime lo que decidiste.
- **Escala de entrada.** Entra desde `scale(0.95)` (0.9 a 0.97) con
  `opacity: 0`.
- **Origen en el disparador.** Un popover, menú o tooltip escala desde su
  botón: `transform-origin: var(--transform-origin)` en Base UI o el
  equivalente de Radix. El modal se queda centrado.
- **Porcentajes en `translate()`.** `translateY(100%)` mueve el elemento su
  propia altura, sea cual sea su contenido.
- **Transform directo en el elemento.** Una variable CSS en el padre que
  mueve a los hijos recalcula estilos de todos ellos en cada frame.
- **Sin render por frame.** Una animación hecha a mano escribe en
  `ref.current.style` o en un motion value; un `setState` por frame tira
  frames.
- **Blur animado menor a 20 px.** De 2 a 5 px basta para suavizar un
  crossfade.
- **`will-change: transform` al final.** Agrégalo al elemento que anima sólo
  cuando viste el salto de un píxel o mediste frames perdidos.
- **`motion` antes de 14.2.** Ahí `x`, `y` y `scale` corren en el hilo
  principal; Jale web usa `motion` ^12.24. Si la animación coincide con una
  navegación o una carga, anima el string completo
  (`transform: "translateX(100px)"`). `layout` y `layoutId` corren en el
  hilo principal en todas las versiones, así que un indicador de pestaña que
  se mueve durante la navegación va en CSS.

Para diagnosticar una animación que tira frames, sigue
[animation-performance](../../../vendor/emilkowalski/animation-performance/SKILL.md).

**Press.** El feedback llega al bajar el dedo (`:active`, `pointerdown`,
`onPressIn`) y la acción se confirma al soltar. Usa `scale(0.97)` en 100 a
160 ms con `ease-out`. El área táctil mide al menos 44×44 pt (48 dp en
Android). Agranda un objetivo chico con un `::before` invisible o con
`hitSlop`, sin crecer el visual. En React Native suma
`pressRetentionOffset`, para que un dedo que se desliza unos píxeles no
cancele el toque.

## 4. Curvas, duraciones y springs

| Situación | Curva |
|---|---|
| Entrar o salir | `ease-out` |
| Moverse o transformarse en pantalla | `ease-in-out` |
| Hover, cambio de color | `ease` |
| Movimiento constante (progreso, marquesina) | `linear` |

Las curvas de CSS son débiles. Usa estas, o tómalas de
[easing.dev](https://easing.dev/):

```css
--ease-out: cubic-bezier(0.23, 1, 0.32, 1);
--ease-in-out: cubic-bezier(0.77, 0, 0.175, 1);
--ease-drawer: cubic-bezier(0.32, 0.72, 0, 1);
```

| Elemento | Duración |
|---|---|
| Press | 100 a 160 ms |
| Tooltip, popover chico | 125 a 200 ms |
| Dropdown, select | 150 a 250 ms |
| Modal, drawer | 200 a 500 ms |

La UI se queda bajo 300 ms. Las excepciones documentadas son modales,
drawers y sheets, hasta 500 ms, y el toast, con los 400 ms en `ease` de
Sonner. Un tooltip que sigue a otro en la misma barra
aparece sin retraso ni animación.

**Spring cuando hubo un dedo.** Un drag, un flick o un gesto que el usuario
puede invertir terminan con spring, porque conserva la velocidad al
interrumpirse. Por defecto va críticamente amortiguado, sin rebote:
`{ type: "spring", duration: 0.4, bounce: 0 }` (damping 1.0 en términos de
Apple). El rebote, de `bounce: 0.1` a `0.3` (damping 0.9 a 0.7), es sólo para lo
que el usuario lanzó: un sheet soltado con impulso o una tarjeta arrojada. El
mismo elemento cerrado con un botón no rebota.

**Gestos.** Mientras el dedo está abajo, la posición lo sigue 1:1, sin
curva ni spring, desde el punto donde el usuario agarró el elemento. Al soltar,
un spring sembrado con la velocidad del gesto. Se descarta por distancia
(40 a 50 % del recorrido) o por velocidad, de modo que un flick corto
basta. En los bordes, resistencia progresiva en lugar de un tope duro. Para
drawers y sheets en web usa Vaul o la librería del repo. Las reglas
completas están en
[gestures](../../../vendor/emilkowalski/gestures/SKILL.md), y la proyección de
impulso con la histéresis de 10 px en
[apple-design](../../../vendor/emilkowalski/apple-design/SKILL.md), secciones 5,
6 y 10.

## 5. Interrupción, salida y asimetría

- **Interrumpible.** Lo que se dispara rápido, como toasts y toggles, usa
  transiciones o springs, que retoman desde el valor presente. Toda
  animación nueva arranca del valor que está en pantalla, y el input sigue
  vivo durante la transición.
- **Sale por donde entró.** Un toast que sube desde abajo sale hacia abajo.
  Una salida es simultánea y más corta que su entrada.
- **Asimetría donde el usuario decide.** Lo deliberado va lento y lo que
  responde el sistema va rápido: mantener para borrar dura 2 s en `linear`,
  y soltar antes regresa en 200 ms con `ease-out`.

**Quitar movimiento.** Al borrar una animación, borra también lo que
depende de ella:

- el estado inicial oculto (`initial={{ opacity: 0 }}`, una clase de
  revelado que arranca en `opacity: 0`);
- el código que la espera: `onAnimationEnd`, `transitionend` o un
  `setTimeout` del mismo largo;
- lo que corría en `onExitComplete`, que pasa al momento de cerrar;
- un foco que se movía al terminar el movimiento, que ahora se mueve de
  inmediato;
- los tests que esperan clases o duraciones.

Si quitar una animación `layout` destapa un salto, la animación se queda y
el salto se reporta. El reporte de una limpieza también lista lo que se
conservó y el propósito que lo salvó. Para barrer un repo entero, sigue
[remove-animations](../../../vendor/emilkowalski/remove-animations/SKILL.md).

## 6. Coreografía

Cuando dos o más elementos se mueven por el mismo evento, aplica
[choreograph](../../../vendor/emilkowalski/choreograph/SKILL.md):

1. Dale a cada elemento un papel: **líder** (lo único que el usuario debe
   notar), **soporte**, **decoración** (sólo opacidad) o **estático**. Dos
   líderes son un problema de jerarquía, y se reporta.
2. Cada contenedor tiene una sola entrada. Si el panel entra, su contenido
   ya está adentro.
3. En superficies frecuentes los elementos aparecen juntos.
4. Un stagger va de 30 a 80 ms entre elementos, el grupo termina en menos de
   300 ms y la importancia ordena los retrasos, que nunca son uniformes.
5. Todo es clicable en cuanto se ve.
6. Lo que sale deja su espacio al instante (`AnimatePresence
   mode="popLayout"`), y cambiar de vista en el mismo contenedor es un
   crossfade.
7. Las partes de un componente comparten familia de curva y duración.
8. Adelante mueve el contenido a la izquierda y atrás a la derecha.

El reporte trae la línea de tiempo (elemento, papel, inicio, duración,
movimiento, relación) y lo que se dejó quieto con su razón. Las recetas
están en [RECIPES.md](../../../vendor/emilkowalski/choreograph/RECIPES.md).

## 7. Hápticos

Sólo en apps móviles, con `expo-haptics`:

| Momento | Llamada |
|---|---|
| Un valor pasa un paso (picker, slider, segmented control) | `selectionAsync()` |
| Algo encaja, un sheet se detiene, un drag se confirma | `impactAsync(Light)` |
| Aterriza algo pesado, se ejecuta una acción destructiva | `impactAsync(Medium)` |
| La operación salió bien o falló | `notificationAsync(Success / Error)` |

El háptico cae en el mismo frame que el cambio visual, uno por evento
semántico (un paso, un snap, un commit), y lo visual funciona sin él, porque muchos usuarios los tienen
apagados.

## 8. Reduced motion y accesibilidad

- **Menos y más suave.** Con `prefers-reduced-motion: reduce` (o
  `useReducedMotion`), conserva opacidad y color, y quita traslación, escala,
  parallax y rebote. La coreografía aparece junta. En React Native las
  transiciones de pantalla pasan a `animation: 'fade'`. El seguimiento 1:1
  de un gesto se queda, porque lo produce el usuario.
- **Hover sólo con puntero fino.** Cada `:hover` con movimiento vive dentro
  de `@media (hover: hover) and (pointer: fine)`; el toque usa `:active`.
- **Transparencia y contraste.** Con `prefers-reduced-transparency`, una
  superficie translúcida se vuelve sólida. Con `prefers-contrast: more`, el
  fondo es casi sólido y el borde contrasta.
- **Texto que crece.** React Native escala el texto (`allowFontScaling`), así
  que una altura animada se mide con `onLayout` en lugar de fijarse.

## 9. Verificar la sensación

El reporte de cada animación dice su frecuencia, su propósito y sus
ingredientes: herramienta, propiedades, curva con duración o configuración
del spring. Después:

- reprodúcela de 2 a 5 veces más lenta o en el panel Animations de DevTools,
  cuadro por cuadro, y revisa origen, curva y sincronía;
- prueba los gestos en un teléfono real, abriendo el dev server por la IP de
  la red local;
- mide el rendimiento web con el panel Performance en un teléfono de gama
  media por depuración remota; bajar el CPU del escritorio no simula una GPU
  débil;
- en React Native, juzga en un build release en el dispositivo más lento que
  se soporta, nunca en Expo Go, y confirma que `app.json` tenga
  `CADisableMinimumFrameDurationOnPhone` para llegar a 120 fps;
- vuelve a verla al día siguiente.

Lo que no se puede juzgar desde el código, como si un stagger se lee como
ola o un spring rebota de más, se nombra en el reporte con el momento exacto
que el dueño debe mirar.
