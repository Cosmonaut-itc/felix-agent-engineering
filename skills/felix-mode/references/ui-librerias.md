# Librerías para UI

Hacer a mano un toast, un menú o un sheet cuesta semanas de foco,
accesibilidad y gestos que una librería ya resolvió. Antes de construir un
componente, resuelve en este orden y detente en el primero que lo tenga:

1. **El sistema de diseño del repo.** Sus componentes, sus tokens y sus
   wrappers.
2. **Lo que ya está en `package.json`.** Una librería instalada gana sobre
   la del glosario aunque compitan. Un repo con Radix sigue en Radix, y uno
   con `@gorhom/bottom-sheet` lo usa en lugar de `formSheet`. El glosario
   nunca propone migrar.
3. **El glosario de abajo.** Recomienda una librería y di para qué sirve en
   una frase.
4. **Una dependencia nueva** es una pausa de Autonomía en felix-mode:
   propónla con su razón y espera el OK.

Si la tarea no está en el glosario, dilo y recomienda desde tu conocimiento,
aclarando que saliste de la lista.

## Glosario

La columna web viene de
[pick-ui-library](../../../vendor/emilkowalski/pick-ui-library/SKILL.md) y la
de React Native de la tabla de herramientas de
[animate-expo](../../../vendor/emilkowalski/animate-expo/SKILL.md).

| Tarea | Web | React Native (Expo) |
|---|---|---|
| Dialog, popover, menú, select accesibles | [base-ui](https://base-ui.com) | Componentes del repo |
| Paleta de comandos ⌘K | [cmdk](https://cmdk.paco.me) | |
| Toasts | [Sonner](https://sonner.emilkowal.ski) | |
| Código de verificación (OTP) | [input-otp](https://input-otp.rodz.dev) | |
| Drawer, bottom sheet | [Vaul](https://vaul.emilkowal.ski) | `presentation: 'formSheet'` de Expo Router |
| Animación general | [motion](https://motion.dev) | `react-native-reanimated` + `react-native-worklets` |
| Gestos | `drag` de motion | `react-native-gesture-handler` |
| Números que cambian | [NumberFlow](https://number-flow.barvian.me) | |
| Gráficas | [recharts](https://recharts.org); en vivo, [Liveline](https://github.com/benjitaylor/liveline) | |
| Drag and drop | [dnd kit](https://dndkit.com) | |
| Listas largas | [Virtuoso](https://virtuoso.dev) | |
| Barra de pestañas | | `NativeTabs` de Expo Router |
| Menú contextual, vista previa al mantener | | `Link.Menu`, `Link.Preview` (iOS) |
| Encabezado que colapsa | | `headerLargeTitleEnabled` (iOS) |
| Jalar para refrescar | | `RefreshControl` |
| UI que sigue al teclado | | `react-native-keyboard-controller` |
| Hápticos | | `expo-haptics` |
| Ilustración, celebración | | `lottie-react-native` |
| Escena animada grande, dibujo libre | | `@shopify/react-native-skia` |
| Estado compartido | [zustand](https://zustand.docs.pmnd.rs) | [zustand](https://zustand.docs.pmnd.rs) |
| Clases condicionales; variantes tipadas | [clsx](https://github.com/lukeed/clsx); [cva](https://cva.style) | |
| Modo oscuro sin parpadeo | [next-themes](https://github.com/pacocoursey/next-themes) | |

En Expo, instala con `npx expo install <paquete>` para que la versión
coincida con el SDK. Una transición CSS resuelve un hover o un fade sin
`motion`.

## Señales de reinvención

- Un toast hecho a mano o con una librería de modales: Sonner.
- Un dropdown o dialog de `<div>` con foco manual: base-ui o la librería de
  primitivas del repo.
- Un número que se anima re-renderizando texto: NumberFlow.
- 1,000+ filas renderizadas directo: Virtuoso.
- Una red de `useState` y props para estado compartido: zustand.
- Ternarios de `className` de tres niveles: clsx, o cva si tiene variantes.
