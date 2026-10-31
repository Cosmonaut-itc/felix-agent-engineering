---
name: launch-qa
description: QA de lanzamiento con usuarios primerizos simulados por Computer Use, de la entrada al primer éxito. Ordena lo hallado, lo arregla en una pasada y repite la ronda.
disable-model-invocation: true
---

# QA de lanzamiento

Eres el líder de QA. Cada persona simulada es un usuario que abre el producto
por primera vez y recorre en un navegador real el camino desde la entrada
hasta el primer éxito. Juntas sus hallazgos en una lista ordenada por cuántas
personas los sufrieron, arreglas todo en una pasada y repites las mismas
personas para probar el arreglo.

## Modelo y herramienta

Cada persona es un trabajador Codex `gpt-6.1-terra` con esfuerzo `medium`.
Sube a `high` por **Escalado** del routing, que también fija ese tope. La
persona ve y toca la pantalla sólo con el MCP de Computer Use
(`computer-use`), sobre un navegador del Mac de pruebas. Si ese MCP falta, la
persona no corre: devuelve el bloqueo en lugar de pasar a Playwright o a un
navegador headless.

## Pasos

1. **Prepara la corrida.** Escribe en el informe:
   - La URL de entrada, en local, staging o un preview. Producción sólo con
     OK explícito del dueño: las personas crean cuentas y meten datos basura.
   - El **primer éxito**: la primera acción con la que un usuario nuevo
     obtiene valor, como «cuenta creada y primer proyecto guardado». Si el
     repositorio o el brief no lo dicen, derívalo del producto y decláralo.
   - Las cuentas, correos y tarjetas de prueba, y lo que ninguna persona
     toca, como pagos reales o correos a terceros.
   - El Mac de pruebas: la Mac Mini, salvo que el `AGENTS.md` del
     repositorio diga otro. Las personas corren en su escritorio: lanza esta
     skill desde un hilo de T3 Code en ese Mac, o, si tu servidor de T3 lo
     tiene enlazado, usa su `environmentId`.
   - El directorio de la ronda en ese Mac:
     `~/qa-runs/<proyecto>/<AAAAMMDD-HHMM>-r<n>/`.

2. **Elige el reparto.** Parte de las cinco personas de
   [personas.md](references/personas.md). Quita las que no aplican al
   producto, adapta las que aplican a medias y suma otras si hay un usuario
   real que ninguna representa. Anota en el informe quién quedó fuera y por
   qué. Si el público del producto es una decisión de negocio que no puedes
   derivar, pregúntalo al dueño con la herramienta de preguntas con opciones.

3. **Despacha a cada persona.** Un `delegate_task` por persona, con
   `role: "test"`, la plantilla de brief de
   [personas.md](references/personas.md) completa y este `target`:
   proveedor `codex`, modelo `gpt-6.1-terra`,
   `options: {reasoningEffort: "medium"}` y, si corres en otro Mac, el
   `environmentId` del Mac de pruebas. El límite por persona es de 20 minutos, salvo que el flujo pida
   más.
   - Computer Use controla un solo escritorio. Despacha una persona a la vez
     por Mac y lanza la siguiente cuando la anterior entregó su resultado
     terminal. Si hay varios Macs con Computer Use, reparte entre ellos.
   - Sin T3 Code, despacha por la ruta Codex de Transporte del routing, con
     el mismo modelo y esfuerzo, en una sesión con Computer Use del Mac de
     pruebas.
   - Veta cada resultado con el contrato. Cada hallazgo trae el texto literal
     de la UI y una captura que existe. Una persona que volvió sin capturas,
     o que leyó el código, se repite con un encargo nuevo.

4. **Junta y ordena.** Copia las capturas al directorio del encargo. Agrupa
   en un solo problema los hallazgos que comparten pantalla y causa. Ordena
   por número de personas distintas que lo sufrieron, después por gravedad
   (bloquea, frena, roza) y después por lo temprano que aparece en el camino.
   Cada problema conserva sus capturas y la lista de personas que lo
   sufrieron.

5. **Arregla todo en una pasada.** Una sola unidad de `felix-mode` cubre
   todos los problemas aceptados, con su playbook, su delegación y su review.
   Antes de tocar código, junta en una sola pregunta al dueño los problemas
   que piden una decisión de producto, como precios, textos legales, quitar
   un paso o cambiar el público. Rebate con evidencia el hallazgo que no sea
   un problema real.

6. **Repite la ronda.** Corre el mismo reparto, con los mismos briefs, el
   mismo entorno y encargos nuevos, contra el build arreglado. Marca cada
   problema como resuelto, persiste o empeoró. Está resuelto cuando ninguna
   de las personas que lo sufrieron vuelve a sufrirlo. Lista aparte los
   hallazgos nuevos. La QA termina cuando todos los problemas aceptados
   quedan resueltos y ningún hallazgo nuevo bloquea. Si no, vuelve al paso 5.
   Cada vuelta cuenta dentro del límite de **Rondas** de `felix-mode`.

## Informe

Guarda `informe.md` en el directorio del encargo, con los datos de la
corrida, el reparto y sus exclusiones, la tabla ordenada de problemas
(personas, gravedad, capturas y estado en cada ronda) y las decisiones que
siguen pendientes del dueño.
