# Datos reales en UI

Los datos de demo se eligen, casi siempre sin notarlo, para que el diseño se
vea bien: nombres que caben en una línea, conteos sin separador de miles y
todos los campos opcionales llenos. Este método deshace esa elección campo
por campo. Viene de
[break-ui](../../../vendor/emilkowalski/break-ui/SKILL.md), de Emil Kowalski.

Aplica a toda pantalla o componente que muestra datos de usuario: listas,
tablas, montos, nombres, fechas, conteos y badges.

## Pasos

1. **Mapa.** Lista cada valor que la pantalla muestra, con su fuente, tipo,
   límite y si es opcional. Incluye los que se olvidan: conteos en
   encabezados, tiempos relativos, texto de badges y estados, etiquetas que
   vienen de datos, avatares y el largo de la lista. Saca el límite del
   schema (Zod, Valibot), la migración, el tipo de la API o el `maxLength`
   del formulario. Un input de 50 caracteres que guarda en una columna de
   255 es un hallazgo. Termina cuando cada valor tiene un límite o dice «sin
   límite».
2. **Fixture de peor caso.** Arma una fixture junto a la de demo, con su
   misma forma, y que entre por la misma frontera: el mock, las props o el
   stub de la API. Cada valor es plausible o es el límite del schema. Reparte
   las fallas entre las primeras filas, como llegan los datos reales. Suma
   las fixtures vacío, uno (cada conteo en 1) y muchos (1,000+ filas si la
   lista no pagina). Los valores salen de
   [CATALOG.md](../../../vendor/emilkowalski/break-ui/CATALOG.md) y de la tabla
   de abajo.
3. **Interruptor.** Un control sólo de desarrollo, «Demo / Peor caso / Vacío
   / Uno / 1,000 filas», que cambia la fixture en la frontera de datos, por
   ejemplo con `?data=worst`. Cambia al instante y nunca llega a producción.
4. **Romperla.** Mira cada estado al ancho real del contenedor, a 320 px y
   al ancho máximo, con zoom de 200 % y en modo oscuro si el producto lo
   tiene. Toma una captura de cada uno. La tabla «Failure signatures» de
   break-ui da la causa y el arreglo de cada falla visible.
5. **Decidir por campo** entre envolver, truncar o recortar. Los nombres que
   identifican algo envuelven. Los metadatos secundarios se truncan al final,
   con una forma de ver el valor completo. Los archivos y correos que
   difieren al final se truncan en medio. Las vistas previas en tarjetas se
   recortan con `line-clamp`. Montos, fechas, RFC y todo lo que el usuario
   compara se muestran completos.
6. **Reporte.** Una fila por falla, la peor primero, con su gravedad: **Roto**
   (ilegible, acción inalcanzable o dato equivocado), **Feo** (legible pero
   visiblemente mal) o **Frágil** (bien hoy, a un paso realista de romperse).
   Cada fila trae el valor que la causó, el arreglo y su `file:line`. Después,
   las decisiones con más de una respuesta correcta, con tu recomendación, y
   lo que aguantó.

En implementación, corrige lo Roto y lo Feo de tu cambio, vuelve a pasar
por todos los estados, Demo incluido, y deja la fixture como prueba de
regresión.

## Valores de México

Del catálogo, éstos fallan seguido en nuestros productos: los plurales con
1, `tabular-nums` en valores que cambian, `0.1 + 0.2` en dinero, todos los
estados de un enum en la misma lista, el usuario actual dentro de la lista,
una fila sin permiso, exactamente el tamaño de página y uno más, correos sin
espacios (`overflow-wrap: anywhere`), iniciales que empiezan con emoji y
acciones que sólo aparecen con hover, inalcanzables en un teléfono.

| Valor | Qué rompe |
|---|---|
| `María Guadalupe Hernández de la Peña Ibáñez` | Dos apellidos con partícula: iniciales, orden por apellido y filas de una línea. |
| `Peña Muñoz`, `Ñuñez` | Orden y búsqueda: `localeCompare` con `'es-MX'`, y buscar «pena» encuentra «Peña». |
| RFC `GODE561231GR8`, CURP `GODE561231HDFRRN09` | Se muestran completos; validar el formato sin rechazar homoclave. |
| `1234567.5` como monto | `$1,234,567.50` con `Intl.NumberFormat('es-MX', { style: 'currency', currency: 'MXN' })`; los totales necesitan ancho. |
| Dinero en flotante | Centavos enteros o decimal; el `0.30000000000000004` nunca llega a pantalla. |
| `2026-03-31T23:30:00-06:00` | En UTC ya es otro día. El corte del día va en `America/Mexico_City`. |
| «1 recepciones», «1 renglones» | `Intl.PluralRules('es-MX')` o una cadena por cantidad. |
| «hace 12 días» | Después de una semana, fecha absoluta con `Intl.DateTimeFormat('es-MX')`. |
| `+52 55 1234 5678` | `type="tel"` y espacio para el formato con lada. |
| Input de monto o cantidad | `inputmode="decimal"` y fuente de 16 px o más, para que iOS no haga zoom. El zoom del usuario queda activo. |
