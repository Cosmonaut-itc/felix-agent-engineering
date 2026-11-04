# Consejo

El consejo reúne a tres modelos para revisar un cambio o para opinar sobre
una decisión. Corre sólo cuando el usuario lo pide con «consejo» o
«council». Sin ese pedido, la review sigue **Revisión** de felix-mode.

## Miembros

| Miembro | Esfuerzo |
|---|---|
| gpt-6.1-sol | `high` |
| opus-5.5 | `high` |
| gpt-6-astra | `low` |

Despacha a los tres en paralelo, cada uno por su ruta de Transporte del
routing, con contexto nuevo y sin permiso de edición. Ningún miembro abre
subagentes. Si un miembro no puede correr, informa cuál y por qué, y no lo
sustituyas. Esa ronda no da APTO de consejo. La puerta del nivel sigue
**Revisión** con los miembros que sí corrieron.

## Review de un cambio

1. Prepara la ronda como pide **Revisión**: riesgo, pre-vuelo y registro.
2. Envía a cada miembro el mismo brief. En alto riesgo usa el brief de
   `interrogate` y añade la línea `NOTA`. En nivel normal usa este:

   ```
   Nivel: <normal o alto> por <señal o ruta>
   Review de sólo lectura. No edites, no hagas commit ni push, no comentes
   en el PR y no abras subagentes.
   Repo: <ruta absoluta> Base: <sha> Head: <sha>
   Objetivo y condición de terminado: <brief original sin reescribir>
   Fuera de alcance: <alcance>
   Checks: <comando y resultado; los que faltan o ya fallaban>
   Diff: git diff <base> [-- <rutas>]; lee también los archivos sin seguimiento.
   Registro: <id, estado y evidencia de cada hallazgo previo> (desde la ronda 2)
   Cada critical o warning trae el comando que corriste, la salida que
   imprimió y la salida que exige el objetivo.
   VEREDICTO: APTO o NO APTO
   NOTA: <1 a 10>
   <id> [critical|warning|nit] <archivo>:<línea> <hallazgo>. Arreglo: <propuesta>
   ```

3. Veta los hallazgos con **Prueba** y atiéndelos con **Hallazgos**. Un
   hallazgo que amplía el objetivo es otra unidad.
4. Repite dentro del límite de **Rondas**. El consejo da APTO cuando los
   tres dan APTO sobre el mismo head, sin critical ni warning abierto.
5. Si un hallazgo sigue disputado después de dos respuestas del miembro,
   pregunta a Felix con las dos posturas, en dos o tres líneas cada una.

El APTO del consejo cumple la review que exige el nivel del cambio. La nota
es un dato del registro y no decide APTO.

## Consulta sobre una decisión

Un plan, una spec, un ADR o un cambio de gobierno se consultan sin diff.
Envía la pregunta, las rutas de contexto, las restricciones y la condición
de terminado. Pide a cada miembro una recomendación, los riesgos que ve y
su evidencia. Resume los acuerdos, los desacuerdos y tu recomendación. Un
desacuerdo que cambia la decisión va a Felix como pregunta con opciones.
La consulta no da veredicto sobre un head, así que no deja tabla ni líneas
`Revisión:`. Su resumen va en tu respuesta.

## Constancia

En una review de un cambio con PR, pon esta sección en el cuerpo, antes
de las líneas `Revisión:` de **Constancia**. Edita la descripción después
de cada ronda, sin publicar un comentario:

```markdown
## Consejo

Rondas: 2

| Miembro | Ronda 1 | Ronda 2 |
| --- | --- | --- |
| gpt-6.1-sol high | 7/10 NO APTO | 9/10 APTO |
| opus-5.5 high | 8/10 APTO | 9/10 APTO |
| gpt-6-astra low | 6/10 NO APTO | 9/10 APTO |
```

Si un miembro no da nota, escribe `sin nota`. Si no corrió, escribe `no
corrió`. Sin PR, pon la misma tabla en la respuesta final.
