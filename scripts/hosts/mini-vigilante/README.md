# Snapshot de la Mini

Productor one-shot `mini-vigilante/muestra@1`, Python 3.9 y biblioteca estándar.
Publica `~/.local/state/pao/mini-vigilante/ultimo.json`. No decide admisión.

```sh
/usr/bin/python3 scripts/hosts/mini-vigilante/muestra.py
/usr/bin/python3 scripts/hosts/mini-vigilante/muestra.py --salida /tmp/muestra-propia/ultimo.json --umbrales scripts/hosts/mini-vigilante/umbrales.json
```

Cada captura consulta `scutil --get LocalHostName` y los seis `sysctl` del
contrato, con argumentos separados, timeout de 5 s por comando y `LC_ALL=C`,
porque `sysctl` formatea `vm.swapusage` con el separador decimal del locale
heredado. El disco usa
`statvfs('/System/Volumes/Data')`: `f_blocks * f_frsize` para total y
`f_bavail * f_frsize` para bytes disponibles al usuario. Swap convierte las
unidades binarias de `vm.swapusage` a bytes; load1 y ncpu sólo se observan.
El boot suma segundos y microsegundos enteros de `kern.boottime` a la época UTC,
sin pasar por float. Se publica siempre con seis decimales y Z.
`ts` se toma al terminar todas las lecturas.

`umbrales.json`, junto al script, debe existir y contener los tres pares
completos. `--umbrales` sustituye el archivo completo. Se rechazan claves
extrañas, tipos no enteros, booleanos, rangos y órdenes inválidos. No se inventan
defaults. memLevelPct usa límites inclusivos ≤30 y ≤15. Presión admite sólo 1,
2 y 4; sus niveles warn=2 y crit=4 son fijos. Disco usa límites estrictos
<50 GiB y <25 GiB. La precedencia es crit > warn > unknown > ok.

Una lectura fallida deja null y unknown, con error corto y una entrada
`{stage, code, message}`. Host desconocido impide formar el bootId. Los mensajes
no incluyen stderr de comandos ni rutas privadas. La identidad de boot sirve
para observación; no autoriza terminar procesos.

El lock `ultimo.lock` cubre captura y escritura. Una instancia ocupada sale 0
y escribe `busy` en stderr. La publicación usa temporal en el mismo directorio,
flush, fsync y os.replace. No hay JSONL. Una muestra publicada tiene
`persist={"error":null,"ultimo":"written"}`. Un fallo de persistencia conserva
el snapshot anterior, sale 3 y escribe una línea en stderr. Configuración
inválida sale 2 sin capturar. Warn, crit y unknown salen 0: el lector debe
validar el contenido y su vigencia. No se imprime JSON en stdout.

## LaunchAgent propuesto

**La instalación requiere el OK de Felix.** No se ejecuta como parte de pruebas.
`bash scripts/hosts/mini-vigilante/instalar.sh` copia por SSH a un temporal,
prepara el plist, hace bootout, espera hasta 10 s y hace bootstrap con un
reintento. Todos los ssh/scp deshabilitan multiplexación. Instala el productor
en `~/bin/pao-mini-vigilante.py` y su configuración en
`~/bin/pao-mini-vigilante-umbrales.json`. El agente de la sesión GUI ejecuta
`/usr/bin/python3` cada 60 s y al cargar. Ambos logs van a
`~/Library/Logs/pao-mini-vigilante.log`; no hay rotación automática.

`instalar.sh --desinstalar` espera a que se suelte el job y quita sólo script,
configuración y plist. Conserva estado y logs.

## Verificación

Sólo en la Mini, con turno por el `scripts/mini/run.sh` de comprador. Desde la
copia remota:

```sh
cd scripts/hosts/mini-vigilante
/usr/bin/python3 -m unittest discover -s tests -v
```

La suite inyecta comandos, reloj y statvfs. Prueba fronteras, precedencia,
normalización de boot, fallos, atomicidad y dos procesos con el mismo lock.
Una muestra real se toma en directorio temporal y se borra al terminar.
